"""Snapshot orchestration: full-site sweep with idempotent merge.

A *snapshot* = countries() + one search pass per country + (optional)
detail/gallery passes for new or changed listings. Everything is
checkpointed (JSON) so interrupted runs resume instead of re-fetching.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

from .models import Listing, ListingDetail, SnapshotSummary

if TYPE_CHECKING:
    from .client import FerrariPreownedClient


@dataclass
class SnapshotResult:
    """The outcome of one full snapshot run."""

    summary: SnapshotSummary
    listings: dict[str, dict[str, Any]] = field(default_factory=dict)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)
    galleries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_dict(),
            "listings": self.listings,
            "details": self.details,
            "galleries": self.galleries,
        }

    def save(self, path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")

    @classmethod
    def load(cls, path) -> "SnapshotResult":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        s = d.get("summary", {})
        summary = SnapshotSummary(
            started_at=datetime.fromisoformat(s["started_at"]) if s.get("started_at") else _utcnow(),
            finished_at=datetime.fromisoformat(s["finished_at"]) if s.get("finished_at") else None,
            countries=s.get("countries", []),
            total_listings=s.get("total_listings", 0),
            unique_listings=s.get("unique_listings", 0),
            duplicates=s.get("duplicates", 0),
            requests_made=s.get("requests_made", 0),
            requests_budget=s.get("requests_budget", 0),
            notes=s.get("notes", []),
        )
        return cls(summary=summary, listings=d.get("listings", {}),
                   details=d.get("details", {}), galleries=d.get("galleries", {}))


def run_snapshot(client: "FerrariPreownedClient", *,
                 countries: Optional[Iterable[str]] = None,
                 limit: int = 44,
                 include_details: bool = False,
                 include_galleries: bool = False,
                 checkpoint_path: Optional[str] = None,
                 progress=None) -> SnapshotResult:
    """Run a full snapshot and return the merged result.

    ``countries`` defaults to every country with a nonzero ad count.
    ``include_details`` adds the detail-SSR pass for every listing (the
    expensive-but-stealthy page GETs); ``include_galleries`` fetches full
    EU THRON galleries. ``checkpoint_path`` persists progress for resume.
    """
    summary = SnapshotSummary(
        started_at=_utcnow(),
        requests_budget=client.transport.config.request_budget,
    )
    result = SnapshotResult(summary=summary)

    if checkpoint_path and Path(checkpoint_path).exists():
        prior = SnapshotResult.load(checkpoint_path)
        result.listings.update(prior.listings)
        result.details.update(prior.details)
        result.galleries.update(prior.galleries)
        summary.notes.append(f"resumed from checkpoint ({len(prior.listings)} listings)")

    all_countries = client.countries()
    counts = None
    if countries is None:
        # Counts only populate in scoped responses; one probe call gets all.
        counts = client.counts()
        wanted = [c.slug for c in all_countries if counts.get(c.slug, 0) > 0]
    else:
        counts = client.counts()
        wanted = list(countries)
    summary.countries = [
        {"slug": c.slug, "name": c.name, "region": c.region,
         "count": counts.get(c.slug, c.count)}
        for c in all_countries if c.slug in set(wanted)
    ]

    total = 0
    for code in wanted:
        count = counts.get(code, 0)
        got = 0
        for listing in client.country_listings(code, limit=limit):
            got += 1
            total += 1
            key = listing.identity_key
            if key in result.listings:
                summary.duplicates += 1
                continue
            result.listings[key] = _listing_to_dict(listing)
            if progress:
                progress(listing)
        summary.total_listings += count if count else got
        if count and got < count:
            summary.notes.append(f"{code}: fetched {got}/{count} — possible page drift")

    if include_details:
        for key, rec in list(result.listings.items()):
            if include_galleries is False and key in result.details:
                continue
            listing = _dict_to_listing(rec)
            try:
                d = client.detail(listing)
                result.details[key] = _detail_to_dict(d)
            except Exception as exc:
                summary.notes.append(f"detail failed for {key}: {exc}")

    if include_galleries:
        for key, rec in list(result.listings.items()):
            listing = _dict_to_listing(rec)
            gid = (rec.get("images") or {}).get("thron_hero_gallery_id") \
                  or (rec.get("images") or {}).get("thron_card_gallery_id")
            if not gid or key in result.galleries:
                continue
            try:
                g = client.gallery(listing)
                result.galleries[key] = {
                    "gallery_id": g.gallery_id,
                    "items": [{"content_id": i.content_id, "thumb": i.thumb_url}
                              for i in g.items],
                }
            except Exception as exc:
                summary.notes.append(f"gallery failed for {key}: {exc}")

    summary.unique_listings = len(result.listings)
    summary.requests_made = client.transport.requests_made
    summary.finished_at = _utcnow()
    if checkpoint_path:
        result.save(checkpoint_path)
    return result


# ---------------------------------------------------------------------------
# (de)serialization helpers — listings round-trip through plain dicts
# ---------------------------------------------------------------------------

def _listing_to_dict(l: Listing) -> dict[str, Any]:
    return {
        "vin": l.vin, "vin_blob": l.vin_blob, "legacy_chassis": l.legacy_chassis,
        "ad_id_blob": l.ad_id_blob, "car_id": l.car_id,
        "model_slug": l.model_slug, "model_name": l.model_name,
        "car_name": l.car_name, "year": l.year, "is_new": l.is_new,
        "ferrari_approved": l.ferrari_approved,
        "special_or_limited": l.special_or_limited,
        "price": l.price, "price_unit": l.price_unit,
        "request_price": l.request_price,
        "odometer": l.odometer, "odometer_unit": l.odometer_unit,
        "exterior_color": l.exterior_color, "trim_color": l.trim_color,
        "engine": l.engine, "gear_box": l.gear_box, "power": l.power,
        "country_slug": l.country_slug, "country_name": l.country_name,
        "region_slug": l.region_slug,
        "dealer": {"name": l.dealer.name, "slug": l.dealer.slug,
                   "city": l.dealer.city, "country": l.dealer.country}
                   if l.dealer else None,
        "latitude": l.latitude, "longitude": l.longitude,
        "images": {
            "homenet": l.images.homenet,
            "thron_card_gallery_id": l.images.thron_card_gallery_id,
            "thron_hero_gallery_id": l.images.thron_hero_gallery_id,
            "thron_video_playlist_id": l.images.thron_video_playlist_id,
        },
        "provenance": l.provenance.to_dict() if l.provenance else None,
    }


def _detail_to_dict(d: ListingDetail) -> dict[str, Any]:
    base = _listing_to_dict(d)
    base.update({
        "chassis": d.chassis, "status": d.status,
        "publish_date": d.publish_date.isoformat() if d.publish_date else None,
        "drive_type": d.drive_type, "zero_to_hundred": d.zero_to_hundred,
        "model_year": d.model_year, "carfax": d.carfax,
        "classic_car": d.classic_car, "one_off": d.one_off,
        "track_car": d.track_car, "few_off": d.few_off,
        "ferrari_original": d.ferrari_original,
        "dealer_full_info": d.dealer_full_info,
        "specification": d.specification,
    })
    return base


def _dict_to_listing(rec: dict[str, Any]) -> Listing:
    from .models import Dealer, ImageSet, Provenance
    dealer = None
    if rec.get("dealer"):
        dealer = Dealer(**{k: v for k, v in rec["dealer"].items()
                           if k in ("name", "slug", "city", "country", "region", "address")})
    images = ImageSet(
        homenet=(rec.get("images") or {}).get("homenet", []),
        thron_card_gallery_id=(rec.get("images") or {}).get("thron_card_gallery_id"),
        thron_hero_gallery_id=(rec.get("images") or {}).get("thron_hero_gallery_id"),
        thron_video_playlist_id=(rec.get("images") or {}).get("thron_video_playlist_id"),
    )
    prov = None
    if rec.get("provenance"):
        p = rec["provenance"]
        prov = Provenance(
            source_url=p.get("source_url", ""),
            fetched_at=datetime.fromisoformat(p["fetched_at"]) if p.get("fetched_at") else _utcnow(),
            raw_sha256=p.get("raw_sha256", ""),
        )
    return Listing(
        vin=rec.get("vin"), vin_blob=rec.get("vin_blob"),
        legacy_chassis=rec.get("legacy_chassis"),
        ad_id_blob=rec.get("ad_id_blob", ""),
        car_id=rec.get("car_id"), model_slug=rec.get("model_slug"),
        model_name=rec.get("model_name"), car_name=rec.get("car_name"),
        year=rec.get("year"), is_new=rec.get("is_new"),
        ferrari_approved=rec.get("ferrari_approved"),
        special_or_limited=rec.get("special_or_limited"),
        price=rec.get("price"), price_unit=rec.get("price_unit"),
        request_price=rec.get("request_price"),
        odometer=rec.get("odometer"), odometer_unit=rec.get("odometer_unit"),
        exterior_color=rec.get("exterior_color"), trim_color=rec.get("trim_color"),
        engine=rec.get("engine"), gear_box=rec.get("gear_box"), power=rec.get("power"),
        country_slug=rec.get("country_slug"), country_name=rec.get("country_name"),
        region_slug=rec.get("region_slug"), dealer=dealer,
        latitude=rec.get("latitude"), longitude=rec.get("longitude"),
        images=images, provenance=prov,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
