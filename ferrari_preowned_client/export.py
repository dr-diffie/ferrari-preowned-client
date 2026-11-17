"""Export listings to JSON, CSV, and XLSX (stdlib-only, zero extra deps).

The XLSX writer is a minimal OOXML generator: styled header, frozen pane,
auto-filter, sized columns. ``export()`` dispatches on the file extension.

Row model maps onto the REAL ``Listing`` / ``ListingDetail`` / ``Dealer`` /
``ImageSet`` fields; unknown values are ``None`` (fail-closed), never guessed.
"""

from __future__ import annotations

import csv
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .models import Gallery, Listing, ListingDetail

# ---------------------------------------------------------------------------
# Column model: (row_key, header, kind) — kind ∈ {n, s, b, d}
# ---------------------------------------------------------------------------

_BASE_COLUMNS: list[tuple[str, str, str]] = [
    ("vin", "VIN", "s"),
    ("legacy_chassis", "Chassis (classic)", "s"),
    ("id_opaque", "ID (opaque, undecrypted)", "s"),
    ("car_name", "Model", "s"),
    ("car_id", "Model Code", "s"),
    ("year", "Year", "n"),
    ("price", "Price", "n"),
    ("price_unit", "Currency", "s"),
    ("odometer", "Odometer", "n"),
    ("odometer_unit", "Odometer Unit", "s"),
    ("exterior_color", "Colour", "s"),
    ("trim_color", "Interior", "s"),
    ("engine", "Engine", "s"),
    ("gear_box", "Gearbox", "s"),
    ("power", "Power", "s"),
    ("power_unit", "Power Unit", "s"),
    ("top_speed", "Top Speed", "s"),
    ("fuel_type", "Fuel", "s"),
    ("country_slug", "Country", "s"),
    ("country_name", "Country Name", "s"),
    ("region_slug", "Region", "s"),
    ("dealer_name", "Dealer", "s"),
    ("dealer_city", "Dealer City", "s"),
    ("dealer_country", "Dealer Country", "s"),
    ("dealer_lat", "Dealer Lat", "n"),
    ("dealer_lon", "Dealer Lon", "n"),
    ("ferrari_approved", "Ferrari Approved", "b"),
    ("classic_car", "Classic", "b"),
    ("special_or_limited", "Special/Limited", "b"),
    ("tailored", "Tailored", "b"),
    ("ferrari_atelier", "Atelier", "b"),
    ("is_new", "New", "b"),
    ("image_count", "Images", "n"),
    ("image_urls", "Image URLs", "s"),
    ("gallery_thumbs", "Gallery Thumbs", "s"),
    ("thron_hero_id", "THRON Gallery ID", "s"),
    ("ad_id_blob", "ID (blob)", "s"),
    ("vin_blob", "VIN blob", "s"),
    ("source_url", "Source URL", "s"),
    ("fetched_at", "Fetched At", "d"),
    ("raw_sha256", "Response SHA-256", "s"),
    ("contract_version", "Contract Version", "s"),
]

_DETAIL_COLUMNS: list[tuple[str, str, str]] = [
    ("detail_chassis", "Chassis (detail)", "s"),
    ("detail_status", "Status", "s"),
    ("detail_publish_date", "Published", "s"),
    ("detail_boarding_date", "Boarded", "s"),
    ("detail_expire_at", "Expires", "s"),
    ("detail_drive_type", "Drive Type", "s"),
    ("detail_zero_to_hundred", "0-100 (s)", "s"),
    ("detail_total_displacement", "Displacement", "s"),
    ("detail_specific_output", "Specific Output", "s"),
    ("detail_one_off", "One-off", "b"),
    ("detail_track_car", "Track Car", "b"),
    ("detail_few_off", "Few-off", "b"),
    ("detail_ferrari_original", "Ferrari Original", "b"),
    ("detail_carfax", "Carfax", "b"),
    ("detail_dealer_phone", "Dealer Phone", "s"),
    ("detail_dealer_email", "Dealer Email", "s"),
    ("detail_dealer_website", "Dealer Website", "s"),
]

EXPORT_COLUMNS: list[tuple[str, str, str]] = _BASE_COLUMNS + _DETAIL_COLUMNS


# Row builder — maps REAL model fields onto columns (fail-closed)
# ---------------------------------------------------------------------------

