"""Offline contract tests against real captured responses."""

from __future__ import annotations

import re

import pytest

from ferrari_preowned_client import contract, decrypt, parsing
from ferrari_preowned_client.models import Listing, ListingDetail


# ---------------------------------------------------------------------------
# Countries parsing
# ---------------------------------------------------------------------------

class TestCountries:
    def test_parses_61_options(self, dk_page0):
        countries = parsing.parse_countries(dk_page0, source_url="test")
        assert len(countries) == 61

    def test_fields(self, dk_page0):
        countries = parsing.parse_countries(dk_page0, source_url="test")
        by_slug = {c.slug: c for c in countries}
        de = by_slug["DE"]
        assert de.name == "Deutschland"
        assert de.region == "EU"
        assert de.slug_seo == "deutschland"
        dk = by_slug["DK"]
        assert dk.slug_seo == "d-nemark"

    def test_counts_populate_for_scoped_country(self, dk_page0):
        countries = parsing.parse_countries(dk_page0, source_url="test")
        by_slug = {c.slug: c for c in countries}
        assert by_slug["DK"].count == 29

    def test_contract_change_detected(self):
        with pytest.raises(parsing.ContractChangedError):
            parsing.parse_countries({"nope": 1}, source_url="test")


# ---------------------------------------------------------------------------
# Listing parsing + decryption
# ---------------------------------------------------------------------------

class TestListingParsing:
    def test_parse_dk_listings(self, dk_page0):
        listings = parsing.parse_listings(
            dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="test", raw_body="")
        assert len(listings) == 29
        assert all(isinstance(l, Listing) for l in listings)

    def test_vins_decrypt(self, dk_page0):
        listings = parsing.parse_listings(
            dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="test", raw_body="")
        with_vin = [l for l in listings if l.vin]
        assert len(with_vin) >= 25  # some are legacy chassis
        for l in with_vin:
            assert re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", l.vin), l.vin

    def test_known_dk_vin(self, dk_page0):
        """The 575M Maranello must decrypt to ZFFBT55B000128196
        (cross-verified against the detail page chassis 128196)."""
        listings = parsing.parse_listings(
            dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="test", raw_body="")
        vins = {l.vin for l in listings if l.vin}
        assert "ZFFBT55B000128196" in vins

    def test_legacy_chassis_detected(self, dk_page0):
        listings = parsing.parse_listings(
            dk_page0, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="test", raw_body="")
        legacy = [l for l in listings if l.legacy_chassis]
        # Classic chassis: bare numbers ("02528") and "4507 GT" style.
        assert any(re.fullmatch(r"\d{3,8}|\d{3,5}\s*[A-Z]{1,3}", c.legacy_chassis)
                   for c in legacy)
        # Every listing must be either VIN, legacy chassis, or explicitly None.
        for l in listings:
            assert (l.vin is None) != (l.legacy_chassis is None) or (l.vin is None and l.legacy_chassis is None)

    def test_us_listings_homenet_galleries(self, us_dede):
        listings = parsing.parse_listings(
            us_dede, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="test", raw_body="")
        assert listings
        with_gallery = [l for l in listings if l.images.homenet]
        assert len(with_gallery) == len(listings)  # US cars all have homenet
        for l in with_gallery:
            assert all(u.startswith("https://gcb.evs.onl/") for u in l.images.homenet)

    def test_be_listings(self, be_current):
        listings = parsing.parse_listings(
            be_current, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="test", raw_body="")
        assert len(listings) == 44
        assert all(l.country_slug == "BE" for l in listings)

    def test_plaintext_vin_passthrough(self, us_enus):
        """Oracle locale responses already carry plaintext VINs."""
        listings = parsing.parse_listings(
            us_enus, keystream=contract.KEYSTREAM_V1, decrypt_vins=True,
            source_url="test", raw_body="")
        plain = [l for l in listings if l.vin and not l.vin_blob.startswith("Iljixbcg")]
        assert len(plain) == 44

    def test_pagination(self, dk_page0):
        total, pages, limit = parsing.parse_pagination(dk_page0)
        assert total == 29 and pages == 1 and limit == 44


# ---------------------------------------------------------------------------
# Decryption primitives
# ---------------------------------------------------------------------------

