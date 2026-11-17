#!/usr/bin/env python3
"""ferrari-preowned — CLI for the Ferrari Pre-Owned client.

Usage:
    ferrari-preowned countries
    ferrari-preowned counts
    ferrari-preowned search DK
    ferrari-preowned detail VIN_OR_BLOB
    ferrari-preowned snapshot [--countries US,DK] [--details] [--galleries]
                              [--checkpoint PATH]
    ferrari-preowned full /mnt/path            # full pipeline + images + exports
    ferrari-preowned smart /mnt/path           # incremental (skip unchanged)
    ferrari-preowned verify                    # keystream oracle self-check

Global options:
    --locale de-DE        site locale (default de-DE)
    --seed N              reproducible persona/pacing
    --pace SECONDS        min interval between requests (default 6)
    --budget N            max requests per run (default 500)
    --no-vin-decrypt      keep VIN blobs opaque
    --proxies FILE        use a proxy pool (optional)
    --workers N           image download workers (default 2)
    --image-quality N     WebP quality (default 82)
    --max-dimension N     cap image long edge (default: original)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .client import FerrariPreownedClient
from .transport import TransportConfig


def _fmt_listing(l) -> str:
    price = f"{l.price:,} {l.price_unit or ''}".strip() if l.price else "—"
    ident = l.vin or l.legacy_chassis or l.ad_id_blob[:18] + "…"
    odo = f"{l.odometer:,} {l.odometer_unit or ''}".strip() if l.odometer else "—"
    return (f"  {ident:<20} {str(l.car_name or '?')[:28]:<28} "
            f"{str(l.year or ''):<5} {price:>15}  {odo}")


def _build_client(args) -> FerrariPreownedClient:
    pool = None
    if getattr(args, "proxies", None):
        from .proxies import ProxyPool, load_proxies_file
        pool = ProxyPool(load_proxies_file(args.proxies))
        pool.precheck()
    cfg = TransportConfig(
        min_interval=args.pace, request_budget=args.budget)
    return FerrariPreownedClient(
        locale=args.locale, pool=pool, decrypt_vins=not args.no_vin_decrypt,
        config=cfg, seed=args.seed)


def cmd_countries(args) -> int:
    with _build_client(args) as client:
        for c in client.countries():
            print(f"{c.slug:<4} {c.name:<28} region={c.region:<3} "
                  f"count={c.count:<5} seo={c.slug_seo}")
    return 0


def cmd_counts(args) -> int:
    with _build_client(args) as client:
        counts = client.counts()
        total = 0
        active = 0
        for slug, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if n > 0:
                active += 1
                total += n
                print(f"{slug:<4} {n:>6}")
        print(f"\n{active} active countries, {total} listings total")
    return 0


def cmd_search(args) -> int:
    with _build_client(args) as client:
        listings, (total, pages, limit) = client.search(
            args.country.upper(), page=args.page)
        print(f"total={total} pages={pages} limit={limit} got={len(listings)}\n")
        for l in listings:
            print(_fmt_listing(l))
    return 0


def cmd_detail(args) -> int:
    with _build_client(args) as client:
        # find the listing by VIN/chassis/blob prefix via a DK search proxy
        # is unreliable; require the user to supply a full blob
        print("detail by identifier requires the full ad id blob;")
        print("use `search` + the snapshot pipeline instead.")
        return 1


def cmd_snapshot(args) -> int:
    with _build_client(args) as client:
        result = client.snapshot(
            countries=_csv_list(args.countries),
            include_details=args.details,
            include_galleries=args.galleries,
            checkpoint_path=args.checkpoint)
        s = result.summary
        print(f"unique={s.unique_listings} total={s.total_listings} "
              f"requests={s.requests_made}/{s.requests_budget}")
    return 0


def cmd_full(args) -> int:
    from .engine import run_pipeline
    t0 = time.time()
    log_fn, on_snapshot, _ = _progress_callbacks(args)
    with _build_client(args) as client:
        result = run_pipeline(
            client, args.target, mode="full",
            countries=_csv_list(args.countries),
            image_workers=args.workers,
            progress=log_fn, on_snapshot=on_snapshot)
    return _print_engine_result(result, time.time() - t0,
                                style=getattr(args, "progress", "line"))


def _progress_callbacks(args):
    """Build (log_fn, on_snapshot) for the requested progress mode."""
    if getattr(args, "progress", "line") == "json":
        import json as _json
        from datetime import datetime, timezone as _tz

        def log_fn(msg: str) -> None:
            print(_json.dumps(
                {"type": "log", "ts": datetime.now(_tz.utc).isoformat(
                    timespec="seconds"), "msg": msg}), flush=True)

        def on_snapshot(snap: dict) -> None:
            print(_json.dumps({"type": "progress", "snapshot": snap}),
                  flush=True)

        return log_fn, on_snapshot, "json"
    return (lambda msg: print(msg, flush=True)), None, "line"


def cmd_smart(args) -> int:
    from .engine import run_pipeline
    t0 = time.time()
    log_fn, on_snapshot, _ = _progress_callbacks(args)
    with _build_client(args) as client:
        result = run_pipeline(
            client, args.target, mode="smart",
            store=_open_store(args),
            countries=_csv_list(args.countries),
            image_workers=args.workers,
            progress=log_fn, on_snapshot=on_snapshot)
    return _print_engine_result(result, time.time() - t0,
                                style=getattr(args, "progress", "line"))


def _open_store(args):
    from .smart import SmartStore
    from pathlib import Path
    if getattr(args, "store", None):
        return SmartStore(args.store)
    return SmartStore(Path(args.target) / "smart_store.json")


def _print_engine_result(result: dict, wall: float, style: str = "line") -> int:
    import json as _json
    if style == "json":
        # machine mode: one final done-event, no pretty block
        print(_json.dumps({
            "type": "done",
            "blocked": bool(result.get("blocked")),
            "interrupted": bool(result.get("interrupted")),
            "error_count": result.get("error_count", 0),
            "summary": result.get("summary"),
            "report_path": result.get("report_path"),
        }), flush=True)
        if result.get("blocked"):
            return 2
        return 1 if result.get("error_count") else 0
    if result.get("blocked"):
        print("\n" + "=" * 64)
        print("  BLOCKED — see run_report.json 'block' section")
        print("  " + result["report"]["block"]["guidance"])
        print("=" * 64)
        return 2
    s = result["summary"]
    print("\n" + "=" * 64)
    print(f"  mode:            {s['mode']}"
          + ("  [INTERRUPTED]" if result["interrupted"] else ""))
    print(f"  listings:        {s['listings']}"
          + (f"  (sold/delisted: {s.get('listings_sold', 0)})"
             if s.get("listings_sold") else ""))
    print(f"  details fetched: {s['details_fetched']}")
    print(f"  galleries:       {s['galleries']}")
    print(f"  images:          {s['images_downloaded']} downloaded "
          f"+ {s['images_skipped']} skipped "
          f"- {s['images_failed']} failed")
    c = s.get("compression") or {}
    if c.get("bytes_original"):
        ratio = (1 - c["bytes_final"] / c["bytes_original"]) * 100
        print(f"  compression:     {c['bytes_original']/1e6:.1f}MB -> "
              f"{c['bytes_final']/1e6:.1f}MB ({ratio:.0f}% saved)")
    if s.get("store_stats"):
        print(f"  store:           {s['store_stats']}")
    audit = result.get("audit")
    if audit:
        print(f"  audit:           {audit['present_files']}/"
              f"{audit['expected_files']} expected files on disk"
              + (f" ({audit['missing_files']} MISSING)" if audit["missing_files"]
                 else " — complete"))
    errs = result["error_count"]
    print(f"  errors:          {errs}"
          + (f" ({result['errors'][0]['kind']}: "
             f"{result['errors'][0]['message'][:60]}…)" if errs else ""))
    print(f"  wall time:       {wall/3600:.1f} h")
    print(f"  report:          {result['report_path']}")
    print("=" * 64)
    return 1 if (errs or (audit and audit["missing_files"])) else 0


def cmd_verify(args) -> int:
    with _build_client(args) as client:
        ks = client.refresh_keystream()
        from . import contract
        ok = ks == contract.KEYSTREAM_V1
        print(f"oracle keystream: {ks.hex()}")
        print(f"matches verified KS17: {ok}")
        return 0 if ok else 1


def _print_pipeline_result(result: dict, wall: float, smart: bool = False) -> None:
    print("\n" + "=" * 60)
    print(f"  listings:  {result.get('listings')}")
    print(f"  details:   {result.get('details')}")
    print(f"  galleries: {result.get('galleries')}")
    if smart:
        print(f"  skipped_unchanged: {result.get('skipped_unchanged')}")
    st = result.get("stats") or {}
    if st:
        orig = st.get("bytes_original", 0)
        fin = st.get("bytes_final", 0)
        ratio = (1 - fin / orig) * 100 if orig else 0
        print(f"  images ok: {result.get('images_ok')} "
              f"(fail {result.get('images_failed')})")
        print(f"  compression: {orig/1e6:.1f}MB → {fin/1e6:.1f}MB "
              f"({ratio:.1f}% saved)")
    errs = result.get("fetch_errors") or []
    if errs:
        print(f"  fetch errors: {len(errs)}")
        for k, e in errs[:10]:
            print(f"    {k}: {e[:100]}")
    print(f"  wall time:  {wall/60:.1f} min")
    print("=" * 60)


def _csv_list(s):
    return [x.strip().upper() for x in s.split(",")] if s else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ferrari-preowned",
        description="Stealthy client for preowned.ferrari.com")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--locale", default="de-DE")
        sp.add_argument("--seed", type=int, default=None)
        sp.add_argument("--pace", type=float, default=6.0)
        sp.add_argument("--budget", type=int, default=500)
        sp.add_argument("--no-vin-decrypt", action="store_true")
        sp.add_argument("--proxies", default=None)

    common_p = argparse.ArgumentParser(add_help=False)
    common_p.add_argument("--locale", default="de-DE")
    common_p.add_argument("--seed", type=int, default=None)
    common_p.add_argument("--pace", type=float, default=6.0)
    common_p.add_argument("--budget", type=int, default=500)
    common_p.add_argument("--no-vin-decrypt", action="store_true")
    common_p.add_argument("--proxies", default=None)

    sub.add_parser("countries", parents=[common_p], help="list all countries")
    sub.add_parser("counts", parents=[common_p], help="per-country counts")
    sp = sub.add_parser("search", parents=[common_p], help="search a country")
    sp.add_argument("country")
    sp.add_argument("--page", type=int, default=0)
    sub.add_parser("verify", parents=[common_p], help="oracle self-check")

    sp = sub.add_parser("snapshot", parents=[common_p], help="data snapshot")
    sp.add_argument("--countries", default=None)
    sp.add_argument("--details", action="store_true")
    sp.add_argument("--galleries", action="store_true")
    sp.add_argument("--checkpoint", default=None)

    sp = sub.add_parser("full", parents=[common_p], help="full pipeline run")
    sp.add_argument("target")
    sp.add_argument("--countries", default=None)
    sp.add_argument("--no-details", action="store_true")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--progress", choices=("line", "json"), default="line",
                    help="output mode: human lines or JSON events")

    sp = sub.add_parser("smart", parents=[common_p], help="incremental run")
    sp.add_argument("target")
    sp.add_argument("--store", default=None)
    sp.add_argument("--countries", default=None)
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--progress", choices=("line", "json"), default="line",
                    help="output mode: human lines or JSON events")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "countries": cmd_countries, "counts": cmd_counts,
        "search": cmd_search, "detail": cmd_detail,
        "snapshot": cmd_snapshot, "full": cmd_full,
        "smart": cmd_smart, "verify": cmd_verify,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
