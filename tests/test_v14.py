"""v1.4: homenet URL persistence, audit dead-skip, prune, overshoot."""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferrari_preowned_client.engine import prune_orphans, RunProgress
from ferrari_preowned_client.models import Gallery, GalleryItem
from ferrari_preowned_client.smart import SmartStore

import json

from ferrari_preowned_client import contract, parsing

_FIXTURE = Path(__file__).parent / "fixtures" / "dk_page0.json"


@pytest.fixture(scope="module")
def dk_listings():
    return list(parsing.parse_listings(
        json.loads(_FIXTURE.read_text()), keystream=contract.KEYSTREAM_V1,
        decrypt_vins=True, source_url="test", raw_body=""))


# ------------------------------------------------------------- persistence

class TestHomenetPersistence:
    def test_record_homenet_roundtrip(self, dk_listings, tmp_path):
        store = SmartStore(tmp_path / "s.json")
        l = dk_listings[0]
        urls = ["https://content.homenetiol.com/a.jpg",
                "https://content.homenetiol.com/a.jpg",  # dup removed
                "https://content.homenetiol.com/b.jpg"]
        store.record_seen(l)
        store.record_homenet(l, urls)
        store.save()
        # fresh load keeps the urls, deduped, order preserved
        store2 = SmartStore(tmp_path / "s.json")
        st = store2.get(l.identity_key)
        assert st.homenet_urls == [
            "https://content.homenetiol.com/a.jpg",
            "https://content.homenetiol.com/b.jpg"]

    def test_engine_records_homenet_during_walk(self, dk_listings,
                                                tmp_path):
        """A homenet car walked by the engine gets its URLs persisted —
        this is the free back-fill (search response is fetched anyway)."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.smart import SmartStore
        import base64

        # turn one listing into a homenet car
        homenet_car = dk_listings[0]
        homenet_car.images.homenet = [
            "https://content.homenetiol.com/h1.jpg",
            "https://content.homenetiol.com/h2.jpg"]
        assert homenet_car.images.homenet

        store = SmartStore(tmp_path / "s.json")
        c = MagicMock()
        probe = MagicMock(); probe.status_code = 200
        c.transport.get.return_value = probe
        c.counts.return_value = {"DK": 1}
        c.country_listings.return_value = [homenet_car]
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")
        img_resp = MagicMock()
        img_resp.status_code = 200
        img_resp.content = png
        img_resp.raise_for_status = lambda: None
        c.transport._session.get.return_value = img_resp

        run_pipeline(c, tmp_path, mode="smart", store=store,
                     countries=["DK"], image_workers=1)
        st = store.get(homenet_car.identity_key)
        assert st.homenet_urls == [
            "https://content.homenetiol.com/h1.jpg",
            "https://content.homenetiol.com/h2.jpg"]
        # audit now EXPECTS the homenet files (completeness)
        from ferrari_preowned_client.engine import audit_images
        res = audit_images(tmp_path / "images", store)
        assert res["expected_files"] >= 2
        assert res["missing_files"] == 0  # engine downloaded them


# ------------------------------------------------------------------- audit

class TestAuditV14:
    def _store_with(self, tmp_path, listing, urls, dead=None):
        store = SmartStore(tmp_path / "s.json")
        store.record_seen(listing)
        store.record_gallery(listing, Gallery(
            gallery_id="g",
            items=[GalleryItem(content_id=str(i), content_url=u)
                   for i, u in enumerate(urls)]))
        if dead:
            store._meta["dead_image_urls"] = list(dead)
        store.save()
        return store

    def test_audit_skips_dead_urls(self, dk_listings, tmp_path):
        store = self._store_with(
            tmp_path, dk_listings[0],
            ["https://cdn.test/alive.jpg", "https://cdn.test/dead.jpg"],
            dead=["https://cdn.test/dead.jpg"])
        from ferrari_preowned_client.engine import audit_images
        res = audit_images(tmp_path / "images", store)
        assert res["expected_files"] == 1  # only the alive URL expected
        assert res["dead_urls_skipped"] == 1

    def test_audit_skips_removed_cars(self, dk_listings, tmp_path):
        store = self._store_with(tmp_path, dk_listings[0],
                                 ["https://cdn.test/x.jpg"])
        store.get(dk_listings[0].identity_key).removed = True
        store.save()
        from ferrari_preowned_client.engine import audit_images
        res = audit_images(tmp_path / "images", store)
        assert res["expected_files"] == 0
        assert res["cars_without_folder"] == 0  # not an error for sold cars


# ------------------------------------------------------------------- prune

class TestPrune:
    def _setup(self, tmp_path, dk_listings, extra_urls=()):
        """Live car with one current URL + one churned file on disk."""
        listing = dk_listings[0]
        key = listing.identity_key
        url = "https://cdn.test/current.jpg"
        store = SmartStore(tmp_path / "s.json")
        store.record_seen(listing)
        store.record_gallery(listing, Gallery(
            gallery_id="g",
            items=[GalleryItem(content_id="0", content_url=url)]))
        folder = tmp_path / "images" / f"{key}_488-2020"
        folder.mkdir(parents=True)
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        keep = folder / f"000_{digest}.webp"
        keep.write_bytes(b"keep")
        orphan = folder / "999_deadbeef01.webp"
        orphan.write_bytes(b"orphan" * 100)
        return store, folder, keep, orphan

    def test_dry_run_reports_but_deletes_nothing(self, tmp_path,
                                                 dk_listings):
        store, folder, keep, orphan = self._setup(tmp_path, dk_listings)
        res = prune_orphans(tmp_path / "images", store, apply=False)
        assert res["refused"] is False
        assert res["orphan_files"] == 1
        assert res["bytes_freed"] > 0
        assert keep.exists() and orphan.exists()  # dry run: nothing gone

    def test_apply_deletes_orphan_keeps_current(self, tmp_path,
                                                dk_listings):
        store, folder, keep, orphan = self._setup(tmp_path, dk_listings)
        res = prune_orphans(tmp_path / "images", store, apply=True)
        assert res["applied"] is True
        assert res["orphan_files"] == 1
        assert not orphan.exists()
        assert keep.exists()  # current-generation file survives

    def test_all_dead_car_is_protected(self, tmp_path, dk_listings):
        """A live car whose CURRENT urls are all dead upstream keeps all
        its files — old generations are the best data we have."""
        listing = dk_listings[0]
        key = listing.identity_key
        url = "https://cdn.test/only.jpg"
        store = SmartStore(tmp_path / "s.json")
        store.record_seen(listing)
        store.record_gallery(listing, Gallery(
            gallery_id="g",
            items=[GalleryItem(content_id="0", content_url=url)]))
        store._meta["dead_image_urls"] = [url]  # URL died upstream
        folder = tmp_path / "images" / f"{key}_488-2020"
        folder.mkdir(parents=True)
        old = folder / "000_abcdef1234.webp"
        old.write_bytes(b"precious old generation")
        res = prune_orphans(tmp_path / "images", store, apply=True)
        assert res["orphan_files"] == 0
        assert old.exists()  # protected: no data loss

    def test_sold_car_files_untouched(self, tmp_path, dk_listings):
        store, folder, keep, orphan = self._setup(tmp_path, dk_listings)
        key = dk_listings[0].identity_key
        store.get(key).removed = True
        store.save()
        res = prune_orphans(tmp_path / "images", store, apply=True)
        assert res["orphan_files"] == 0
        assert res["sold_car_files_untouched"] == 2  # keep + churn file
        assert keep.exists() and orphan.exists()

    def test_stale_store_refusal(self, tmp_path, dk_listings):
        """Most live cars without any URLs => refuse (would mass-delete)."""
        store = SmartStore(tmp_path / "s.json")
        for l in dk_listings[:10]:
            store.record_seen(l)  # no galleries recorded at all
        store.save()
        res = prune_orphans(tmp_path / "images", store)
        assert res["refused"] is True
        assert "stale" in res["reason"]

    def test_unattributed_folders_never_touched(self, tmp_path,
                                                dk_listings):
        store, folder, keep, orphan = self._setup(tmp_path, dk_listings)
        stranger = tmp_path / "images" / "UNKNOWN_CAR_1999"
        stranger.mkdir(parents=True)
        (stranger / "001_whoknows.webp").write_bytes(b"x")
        res = prune_orphans(tmp_path / "images", store, apply=True)
        assert res["orphan_files"] == 1  # only the known car's churn file
        assert res["unattributed_files_untouched"] == 1
        assert (stranger / "001_whoknows.webp").exists()


# --------------------------------------------------------------- overshoot

class TestOvershoot:
    def test_cars_overshoot_marked(self):
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 49
        p.set_cars_total(3397)
        for _ in range(3428):
            p.car_done()
        line = p.render(force=True)
        assert "cars 3428/3397+" in line

    def test_cars_exact_no_plus(self):
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 49
        p.set_cars_total(100)
        for _ in range(100):
            p.car_done()
        line = p.render(force=True)
        assert "cars 100/100 " in line or line.rstrip().endswith("100/100")
        assert "100/100+" not in line


# ------------------------------------------------------------------- misc

class TestDeadUrlProperty:
    def test_property_reads_meta(self, dk_listings, tmp_path):
        store = SmartStore(tmp_path / "s.json")
        store._meta["dead_image_urls"] = ["a", "b"]
        assert store.dead_image_urls == {"a", "b"}
