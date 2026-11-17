"""End-to-end pipeline: fetch listings → details → galleries → images.

Production runner for "get everything" runs. Two concurrency planes:
- The FETCH plane walks countries with normal stealth pacing (the
  transport serializes API/page requests and their pacing).
- The IMAGE plane downloads gallery images concurrently through the SAME
  session/persona with browser-like image headers, burst pacing, and
  resume logic.

Result: total wall time ≈ max(fetch time, image time), not the sum.

Exports (JSON/CSV/XLSX) are written at the end from the collected state,
including per-listing image results (paths + counts) merged into rows.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from .client import FerrariPreownedClient
from .errors import ThrottledError
from .images import ImageConfig, ImageDownloader
from .models import Gallery, Listing, ListingDetail

_SENTINEL = object()


class Progress:
    """Thread-safe progress tracker for the pipeline."""

    def __init__(self):
        self._lock = threading.Lock()
        self.countries_done = 0
        self.countries_total = 0
        self.listings_seen = 0
        self.images_ok = 0
        self.images_failed = 0
        self.images_total = 0
        self.started = time.monotonic()
        self.phase = "init"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "countries_done": self.countries_done,
                "countries_total": self.countries_total,
                "listings_seen": self.listings_seen,
                "images_ok": self.images_ok,
                "images_failed": self.images_failed,
                "images_total": self.images_total,
                "elapsed_s": round(time.monotonic() - self.started),
            }

    def render(self) -> str:
        s = self.snapshot()
        return (f"[{s['phase']}] countries {s['countries_done']}/{s['countries_total']}"
                f" | cars {s['listings_seen']} | images {s['images_ok']}/{s['images_total']}"
                f" (fail {s['images_failed']}) | {s['elapsed_s']}s")


def run_full_snapshot(client: FerrariPreownedClient,
                      target_dir: "str | Path",
                      *,
                      countries: Optional[Iterable[str]] = None,
                      include_details: bool = True,
                      image_config: Optional[ImageConfig] = None,
                      image_workers: int = 2,
                      progress: Optional[Callable[[str], None]] = None,
                      ) -> dict:
    """Fetch everything, download all images, export JSON/CSV/XLSX.

    Returns a summary dict. Wall time ≈ max(fetch, images).
    """
    from .export import export_csv, export_json, export_xlsx

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    images_dir = target / "images"

    prog = Progress()

    def log(msg: str) -> None:
        print(msg, flush=True) if progress is None else progress(msg)

    downloader = ImageDownloader(client.transport,
                                 config=image_config or ImageConfig())

    # ---- shared state -----------------------------------------------------
    listings: dict[str, Listing] = {}
    details: dict[str, ListingDetail] = {}
    galleries: dict[str, Gallery] = {}
    image_results: dict[str, dict] = {}
    fetch_errors: list[tuple[str, str]] = []
    state_lock = threading.Lock()
    gallery_q: "queue.Queue" = queue.Queue()

    # ---- fetch plane ------------------------------------------------------
    def fetch_worker() -> None:
        prog.phase = "fetch"
        counts = client.counts()
        wanted = list(countries) if countries is not None else \
            [c for c, n in counts.items() if n > 0]
        with state_lock:
            prog.countries_total = len(wanted)
        log(prog.render())

        for slug in wanted:
            try:
                country_listings = list(client.country_listings(slug))
            except Exception as exc:
                fetch_errors.append((slug, f"country_listings: {str(exc)[:150]}"))
                continue
            for listing in country_listings:
                key = listing.identity_key
                with state_lock:
                    listings[key] = listing
                    prog.listings_seen += 1

                if listing.images.homenet:
                    # US-style: full gallery already inline in the search
                    # response — build a Gallery from the inline URLs.
                    from .models import GalleryItem
                    gal = Gallery(
                        gallery_id="homenet:" + (listing.ad_id_blob or key),
                        items=[GalleryItem(content_id=str(i), content_url=u)
                               for i, u in enumerate(listing.images.homenet)],
                    )
                    with state_lock:
                        galleries[key] = gal
                    gallery_q.put((listing, gal))
                    continue

                if include_details:
                    try:
                        det = client.detail(listing)
                        with state_lock:
                            details[key] = det
                        gid = det.images.thron_hero_gallery_id or \
                            det.images.thron_card_gallery_id
                        if gid:
                            gal = client.gallery(det)
                            with state_lock:
                                galleries[key] = gal
                            gallery_q.put((listing, gal))
                    except Exception as exc:
                        fetch_errors.append((key, f"detail: {str(exc)[:150]}"))
                else:
                    gid = listing.images.thron_card_gallery_id
                    if gid:
                        try:
                            gal = client.gallery(listing)
                            with state_lock:
                                galleries[key] = gal
                            gallery_q.put((listing, gal))
                        except Exception as exc:
                            fetch_errors.append((key, f"gallery: {str(exc)[:150]}"))
            with state_lock:
                prog.countries_done += 1
            log(prog.render())

        for _ in range(image_workers):
            gallery_q.put(_SENTINEL)

    # ---- image plane ------------------------------------------------------
    def image_worker() -> None:
        while True:
            item = gallery_q.get()
            if item is _SENTINEL:
                gallery_q.task_done()
                return
            listing, gal = item
            urls = [u for u in gal.images if u]
            with state_lock:
                prog.images_total += len(urls)
            try:
                res = downloader.download_gallery(listing, gal, images_dir)
                # res["ok"] already counts skipped_existing (resume) as ok
                with state_lock:
                    prog.images_ok += res["ok"]
                    prog.images_failed += res["total"] - res["ok"]
                    image_results[listing.identity_key] = {
                        "ok": res["ok"], "total": res["total"],
                        "folder": res["folder"],
                    }
            except ThrottledError:
                with state_lock:
                    prog.images_failed += len(urls)
            gallery_q.task_done()
            log(prog.render())

    fetch_thread = threading.Thread(target=fetch_worker, name="fetch", daemon=True)
    image_threads = [threading.Thread(target=image_worker, name=f"img{i}",
                                      daemon=True)
                     for i in range(image_workers)]
    fetch_thread.start()
    for t in image_threads:
        t.start()
    fetch_thread.join()
    for t in image_threads:
        t.join()

    # ---- exports ----------------------------------------------------------
    prog.phase = "export"
    log(prog.render())
    listing_list = list(listings.values())
    export_json(listing_list, details, target / "ferrari_preowned.json")
    export_csv(listing_list, details, target / "ferrari_preowned.csv")
    export_xlsx(listing_list, details, target / "ferrari_preowned.xlsx",
                galleries=galleries)

    prog.phase = "done"
    log(prog.render())

    return {
        "target_dir": str(target),
        "listings": len(listings),
        "details": len(details),
        "galleries": len(galleries),
        "images_ok": prog.images_ok,
        "images_failed": prog.images_failed,
        "fetch_errors": fetch_errors,
        "stats": downloader.stats,
        "progress": prog.snapshot(),
    }


__all__ = ["run_full_snapshot", "Progress"]
