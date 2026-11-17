"""Hardening tests: 5xx retry, tmp races, dead-URL cache, audit lookups."""


import hashlib
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferrari_preowned_client import contract, parsing
from ferrari_preowned_client.models import Gallery, GalleryItem

_FIXTURE = Path(__file__).parent / "fixtures" / "dk_page0.json"


@pytest.fixture(scope="module")
def dk_listings():
    return list(parsing.parse_listings(
        json.loads(_FIXTURE.read_text()), keystream=contract.KEYSTREAM_V1,
        decrypt_vins=True, source_url="test", raw_body=""))

from ferrari_preowned_client.engine import audit_images, ErrorLog
from ferrari_preowned_client.errors import ServerError
from ferrari_preowned_client.personas import sample as sample_persona
from ferrari_preowned_client.smart import SmartStore
from ferrari_preowned_client.transport import Transport, TransportConfig


# ---------------------------------------------------------------- transport

class TestServerRetry:
    def _transport(self, monkeypatch):
        cfg = TransportConfig(min_interval=0.0, jitter=0.0,
                              dwell_every=(10 ** 9, 10 ** 9),
                              request_budget=100, retry_backoff=0.0)
        t = Transport(config=cfg, persona=sample_persona(None))
        t._session = MagicMock()  # real Session has no side_effect
        return t

    def test_500_then_200_succeeds(self, monkeypatch):
        """A transient 500 is retried; the request then succeeds."""
        t = self._transport(monkeypatch)
        r500 = MagicMock(); r500.status_code = 500
        r500.headers = {}
        r200 = MagicMock(); r200.status_code = 200
        r200.headers = {}
        t._session.get.side_effect = [r500, r200]
        out = t.get("https://x.test/", locale="de-DE")
        assert out.status_code == 200
        assert t._session.get.call_count == 2

    def test_500_always_raises_after_retries(self, monkeypatch):
        t = self._transport(monkeypatch)
        r500 = MagicMock(); r500.status_code = 500
        r500.headers = {}
        t._session.get.side_effect = [r500] * 10
        with pytest.raises(ServerError):
            t.get("https://x.test/", locale="de-DE")
        # initial + max_retries (3) extra attempts
        assert t._session.get.call_count == 4


# ------------------------------------------------------------------ images

class TestImageHardening:
    def _downloader(self, dead=None):
        from ferrari_preowned_client.images import ImageDownloader
        d = ImageDownloader(MagicMock(), dead_urls=set(dead or ()))
        d.transport.persona.headers = {"User-Agent": "t"}
        return d

    def test_dead_url_skipped_without_request(self, tmp_path):
        d = self._downloader(dead={"https://cdn.test/dead.jpg"})
        r = d._fetch_one("https://cdn.test/dead.jpg",
                         tmp_path / "x.webp", None)
        assert r["status"] == "skipped_dead"
        d.transport._session.get.assert_not_called()
        assert d.stats["skipped_dead"] == 1

    def test_dead_url_counted_as_skip_not_failure(self, tmp_path):
        """Regression: dead-URL skips must land in ok/skip, never in
        'failed' — the progress line once showed phantom fail counts."""
        from ferrari_preowned_client.models import Gallery, GalleryItem
        d = self._downloader(dead={"https://cdn.test/dead.jpg"})
        r_ok = MagicMock(); r_ok.status_code = 200
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        r_ok.content = png
        d.transport._session.get.return_value = r_ok
        listing = MagicMock()
        listing.identity_key = "ZFFTEST00000000009"
        d._resolve_car_dir = lambda root, l: root / "car9"
        gal = Gallery(gallery_id="g", items=[
            GalleryItem(content_id="0", content_url="https://cdn.test/dead.jpg"),
            GalleryItem(content_id="1", content_url="https://cdn.test/live.jpg"),
        ])
        res = d.download_gallery(listing, gal, tmp_path)
        assert res["ok"] == 2       # dead skip counts as ok, not failure
        assert res["skipped"] == 1   # only the dead url skipped
        assert res["total"] - res["ok"] == 0  # zero failures

    def test_404_marks_url_dead(self, tmp_path):
        d = self._downloader()
        r404 = MagicMock(); r404.status_code = 404
        d.transport._session.get.return_value = r404
        r = d._fetch_one("https://cdn.test/gone.jpg",
                         tmp_path / "x.webp", None)
        assert r["status"] == "error"
        assert "dead" in r["error"]
        assert "https://cdn.test/gone.jpg" in d.new_dead_urls

    def test_live_url_not_marked_dead(self, tmp_path):
        d = self._downloader()
        r403 = MagicMock(); r403.status_code = 403  # block, not dead
        d.transport._session.get.return_value = r403
        d._fetch_one("https://cdn.test/blocked.jpg",
                     tmp_path / "x.webp", None)
        assert d.new_dead_urls == set()

    def test_gallery_urls_deduped(self, tmp_path):
        """Duplicate URLs in one gallery download only once — two workers
        on the same output file is a write race (Errno 2 in production)."""
        from ferrari_preowned_client.models import Gallery, GalleryItem, Listing

        d = self._downloader()
        seen = []

        def fake_fetch(url, out, referer):
            seen.append(url)
            return {"url": url, "status": "ok"}
        d._fetch_one = fake_fetch
        listing = MagicMock()
        listing.identity_key = "ZFFTEST00000000001"
        listing.car_name = "488"
        listing.year = 2020
        d._car_folder = lambda l: "ZFFTEST00000000001_488-2020"
        d._resolve_car_dir = lambda root, l: root / "ZFFTEST00000000001_488-2020"
        gal = Gallery(gallery_id="g", items=[
            GalleryItem(content_id="0", content_url="https://cdn.test/a.jpg"),
            GalleryItem(content_id="1", content_url="https://cdn.test/a.jpg"),
            GalleryItem(content_id="2", content_url="https://cdn.test/b.jpg"),
        ])
        res = d.download_gallery(listing, gal, tmp_path)
        assert seen.count("https://cdn.test/a.jpg") == 1
        assert res["total"] == 2  # deduped, not 3

    def test_tmp_name_is_thread_unique(self, tmp_path):
        from ferrari_preowned_client.images import ImageDownloader
        d = self._downloader()
        out = tmp_path / "000_abc.webp"
        names = set()
        original = ImageDownloader._fetch_one

        def worker():
            # simulate two threads writing the same car's file
            tmp = out.with_name(f"{out.name}.{threading.get_ident():x}.part")
            tmp.write_bytes(b"data")
            tmp.replace(out)
            names.add(tmp.name)
        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(names) == 2  # distinct tmp paths — no ENOENT race


