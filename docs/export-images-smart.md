# Exports, Images, Smart Mode, CLI, Run Engine

The five production surfaces on top of the core client.

## Exports (`ferrari_preowned_client.export`)

Zero-dependency export of listings to JSON / CSV / XLSX.

```python
from ferrari_preowned_client import export

export(result, path="cars.xlsx")        # dispatches on extension
export(result, path="cars.csv")
export(result, path="cars.json")        # nested, provenance preserved
```

- **59 columns** mapped 1:1 onto the real model fields (VIN, legacy
  chassis, price, odometer, dealer geo, THRON ids, detail-only fields,
  provenance: source URL / fetched-at / raw SHA-256 / contract version).
- `image_urls` / `gallery_thumbs` columns carry newline-joined URL lists;
  `image_count` is numeric.
- XLSX is a hand-rolled OOXML writer (stdlib `zipfile` only): styled dark
  header, frozen top pane, auto-filter, sized columns; numbers stay
  numeric. Verified to open in LibreOffice/Excel.
- Dedupe by `identity_key` (VIN > chassis > id-blob) is built in.

## Images (`ferrari_preowned_client.images`)

```python
from ferrari_preowned_client import ImageDownloader, ImageConfig

cfg = ImageConfig(quality=82, max_dimension=None, burst_size=6)
dl  = ImageDownloader(client.transport, cfg)
res = dl.download_gallery(listing, gallery, "/path/images")
# res = {"ok": n, "total": n, "skipped": n, "folder": ...}
```

- Rides the SAME persona/session as the data client (one coherent
  browser identity).
- Browser-like image requests: `Sec-Fetch-Dest: image`, listing page as
  referer, per-car bursts with pauses.
- **On-the-fly WebP compression** (Pillow; optional): EXIF-orientation
  applied, alpha preserved, `method=6`, strict **never-grow rule** — the
  original bytes are kept whenever recompression wouldn't help.
- **Resume-safe**: files are named `{index:03d}_{sha256(url)[:10]}.webp`;
  an existing non-empty file is skipped → re-runs only fetch NEW images.
- Throttle-aware: one 60s cooldown retry on HTTP 202, then stop.
- Verified live on DK: 762 images, 0 failures, **76.5% size reduction**
  (55.4 MB → 13.0 MB).

## Smart mode (`ferrari_preowned_client.smart`)

Incremental runs that skip everything we already own — as long as it is
**100% identical**.

```python
from ferrari_preowned_client import run_smart_snapshot

result = run_smart_snapshot(client, "/mnt/.../PreOwned",
                            countries=None)   # all countries
```

How it decides (per listing, keyed by `identity_key`):

| Fresh search says | Store says | Action |
|---|---|---|
| ad hash identical | detail + gallery known | **skip entirely** (0 requests) |
| ad hash differs | — | re-fetch detail + gallery; download only NEW image URLs |
| not in store | — | full fetch (detail + gallery + images) |
| in store, absent from search | — | mark `removed` (history preserved) |

The store (`smart_store.json`) records ad/detail hashes, the full image
URL list, and first-seen/last-seen/last-change timestamps per listing.
Because image filenames hash the URL, a changed gallery automatically
downloads only the new files. Verified live on DK: run 1 fetches
everything; run 2 skips **29/29 unchanged listings with zero** detail or
gallery requests.

## CLI (`ferrari-preowned`)

```console
$ ferrari-preowned counts
US     1102
DE      319
...

$ ferrari-preowned search DK
$ ferrari-preowned countries
$ ferrari-preowned verify                     # keystream oracle self-check

$ ferrari-preowned full  /mnt/data/PreOwned   # full pipeline + images + exports
$ ferrari-preowned smart /mnt/data/PreOwned   # incremental (skip unchanged)
```

Options: `--locale --seed --pace --budget --no-vin-decrypt --proxies FILE
--workers N --countries US,DK --store PATH`. Exit code is non-zero when
a run had fetch errors. Install via `pip install .` (entry point
`ferrari-preowned`) or run as `python -m ferrari_preowned_client.cli`.

## URL ground truth (sitemap-verified 2026-09-26)

The first global run's post-mortem (305 detail-404s across 19 countries)
revealed that *constructed* URL segments diverge from the site's canonical
slugs in several ways. **`sitemap-listing.xml` / `sitemap-ads.xml`
(robots.txt → sitemap-index.xml) are the canonical ground truth** for every
URL shape — when in doubt, diff against them, don't guess.

Rules recovered from the sitemap:

* **Region segments**: `MEA → naher-osten-afrika` (NOT
  `mittlerer-osten-und-afrika`), `GC → gro-china` (NOT `china`) —
  `contract.REGION_SEO` encodes all six.
