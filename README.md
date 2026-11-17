<div align="center">

# 🐴 ferrari-preowned-client

**A stealthy, fully-verified Python client for [preowned.ferrari.com](https://preowned.ferrari.com) — search API, detail pages, THRON image galleries, and VIN decryption with a self-healing keystream oracle.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-108%20passing-brightgreen)](#tests)
[![Live Verified](https://img.shields.io/badge/live%20verified-2026--09--24-success)](#verification)
[![Dependencies](https://img.shields.io/badge/dependencies-requests%20only-orange)](#design-principles)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![Contract](https://img.shields.io/badge/contract-preowned--api--2026--09--24-informational)](ferrari_preowned_client/contract.py)

*No browser. No CAPTCHA. No headless anything. Just plain HTTP that behaves like a person.*

</div>

---

## ✨ What it does

| Capability | How | Cost |
|---|---|---|
| 🌍 **Country catalog** | all 61 countries with regions + SEO slugs | 1 request |
| 🔢 **Live listing counts** | every country's ad count in one probe | 2 requests |
| 🚗 **Full listings per country** | search API, `limit=44` (verified max), auto-pagination, dedupe | ~1 request / 44 cars |
| 🔓 **VIN decryption** | static keystream, cracked & triple-verified — plaintext VINs from encrypted blobs | free |
| 🖼️ **Image galleries** | US cars: full gallery *inside* the search response; EU cars: THRON playlist endpoint | 1 request / EU car |
| 📄 **Deep detail records** | 93-field SSR records (chassis, publish date, full spec, dealer hours) via plain page GETs | 1 cached GET / car |
| 🧾 **Exports** | JSON / CSV / **XLSX** (stdlib OOXML writer — styled header, frozen pane, auto-filter) | free |
| 🖼️ **Image downloads** | on-the-fly WebP compression (never-grow rule), per-car folders, resume-safe | ~1 request / image |
| 🧠 **Smart incremental mode** | skip 100%-identical listings; fetch only diffs; removal tracking | near-zero on re-runs |
| 💻 **CLI** | `ferrari-preowned counts / search / full / smart / verify` | — |
| 🛡️ **Stealth by design** | coherent personas, referer realism, human pacing, dwell pauses, stop-on-block | — |
| 🔁 **Self-healing crypto** | keystream rotation? one oracle call re-derives it | 1 request |
| 🧦 **Optional proxies** | pre-checked, sticky-routed, health-tracked pool | — |

## 📦 What's new in v1.3

**Bullet-proofing (v1.2)**
- IP-block fail-safes: pre-flight probe classifies CloudFront/WAF blocks
  *before* the run starts (store untouched, exit code 2, actionable report
  section with egress IP + guidance). Any mid-run block aborts the whole
  run; 10 consecutive image-CDN 403s abort the image plane.
- Transport retries transient 5xx with `Retry-After`-honoring backoff
  (a single server hiccup no longer skips a whole country).
- Dead-upstream image 404s are cached in the store meta
  (`dead_image_urls`) — recorded once, skipped with **zero requests** on
  every later run.
- Gallery URLs are deduped and `.part` temp files are per-thread — the
  two-workers-one-tmp-path race is eliminated.
- Removals are recorded per-country at `country_done`, scoped and
  checkpointed immediately (crash-safe); a partially walked country never
  records removals; a blocked-at-startup run never touches the store.
- Audit folder lookups match by 64-char identity prefix (80-char-truncated
  keys) and union legacy sibling folders; orphan files are counted so
  disk waste is visible.
- 2.9× measured image-plane speedup (4 workers, burst 12 @0.8–2.0s,
  per-image sleep 0.05–0.15s) — still 3–5× gentler than a real browser
  tab. The API plane keeps its full stealth pacing.

**Progress reporting (v1.3)**
- Live `sold N` counter: delisted cars are shown in the progress line and
  logged per-country as they are detected.
- Per-country expected totals from the `counts()` probe: `(DE 312/594)`.
- Global car denominator fixed before the walk starts: `cars 1234/3393`
  plus an observed fetch ETA (wall-time per walked car).
- No duplicate lines, no lost country tags, windowed honest rates (an
  idle queue shows `0.0/s`, never a stale cumulative average).
- `--progress json`: machine-readable events (`progress` snapshots,
  `log` lines, final `done` verdict) for programmatic consumers —
  the library equivalent is `run_pipeline(..., on_snapshot=...)`.

**Production-proven** — three full campaigns: a 29.7h global crawl (48
countries, 3,104 listings, 78,645 images), a 15h 50-day-churn crawl
(2,840 requests, 2,834×HTTP 200, 0 throttles/blocks), and a 1.4h
incremental verification run (323/323 HTTP 200, 100% disk audit).

## 🚀 Quick start

```bash
pip install requests        # the only dependency
```

```python
from ferrari_preowned_client import FerrariPreownedClient

with FerrariPreownedClient(locale="de-DE") as client:
    # Every country + live counts
    counts = client.counts()                    # {'US': 1104, 'DE': 319, ...}

    # All listings for a country — VINs decrypted
    for car in client.country_listings("DK"):
        print(car.vin, car.car_name, car.year, car.price, car.odometer)
        # ZFFBT55B000128196 575M Maranello 2002 135000 39500

    # Deep record for one car (chassis, publish date, full spec)
    detail = client.detail(car)
    print(detail.chassis, detail.publish_date, detail.drive_type)

    # Full EU image gallery (36 items for the 575M)
    gallery = client.gallery(detail)
```

**Full global snapshot** (3,000+ listings, checkpointed, resumable):

```python
result = client.snapshot(checkpoint_path="snapshot.json")
print(result.summary.unique_listings, "unique cars across",
      len(result.summary.countries), "countries")
```

**With the proxy pool** (optional):

```python
from ferrari_preowned_client import ProxyPool, load_proxies_file

pool = ProxyPool(load_proxies_file("free-proxies.txt"))
pool.precheck()                       # two-stage: reachability + real GET
with FerrariPreownedClient(pool=pool) as client:
    ...
```

**Everything: data + images + exports + incremental skips** (CLI):

```console
$ ferrari-preowned full  /mnt/data/PreOwned        # first run: everything
$ ferrari-preowned smart /mnt/data/PreOwned        # later runs: only diffs
```

## 🕵️ The VIN decryption (the fun part)

Ferrari's current API returns `vin` and `id` as encrypted blobs
(`IljixbcgVnJGsKsQfDGsr…`). We reverse-engineered the scheme during a
live session on 2026-09-24:

```
blob = base64(urldecode(value))
     = [16B constant header][32B nonce][XOR-encrypted payload]

payload(vin) = "ZFFBT55B000128196"      ← decrypted with a static 17-byte keystream
payload(id)  = "ZFFBT55B000128196-17559…"
```

The keystream is **static and position-only** — derived once via the site's
own oracle (it serves plaintext VINs for US/CA cars in en-US locale, per the
frontend's `canDisplayVin`), then verified on 115/117 captured blobs across
three countries and cross-checked against detail-page chassis numbers.

**If Ferrari rotates the key**, the client re-derives it in one call:

```python
client.refresh_keystream()   # majority-vote oracle, tolerates bad data rows
```

See [docs/future-proofing.md](docs/future-proofing.md) for the complete
what-if-they-change-X runbook.

## 📊 Data you get

**`Listing`** (search API, 47 upstream fields): VIN, model, year, price + currency,
odometer, colors, engine, gearbox, power, fuel, Ferrari-Approved flags,
dealer (name/city/geo), country, and — for US cars — the **complete image
gallery URLs** right in the response.

**`ListingDetail`** (detail page, 93 fields) adds: chassis number, publish
date, status/expiry, drive type, 0–100, displacement, specific output,
carfax flag, classic/one-off/track-car flags, full `specification`
sections, `overviewData` (incl. maintenance dates), options &
standard equipment, dealer hours & contacts, extra THRON galleries
(hero + video playlist).

Every record carries **`Provenance`**: source URL, fetch timestamp,
raw SHA-256, and the contract version — so your stored data is always
auditable back to the exact response it came from.

## 🛡️ Stealth model (the short version)

- **One coherent browser persona per run** (UA + matching Sec-Fetch/client-hint headers) — never a mid-session UA change, which is itself a bot signal
- **Human pacing**: ≥6 s + jitter between requests, occasional 25–90 s dwell pauses; no parallelism, ever
- **Referer realism**: API calls refer the listing page; detail GETs refer the search page — the exact pattern the real frontend emits
- **Cached page GETs over API calls** where possible (CloudFront loves us)
- **Stop-on-block**: 403/405 halts with `BlockedError`; 202-empty halts with `ThrottledError`. The library **never rotates identities to evade a WAF signal** — that's both adversarial and the loudest bot fingerprint there is
- **Hard request budget** (default 500/run)

Full reasoning in [docs/stealth.md](docs/stealth.md).

## 🧪 Tests

47 core tests + 50 added for export/images/smart/CLI/engine — zero network in CI:

- **Offline contract tests** run against *real captured responses* (DK/US/BE search JSON, full detail-page HTML, THRON playlist, 44 oracle pairs) — including the known-VIN decryption cross-check (`ZFFBT55B000128196` ↔ chassis `128196`) and the known Ferrari data quirk (a car whose id-blob embeds an outdated VIN → majority-vote derivation).
- **Behavior tests** (mocked transport): pacing, budget, throttle/block semantics, ledger completeness, persona coherence, proxy sticky-routing/cooldown/death, strict-decryption fail-closed.
- **Export tests**: CSV/JSON/XLSX round-trips on the real DK fixture; column coverage; dedupe; the XLSX opens with correct row/freeze/filter structure; **the model catalog must resolve every real URL segment** captured on the live listing page (incl. `ferrari-purosangue` and `250-gt-2+2`).
- **SmartStore tests**: new/unchanged/changed classification, persistence round-trip, removal tracking, corrupt-store recovery, gallery change detection.
- **Engine tests (v1.1)**: put-time totals (denominator never grows), split counters + ETA rendering, immediate error logging + registry, run-report round-trip incl. ledger stats, disk audit detects missing files, full offline pipeline runs for both modes (smart run proves `detail()` is never called for unchanged cars; a failing detail surfaces in the report while the run still completes).

```bash
python -m pytest tests/ -q
# 108 passed
```

<a name="verification"></a>
## ✔️ Verification

Everything in the contract was **verified against the live site** on
2026-09-24 (~35 sequential requests, full ledger kept): 61-country catalog,
48 active countries / 3,076 listings globally, `limit=44` pagination,
0-based pages, multi-country calls, THRON playlists (no auth), detail-SSR
URL construction, and the keystream oracle. The failure modes (202-throttle
window, `limit>total` → empty ads, 55+ → 504, region-less → total 0) are
encoded as constants and tests, not folklore.

Live smoke re-run after every library change:
`countries → counts → search(DK) → detail → gallery` = 5 requests, ~35 s,
all VINs decrypted, chassis cross-check green.

## 📁 Repository layout

```
ferrari-preowned-client/
├── ferrari_preowned_client/
│   ├── contract.py      # every verified constant (single point of truth)
│   ├── personas.py      # coherent browser identities
│   ├── decrypt.py       # blob decryption + oracle self-heal
│   ├── parsing.py       # response parsers (fail-closed)
│   ├── models.py        # Listing / ListingDetail / Gallery / Provenance
│   ├── proxies.py       # pre-checked sticky pool
│   ├── transport.py     # pacing / budget / ledger / stop-on-block
│   ├── snapshot.py      # orchestration + checkpoints
│   ├── client.py        # the facade (incl. model-catalog URL builder)
│   ├── export.py        # JSON / CSV / XLSX (stdlib OOXML)
│   ├── images.py        # smart image downloader (WebP, resume-safe)
│   ├── pipeline.py      # two-plane full-run engine
│   ├── smart.py         # incremental SmartStore + smart runs
│   ├── cli.py           # `ferrari-preowned` entry point
│   └── errors.py        # typed, evidenced errors
├── tests/               # 66 tests + real captured fixtures
├── examples/snapshot_demo.py
├── docs/
│   ├── api.md           # full API reference
│   ├── export-images-smart.md  # exports, image downloader, smart mode, CLI
│   ├── stealth.md       # the stealth model & its limits
│   ├── proxies.md       # pool design & routing philosophy
│   └── future-proofing.md  # the runbook for when Ferrari changes things
└── pyproject.toml
```

## 📜 License

MIT — internal tooling for the Modena Maestro project. Use respectfully:
this library is for legitimate monitoring/research at conservative volumes,
not for hammering Ferrari's infrastructure. The pace limits are a feature.

<div align="center">

*Built with patience, captured evidence, and one very cooperative 575M Maranello.*

</div>
