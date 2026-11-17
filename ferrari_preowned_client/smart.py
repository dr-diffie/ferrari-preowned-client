"""Smart incremental mode: skip what we already own, fetch only the diff.

A ``SmartStore`` keeps a local state file recording, for every listing we
have ever fetched (keyed by ``identity_key`` = VIN > chassis > id-blob):

* the content hash of the listing's raw ad payload (search response),
* the detail hash (detail-page ad payload),
* the full image-URL list of its gallery (with per-URL download state).

On each run the pipeline compares fresh search results against the store:

* **unchanged** — raw ad hash identical AND same gallery URLs → fully
  skipped (zero detail/gallery/image requests).
* **changed** — price/odometer/etc. changed (hash differs) → re-fetch
  detail + new images; keep unchanged ones.
* **new** — full fetch (detail + gallery + images).

The store also records removals: listings in the store but absent from the
fresh search results are marked ``removed`` (with last-seen timestamps)
rather than deleted, so history is preserved.

Everything is deterministic and auditable: the store file is plain JSON
with the same provenance discipline as the rest of the library.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .models import Gallery, Listing, ListingDetail


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_obj(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
        .encode()).hexdigest()[:16]


#: Raw-ad fields excluded from comparison hashes. The ``id`` blob embeds a
#: per-capture epoch timestamp (payload = VIN + epoch-millis) and the vin
#: blob carries a decorative nonce — both rotate between captures while
#: the car itself is completely unchanged. Verified live 2026-09-25: with
#: these fields hashed, every listing re-classifies as "changed" on every
#: run; excluding them, byte-identical cars classify as "unchanged".
_VOLATILE_AD_FIELDS = {"id", "vin"}


def _listing_hash(listing: "Listing") -> str:
    """Semantic comparison hash: parsed fields + normalized raw ad.

    Hashing the parsed fields means the hash covers exactly what we export
    (all 59 columns); the normalized raw catches changes in fields we
    pass through but don't parse. Volatile blobs are excluded on purpose.
    """
    d = dataclasses.asdict(listing)
    d.pop("raw", None)
    d.pop("provenance", None)      # timestamps differ per capture
    d.pop("vin_blob", None)
    d.pop("ad_id_blob", None)
    raw = {k: v for k, v in (listing.raw or {}).items()
           if k not in _VOLATILE_AD_FIELDS}
    return _hash_obj({"semantic": d, "raw": raw})


def _detail_hash(detail: "ListingDetail") -> str:
    """Normalized hash of a detail-page ad (volatile blobs excluded)."""
    raw = {k: v for k, v in (detail.raw or {}).items()
           if k not in _VOLATILE_AD_FIELDS}
    return _hash_obj(raw)


class ListingState:
    """Per-listing record inside the SmartStore."""

    __slots__ = ("identity_key", "ad_hash", "detail_hash", "image_urls",
                 "homenet_urls", "first_seen", "last_seen", "last_change",
                 "removed", "country")

    def __init__(self, identity_key: str, ad_hash: Optional[str] = None,
                 detail_hash: Optional[str] = None,
                 image_urls: Optional[list[str]] = None,
                 homenet_urls: Optional[list[str]] = None,
                 first_seen: Optional[str] = None,
                 last_seen: Optional[str] = None,
                 last_change: Optional[str] = None,
                 removed: bool = False,
                 country: Optional[str] = None):
        self.identity_key = identity_key
        self.ad_hash = ad_hash
        self.detail_hash = detail_hash
        self.image_urls = list(image_urls or [])
        # US-market cars: gallery URLs come inline with every search
        # response (homenet partner). Persisted since v1.4 so the audit
        # and the dead-URL cache can see them.
        self.homenet_urls = list(homenet_urls or [])
        self.first_seen = first_seen or _utcnow_iso()
        self.last_seen = last_seen or self.first_seen
        self.last_change = last_change or self.first_seen
        self.removed = removed
        self.country = country

    def to_dict(self) -> dict:
        return {
            "ad_hash": self.ad_hash, "detail_hash": self.detail_hash,
            "image_urls": self.image_urls,
            "homenet_urls": self.homenet_urls,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen, "last_change": self.last_change,
            "removed": self.removed, "country": self.country,
        }

    @classmethod
    def from_dict(cls, identity_key: str, d: dict) -> "ListingState":
        return cls(
            identity_key,
            ad_hash=d.get("ad_hash"),
            detail_hash=d.get("detail_hash"),
            image_urls=d.get("image_urls"),
            homenet_urls=d.get("homenet_urls"),
            first_seen=d.get("first_seen"),
            last_seen=d.get("last_seen"),
            last_change=d.get("last_change"),
            removed=bool(d.get("removed")),
            country=d.get("country"),
        )


class SmartStore:
    """Local state store for incremental runs. JSON-file backed."""

    VERSION = 1

    def __init__(self, path: "str | Path"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._listings: dict[str, ListingState] = {}
        self._meta: dict[str, Any] = {"version": self.VERSION,
                                      "runs": 0, "created": _utcnow_iso()}
        self._dirty = False
        self.load()

    # ------------------------------------------------------------ lifecycle

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # corrupt store -> treat as empty; never crash a run
        self._meta.update({k: v for k, v in d.get("meta", {}).items()
                           if k != "version"})
        self._listings = {
            k: ListingState.from_dict(k, v)
            for k, v in (d.get("listings") or {}).items()
        }

    def save(self) -> None:
        with self._lock:
            payload = {
                "meta": {**self._meta,
                         "listings_count": len(self._listings),
                         "updated": _utcnow_iso()},
                "listings": {k: v.to_dict()
                             for k, v in self._listings.items()},
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False

    # ------------------------------------------------------------- queries

    def get(self, identity_key: str) -> Optional[ListingState]:
        return self._listings.get(identity_key)

    def has_identical(self, listing: Listing) -> bool:
        """True when the listing's raw ad is byte-identical to the store."""
        st = self._listings.get(listing.identity_key)
        if st is None or st.removed or st.ad_hash is None:
            return False
        return st.ad_hash == _listing_hash(listing)

    def classify(self, listing: Listing) -> str:
        """"new" | "unchanged" | "changed"."""
        st = self._listings.get(listing.identity_key)
        if st is None:
            return "new"
        if st.ad_hash == _listing_hash(listing):
            return "unchanged"
        return "changed"

    def gallery_changed(self, listing: Listing,
                        gallery_urls: Iterable[str]) -> bool:
        st = self._listings.get(listing.identity_key)
        if st is None:
            return True
        return sorted(st.image_urls or []) != sorted(gallery_urls)

    # ------------------------------------------------------------- updates

    def record_seen(self, listing: Listing) -> None:
        """Update last_seen for a listing (call after a successful fetch)."""
        with self._lock:
            st = self._listings.get(listing.identity_key)
            if st is None:
                st = ListingState(listing.identity_key,
                                  ad_hash=_listing_hash(listing))
                self._listings[listing.identity_key] = st
            else:
                st.ad_hash = _listing_hash(listing)
                st.last_seen = _utcnow_iso()
                st.removed = False
            st.country = listing.country_slug or st.country
            self._dirty = True

    def record_detail(self, listing: Listing, detail: ListingDetail) -> None:
        with self._lock:
            st = self._listings.get(listing.identity_key)
            if st is None:
                st = ListingState(listing.identity_key)
                self._listings[listing.identity_key] = st
            st.detail_hash = _detail_hash(detail)
            st.last_seen = _utcnow_iso()
            self._dirty = True

    def record_gallery(self, listing: Listing, gallery: Gallery) -> None:
        with self._lock:
            st = self._listings.get(listing.identity_key)
            if st is None:
                st = ListingState(listing.identity_key)
                self._listings[listing.identity_key] = st
            st.image_urls = [u for u in gallery.images if u]
            st.last_seen = _utcnow_iso()
            self._dirty = True
            st.last_change = _utcnow_iso()

    @property
    def dead_image_urls(self) -> set[str]:
        """URLs the image plane marked dead upstream (404 at origin).

        Never re-requested (zero-cost skip); excluded from the audit's
        expected set — a dead URL has no file on disk by design.
        """
        return set(self._meta.get("dead_image_urls") or ())

    def record_homenet(self, listing: Listing, urls: list[str]) -> None:
        """Persist the search-response gallery URLs of a US-market car
        (homenet partner). The URLs churn on the partner side; the
        current generation replaces the old one (old files become
        audit-visible orphans -> prune). Deduped, order-preserving:
        the order IS the on-disk filename index."""
        cleaned = [u for u in dict.fromkeys(urls) if u]
        with self._lock:
            st = self._listings.get(listing.identity_key)
            if st is None:
                st = ListingState(listing.identity_key)
                self._listings[listing.identity_key] = st
            if st.homenet_urls != cleaned:
                st.homenet_urls = cleaned
                st.last_change = _utcnow_iso()
            st.last_seen = _utcnow_iso()
            self._dirty = True

    def record_removals(self, seen_keys: Iterable[str],
                        *, only_countries: Optional[Iterable[str]] = None,
                        ) -> list[str]:
        """Mark store entries absent from ``seen_keys`` as removed.

        ``only_countries`` scopes the removal pass to listings whose
        recorded country is in that set — a subset run (e.g. only DK/AT/CH)
        must NOT mark cars from untouched countries as removed.
        """
        now = _utcnow_iso()
        scope = set(only_countries) if only_countries is not None else None
        removed: list[str] = []
        with self._lock:
            for k, st in self._listings.items():
                if scope is not None and (st.country is None or
                                          st.country not in scope):
                    continue
                if k not in seen_keys and not st.removed:
                    st.removed = True
                    st.last_change = now
                    removed.append(k)
                    self._dirty = True
        return removed

    # ------------------------------------------------------------- stats

    def stats(self) -> dict:
        with self._lock:
            return {
                "total": len(self._listings),
                "removed": sum(1 for s in self._listings.values() if s.removed),
                "with_details": sum(1 for s in self._listings.values()
                                    if s.detail_hash),
                "with_images": sum(1 for s in self._listings.values()
                                   if s.image_urls),
                "runs": self._meta.get("runs", 0),
            }