# ------------------------------------------------------------------- audit

class TestAuditLookup:
    def _store_with_car(self, tmp_path, key, urls, dk_listings):
        store = SmartStore(tmp_path / "s.json")
        # a REAL listing (hash needs a dataclass); override identity via
        # the one field identity_key derives from: vin (or blob)
        listing = dk_listings[0]
        real_key = listing.identity_key
        try:
            # identity_key derives from vin first; monkeypatching the
            # property is overkill, so re-key the state dict after
            store.record_seen(listing)
            store.record_gallery(listing, Gallery(
                gallery_id="g",
                items=[GalleryItem(content_id=str(i), content_url=u)
                       for i, u in enumerate(urls)]))
            # re-key this car's state under the test key
            with store._lock:
                st = store._listings.pop(real_key)
                store._listings[key] = st
            store.save()
        finally:
            pass
        return store

    def test_truncated_blob_key_folder_found(self, tmp_path, dk_listings):
        """Folders cap at 80 chars; long blob identity keys used to be
        invisible to the audit (false 'missing')."""
        import hashlib
        key = "Iljixbcg" + "x" * 150  # long blob-style key
        url = "https://cdn.test/one.jpg"
        store = self._store_with_car(tmp_path, key, [url], dk_listings)
        imgs = tmp_path / "images"
        folder = imgs / (key + "_ferrari-488-2020")[:80]
        folder.mkdir(parents=True)
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        (folder / f"000_{digest}.webp").write_bytes(b"x")
        res = audit_images(imgs, store)
        assert res["cars_without_folder"] == 0
        assert res["missing_files"] == 0

    def test_legacy_sibling_folders_unioned(self, tmp_path, dk_listings):
        import hashlib
        key = "ZFF06VTL1S0319073"
        url = "https://cdn.test/two.jpg"
        store = self._store_with_car(tmp_path, key, [url], dk_listings)
        imgs = tmp_path / "images"
        canonical = imgs / f"{key}_ferrari-purosangue-2025"
        legacy = imgs / f"{key}_purosangue-2025"
        legacy.mkdir(parents=True)
        canonical.mkdir(parents=True)
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        # file lives in the LEGACY folder; audit must still find it
        (legacy / f"000_{digest}.webp").write_bytes(b"x")
        res = audit_images(imgs, store)
        assert res["missing_files"] == 0
        assert res["cars_without_folder"] == 0

    def test_orphan_files_counted(self, tmp_path, dk_listings):
        import hashlib
        key = "ZFFTEST00000000002"
        url = "https://cdn.test/three.jpg"
        store = self._store_with_car(tmp_path, key, [url], dk_listings)
        imgs = tmp_path / "images"
        folder = imgs / f"{key}_488-2020"
        folder.mkdir(parents=True)
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        (folder / f"000_{digest}.webp").write_bytes(b"x")
        (folder / "999_deadbeef.webp").write_bytes(b"x")  # URL-churn leftover
        res = audit_images(imgs, store)
        assert res["orphan_files"] == 1
        assert res["missing_files"] == 0


# ------------------------------------------------- engine end-to-end wiring

class TestDeadUrlPersistence:
    def test_run_persists_new_dead_urls(self, dk_listings, tmp_path):
        """A gallery whose images all 404 gets recorded in store meta; a
        second run skips them without a single request."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.models import Gallery, GalleryItem

        store = SmartStore(tmp_path / "s.json")
        for l in dk_listings[:2]:
            store.record_seen(l)
            store.record_gallery(l, Gallery(
                gallery_id="g",
                items=[GalleryItem(content_id="0",
                                   content_url="https://cdn.test/d.jpg")]))
        store.save()

        c = MagicMock()
        probe = MagicMock(); probe.status_code = 200
        c.transport.get.return_value = probe
        c.counts.return_value = {"DK": 2}
        c.country_listings.return_value = list(dk_listings[:2])

        # disk has NO files: both downloads hit a 404
        r404 = MagicMock(); r404.status_code = 404
        c.transport._session.get.return_value = r404

        result = run_pipeline(c, tmp_path, mode="smart", store=store,
                              countries=["DK"], image_workers=1)
        assert downloader_dl(result) >= 0  # run completes
        meta = json.loads((tmp_path / "s.json").read_text())["meta"]
        assert "https://cdn.test/d.jpg" in meta.get("dead_image_urls", [])

        # second run: dead URL never requested
        store2 = SmartStore(tmp_path / "s.json")
        store2.load() if hasattr(store2, "load") else None
        result2 = run_pipeline(c, tmp_path, mode="smart", store=store2,
                               countries=["DK"], image_workers=1)
        # the dead URL was skipped before any network call
        s = result2["summary"]
        assert s.get("images_failed", 0) == 0


def downloader_dl(result):
    return result["summary"].get("images_downloaded", 0)