class TestDecrypt:
    def test_is_encrypted(self):
        assert decrypt.is_encrypted("IljixbcgVnJGsKsQfDGsrAAAA")
        assert not decrypt.is_encrypted("ZFF09RPA7R0304742")
        assert not decrypt.is_encrypted(None)

    def test_decrypt_vin_roundtrip(self, dk_page0):
        for ad in dk_page0["results"]["ads"][:10]:
            blob = ad["vin"]
            if not decrypt.is_encrypted(blob):
                continue
            text = decrypt.decrypt_vin(blob)
            assert re.fullmatch(r"[A-HJ-NPR-Z0-9]{7,17}", text)

    def test_decrypt_id_vin_matches_vin_blob(self, dk_page0):
        for ad in dk_page0["results"]["ads"][:10]:
            if not (decrypt.is_encrypted(ad["vin"]) and decrypt.is_encrypted(ad["id"])):
                continue
            v1 = decrypt.decrypt_vin(ad["vin"])
            v2 = decrypt.decrypt_id_vin(ad["id"])
            assert v1 == v2

    def test_derive_keystream_from_oracle(self, oracle_pairs):
        """44 oracle pairs must derive the verified keystream (majority).

        One pair is known-inconsistent (Ferrari data quirk: a car whose
        id-blob embeds an outdated VIN), so majority agreement is used.
        """
        pairs = [(p["vin"], p["id_blob"]) for p in oracle_pairs if len(p["vin"]) == 17]
        ks, agreement = decrypt.derive_keystream_majority(pairs)
        assert ks == contract.KEYSTREAM_V1
        assert agreement >= 0.9  # 43/44 = 97.7%

    def test_verify_keystream(self, oracle_pairs):
        pairs = [(p["vin"], p["id_blob"]) for p in oracle_pairs if len(p["vin"]) == 17]
        assert decrypt.verify_keystream(contract.KEYSTREAM_V1, pairs)
        wrong = bytes(b ^ 1 for b in contract.KEYSTREAM_V1)
        assert not decrypt.verify_keystream(wrong, pairs)

    def test_wrong_keystream_raises(self, dk_page0):
        blob = next(a["vin"] for a in dk_page0["results"]["ads"]
                    if decrypt.is_encrypted(a["vin"]))
        wrong = bytes(b ^ 0xFF for b in contract.KEYSTREAM_V1)
        with pytest.raises(decrypt.KeystreamError):
            decrypt.decrypt_vin(blob, wrong)

    def test_rotated_keystream_detected_not_guessed(self, dk_page0):
        """A rotated keystream yields KeystreamError, never garbage."""
        blob = next(a["vin"] for a in dk_page0["results"]["ads"]
                    if decrypt.is_encrypted(a["vin"]))
        wrong = bytes(b ^ 0x01 for b in contract.KEYSTREAM_V1)
        with pytest.raises(decrypt.KeystreamError):
            decrypt.decrypt_vin(blob, wrong)


# ---------------------------------------------------------------------------
# Detail page SSR parsing
# ---------------------------------------------------------------------------

class TestDetailParsing:
    def test_parse_detail(self, dk_detail_html):
        detail = parsing.parse_detail_page(
            dk_detail_html, keystream=contract.KEYSTREAM_V1,
            decrypt_vins=True, source_url="test")
        assert isinstance(detail, ListingDetail)
        assert detail.car_name == "575M Maranello"
        assert detail.year == 2002
        assert detail.price == 135000
        assert detail.chassis == "128196"
        assert detail.vin == "ZFFBT55B000128196"
        assert detail.classic_car is True
        assert detail.status == "published"
        assert detail.dealer.name == "Formula Automobile"
        assert detail.publish_date is not None

    def test_detail_hero_gallery(self, dk_detail_html):
        detail = parsing.parse_detail_page(
            dk_detail_html, keystream=contract.KEYSTREAM_V1,
            decrypt_vins=True, source_url="test")
        assert detail.images.thron_hero_gallery_id == "c9bbb318-dbe4-4041-8cf9-fabe6e3cb53e"
        assert detail.images.thron_card_gallery_id == "a0537f0b-7e7d-4f34-9760-e465fb2d5cf3"

    def test_specification_sections(self, dk_detail_html):
        detail = parsing.parse_detail_page(
            dk_detail_html, keystream=contract.KEYSTREAM_V1,
            decrypt_vins=True, source_url="test")
        sections = {s.get("section") for s in detail.specification}
        assert "Engine" in sections

    def test_related_listings(self, dk_detail_html):
        rel = parsing.parse_related_listings(
            dk_detail_html, keystream=contract.KEYSTREAM_V1,
            decrypt_vins=True, source_url="test")
        assert len(rel) >= 5


# ---------------------------------------------------------------------------
# THRON gallery parsing
# ---------------------------------------------------------------------------

class TestGalleryParsing:
    def test_parse_playlist(self, thron_playlist):
        gallery = parsing.parse_gallery(
            thron_playlist, gallery_id="c9bbb318-dbe4-4041-8cf9-fabe6e3cb53e",
            source_url="test")
        assert len(gallery.items) == 36
        assert gallery.items[0].content_id == "3199d71f-0cb5-4b85-98ad-493652caa925"
        assert gallery.items[0].thumb_url
        assert all(i.content_id for i in gallery.items)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

class TestUrls:
    def test_listing_url(self):
        from ferrari_preowned_client.models import Country
        c = Country(slug="DK", name="Dänemark", region="EU", slug_seo="d-nemark")
        url = parsing.build_listing_url("de-DE", "EU", c)
        assert url == "https://preowned.ferrari.com/de-DE/r/europa/ferrari-gebraucht/d-nemark/rfc"

    def test_detail_url(self):
        url = parsing.build_detail_url(
            "de-DE", "EU", "d-nemark", "formula-automobile", "575m-maranello",
            "IljixbcgVnJGsKsQfDGsrp441KHG0ZKTs%2BFwerDKH1mdNzQwNxzQB4UpfuADNo8JuIYTbkKwhxZEuEH9tf3xAxm37ulnJXgabjOHFLhGGA%3D%3D")
        assert url.startswith("https://preowned.ferrari.com/de-DE/a/europa/ferrari-gebraucht/d-nemark/formula-automobile/575m-maranello/")
        assert url.endswith("%3D%3D")
