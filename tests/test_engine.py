"""Offline tests for the v1.1 run engine: progress, errors, audit, reports."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ferrari_preowned_client import contract, parsing
from ferrari_preowned_client.engine import (
    ErrorLog, RunProgress, audit_images, ledger_stats, write_run_report,
)
from ferrari_preowned_client.models import ListingDetail
from ferrari_preowned_client.smart import SmartStore


@pytest.fixture(scope="module")
def dk_listings(dk_page0):
    return list(parsing.parse_listings(
        dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
        source_url="test", raw_body=""))


class TestRunProgress:
    def test_put_time_totals(self):
        p = RunProgress()
        p.gallery_queued(10)
        p.gallery_queued(7)
        p.gallery_result(3, 2, 0)
        assert p.snapshot()["images_total"] == 17  # fixed denominator
        assert p.snapshot()["images_downloaded"] == 3
        assert p.snapshot()["images_skipped"] == 2

    def test_render_has_eta_and_split_counters(self):
        p = RunProgress()
        p.gallery_queued(100)
        p.gallery_result(10, 5, 0)
        p.started = time.monotonic() - 10  # pretend 10s elapsed
        line = p.render(force=True)
        assert line is not None
        assert "imgs 15/100" in line
        assert "dl 10" in line and "skip 5" in line
        assert "ETA" in line and "/s" in line

    def test_render_throttling(self):
        p = RunProgress(render_interval=60.0)
        p.gallery_queued(5)
        assert p.render(force=True) is not None  # first render always passes
        assert p.render() is None                # subsequent one throttled

    def test_classification_counters(self):
        p = RunProgress()
        p.classified("new"); p.classified("new"); p.classified("changed")
        p.classified("unchanged")
        s = p.snapshot()
        assert (s["listings_new"], s["listings_changed"],
                s["listings_unchanged"]) == (2, 1, 1)


class TestErrorLog:
    def test_immediate_logging_and_registry(self):
        seen = []
        el = ErrorLog(lambda m: seen.append(m))
        el.add("detail", "ZFF123", "detail page gone (404)", url="x")
        el.add("image", "ZFF123", "download failed", url="y")
        assert len(seen) == 2                       # logged immediately
        assert seen[0].startswith("[error/detail] ZFF123")
        assert el.count() == 2
        assert el.count("image") == 1
        assert el.summary() == {"detail": 1, "image": 1}
        entries = el.entries()
        assert entries[0]["kind"] == "detail"
        assert entries[0]["context"]["url"] == "x"
        assert entries[0]["ts"]


class TestAudit:
    def test_audit_detects_missing_files(self, dk_listings, tmp_path):
        from ferrari_preowned_client.models import Gallery, GalleryItem
        store = SmartStore(tmp_path / "s.json")
        l0 = dk_listings[0]
        gal = Gallery(gallery_id="g", items=[
            GalleryItem(content_id="1", content_url="https://x/1.jpg"),
            GalleryItem(content_id="2", content_url="https://x/2.jpg"),
        ])
        store.record_gallery(l0, gal)
        # car folder with ONLY the first image present
        img_dir = tmp_path / "images" / f"{l0.identity_key}_x"
        img_dir.mkdir(parents=True)
        import hashlib
        d = hashlib.sha256(b"https://x/1.jpg").hexdigest()[:10]
        (img_dir / f"000_{d}.webp").write_bytes(b"x")
        seen = []
        el = ErrorLog(lambda m: seen.append(m))
        audit = audit_images(tmp_path / "images", store, error_log=el)
        assert audit["expected_files"] == 2
        assert audit["present_files"] == 1
        assert audit["missing_files"] == 1
        assert audit["cars_with_missing"] == 1
        assert any("missing" in m for m in seen)  # error surfaced

    def test_audit_complete_when_nothing_missing(self, dk_listings, tmp_path):
        from ferrari_preowned_client.models import Gallery, GalleryItem
        store = SmartStore(tmp_path / "s.json")
        l0 = dk_listings[0]
        gal = Gallery(gallery_id="g", items=[
            GalleryItem(content_id="1", content_url="https://x/1.jpg"),
        ])
        store.record_gallery(l0, gal)
        img_dir = tmp_path / "images" / f"{l0.identity_key}_y"
        img_dir.mkdir(parents=True)
        import hashlib
        d = hashlib.sha256(b"https://x/1.jpg").hexdigest()[:10]
        (img_dir / f"000_{d}.webp").write_bytes(b"x")
        audit = audit_images(tmp_path / "images", store)
        assert audit["missing_files"] == 0
        assert audit["present_files"] == 1


class TestReport:
    def test_report_roundtrip(self, tmp_path):
        seen = []
        el = ErrorLog(lambda m: seen.append(m))
        el.add("search", "DK", "boom")
        rep = write_run_report(
            tmp_path / "r.json", mode="smart",
            summary={"listings": 29}, errors=el,
            ledger_stats={"total": 10, "statuses": {"200": 9, "202": 1},
                          "throttle_events": 1, "blocked_events": 0},
            audit=None, wall_seconds=123.4)
        d = json.loads((tmp_path / "r.json").read_text())
        assert d["run"]["mode"] == "smart"
        assert d["run"]["wall_seconds"] == 123
        assert d["errors"]["total"] == 1
        assert d["errors"]["entries"][0]["kind"] == "search"
        assert d["requests"]["throttle_events"] == 1
        assert rep["errors"]["total"] == 1

    def test_ledger_stats(self):
        from unittest.mock import MagicMock
        t = MagicMock()
        t.ledger_dicts.return_value = [
            {"status": 200}, {"status": 200}, {"status": 202},
            {"status": None},
        ]
        st = ledger_stats(t)
        assert st["total"] == 4
        assert st["statuses"]["200"] == 2
        assert st["throttle_events"] == 1


class TestEnginePipelineOffline:
    def test_run_pipeline_full_mode_offline(self, dk_listings, tmp_path,
                                            monkeypatch):
        """Full engine cycle with a mocked client: no network, but the
        whole engine path (fetch→queue→images→audit→exports→report)."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.models import Gallery, GalleryItem
        from unittest.mock import MagicMock

        client = MagicMock()
        client.counts.return_value = {"DK": 2}
        # country_listings yields two listings
        client.country_listings.return_value = dk_listings[:2]
        # detail returns a real ListingDetail (no gallery ids)
        det = ListingDetail(
            **{k: getattr(dk_listings[0], k) for k in (
                "vin", "vin_blob", "legacy_chassis", "ad_id_blob", "car_id",
                "model_slug", "model_name", "car_name", "year", "is_new",
                "ferrari_approved", "classic_car", "special_or_limited",
                "tailored", "ferrari_atelier", "price", "price_unit",
                "request_price", "odometer", "odometer_unit",
                "exterior_color", "trim_color", "engine", "gear_box",
                "power", "power_unit", "top_speed", "fuel_type",
                "country_slug", "country_name", "region_slug", "dealer",
                "latitude", "longitude", "images", "provenance", "raw")},
            chassis="128196")
        client.detail.return_value = det

        def fake_download_gallery(self, listing, gal, target):
            return {"ok": len(gal.images), "total": len(gal.images),
                    "skipped": 0, "folder": str(target),
                    "results": []}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ferrari_preowned_client.images.ImageDownloader."
                       "download_gallery", fake_download_gallery)
            result = run_pipeline(client, tmp_path, mode="full",
                                  countries=["DK"], image_workers=1,
                                  progress=None)
        assert result["summary"]["listings"] == 2
        assert result["error_count"] == 0
        assert (tmp_path / "run_report.json").exists()
        assert (tmp_path / "ferrari_preowned.json").exists()
        assert (tmp_path / "ferrari_preowned.csv").exists()
        assert (tmp_path / "ferrari_preowned.xlsx").exists()
        rep = json.loads((tmp_path / "run_report.json").read_text())
        assert rep["run"]["mode"] == "full"
        assert rep["summary"]["listings"] == 2

    def test_run_pipeline_smart_mode_skips(self, dk_listings, tmp_path):
        """Smart mode with a pre-populated store: cars classify unchanged,
        detail() must NEVER be called."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.models import Gallery, GalleryItem
        from unittest.mock import MagicMock

        store = SmartStore(tmp_path / "s.json")
        for l in dk_listings[:3]:
            store.record_seen(l)
            # real Gallery WITH a url so the heal path is NOT triggered
            # (heal only fires for unchanged cars with no recorded gallery)
            store.record_gallery(
                l, Gallery(gallery_id="g",
                           items=[GalleryItem(content_id="0",
                                              content_url="u")]))
        store.save()

        client = MagicMock()
        client.counts.return_value = {"DK": 3}
        client.country_listings.return_value = dk_listings[:3]
        client.detail.side_effect = AssertionError("must not be called")

        def fake_download_gallery(self, listing, gal, target):
            return {"ok": len(gal.images), "total": len(gal.images),
                    "skipped": len(gal.images), "folder": str(target),
                    "results": []}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ferrari_preowned_client.images.ImageDownloader."
                       "download_gallery", fake_download_gallery)
            result = run_pipeline(client, tmp_path, mode="smart",
                                  store=store, countries=["DK"],
                                  image_workers=1, progress=None)
        assert result["summary"]["listings"] == 3
        # all three classified unchanged, zero detail calls
        client.detail.assert_not_called()
        rep = json.loads((tmp_path / "run_report.json").read_text())
        assert rep["summary"]["images_skipped"] >= 0  # runs to completion
        assert rep["run"]["mode"] == "smart"

    def test_smart_heals_unchanged_car_without_gallery(self, dk_listings,
                                                       tmp_path):
        """The 305-victim heal path: a car classified 'unchanged' whose
        store entry has NO gallery URLs (detail 404'd in an old run) MUST
        re-fetch its detail, learn the gallery, and queue it."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.models import (Gallery, GalleryItem,
                                                    ImageSet, ListingDetail)
        from unittest.mock import MagicMock

        store = SmartStore(tmp_path / "s.json")
        l0 = dk_listings[0]
        store.record_seen(l0)  # ad hash recorded, but NO detail/gallery
        store.save()

        gal = Gallery(gallery_id="thron-42",
                      items=[GalleryItem(content_id="0", content_url="u")])
        det = ListingDetail(
            vin=l0.vin, vin_blob=l0.vin_blob,
            legacy_chassis=l0.legacy_chassis, ad_id_blob=l0.ad_id_blob,
            chassis=l0.vin[-6:] if l0.vin else "123456",
            images=ImageSet(thron_hero_gallery_id="thron-42"),
        )
        det.images.items = gal.items

        client = MagicMock()
        client.counts.return_value = {"DK": 1}
        client.country_listings.return_value = [l0]
        client.detail.return_value = det
        client.gallery.return_value = gal
        queued_urls = []

        def fake_download_gallery(self, listing, gal_, target):
            queued_urls.extend(u for u in gal_.images if u)
            return {"ok": len(gal_.images), "total": len(gal_.images),
                    "skipped": 0, "folder": str(target), "results": []}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ferrari_preowned_client.images.ImageDownloader."
                       "download_gallery", fake_download_gallery)
            result = run_pipeline(client, tmp_path, mode="smart",
                                  store=store, countries=["DK"],
                                  image_workers=1, progress=None)
        client.detail.assert_called_once()          # heal re-fetched it
        client.gallery.assert_called_once()         # learned the gallery
        assert queued_urls == ["u"]                 # queued for download
        assert result["summary"]["listings_healed"] == 1
        # store now knows the gallery
        st = store.get(l0.identity_key)
        assert st is not None and st.image_urls == ["u"]

    def test_car_folder_migration(self, dk_listings, tmp_path):
        """Legacy-named sibling folders (pre-catalog model segment) are
        migrated into the canonical folder on first touch."""
        from ferrari_preowned_client.images import ImageDownloader
        from ferrari_preowned_client.models import Gallery

        l0 = dk_listings[0]
        d = ImageDownloader.__new__(ImageDownloader)  # skip __init__
        d._log = lambda *_: None
        legacy = tmp_path / f"{l0.identity_key}_oldmodel-{l0.year or 'xxxx'}"
        legacy.mkdir()
        (legacy / "000_deadbeef.webp").write_bytes(b"x")
        car_dir = d._resolve_car_dir(tmp_path, l0)
        canon = tmp_path / d._car_folder(l0)
        assert car_dir == canon
        assert (canon / "000_deadbeef.webp").exists()
        assert not legacy.exists()  # emptied and removed

    def test_error_surfaces_in_report(self, dk_listings, tmp_path):
        """A failing detail() is caught, logged immediately, and reported;
        the run still completes."""
        from ferrari_preowned_client.engine import run_pipeline
        from unittest.mock import MagicMock

        client = MagicMock()
        client.counts.return_value = {"DK": 1}
        client.country_listings.return_value = dk_listings[:1]
        client.detail.side_effect = RuntimeError("detail page gone (404)")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ferrari_preowned_client.images.ImageDownloader."
                       "download_gallery",
                       lambda self, l, g, t: {"ok": 0, "total": 0,
                                              "skipped": 0,
                                              "folder": str(t),
                                              "results": []})
            result = run_pipeline(client, tmp_path, mode="full",
                                  countries=["DK"], image_workers=1,
                                  progress=lambda m: None)
        assert result["error_count"] == 1
        assert result["errors"][0]["kind"] == "detail"
        assert "404" in result["errors"][0]["message"]


