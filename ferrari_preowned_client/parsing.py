"""Parsers: raw API/SSR payloads -> typed models.

Fail-closed philosophy: a field the contract expects but the response lacks
becomes ``None`` (with a note), never a fabricated value. Structural drift
(keys that must exist for the response to make sense at all) raises
:class:`ContractChangedError`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from . import contract, decrypt
from .errors import ContractChangedError
from .models import (
    Country, Dealer, Gallery, GalleryItem, ImageSet, Listing, ListingDetail,
    Provenance, sha256_hex,
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


# ---------------------------------------------------------------------------
# Search API responses
# ---------------------------------------------------------------------------

def parse_countries(payload: dict[str, Any], *, source_url: str) -> list[Country]:
    try:
        opts = payload["results"]["filters"]["countries"]["options"]
    except (KeyError, TypeError) as exc:
        raise ContractChangedError(
            "countries response shape changed",
            evidence={"keys": list(payload.keys())},
        ) from exc
    out = []
    for o in opts:
        if not isinstance(o, dict) or not o.get("slug"):
            continue
        out.append(Country(
            slug=o["slug"],
            name=o.get("name") or o["slug"],
            region=o.get("region") or "",
            slug_seo=o.get("slugSeo"),
            count=int(o.get("count") or 0),
            allow_multiple=bool(o.get("allowMultiple")),
            hide_ads_count=bool(o.get("hideAdsCount")),
        ))
    return out


def parse_pagination(payload: dict[str, Any]) -> tuple[int, int, int]:
    p = (payload.get("results") or {}).get("pagination") or {}
    return int(p.get("total") or 0), int(p.get("pages") or 0), int(p.get("limit") or 0)


def _identity(ad: dict[str, Any], keystream: bytes, decrypt_vins: bool):
    """Return (vin, vin_blob, legacy_chassis, ad_id_blob)."""
    vin_blob = ad.get("vin")
    id_blob = ad.get("id")
    if not isinstance(id_blob, str) or not id_blob:
        raise ContractChangedError("ad without id field")
    vin = None
    legacy = None
    if decrypt_vins and decrypt.is_encrypted(vin_blob):
        try:
            text = decrypt.decrypt_vin(vin_blob, keystream)
            kind = decrypt.classify_payload(text)
            if kind == "vin":
                vin = text
            elif kind == "legacy":
                legacy = text
        except decrypt.KeystreamError:
            vin = None  # caller decides via strict mode whether to fail
    elif isinstance(vin_blob, str) and not decrypt.is_encrypted(vin_blob):
        # Oracle locales already serve plaintext.
        kind = decrypt.classify_payload(vin_blob)
        if kind == "vin":
            vin = vin_blob
        elif kind == "legacy":
            legacy = vin_blob
    return vin, (vin_blob if isinstance(vin_blob, str) else None), legacy, id_blob


def parse_listing(ad: dict[str, Any], *, keystream: bytes, decrypt_vins: bool,
                  source_url: str, raw_body: str) -> Listing:
    vin, vin_blob, legacy, id_blob = _identity(ad, keystream, decrypt_vins)
    model = ad.get("model") or {}
    country = ad.get("country") or {}
    region = ad.get("region") or {}
    card_images = ad.get("cardImages") or {}

    images = ImageSet(
        homenet=[u for u in (card_images.get("homenetImages") or []) if isinstance(u, str)],
        thron_card_gallery_id=card_images.get("thronGalleryId"),
    )

    prov = Provenance(
        source_url=source_url,
        fetched_at=_utcnow(),
        raw_sha256=sha256_hex(json.dumps(ad, sort_keys=True)),
    )

    return Listing(
        vin=vin, vin_blob=vin_blob, legacy_chassis=legacy, ad_id_blob=id_blob,
        car_id=ad.get("carId"), model_slug=model.get("slug"),
        model_name=model.get("name"), car_name=ad.get("carName"),
        year=ad.get("year"), is_new=_bool(ad.get("isNew")),
        ferrari_approved=_bool(ad.get("ferrariApproved")),
        classic_car=None, special_or_limited=_bool(ad.get("specialOrLimitedSerie")),
        tailored=_bool(ad.get("tailored")), ferrari_atelier=_bool(ad.get("ferrariAtelier")),
        price=ad.get("price"), price_unit=ad.get("priceUnit"),
        request_price=_bool(ad.get("requestPrice")),
        odometer=ad.get("odometer"), odometer_unit=ad.get("odometerUnit"),
        exterior_color=ad.get("exteriorColor"), trim_color=ad.get("trimColor"),
        engine=ad.get("engine"), gear_box=ad.get("gearBox"),
        power=ad.get("power"), power_unit=ad.get("powerUnit"),
        top_speed=ad.get("topSpeed"), fuel_type=ad.get("fuelType"),
        country_slug=country.get("slug"), country_name=country.get("name"),
        region_slug=region.get("slug"),
        dealer=Dealer.from_api(ad.get("dealer") or {}),
        latitude=(ad.get("location") or {}).get("lat"),
        longitude=(ad.get("location") or {}).get("lon"),
        images=images, provenance=prov, raw=ad,
    )


def parse_listings(payload: dict[str, Any], *, keystream: bytes, decrypt_vins: bool,
                   source_url: str, raw_body: str) -> list[Listing]:
    ads = (payload.get("results") or {}).get("ads")
    if ads is None:
        raise ContractChangedError("search response missing results.ads")
    return [
        parse_listing(a, keystream=keystream, decrypt_vins=decrypt_vins,
                      source_url=source_url, raw_body=raw_body)
        for a in ads if isinstance(a, dict)
    ]


# ---------------------------------------------------------------------------
# Detail page SSR
# ---------------------------------------------------------------------------

def parse_next_data(html: str) -> dict[str, Any]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ContractChangedError("page has no __NEXT_DATA__ script")
    return json.loads(m.group(1))


def parse_detail_page(html: str, *, keystream: bytes, decrypt_vins: bool,
                      source_url: str) -> ListingDetail:
    nd = parse_next_data(html)
    try:
        ad = nd["props"]["pageProps"]["initialProps"]["adDetailsData"]["ads"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ContractChangedError(
            "detail page SSR shape changed",
            evidence={"page_keys": list(nd.get("props", {}).get("pageProps", {}).keys())},
        ) from exc

    base = parse_listing(ad, keystream=keystream, decrypt_vins=decrypt_vins,
                         source_url=source_url, raw_body=html)

    card_images = ad.get("cardImages") or {}
    images = base.images
    images.thron_hero_gallery_id = (ad.get("heroImages") or {}).get("thronGalleryId")
    images.thron_video_playlist_id = (ad.get("videoPlaylist") or {}).get("thronGalleryId")

    sorting = ad.get("sortingFields") or {}
    detail = ListingDetail(
        **{k: getattr(base, k) for k in (
            "vin", "vin_blob", "legacy_chassis", "ad_id_blob", "car_id",
            "model_slug", "model_name", "car_name", "year", "is_new",
            "ferrari_approved", "classic_car", "special_or_limited",
            "tailored", "ferrari_atelier", "price", "price_unit",
            "request_price", "odometer", "odometer_unit", "exterior_color",
            "trim_color", "engine", "gear_box", "power", "power_unit",
            "top_speed", "fuel_type", "country_slug", "country_name",
            "region_slug", "dealer", "latitude", "longitude", "images",
            "provenance", "raw",
        )},
        chassis=ad.get("chassis"),
        status=ad.get("status"),
        publish_date=_parse_iso(sorting.get("publishDate")),
        boarding_date=_parse_iso(sorting.get("boardingDate")),
        expire_at=_parse_iso(ad.get("expireAt")),
        drive_type=ad.get("driveType"),
        zero_to_hundred=ad.get("zeroToHundred"),
        total_displacement=ad.get("totalDisplacement"),
        specific_output=ad.get("specificOutput"),
        model_year=ad.get("modelYear"),
        body_style=ad.get("bodyStyle"),
        one_off=_bool(ad.get("oneOffCar")),
        track_car=_bool(ad.get("trackCar")),
        few_off=_bool(ad.get("fewOff")),
        ferrari_original=_bool(ad.get("ferrariOriginal")),
        carfax=_bool(ad.get("carfax")),
        highlight=_bool(ad.get("highlight")),
        dealer_notes=ad.get("dealerNotes"),
        specification=ad.get("specification") or [],
        overview_data=ad.get("overviewData") or [],
        standard_equipment=ad.get("standardEquipment") or [],
        optionals=ad.get("optionals") or [],
        dealer_full_info=ad.get("dealerFullInfo") or {},
        dealer_short_info=ad.get("dealerShortInfo") or {},
        genuine_accessories=ad.get("genuineAccessories") or [],
        fuel_consumption=ad.get("fuelConsumption") or [],
        co2_emissions=ad.get("co2Emissions") or [],
        certification_logos=ad.get("certificationLogos") or [],
    )
    detail.classic_car = _bool(ad.get("classicCar"))
    detail.raw = ad
    if detail.provenance:
        detail.provenance.raw_sha256 = sha256_hex(html)
    return detail


def parse_related_listings(html: str, *, keystream: bytes, decrypt_vins: bool,
                           source_url: str) -> list[Listing]:
    nd = parse_next_data(html)
    ads = (nd.get("props", {}).get("pageProps", {}).get("initialProps", {})
           .get("adsRelatedData", {}).get("ads") or [])
    return [
        parse_listing(a, keystream=keystream, decrypt_vins=decrypt_vins,
                      source_url=source_url, raw_body=html)
        for a in ads if isinstance(a, dict)
    ]


# ---------------------------------------------------------------------------
# THRON playlists
# ---------------------------------------------------------------------------

def parse_gallery(payload: list[dict[str, Any]], *, gallery_id: str,
                  source_url: str) -> Gallery:
    items = []
    for it in payload or []:
        if not isinstance(it, dict):
            continue
        dim = it.get("deliveryInfoMinimal") or {}
        items.append(GalleryItem(
            content_id=it.get("xcontentId") or "",
            name=it.get("name"),
            content_type=it.get("contentType"),
            thumb_url=(dim.get("thumbsUrl") or [None])[0] if isinstance(dim.get("thumbsUrl"), list) else dim.get("defaultThumbUrl"),
            content_url=dim.get("contentUrl"),
            raw=it,
        ))
    return Gallery(
        gallery_id=gallery_id, items=items,
        provenance=Provenance(source_url=source_url, fetched_at=_utcnow(),
                              raw_sha256=sha256_hex(json.dumps(payload, sort_keys=True))),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def _parse_iso(v: Any) -> Optional[datetime]:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utcnow() -> datetime:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def build_listing_url(locale: str, region_slug: str, country: Country,
                      fixed_string: Optional[str] = None) -> str:
    """SEO listing URL (used as referer and as the zero-API fallback)."""
    fixed = fixed_string or contract.FIXED_STRINGS.get(locale, contract.DEFAULT_FIXED_STRING)
    rslug = contract.REGION_SEO.get(region_slug, region_slug.lower())
    cslug = country.slug_seo or country.slug.lower()
    return f"{contract.BASE_URL}{contract.LISTING_URL_TEMPLATE.format(locale=locale, region_slug=rslug, fixed_string=fixed, country_slug=cslug)}"


def build_detail_url(locale: str, region_slug: str, country_slug_seo: str,
                     dealer_slug: str, model_slug: str, id_blob: str,
                     fixed_string: Optional[str] = None) -> str:
    """Detail page URL for an ad (constructed, verified pattern)."""
    fixed = fixed_string or contract.FIXED_STRINGS.get(locale, contract.DEFAULT_FIXED_STRING)
    rslug = contract.REGION_SEO.get(region_slug, region_slug.lower())
    path = contract.DETAIL_URL_TEMPLATE.format(
        locale=locale, region_slug=rslug, fixed_string=fixed,
        country_slug=country_slug_seo, dealer_slug=dealer_slug,
        model_slug=model_slug, id_blob=id_blob)
    return f"{contract.BASE_URL}{path}"