* **Ferrari's slugifier drops umlauts to a dash, keeping edge dashes**:
  `Dänemark → d-nemark`, `Großbritannien → gro-britannien`,
  `Österreich → -sterreich` (leading dash!), `V.A.E. → v-a-e-`,
  `...S.A. → ...-s-a-` (trailing dash). `_slugify_dash()` implements this.
* **Country segments** come from the countries catalog `slugSeo`
  (preferred) with `_slugify_dash(name)` fallback — `_country_seo()`.
* **Dealer segments** come from the dealers catalog (dealerCode →
  slugSeo, learned from every search response via
  `_absorb_dealer_catalog()`) — `_dealer_slug()`.
* **Model segments** from the models catalog `slugSeo` — `_model_slug()`.

## Run engine v1.1 (`ferrari_preowned_client.engine`)

`run_pipeline()` powers both `full` and `smart` CLI modes. Redesigned after
the first 14-hour global run exposed the weaknesses, then hardened again
after the 29.7-hour run's post-mortem:

* **Heal path** — a car classified `unchanged` whose store entry has NO
  gallery URLs (e.g. its detail 404'd before the URL fixes) re-fetches its
  detail, learns the gallery, and queues it. Visible as the `heal N`
  counter. The 304 victims of the URL bugs heal themselves this way on the
  next smart run — no manual surgery.
* **Legacy folder migration** — pre-catalog car folders
  (`VIN_purosangue-2025`) are migrated into the canonical name
  (`VIN_ferrari-purosangue-2025`) on first touch, so resume-skip and the
  audit always agree on one location.

**Honest progress.** Totals are incremented at *queue* time — the
denominator never grows mid-run. Counters are split
(`downloaded / skipped / failed`), and every line carries rate + ETA:

```
[7h12m] fetch 41/48 (US) | new 2104 chg 3 same 1013 | imgs 50104/58186 (86%) dl 49871 skip 204 fail 29 | 1.9/s ETA 1h22m
```

A 30s **heartbeat** renders even when nothing completes (long
compression), so the run never *looks* stuck.

**Typed, immediate error handling.** Every failure is logged the moment
it happens (`[error/detail] ZFF…: detail page gone (404)`), collected
into a registry, and written to **`run_report.json`** with full context
(kind, subject, message, timestamps), a request-ledger summary (status
counts, throttle/block events), and the disk audit. SIGINT/SIGTERM →
graceful stop: store checkpointed, report written, non-zero exit code.
The CLI exit code is non-zero whenever any error or audit gap occurred —
silent failure is impossible.

**Post-run audit (never miss data).** `audit_images()` verifies every
gallery URL recorded in the store maps to an existing file on disk and
reports anything missing; the next smart run auto-completes gaps via
resume-safe queueing.

```python
from ferrari_preowned_client import run_pipeline
result = run_pipeline(client, "/mnt/data/PreOwned", mode="smart")
result["error_count"], result["audit"]["missing_files"]  # both 0 = clean
```

## Progress output (v1.3 format)

```
[13m00s] fetch 5/49 (DE 312/594) cars 1234/3397 ETA 1h12m | new 0 chg 18 same 303 heal 3 sold 7 | imgs 5181/5181 (100%) dl 7 skip 5174 fail 0 | 0.0/s ETA —
```

- `fetch C/N (CC walked/expected)` — live country progress against the
  pre-known per-country total from the `counts()` probe; the tag is kept
  through audit/done phases (never lost between countries).
- `cars D/T ETA` — global car denominator (fixed when the run starts,
  never grows) plus an observed fetch ETA (wall-time per walked car,
  hidden below 20 cars).
- `new/chg/same/heal/sold` — per-run classification, including the live
  sold/delisted counter (per-country removals are recorded and
  checkpointed at `country_done`; a partially walked country records
  nothing).
- `imgs D/T (P%) dl/skip/fail` — image plane with put-time totals.
- `rate/s ETA` — **windowed** rate (delta since the last rendered line):
  an idle queue honestly shows `0.0/s`, never a stale cumulative average.
- Identical consecutive lines are suppressed (workers + heartbeat used
  to double-print); every number shown is true at that moment.

## JSON progress mode

`--progress json` (CLI) / `on_snapshot=` (library) emits one JSON object
per line:

- `{"type": "progress", "snapshot": {...}}` — structured counters on every
  rendered line (`cars_total`, `cars_done`, `listings_sold`, image split…)
- `{"type": "log", "ts": ..., "msg": ...}` — all log and error lines
- `{"type": "done", "blocked": ..., "summary": ..., "report_path": ...}` —
  final verdict (exit 0 clean / 1 errors / 2 blocked)