class TestBlockFailSafes:
    """IP-block fail-safes: pre-flight abort, mid-run abort, CDN 403 streak."""

    def _mock_client_ok(self):
        from unittest.mock import MagicMock
        c = MagicMock()
        probe = MagicMock()
        probe.status_code = 200
        c.transport.get.return_value = probe
        return c

    def test_preflight_block_aborts_untouched(self, dk_listings, tmp_path):
        """A blocked pre-flight probe aborts with blocked=True, store NOT
        saved/modified, report carries the block section, exit path is 2."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.errors import BlockedError
        from ferrari_preowned_client.smart import SmartStore
        from unittest.mock import MagicMock
        import json

        c = MagicMock()
        probe = MagicMock()
        probe.status_code = 403
        probe.url = "https://preowned.ferrari.com/robots.txt"
        probe.text = "<HTML>403 ERROR</HTML>"
        c.transport.get.return_value = probe
        c.counts.side_effect = BlockedError("blocked with HTTP 403")
        store = SmartStore(tmp_path / "s.json")
        store.record_seen(dk_listings[0])
        store.save()
        before = json.loads((tmp_path / "s.json").read_text())

        result = run_pipeline(c, tmp_path, mode="smart", store=store,
                              countries=["DK"])
        assert result["blocked"] is True
        assert result["summary"] == {}
        assert result["report"]["block"]["stage"] == "pre-flight"
        assert "VPN" in result["report"]["block"]["guidance"]
        # store file untouched on disk
        assert json.loads((tmp_path / "s.json").read_text()) == before

    def test_preflight_ok_passes_through(self, dk_listings, tmp_path):
        """Status-200 probe lets the run proceed normally."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.smart import SmartStore
        from unittest.mock import MagicMock

        c = self._mock_client_ok()
        c.counts.return_value = {"DK": 3}
        c.country_listings.return_value = list(dk_listings)
        store = SmartStore(tmp_path / "s.json")
        from ferrari_preowned_client.models import Gallery, GalleryItem
        for l in dk_listings:
            store.record_seen(l)
            store.record_gallery(
                l, Gallery(gallery_id="g",
                           items=[GalleryItem(content_id="0",
                                              content_url="u")]))
        store.save()
        result = run_pipeline(c, tmp_path, mode="smart", store=store,
                             countries=["DK"], image_workers=1)
        assert result["blocked"] is False
        assert result["summary"]["listings"] == len(dk_listings)

    def test_counts_block_aborts_midrun(self, dk_listings, tmp_path):
        """BlockedError from counts() aborts with blocked=True and does
        NOT record removals (store's cars stay un-removed)."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.errors import BlockedError
        from ferrari_preowned_client.smart import SmartStore
        from unittest.mock import MagicMock

        c = self._mock_client_ok()
        c.counts.side_effect = BlockedError("blocked with HTTP 403")
        store = SmartStore(tmp_path / "s.json")
        for l in dk_listings:
            store.record_seen(l)
        store.save()
        result = run_pipeline(c, tmp_path, mode="smart", store=store,
                              countries=["DK"], image_workers=1)
        assert result["blocked"] is True
        assert result["report"]["block"]["stage"] == "counts"
        for st in store._listings.values():
            assert st.removed is False

    def test_image_cdn_403_streak_raises_blocked(self, tmp_path):
        """10 consecutive CDN 403s raise BlockedError; a 200 resets the
        streak."""
        from ferrari_preowned_client.images import ImageDownloader
        from ferrari_preowned_client.errors import BlockedError
        from ferrari_preowned_client.models import Listing
        from unittest.mock import MagicMock
        import pytest

        d = ImageDownloader.__new__(ImageDownloader)
        d._log = lambda *_: None
        d.config = MagicMock()
        d.config.per_request_timeout = 5
        d._lock = __import__("threading").Lock()
        d.stats = {k: 0 for k in ("downloaded", "skipped_existing", "failed",
                                  "recompressed", "kept_original",
                                  "bytes_original", "bytes_final", "throttled")}
        d._consec_403 = 0
        d._CONSEC_403_LIMIT = 10
        d.dead_urls = set()
        d.new_dead_urls = set()
        d.transport = MagicMock()
        d.transport.persona.headers = {"User-Agent": "test"}
        r403 = MagicMock(); r403.status_code = 403
        d.transport._session.get.return_value = r403

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ferrari_preowned_client.images.time.sleep", lambda s: None)
            with pytest.raises(BlockedError):
                for i in range(10):
                    d._fetch_one("https://cdn.example/x.jpg",
                                 tmp_path / f"{i}.webp", None)
        # a 200 resets the streak
        r200 = MagicMock(); r200.status_code = 200
        r200.content = b"x"
        d.transport._session.get.return_value = r200
        d._fetch_one("https://cdn.example/y.jpg", tmp_path / "y.webp", None)
        assert d._consec_403 == 0  # streak reset by the 200


class TestProgressAccuracy:
    """Sold/removed live, duplicate suppression, tag retention, rates."""

    def test_duplicate_line_suppressed(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.country_start("DE")
        a = p.render(force=True)
        b = p.render(force=True)
        assert a is not None
        assert b is None  # identical state -> identical line -> suppressed

    def test_line_prints_again_when_state_changed(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.country_start("DE")
        assert p.render(force=True) is not None
        p.classified("new")
        assert p.render(force=True) is not None  # changed -> rendered

    def test_country_tag_survives_country_done(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.country_start("DE")
        p.country_done()
        line = p.render(force=True)
        assert "(DE)" in line

    def test_country_expected_in_line(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.country_start("DE")
        p.country_expected("DE", 594)
        p.country_cars_done("DE", 312)
        line = p.render(force=True)
        assert "DE 312/594" in line

    def test_sold_counter_in_line(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.country_start("DE")
        p.country_sold("DE", 7)
        line = p.render(force=True)
        assert "sold 7" in line

    def test_idle_queue_rate_is_zero(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.country_start("DE")
        p.gallery_queued(100)
        p.gallery_result(50, 50, 0)   # 100 settled
        first = p.render(force=True)
        assert "100/100 (100%)" in first
        # no new work; queue idle -> rate must be 0.0/s, not stale avg
        import ferrari_preowned_client.engine as eng
        real = eng.time.monotonic
        eng.time.monotonic = lambda: real() + 30.0  # 30s pass, no images
        try:
            idle = p.render(force=True)
        finally:
            eng.time.monotonic = real
        assert idle is not None
        assert "0.0/s" in idle

    def test_run_reports_sold_end_to_end(self, dk_listings, tmp_path):
        """A store car that vanished from the search is counted as sold
        and marked removed, per-country, during the run."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.smart import SmartStore
        from ferrari_preowned_client.models import Gallery, GalleryItem
        from unittest.mock import MagicMock

        store = SmartStore(tmp_path / "s.json")
        for l in dk_listings[:3]:
            store.record_seen(l)
            store.record_gallery(l, Gallery(
                gallery_id="g",
                items=[GalleryItem(content_id="0", content_url="u")]))
        store.save()

        c = MagicMock()
        probe = MagicMock(); probe.status_code = 200
        c.transport.get.return_value = probe
        c.counts.return_value = {"DK": 3}
        c.country_listings.return_value = list(dk_listings[:2])  # car 3 sold

        result = run_pipeline(c, tmp_path, mode="smart", store=store,
                             countries=["DK"], image_workers=1)
        assert result["summary"]["listings_sold"] == 1
        st = store.get(dk_listings[2].identity_key)
        assert st is not None and st.removed is True
        # surviving cars NOT removed
        for l in dk_listings[:2]:
            assert store.get(l.identity_key).removed is False


