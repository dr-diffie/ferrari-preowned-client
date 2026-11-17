"""Smart, stealth-preserving image downloader.

Design constraints:
- Rides the same persona/session/transport as the data client: the site
  sees one coherent browser, not a separate image harvester.
- Image requests look like a real browser loading a listing gallery:
  ``Sec-Fetch-Dest: image`` + listing page as referer, in small per-car
  bursts (a user clicking through a gallery) separated by normal pacing.
- On-the-fly compression with Pillow: EXIF-orientation applied, alpha
  preserved, adaptive quality, and a strict never-grow rule (original
  bytes kept when recompression wouldn't help).
- Resume-safe: existing non-empty files are skipped (idempotent re-runs).
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
import random
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .errors import BlockedError, ThrottledError
from .transport import Transport

if TYPE_CHECKING:
    from .models import Gallery, Listing, ListingDetail

try:
    from PIL import Image, ImageOps
    _HAS_PIL = True
except ImportError:  # pragma: no cover
    _HAS_PIL = False


class ImageConfig:
    """Tunables for image downloading."""

    def __init__(self, *,
                 quality: int = 82,
                 max_dimension: Optional[int] = None,
                 reencode_webp: bool = True,
                 burst_size: int = 12,
                 burst_pause_range: tuple[float, float] = (0.8, 2.0),
                 per_request_timeout: float = 30.0):
        self.quality = quality
        self.max_dimension = max_dimension
        self.reencode_webp = reencode_webp
        self.burst_size = burst_size
        self.burst_pause_range = burst_pause_range
        self.per_request_timeout = per_request_timeout

    def describe(self) -> str:
        return (f"quality={self.quality} max_dim={self.max_dimension} "
                f"webp={self.reencode_webp}")


class ImageDownloader:
    """Downloads listing galleries as a browser would, compressing on the fly."""

    def __init__(self, transport: Transport, config: Optional[ImageConfig] = None,
                 dead_urls: Optional[set] = None):
        self.transport = transport
        self.config = config or ImageConfig()
        self._lock = threading.Lock()
        self._log: Callable[[str], None] = print  # engine replaces this
        # known-dead upstream URLs (persisted by the engine in the store):
        # skip instead of re-fetching 404s every run
        self.dead_urls: set = set(dead_urls or ())
        self.new_dead_urls: set = set()
        self._consec_403 = 0
        self._CONSEC_403_LIMIT = 10
        self.stats = {
            "downloaded": 0, "skipped_existing": 0, "skipped_dead": 0,
            "failed": 0,
            "recompressed": 0, "kept_original": 0,
            "bytes_original": 0, "bytes_final": 0, "throttled": 0,
        }

    # -- public API --------------------------------------------------------

    def download_gallery(self, listing: "Listing",
                         gallery: "Gallery",
                         target_dir: "str | Path",
                         referer: Optional[str] = None,
                         *, on_progress: Optional[Callable[[int, int], None]] = None,
                         ) -> dict:
        """Download one car's gallery. Returns a per-car result dict."""
        car_dir = self._resolve_car_dir(Path(target_dir), listing)
        car_dir.mkdir(parents=True, exist_ok=True)
        # dedupe preserving order: a duplicate URL in one gallery would
        # put two workers in a write race on the same output file
        urls = list(dict.fromkeys(u for u in (gallery.images or []) if u))
        results = []
        n_ok = 0
        n_skip = 0
        # pre-check which files are missing; if none, this is a pure
        # resume-skip gallery: record the skips and take NO burst pauses.
        missing = [(i, url, out) for i, (url, out) in enumerate([
            (u, car_dir / self._image_filename(u, j))
            for j, u in enumerate(urls)])]
        # (fast path when everything exists)
        if all(out.exists() and out.stat().st_size > 0 for _, _, out in missing):
            with self._lock:
                self.stats["skipped_existing"] += len(urls)
            return {
                "car": listing.car_name, "vin": listing.vin,
                "folder": str(car_dir), "ok": len(urls), "total": len(urls),
                "skipped": len(urls), "results": [
                    {"url": u, "path": str(o), "status": "skipped_existing"}
                    for _, u, o in missing],
            }
        for i, url in enumerate(urls):
            out = car_dir / self._image_filename(url, i)
            r = self._fetch_one(url, out, referer or self._referer_for(listing))
            results.append(r)
            if r["status"] == "ok":
                n_ok += 1
            elif r["status"] == "skipped_existing":
                n_ok += 1
                n_skip += 1
            if on_progress:
                on_progress(i + 1, len(urls))
            # burst pacing only between bursts with actual fetches
            if (i + 1) % self.config.burst_size == 0 and i + 1 < len(urls):
                lo, hi = self.config.burst_pause_range
                time.sleep(random.uniform(lo, hi))
        return {
            "car": listing.car_name, "vin": listing.vin,
            "folder": str(car_dir), "ok": n_ok, "total": len(urls),
            "skipped": n_skip, "results": results,
        }

    # -- internals ---------------------------------------------------------

    def _referer_for(self, listing: "Listing") -> str:
        """A plausible referer for image loads: the site's search page."""
        return "https://preowned.ferrari.com/de-DE"

    def _car_folder(self, listing: "Listing") -> str:
        ident = listing.identity_key
        year = listing.year or "xxxx"
        name = (listing.car_name or "ferrari").lower()
        safe = "".join(ch if ch.isalnum() else "-" for ch in f"{name}-{year}")
        safe = "-".join(p for p in safe.split("-") if p)
        return f"{ident}_{safe}"[:80]

    def _resolve_car_dir(self, target_dir: "Path", listing: "Listing") -> Path:
        """Canonical car dir, migrating legacy-named siblings into it.

        Older runs named folders with the slugified carName as model
        segment (e.g. 'VIN_purosangue-2025'); the catalog-slugSeo era
        renamed the segment ('VIN_ferrari-purosangue-2025'). Files under
        the legacy name are MOVED into the canonical folder (one-time,
        idempotent) so resume-skip and the audit agree on one location.
        """
        car_dir = target_dir / self._car_folder(listing)
        prefix = f"{listing.identity_key}_"
        if car_dir.exists() or not target_dir.exists():
            return car_dir
        for d in target_dir.iterdir():
            if d.is_dir() and d.name.startswith(prefix) and d != car_dir:
                car_dir.mkdir(parents=True, exist_ok=True)
                moved = 0
                for f in d.iterdir():
                    if f.is_file():
                        dest = car_dir / f.name
                        if not dest.exists():
                            f.rename(dest)
                            moved += 1
                try:
                    d.rmdir()
                except OSError:
                    pass  # leftover subdir — leave in place
                if moved:
                    self._log(f"[images] migrated {moved} files: "
                              f"{d.name} -> {car_dir.name}")
                break
        return car_dir

    def _image_filename(self, url: str, index: int) -> str:
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        return f"{index:03d}_{digest}.webp"

    def _proxies(self):
        """Route image fetches through the pool when one is attached."""
        if self.transport.pool is None:
            return None
        proxy_url = self.transport.pool.pick()
        if proxy_url is None:
            return None
        self.transport.pool.release(proxy_url, ok=True)
        return {"http": proxy_url, "https": proxy_url}

    def _headers_for(self, referer: Optional[str]) -> dict:
        h = dict(self.transport.persona.headers)
        h.update({
            "Accept": "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        })
        if referer:
            h["Referer"] = referer
        return h

    def _fetch_one(self, url: str, out: Path, referer: Optional[str]) -> dict:
        # known-dead upstream URL: skip without a request (404 verified
        # in a previous run; re-checking would only burn requests)
        with self._lock:
            if url in self.dead_urls:
                self.stats["skipped_dead"] += 1
                return {"url": url, "path": str(out),
                        "status": "skipped_dead"}
        # resume-skip: already-downloaded file is final
        if out.exists() and out.stat().st_size > 0:
            with self._lock:
                self.stats["skipped_existing"] += 1
            return {"url": url, "path": str(out), "status": "skipped_existing"}

        headers = self._headers_for(referer)
        for attempt in range(2):  # one throttle-cooldown retry
            try:
                # polite per-image pacing: a real browser loads a whole
                # gallery in ~2s over 6 parallel connections; 0.05-0.15s
                # per image still keeps us several times gentler.
                time.sleep(random.uniform(0.05, 0.15))
                proxies = self._proxies()
                resp = self.transport._session.get(
                    url, headers=headers,
                    timeout=self.config.per_request_timeout, proxies=proxies)
                if resp.status_code == 202:
                    with self._lock:
                        self.stats["throttled"] += 1
                    if attempt == 0:
                        time.sleep(60.0)  # observed throttle window
                        continue
                    raise ThrottledError("image fetch throttled twice")
                if resp.status_code == 403:
                    # consecutive 403s across the CDN = the IP (or CDN)
                    # is blocking us — abort the plane instead of grinding
                    # through every remaining gallery.
                    with self._lock:
                        self._consec_403 += 1
                        n = self._consec_403
                    if n >= self._CONSEC_403_LIMIT:
                        raise BlockedError(
                            f"image CDN returned 403 {n} times in a row — "
                            f"IP/CDN block suspected",
                            evidence={"url": url})
                    return {"url": url, "path": str(out), "status": "error",
                            "error": "403 Forbidden"}
                with self._lock:
                    self._consec_403 = 0
                if resp.status_code == 404:
                    # upstream removed the file (Ferrari's US image
                    # partner does this routinely): remember it so the
                    # next runs skip instead of retrying forever
                    with self._lock:
                        self.new_dead_urls.add(url)
                    return {"url": url, "path": str(out), "status": "error",
                            "error": "404 upstream (marked dead)"}
                resp.raise_for_status()
                raw = resp.content
                break
            except (ThrottledError, BlockedError):
                raise  # control-flow errors must propagate, never be retried
            except Exception as exc:
                if attempt == 0:
                    time.sleep(5.0)
                    continue
                with self._lock:
                    self.stats["failed"] += 1
                return {"url": url, "path": str(out), "status": "error",
                        "error": str(exc)[:200]}
        else:
            return {"url": url, "path": str(out), "status": "error",
                    "error": "exhausted retries"}

        final = self._compress(raw)
        with self._lock:
            self.stats["downloaded"] += 1
            self._update_stats(raw, final)
        tmp = out.with_name(f"{out.name}.{threading.get_ident():x}.part")
        tmp.write_bytes(final)
        tmp.replace(out)
        return {"url": url, "path": str(out), "status": "ok",
                "bytes": len(final), "original_bytes": len(raw)}

    def _update_stats(self, raw: bytes, final: bytes) -> None:
        self.stats["bytes_original"] += len(raw)
        stats_key = "recompressed" if len(final) < len(raw) else "kept_original"
        self.stats[stats_key] += 1
        self.stats["bytes_final"] += len(final)

    def _compress(self, raw: bytes) -> bytes:
        """Recompress to WebP (or keep original if smaller / PIL missing)."""
        if not _HAS_PIL:
            return raw
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.mode or img.mode == "P"
                                  else "RGB")
            if self.config.max_dimension:
                img.thumbnail((self.config.max_dimension, self.config.max_dimension))
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=self.config.quality, method=6,
                     exact=False)
            out = buf.getvalue()
            return out if len(out) < len(raw) else raw
        except Exception:
            return raw  # any compression failure keeps the original bytes


__all__ = ["ImageDownloader", "ImageConfig"]
