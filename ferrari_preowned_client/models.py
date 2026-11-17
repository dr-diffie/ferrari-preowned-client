"""Typed data models for Ferrari Pre-Owned listings.

Two levels of fidelity, mirroring the two data sources:

* :class:`Listing` — everything the **search API** returns per ad
  (47 fields). Enough for monitoring price/km/dealer/gallery(US).
* :class:`ListingDetail` — the **detail page SSR** ad (93 fields):
  adds chassis, publishDate, full specification, extra THRON galleries,
  dealer hours/contacts, etc.

Both models are deliberately permissive about unknown upstream fields
(``raw`` passthrough) but strict about the fields we interpret: a missing
value is ``None`` (never invented), and every instance carries provenance
(source URL, fetch time, raw SHA-256, contract version).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import contract


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass
class Provenance:
    """Where a record came from and when it was observed."""

    source_url: str
    fetched_at: datetime
    raw_sha256: str
    contract_version: str = contract.CONTRACT_VERSION

    def to_dict(self) -> dict[str, str]:
        return {
            "source_url": self.source_url,
            "fetched_at": self.fetched_at.isoformat(),
            "raw_sha256": self.raw_sha256,
            "contract_version": self.contract_version,
        }


@dataclass
class Country:
    """A country filter option (from the countries() call)."""

    slug: str
    name: str
    region: str
    slug_seo: Optional[str] = None
    count: int = 0
    allow_multiple: bool = False
    hide_ads_count: bool = False


@dataclass
class Dealer:
    name: Optional[str] = None
    slug: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Dealer":
        loc = d.get("location") or {}
        return cls(
            name=d.get("name"), slug=d.get("slug"), city=d.get("city"),
            country=d.get("country"), region=d.get("region"),
            address=d.get("address"),
            lat=loc.get("lat"), lon=loc.get("lon"),
        )


@dataclass
class ImageSet:
    """Images for a listing, normalized across the two gallery systems."""

    #: US-style direct image URLs (gcb.evs.onl) from the search response.
    homenet: list[str] = field(default_factory=list)
    #: EU-style THRON content id for the card carousel (4 images).
    thron_card_gallery_id: Optional[str] = None
    #: EU-style THRON gallery id that yields the FULL gallery (detail page).
    thron_hero_gallery_id: Optional[str] = None
    #: THRON gallery id containing videos (detail page).
    thron_video_playlist_id: Optional[str] = None

    @property
    def count(self) -> int:
        if self.homenet:
            return len(self.homenet)
        return 0

    def thron_image_url(self, content_id: str, height: int = 0) -> str:
        """Build a direct (no-auth) THRON CDN image URL for a content id."""
        return (
            f"https://ferrari-cdn.thron.com/delivery/public/image/ferrari/"
            f"{content_id}/3zayf6/std/0x{height}/image?quality=auto-high&format=auto"
        )


@dataclass
class Listing:
    """One ad from the search API (page-level data)."""

    # Identity
    vin: Optional[str]                  # decrypted when possible
    vin_blob: Optional[str]             # original encrypted value
    legacy_chassis: Optional[str]       # short numeric chassis (pre-VIN cars)
    ad_id_blob: str                     # encrypted listing id (URL tail)
    car_id: Optional[str] = None        # e.g. "133C"
    model_slug: Optional[str] = None
    model_name: Optional[str] = None
    car_name: Optional[str] = None

    # Classification
    year: Optional[int] = None
    is_new: Optional[bool] = None
    ferrari_approved: Optional[bool] = None
    classic_car: Optional[bool] = None
    special_or_limited: Optional[bool] = None
    tailored: Optional[bool] = None
    ferrari_atelier: Optional[bool] = None

    # Commercial
    price: Optional[int] = None
    price_unit: Optional[str] = None
    request_price: Optional[bool] = None
    odometer: Optional[int] = None
    odometer_unit: Optional[str] = None

    # Vehicle
    exterior_color: Optional[str] = None
    trim_color: Optional[str] = None
    engine: Optional[str] = None
    gear_box: Optional[str] = None
    power: Optional[str] = None
    power_unit: Optional[str] = None
    top_speed: Optional[str] = None
    fuel_type: Optional[str] = None

    # Place
    country_slug: Optional[str] = None
    country_name: Optional[str] = None
    region_slug: Optional[str] = None
    dealer: Optional[Dealer] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Images
    images: ImageSet = field(default_factory=ImageSet)

    # Provenance
    provenance: Optional[Provenance] = None

    #: Full raw ad dict (passthrough, never interpreted by the library).
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identity_key(self) -> str:
        """Stable identity: decrypted VIN > chassis > raw id blob."""
        return self.vin or self.legacy_chassis or self.ad_id_blob


@dataclass
class ListingDetail(Listing):
    """Ad + detail-page-only fields (93-field SSR object)."""

    chassis: Optional[str] = None
    status: Optional[str] = None
    publish_date: Optional[datetime] = None
    boarding_date: Optional[datetime] = None
    expire_at: Optional[datetime] = None
    drive_type: Optional[str] = None
    zero_to_hundred: Optional[str] = None
    total_displacement: Optional[str] = None
    specific_output: Optional[str] = None
    model_year: Optional[int] = None
    body_style: Optional[str] = None
    one_off: Optional[bool] = None
    track_car: Optional[bool] = None
    few_off: Optional[bool] = None
    ferrari_original: Optional[bool] = None
    carfax: Optional[bool] = None
    highlight: Optional[bool] = None
    dealer_notes: Optional[str] = None
    specification: list[dict[str, Any]] = field(default_factory=list)
    overview_data: list[dict[str, Any]] = field(default_factory=list)
    standard_equipment: list[dict[str, Any]] = field(default_factory=list)
    optionals: list[dict[str, Any]] = field(default_factory=list)
    dealer_full_info: dict[str, Any] = field(default_factory=dict)
    dealer_short_info: dict[str, Any] = field(default_factory=dict)
    genuine_accessories: list[dict[str, Any]] = field(default_factory=list)
    fuel_consumption: list[dict[str, Any]] = field(default_factory=list)
    co2_emissions: list[dict[str, Any]] = field(default_factory=list)
    certification_logos: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GalleryItem:
    """One item of a THRON playlist (image or video)."""

    content_id: str
    name: Optional[str] = None
    content_type: Optional[str] = None
    thumb_url: Optional[str] = None
    content_url: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Gallery:
    """Full gallery for a listing, fetched via the playlist endpoint."""

    gallery_id: str
    items: list[GalleryItem] = field(default_factory=list)
    provenance: Optional[Provenance] = None

    @property
    def images(self) -> list[str]:
        return [i.thumb_url or i.content_url or "" for i in self.items if i.thumb_url or i.content_url]

    @property
    def videos(self) -> list[GalleryItem]:
        return [i for i in self.items if (i.content_type or "").upper().startswith("VIDEO")]


@dataclass
class SnapshotSummary:
    """Outcome of a full snapshot run."""

    started_at: datetime
    finished_at: Optional[datetime] = None
    countries: list[dict[str, Any]] = field(default_factory=list)
    total_listings: int = 0
    unique_listings: int = 0
    duplicates: int = 0
    requests_made: int = 0
    requests_budget: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "countries": self.countries,
            "total_listings": self.total_listings,
            "unique_listings": self.unique_listings,
            "duplicates": self.duplicates,
            "requests_made": self.requests_made,
            "requests_budget": self.requests_budget,
            "notes": self.notes,
        }