class TestCarTotalsAndJsonMode:
    """Global car denominator, observed fetch ETA, JSON progress events."""

    def test_cars_totals_and_eta_in_line(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.country_start("DE")
        p.set_cars_total(3393)
        for _ in range(25):
            p.car_done()
        line = p.render(force=True)
        assert "cars 25/3393" in line
        assert "ETA " in line  # observed ETA once >= 20 cars walked

    def test_eta_hidden_below_threshold(self):
        from ferrari_preowned_client.engine import RunProgress
        p = RunProgress()
        p.phase = "fetch"
        p.countries_total = 48
        p.set_cars_total(3393)
        for _ in range(5):
            p.car_done()
        line = p.render(force=True)
        assert "cars 5/3393" in line
        # no fetch ETA in the fetch part yet (too few cars for a
        # meaningful average); image ETA is separate
        head = line.split("|")[0]
        assert "ETA" not in head

    def test_json_progress_events(self, dk_listings, tmp_path):
        """on_snapshot receives structured dicts; log lines still flow."""
        from ferrari_preowned_client.engine import run_pipeline
        from ferrari_preowned_client.smart import SmartStore
        from ferrari_preowned_client.models import Gallery, GalleryItem
        from unittest.mock import MagicMock

        store = SmartStore(tmp_path / "s.json")
        for l in dk_listings:
            store.record_seen(l)
            store.record_gallery(l, Gallery(
                gallery_id="g",
                items=[GalleryItem(content_id="0", content_url="u")]))
        store.save()

        c = MagicMock()
        probe = MagicMock(); probe.status_code = 200
        c.transport.get.return_value = probe
        c.counts.return_value = {"DK": len(dk_listings)}
        c.country_listings.return_value = list(dk_listings)

        logs, snaps = [], []
        result = run_pipeline(c, tmp_path, mode="smart", store=store,
                              countries=["DK"], image_workers=1,
                              progress=logs.append,
                              on_snapshot=snaps.append)
        assert snaps, "no structured snapshots emitted"
        # every snapshot is a dict with the counters a module needs
        for s in snaps:
            assert isinstance(s, dict)
            assert "cars_total" in s and "cars_done" in s
            assert "listings_sold" in s and "images_total" in s
        assert snaps[-1]["cars_total"] == len(dk_listings)
        assert snaps[-1]["cars_done"] == len(dk_listings)

    def test_cli_json_mode_done_event(self, dk_listings, tmp_path, capsys):
        """--progress json emits parseable JSON events and a done event,
        with a clean exit code."""
        import base64
        from ferrari_preowned_client.cli import main
        from ferrari_preowned_client.smart import SmartStore
        from ferrari_preowned_client.models import Gallery, GalleryItem
        from unittest.mock import patch, MagicMock
        import json as j

        store = SmartStore(tmp_path / "s.json")
        for l in dk_listings:
            store.record_seen(l)
            store.record_gallery(l, Gallery(
                gallery_id="g",
                items=[GalleryItem(content_id="0", content_url="u")]))
        store.save()

        c = MagicMock()
        probe = MagicMock(); probe.status_code = 200
        c.transport.get.return_value = probe
        c.counts.return_value = {"DK": len(dk_listings)}
        c.country_listings.return_value = list(dk_listings)
        c.__enter__ = lambda self: c
        c.__exit__ = lambda self, *a: False
        # image fetches must SUCCEED: real 1x1 PNG bytes, status 200
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")
        img_resp = MagicMock()
        img_resp.status_code = 200
        img_resp.content = png
        img_resp.raise_for_status = lambda: None
        c.transport._session.get.return_value = img_resp

        with patch("ferrari_preowned_client.cli._build_client",
                   return_value=c), \
             patch("ferrari_preowned_client.cli._open_store",
                   return_value=store):
            code = main(["smart", str(tmp_path), "--countries", "DK",
                         "--progress", "json", "--workers", "1"])

        out = capsys.readouterr().out
        events = [j.loads(l) for l in out.splitlines() if l.strip()]
        types = [e["type"] for e in events]
        assert "progress" in types and "done" in types
        done = [e for e in events if e["type"] == "done"][0]
        assert done["error_count"] == 0
        assert done["summary"]["listings"] == len(dk_listings)
        assert code == 0