# ---------------------------------------------------------------------------
# Smart pipeline
# ---------------------------------------------------------------------------

def run_smart_snapshot(client, target_dir: "str | Path", *,
                       store_path: Optional["str | Path"] = None,
                       countries: Optional[Iterable[str]] = None,
                       image_config=None, image_workers: int = 2,
                       progress: Optional[Callable[[str], None]] = None) -> dict:
    """Incremental run: fetch only what's new or changed.

    Uses the same two-plane pipeline as ``run_full_snapshot`` but consults
    the SmartStore before every detail/gallery/image fetch:

    * ``unchanged`` (identical raw ad) → zero further requests; images
      already on disk are not touched.
    * ``changed`` / ``new`` → detail + gallery re-fetched; image download
      resumes automatically (URL-hash filenames skip existing files, so
      only genuinely NEW image URLs are downloaded).
    """
    from .images import ImageConfig, ImageDownloader
    from .pipeline import Progress

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    images_dir = target / "images"
    store = SmartStore(store_path or target / "smart_store.json")
    store._meta["runs"] = int(store._meta.get("runs", 0)) + 1

    prog = Progress()

    def log(msg: str) -> None:
        print(msg, flush=True) if progress is None else progress(msg)

    downloader = ImageDownloader(client.transport,
                                 config=image_config or ImageConfig())

    import queue as _queue
    from .models import GalleryItem

    listings: dict[str, Listing] = {}
    details: dict[str, ListingDetail] = {}
    galleries: dict[str, Gallery] = {}
    skipped: list[str] = []
    state_lock = threading.Lock()
    gallery_q = _queue.Queue()
    _SENTINEL = object()

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
            # throttle-aware country walk: a 202 mid-pagination pauses,
            # cools down, and retries the country (max 3 attempts) instead
            # of silently abandoning the rest of its listings.
            country_listings: Optional[list[Listing]] = None
            for attempt in range(3):
                try:
                    country_listings = list(client.country_listings(slug))
                    break
                except ThrottledError as exc:
                    cooldown = max(90.0, float(exc.retry_after or 0.0))
                    log(f"[fetch] throttled on {slug} (attempt {attempt + 1}/3)"
                        f" — cooling down {cooldown:.0f}s")
                    time.sleep(cooldown)
                except Exception as exc:
                    fetch_errors.append((slug, f"country_listings: {str(exc)[:150]}"))
                    break
            if country_listings is None:
                continue
            for listing in country_listings:
                key = listing.identity_key
                with state_lock:
                    listings[key] = listing
                    prog.listings_seen += 1

                cls = store.classify(listing)
                if cls == "unchanged":
                    with state_lock:
                        skipped.append(key)
                    store.record_seen(listing)
                    # Resume-safe image completion: unchanged cars still
                    # queue their gallery for the image plane — files on
                    # disk resume-skip locally (zero network), any MISSING
                    # files (e.g. a prior run was killed mid-drain) are
                    # downloaded. US homenet URLs come from this fresh
                    # search; EU URLs from the store's recorded gallery.
                    gal = None
                    if listing.images.homenet:
                        gal = Gallery(
                            gallery_id="homenet:" + (listing.ad_id_blob or key),
                            items=[GalleryItem(content_id=str(i), content_url=u)
                                   for i, u in enumerate(listing.images.homenet)],
                        )
                    else:
                        st = store.get(key)
                        if st and st.image_urls:
                            gal = Gallery(
                                gallery_id="store:" + key,
                                items=[GalleryItem(content_id=str(i), content_url=u)
                                       for i, u in enumerate(st.image_urls)],
                            )
                    if gal is not None and gal.items:
                        with state_lock:
                            galleries[key] = gal
                        gallery_q.put((listing, gal))
                    continue  # zero data requests for this car

                if listing.images.homenet:
                    gal = Gallery(
                        gallery_id="homenet:" + (listing.ad_id_blob or key),
                        items=[GalleryItem(content_id=str(i), content_url=u)
                               for i, u in enumerate(listing.images.homenet)],
                    )
                    with state_lock:
                        galleries[key] = gal
                    gallery_q.put((listing, gal))
                    store.record_seen(listing)
                    continue

                # ALWAYS record the (semantic) ad hash on a successful
                # search sighting — this is what makes future runs skip
                # unchanged cars. Verified live: raw ads are byte-identical
                # between captures, so classify() then returns unchanged.
                store.record_seen(listing)

                try:
                    det = client.detail(listing)
                    with state_lock:
                        details[key] = det
                    store.record_detail(listing, det)
                    gid = det.images.thron_hero_gallery_id or \
                        det.images.thron_card_gallery_id
                    if gid:
                        gal = client.gallery(det)
                        with state_lock:
                            galleries[key] = gal
                        store.record_gallery(listing, gal)
                        gallery_q.put((listing, gal))
                except Exception as exc:
                    fetch_errors.append((key, f"detail: {str(exc)[:150]}"))
            with state_lock:
                prog.countries_done += 1
            log(prog.render())
            # checkpoint the store after every country so a crash at hour 5
            # loses nothing already fetched
            store.save()

        for _ in range(image_workers):
            gallery_q.put(_SENTINEL)

    fetch_errors: list = []

    def store_has_complete_detail(store_, listing) -> bool:
        st = store_.get(listing.identity_key)
        return bool(st and st.detail_hash and st.image_urls)

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
            except ThrottledError:
                with state_lock:
                    prog.images_failed += len(urls)
            gallery_q.task_done()
            log(prog.render())

    from .errors import ThrottledError  # noqa: F401 (used in image_worker)

    fetch_thread = threading.Thread(target=fetch_worker, daemon=True)
    image_threads = [threading.Thread(target=image_worker, daemon=True)
                     for _ in range(image_workers)]
    fetch_thread.start()
    for t in image_threads:
        t.start()
    fetch_thread.join()
    for t in image_threads:
        t.join()

    # ---- removals + exports + save ---------------------------------------
    store.record_removals([l.identity_key for l in listings.values()])
    store.save()

    prog.phase = "export"
    listing_list = list(listings.values())
    from .export import export_csv, export_json, export_xlsx
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
        "skipped_unchanged": len(skipped),
        "images_ok": prog.images_ok,
        "images_failed": prog.images_failed,
        "fetch_errors": fetch_errors,
        "store_stats": store.stats(),
        "stats": downloader.stats,
        "progress": prog.snapshot(),
    }


__all__ = ["SmartStore", "ListingState", "run_smart_snapshot"]
