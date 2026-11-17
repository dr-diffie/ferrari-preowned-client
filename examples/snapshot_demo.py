"""Fetch a full snapshot of every country's listings.

Usage:
    python examples/snapshot_demo.py [--countries US,DK] [--with-details] [--proxy-file ../free-proxies.txt]

The demo writes ``snapshot.json`` and prints a summary. Pacing is the
verified-safe default (~6-10s between requests) — a full global run for
~3,000 listings takes roughly 40-60 minutes by design.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferrari_preowned_client import (
    FerrariPreownedClient, ProxyPool, load_proxies_file,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", help="Comma-separated country codes (default: all active)")
    ap.add_argument("--with-details", action="store_true", help="Also fetch detail pages")
    ap.add_argument("--proxy-file", help="Optional proxy list file")
    ap.add_argument("--out", default="snapshot.json")
    args = ap.parse_args()

    pool = None
    if args.proxy_file:
        proxies = load_proxies_file(args.proxy_file)
        pool = ProxyPool(proxies)
        print(f"pre-checking {len(proxies)} proxies…")
        working = pool.precheck()
        print(f"  {len(working)} working")

    countries = args.countries.split(",") if args.countries else None

    with FerrariPreownedClient(locale="de-DE", pool=pool) as client:
        print(f"persona: {client.persona.family}")
        result = client.snapshot(
            countries=countries,
            include_details=args.with_details,
            include_galleries=False,
            checkpoint_path=args.out,
            progress=lambda l: print(f"  {l.vin or l.legacy_chassis or l.ad_id_blob[:20]} | "
                                      f"{l.car_name} {l.year} | {l.price} {l.price_unit}"),
        )
        result.save(args.out)

    s = result.summary
    print("\n=== SNAPSHOT COMPLETE ===")
    print(f"countries:       {len(s.countries)}")
    print(f"total listings:  {s.total_listings}")
    print(f"unique listings: {s.unique_listings}")
    print(f"duplicates:      {s.duplicates}")
    print(f"requests:        {s.requests_made}/{s.requests_budget}")
    for note in s.notes:
        print(f"note: {note}")
    print(f"saved: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