def _d_or_s(v: Any, fmt: str = "s") -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def listing_to_row(listing: Listing, detail: Optional[ListingDetail] = None,
                   *, gallery: Optional[Gallery] = None) -> dict[str, Any]:
    """Flatten a Listing (+detail +gallery) into an ordered export row.

    Everything comes from real parsed fields; missing = ``None``.
    """
    d = listing.dealer
    prov = listing.provenance
    img = listing.images

    homenet_urls = list(img.homenet or [])
    gallery_urls = [u for u in gallery.images if u] if gallery else []
    all_urls = homenet_urls + gallery_urls
    all_urls = [u for u in all_urls if u]

    dfull: dict[str, Any] = (detail.dealer_full_info if detail else {}) or {}

    row: dict[str, Any] = {
        "vin": listing.vin,
        "legacy_chassis": listing.legacy_chassis,
        "id_opaque": None if (listing.vin or listing.legacy_chassis)
                     else listing.ad_id_blob,
        "car_name": listing.car_name,
        "car_id": listing.car_id,
        "year": listing.year,
        "price": listing.price,
        "price_unit": listing.price_unit,
        "odometer": listing.odometer,
        "odometer_unit": listing.odometer_unit,
        "exterior_color": listing.exterior_color,
        "trim_color": listing.trim_color,
        "engine": listing.engine,
        "gear_box": listing.gear_box,
        "power": listing.power,
        "power_unit": listing.power_unit,
        "top_speed": listing.top_speed,
        "fuel_type": listing.fuel_type,
        "country_slug": listing.country_slug,
        "country_name": listing.country_name,
        "region_slug": listing.region_slug,
        "dealer_name": d.name if d else None,
        "dealer_city": d.city if d else None,
        "dealer_country": d.country if d else None,
        "dealer_lat": d.lat if d else None,
        "dealer_lon": d.lon if d else None,
        "ferrari_approved": listing.ferrari_approved,
        "classic_car": listing.classic_car,
        "special_or_limited": listing.special_or_limited,
        "tailored": listing.tailored,
        "ferrari_atelier": listing.ferrari_atelier,
        "is_new": listing.is_new,
        "image_count": len(all_urls),
        "image_urls": "\n".join(all_urls),
        "gallery_thumbs": "\n".join(gallery_urls),
        "thron_hero_id": img.thron_hero_gallery_id or img.thron_card_gallery_id,
        "ad_id_blob": listing.ad_id_blob,
        "vin_blob": listing.vin_blob,
        "source_url": prov.source_url if prov else None,
        "fetched_at": _d_or_s(prov.fetched_at) if prov else None,
        "raw_sha256": prov.raw_sha256 if prov else None,
        "contract_version": prov.contract_version if prov else None,
    }
    if detail is not None:
        dfull = detail.dealer_full_info or {}
        row.update({
            "detail_chassis": detail.chassis,
            "detail_status": detail.status,
            "detail_publish_date": _d_or_s(detail.publish_date),
            "detail_boarding_date": _d_or_s(detail.boarding_date),
            "detail_expire_at": _d_or_s(detail.expire_at),
            "detail_drive_type": detail.drive_type,
            "detail_zero_to_hundred": detail.zero_to_hundred,
            "detail_total_displacement": detail.total_displacement,
            "detail_specific_output": detail.specific_output,
            "detail_one_off": detail.one_off,
            "detail_track_car": detail.track_car,
            "detail_few_off": detail.few_off,
            "detail_ferrari_original": detail.ferrari_original,
            "detail_carfax": detail.carfax,
            "detail_dealer_phone": dfull.get("phone"),
            "detail_dealer_email": dfull.get("email"),
            "detail_dealer_website": dfull.get("website"),
        })
    else:
        row.update({k: None for k, _, _ in _DETAIL_COLUMNS})
    return row


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def export_json(listings: Iterable[Listing],
                details: Optional[Mapping[str, ListingDetail]] = None,
                path: "str | Path" = "ferrari_preowned.json") -> dict[str, Any]:
    """Write a structured JSON export (nested, not flattened)."""
    import dataclasses
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for listing in listings:
        key = listing.identity_key
        if key in seen:
            continue
        seen.add(key)
        det = _lookup_detail(details, listing)
        entry = dataclasses.asdict(listing)
        if det is not None:
            entry["detail"] = dataclasses.asdict(det)
        out.append(entry)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(out),
        "listings": out,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                 encoding="utf-8")
    return {"path": str(p), "count": len(out), "format": "json"}


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def export_csv(listings: Iterable[Listing],
               details: Optional[Mapping[str, ListingDetail]] = None,
               path: "str | Path" = "ferrari_preowned.csv") -> dict[str, Any]:
    """Write a flattened CSV with all export columns."""
    rows, seen = [], set()
    for listing in listings:
        key = listing.identity_key
        if key in seen:
            continue
        seen.add(key)
        det = _lookup_detail(details, listing)
        rows.append(listing_to_row(listing, det))
    headers = [h for _, h, _ in EXPORT_COLUMNS]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ad = None
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for row in rows:
            w.writerow([row.get(k) for k, _, _ in EXPORT_COLUMNS])
    return {"path": str(p), "count": len(rows), "format": "csv"}


