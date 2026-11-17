"""Offline tests for export, images, smart store, and CLI parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ferrari_preowned_client import contract, parsing
from ferrari_preowned_client.export import (
    EXPORT_COLUMNS, export_csv, export_json, export_xlsx, listing_to_row,
)
from ferrari_preowned_client.smart import SmartStore


@pytest.fixture(scope="module")
def dk_listings(dk_page0):
    return list(parsing.parse_listings(
        dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
        source_url="test", raw_body=""))


class TestExport:
    def test_row_covers_all_columns(self, dk_listings):
        row = listing_to_row(dk_listings[0])
        colkeys = [k for k, _, _ in EXPORT_COLUMNS]
        assert set(colkeys) <= set(row.keys()), \
            [k for k in colkeys if k not in row]

    def test_row_values_real(self, dk_listings):
        row = listing_to_row(dk_listings[0])
        assert row["vin"] == "ZFFBT55B000128196"
        assert row["car_name"] == "575M Maranello"
        assert row["price"] == 135000
        assert row["country_slug"] == "DK"

    def test_csv_roundtrip(self, dk_listings, tmp_path):
        p = tmp_path / "t.csv"
        r = export_csv(dk_listings, None, p)
        assert r["count"] == len(dk_listings) == 29
        import csv
        rows = list(csv.reader(open(p, encoding="utf-8-sig")))
        assert len(rows[0]) == len(EXPORT_COLUMNS)
        assert len(rows) - 1 == 29
        assert rows[1][0] == "ZFFBT55B000128196"  # VIN first column

    def test_json_roundtrip(self, dk_listings, tmp_path):
        p = tmp_path / "t.json"
        r = export_json(dk_listings, None, p)
        d = json.loads(p.read_text())
        assert d["count"] == 29
        assert d["listings"][0]["vin"] == "ZFFBT55B000128196"

    def test_xlsx_opens_and_has_data(self, dk_listings, tmp_path):
        p = tmp_path / "t.xlsx"
        r = export_xlsx(dk_listings, None, p)
        import zipfile
        assert p.stat().st_size > 5000
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            assert "xl/worksheets/sheet1.xml" in names
            assert "[Content_Types].xml" in names
            xml = z.read("xl/worksheets/sheet1.xml").decode()
        assert xml.count("<row ") == 30          # header + 29
        assert "575M Maranello" in xml
        assert 'autoFilter ref="A1:' in xml
        assert 'state="frozen"' in xml

    def test_dedupe_by_identity(self, dk_listings, tmp_path):
        doubled = dk_listings + dk_listings
        r = export_csv(doubled, None, tmp_path / "t.csv")
        assert r["count"] == 29


class TestSmartStore:
    def test_classify_new_unchanged_changed(self, dk_listings, tmp_path):
        store = SmartStore(tmp_path / "s.json")
        l0 = dk_listings[0]
        assert store.classify(l0) == "new"
        store.record_seen(l0)
        assert store.classify(l0) == "unchanged"
        # mutate a field in the raw ad -> hash changes -> changed
        import copy
        l0b = copy.deepcopy(l0)
        l0b.raw["price"] = 999999
        assert store.classify(l0b) == "changed"

    def test_id_blob_epoch_rotation_is_unchanged(self, dk_listings, tmp_path):
        """The id blob embeds a rotating epoch; identical cars must stay
        'unchanged' across captures (verified live 2026-09-25)."""
        import copy
        store = SmartStore(tmp_path / "s.json")
        l0 = dk_listings[0]
        store.record_seen(l0)
        # rotate chars deep inside the id blob (simulates a new epoch)
        l0b = copy.deepcopy(l0)
        b = list(l0b.raw["id"])
        b[60] = "A" if b[60] != "A" else "B"
        l0b.raw["id"] = "".join(b)
        l0b.ad_id_blob = l0b.raw["id"]
        # same for the vin blob nonce
        v = list(l0b.raw["vin"])
        v[40] = "A" if v[40] != "A" else "B"
        l0b.raw["vin"] = "".join(v)
        l0b.vin_blob = l0b.raw["vin"]
        assert store.classify(l0b) == "unchanged"
        assert store.has_identical(l0b) is True

    def test_persistence_roundtrip(self, dk_listings, tmp_path):
        p = tmp_path / "s.json"
        s1 = SmartStore(p)
        for l in dk_listings:
            s1.record_seen(l)
        s1.save()
        s2 = SmartStore(p)
        assert s2.stats()["total"] == 29
        for l in dk_listings:
            assert s2.classify(l) == "unchanged"

    def test_removals(self, dk_listings, tmp_path):
        p = tmp_path / "s.json"
        s1 = SmartStore(p)
        for l in dk_listings:
            s1.record_seen(l)
        s1.save()
        removed = s1.record_removals([dk_listings[0].identity_key])
        assert len(removed) == 28
        st = s1.get(dk_listings[5].identity_key)
        assert st.removed is True

    def test_scoped_removals_ignore_other_countries(self, dk_listings,
                                                    tmp_path):
        """A subset run (DK only) must not mark US/AT cars as removed."""
        import copy
        p = tmp_path / "s.json"
        s1 = SmartStore(p)
        for l in dk_listings[:3]:
            l = copy.deepcopy(l)
            l.country_slug = "DK"
            s1.record_seen(l)
        for l in dk_listings[3:6]:
            l = copy.deepcopy(l)
            l.country_slug = "US"
            s1.record_seen(l)
        s1.save()
        # DK-only run: all DK cars still present, nothing removed
        dk_keys = [k for k, st in s1._listings.items()
                   if st.country == "DK"]
        removed = s1.record_removals(dk_keys, only_countries=["DK"])
        assert removed == []          # US cars untouched
        st_us = [st for st in s1._listings.values() if st.country == "US"]
        assert all(not st.removed for st in st_us)

    def test_corrupt_store_recovers(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{corrupt json", encoding="utf-8")
        s = SmartStore(p)  # must not raise
        assert s.stats()["total"] == 0

    def test_gallery_change_detection(self, dk_listings, tmp_path):
        from ferrari_preowned_client.models import Gallery, GalleryItem
        store = SmartStore(tmp_path / "s.json")
        l0 = dk_listings[0]
        gal = Gallery(gallery_id="g", items=[
            GalleryItem(content_id="1", content_url="https://x/1.jpg"),
            GalleryItem(content_id="2", content_url="https://x/2.jpg"),
        ])
        store.record_gallery(l0, gal)
        assert store.gallery_changed(l0, ["https://x/1.jpg", "https://x/2.jpg"]) is False
        assert store.gallery_changed(l0, ["https://x/1.jpg"]) is True


class TestImageDownloaderOffline:
    def test_car_folder_naming(self, dk_listings):
        from ferrari_preowned_client.images import ImageDownloader, ImageConfig
        from unittest.mock import MagicMock
        dl = ImageDownloader(MagicMock(), ImageConfig())
        f = dl._car_folder(dk_listings[0])
        assert f == "ZFFBT55B000128196_575m-maranello-2002"

    def test_image_filename_stable_and_hashed(self):
        from ferrari_preowned_client.images import ImageDownloader, ImageConfig
        from unittest.mock import MagicMock
        dl = ImageDownloader(MagicMock(), ImageConfig())
        a = dl._image_filename("https://x/1.jpg", 3)
        b = dl._image_filename("https://x/1.jpg", 3)
        assert a == b and a.startswith("003_") and a.endswith(".webp")
        assert dl._image_filename("https://x/2.jpg", 3) != a

    def test_compress_never_grows(self):
        from ferrari_preowned_client.images import ImageDownloader, ImageConfig
        from unittest.mock import MagicMock
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        import io
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), "red").save(buf, "JPEG", quality=95)
        raw = buf.getvalue()
        dl = ImageDownloader(MagicMock(), ImageConfig())
        out = dl._compress(raw)
        assert isinstance(out, bytes) and len(out) > 0
        assert len(out) <= len(raw)  # never grow

    def test_resume_skip(self, dk_listings, tmp_path):
        from ferrari_preowned_client.images import ImageDownloader, ImageConfig
        from ferrari_preowned_client.models import Gallery, GalleryItem
        from unittest.mock import MagicMock
        dl = ImageDownloader(MagicMock(), ImageConfig())
        out = tmp_path / "000_abc.webp"
        out.write_bytes(b"x" * 10)
        r = dl._fetch_one("https://x/1.jpg", out, None)
        assert r["status"] == "skipped_existing"
        assert dl.stats["skipped_existing"] == 1


class TestCli:
    def test_parser_full_smart(self):
        from ferrari_preowned_client.cli import build_parser
        p = build_parser()
        a = p.parse_args(["full", "/tmp/x", "--countries", "dk,us"])
        assert a.target == "/tmp/x" and a.countries == "dk,us"
        a2 = p.parse_args(["smart", "/tmp/x", "--workers", "3"])
        assert a2.workers == 3
        a3 = p.parse_args(["search", "DK", "--page", "2"])
        assert a3.country == "DK" and a3.page == 2

    def test_version_flag(self, capsys):
        from ferrari_preowned_client.cli import main
        with pytest.raises(SystemExit) as ei:
            main(["--version"])
        assert ei.value.code == 0
        assert "1.0.0" in capsys.readouterr().out


class TestModelCatalog:
    def test_catalog_absorption_and_slug_resolution(self, dk_page0):
        """The model catalog from a search response drives detail URLs."""
        from ferrari_preowned_client.client import FerrariPreownedClient
        from unittest.mock import MagicMock
        c = FerrariPreownedClient.__new__(FerrariPreownedClient)
        c._model_catalog = {}
        c._absorb_model_catalog(dk_page0)
        # F175 -> ferrari-purosangue (the bug that caused 6 404s)
        assert c._model_catalog.get("F175") == "ferrari-purosangue"
        listings = parsing.parse_listings(
            dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="t", raw_body="")
        # find the Purosangue ad (carName 'Purosangue', no carId in ad!)
        puro = [l for l in listings if "urosangue" in (l.car_name or "")]
        assert puro
        for l in puro:
            assert c._model_slug(l) == "ferrari-purosangue"
        # classics: 250 GT 2+2 -> 250-gt-2+2 (NOT '250-gt-2-2')
        classics = [l for l in listings if "250 GT" in (l.car_name or "")]
        for l in classics:
            assert c._model_slug(l) == "250-gt-2+2"

    def test_real_dk_urls_all_resolvable(self, dk_page0):
        """Every real href on the captured listing page must be buildable."""
        import re
        from ferrari_preowned_client.client import FerrariPreownedClient
        listings = parsing.parse_listings(
            dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="t", raw_body="")
        c = FerrariPreownedClient.__new__(FerrariPreownedClient)
        c._model_catalog = {}
        c._absorb_model_catalog(dk_page0)
        # real model segments seen in the captured page
        real = set()
        for m in re.finditer(
                r'href="/de-DE/a/europa/ferrari-gebraucht/d-nemark/'
                r'formula-automobile/([^/]+)/', _read_dk_listing_html()):
            real.add(m.group(1))
        assert real  # e.g. ferrari-purosangue, 575m-maranello, 250-gt-2+2
        ours = {c._model_slug(l) for l in listings}
        missing = real - ours
        assert not missing, f"unresolved real model segments: {missing}"


def _read_dk_listing_html() -> str:
    p = Path(__file__).resolve().parent / "fixtures" / "dk_listing.html"
    return p.read_text(encoding="utf-8", errors="replace")