def _lookup_detail(details, listing) -> Optional[ListingDetail]:
    if not details:
        return None
    key = listing.identity_key
    v = details.get(key)
    if v is not None:
        return v
    # snapshot stores details under ad_id_blob key — try that too
    return details.get(listing.ad_id_blob)


# XLSX — minimal OOXML writer (stdlib zipfile only)
# ---------------------------------------------------------------------------

def _xlsx_escape(text: Any) -> str:
    s = "" if text is None else str(text)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


_COL_LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]


def _col_name(idx: int) -> str:
    """0-based column index -> A1-style letters."""
    name = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = _COL_LETTERS[rem] + name
    return name


def export_xlsx(listings: Iterable[Listing],
                details: Optional[Mapping[str, ListingDetail]] = None,
                path: "str | Path" = "ferrari_preowned.xlsx",
                *, galleries: Optional[Mapping[str, Gallery]] = None) -> dict[str, Any]:
    """Write an XLSX with styled header, frozen pane, auto-filter.

    Uses inline strings (no shared-strings table) — simpler and still
    fully standard. Numbers stay numeric; everything else inline string.
    """
    rows, seen = [], set()
    for listing in listings:
        key = listing.identity_key
        if key in seen:
            continue
        seen.add(key)
        det = _lookup_detail(details, listing)
        gal = galleries.get(listing.ad_id_blob) if galleries else None
        rows.append(listing_to_row(listing, det, gallery=gal))

    n_cols = len(EXPORT_COLUMNS)
    n_rows = len(rows)
    last_col = _col_name(n_cols - 1)

    # column widths: wide for text-heavy columns
    WIDE = {"image_urls", "gallery_thumbs", "source_url", "car_name",
            "ad_id_blob", "vin", "id_opaque", "dealer_name", "engine",
            "vin_blob", "detail_dealer_website", "detail_dealer_email",
            "detail_dealer_phone"}
    cols_xml = "".join(
        f'<col min="{i+1}" max="{i+1}" width="{40 if key in WIDE else 14}" customWidth="1"/>'
        for i, (key, _, _) in enumerate(EXPORT_COLUMNS)
    )

    # header row (styled + frozen)
    header_cells = "".join(
        f'<c r="{_col_name(c)}1" t="inlineStr" s="1">'
        f'<is><t>{_xlsx_escape(h)}</t></is></c>'
        for c, (_, h, _) in enumerate(EXPORT_COLUMNS)
    )
    row_parts: list[str] = [f'<row r="1">{header_cells}</row>']
    for r_i, row in enumerate(rows, start=2):
        cells = []
        for c_i, (key, _, kind) in enumerate(EXPORT_COLUMNS):
            v = row.get(key)
            if v is None or v == "":
                continue
            ref = f"{_col_name(c_i)}{r_i}"
            if isinstance(v, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{1 if v else 0}</v></c>')
            elif kind == "n" and isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                s = _xlsx_escape(v)
                cells.append(
                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{s}</t></is></c>')
        row_parts.append(f'<row r="{r_i}">{"".join(cells)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0" activeCell="A2">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        f'<cols>{cols_xml}</cols>'
        f'<sheetData>{"".join(row_parts)}</sheetData>'
        f'<autoFilter ref="A1:{last_col}{max(n_rows + 1, 2)}"/>'
        '</worksheet>'
    )

    style_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid">'
        '<fgColor rgb="FF1A1A2E"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="PreOwned" sheetId="1" r:id="rId2"/></sheets>'
        '</workbook>'
    )

    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", style_xml)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return {"path": str(p), "count": n_rows, "format": "xlsx"}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def export(listings: "Iterable[Listing] | SnapshotResult",
           details: Optional[Mapping[str, ListingDetail]] = None,
           path: "str | Path" = "ferrari_preowned",
           *, galleries: Optional[Mapping[str, Gallery]] = None) -> dict[str, Any]:
    """Export listings to JSON/CSV/XLSX, dispatching on the file extension."""
    from .snapshot import SnapshotResult
    if isinstance(listings, SnapshotResult):
        details = details or listings.details
        listings = listings.listings.values()
    if details:
        detail_objs = {}
        for k, v in details.items():
            detail_objs[k] = v
        details = detail_objs
    p = Path(path)
    if p.suffix.lower() == ".json":
        return export_json(listings, details, p)
    if p.suffix.lower() == ".csv":
        return export_csv(listings, details, p)
    if p.suffix.lower() == ".xlsx":
        return export_xlsx(listings, details, p, galleries=galleries)
    raise ValueError(f"Unsupported export format: {p.suffix!r} "
                     f"(use .json, .csv, or .xlsx)")


__all__ = ["export", "export_json", "export_csv", "export_xlsx",
           "listing_to_row", "EXPORT_COLUMNS"]
