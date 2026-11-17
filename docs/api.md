# API Reference

All public names are exported from the package root:

```python
from ferrari_preowned_client import FerrariPreownedClient
```

## `FerrariPreownedClient`

```python
FerrariPreownedClient(
    locale="de-DE",            # de-DE, en-US, fr-CA, en-CA
    persona=None,              # Persona; random coherent one if omitted
    pool=None,                 # ProxyPool; direct requests if omitted
    decrypt_vins=True,         # decrypt VIN blobs with the verified keystream
    strict_decryption=False,   # raise KeystreamError instead of degrading
    config=None,               # TransportConfig
    seed=None,                 # RNG seed (tests / reproducible runs)
)
```

Context-manager safe (`with ... as client:`).

### Methods

| Method | Requests | Returns |
|---|---|---|
| `countries(refresh=False)` | 1 POST | `list[Country]` — 61 options with `slug/name/region/slug_seo` |
| `counts()` | 2 POST | `dict[str, int]` — live per-country ad counts (all countries in one probe) |
| `search(country, page=0, limit=44, region=None, ...)` | 1 POST | `(list[Listing], (total, pages, limit))` |
| `country_listings(country, limit=44, max_pages=None)` | 1+ POST | `Iterator[Listing]` — auto-paginates, dedupes by ad id |
| `detail(listing)` | 1 GET | `ListingDetail` — full 93-field SSR record (chassis, publishDate, specification, …) |
| `gallery(listing_or_detail)` | 1 GET | `Gallery` — full THRON playlist (EU cars) |
| `refresh_keystream()` | 1 POST | `bytes` — re-derives the VIN keystream from the en-US oracle |
| `snapshot(countries=None, include_details=False, include_galleries=False, checkpoint_path=None, progress=None)` | many | `SnapshotResult` |

### Attributes

- `client.transport.ledger` — every request made (method, URL, status, bytes, latency, proxy, cache header). `ledger_dicts()` for JSON.
- `client.transport.requests_made` / `config.request_budget`
- `client.persona` — the active browser persona (UA + headers).

## Models

- **`Country`** — `slug, name, region, slug_seo, count, allow_multiple, hide_ads_count`
- **`Listing`** — 40+ typed fields (see `models.py`); key ones: `vin` (decrypted), `vin_blob`, `legacy_chassis`, `ad_id_blob`, `car_name`, `year`, `price`, `price_unit`, `odometer`, `odometer_unit`, `exterior_color`, `trim_color`, `engine`, `gear_box`, `power`, `dealer`, `country_slug`, `images.homenet` (full US gallery), `images.thron_card_gallery_id`, `provenance`, `raw` (full upstream dict)
- **`ListingDetail(Listing)`** — adds `chassis`, `publish_date`, `status`, `expire_at`, `drive_type`, `zero_to_hundred`, `total_displacement`, `specific_output`, `model_year`, `carfax`, `classic_car`, `one_off`, `track_car`, `few_off`, `ferrari_original`, `highlight`, `dealer_notes`, `specification` (sections), `overview_data`, `standard_equipment`, `optionals`, `dealer_full_info`, `certification_logos`, plus gallery ids: `images.thron_hero_gallery_id`, `images.thron_video_playlist_id`
- **`Gallery`** — `items: list[GalleryItem]` with `content_id, name, content_type, thumb_url, content_url`; `Gallery.images` / `Gallery.videos`
- **`SnapshotResult`** — `summary` + `listings/details/galleries` dicts keyed by VIN; `.save(path)` / `.load(path)`
- **`Provenance`** — on every record: `source_url`, `fetched_at`, `raw_sha256`, `contract_version`

## Errors

| Error | Meaning | Library behavior |
|---|---|---|
| `ThrottledError` | HTTP 202 empty (rate limit) | raised immediately, never retried through |
| `BlockedError` | HTTP 403/405 (WAF/contract) | raised immediately, never bypassed |
| `ServerError` | HTTP 5xx | raised after bounded retries |
| `KeystreamError` | VIN decryption failed | `strict=False`: vin=None + blob kept; `strict=True`: raised |
| `ContractChangedError` | response shape drifted | raised with evidence dict |
| `BudgetExceededError` | request budget exhausted | raised before the request is sent |
| `TransportError` / `ProxyError` / `NoWorkingProxyError` | network / pool failures | raised with evidence |

Every error carries `.evidence` (dict) for diagnosis.

## `TransportConfig`

```python
TransportConfig(
    min_interval=6.0,      # seconds between requests (verified safe)
    jitter=4.0,            # +0..4s uniform
    request_budget=500,    # hard cap per run
    dwell_every=(8, 14),   # every N requests …
    dwell_seconds=(25, 90) # … pause like a human reading a page
    timeout=60.0,
    max_retries=3,
    retry_backoff=8.0,
)
```

## `ProxyPool`

```python
pool = ProxyPool(load_proxies_file("free-proxies.txt"))
pool.precheck(timeout=12)      # two-stage: TCP reachability + real GET through proxy
pool.pick() -> "http://..."    # sticky routing; same proxy until failure
pool.release(url, ok=True, latency=1.2)
pool.rotate()                  # between runs, not between requests
pool.stats()                   # health, cooldowns, latencies
```

See [proxies.md](proxies.md) for the routing philosophy.

## Decrypt helpers (`ferrari_preowned_client.decrypt`)

- `decrypt_vin(blob) -> str`
- `decrypt_id_vin(id_blob) -> str`
- `is_encrypted(value) -> bool`
- `derive_keystream(plaintext_vin, id_blob) -> bytes` (oracle self-heal primitive)
- `derive_keystream_majority(pairs) -> (bytes, agreement_ratio)`
- `verify_keystream(ks, pairs, min_agreement=0.8) -> bool`

## `engine.run_pipeline(...)`

Two-plane run engine shared by `full`/`smart` (v1.3):

```python
from ferrari_preowned_client.engine import run_pipeline

result = run_pipeline(
    client, "/path/to/store", mode="smart",   # or "full"
    countries=["DK", "AT"],                   # optional subset
    image_workers=4,
    progress=my_log_fn,        # human lines (default: print)
    on_snapshot=my_event_fn,   # structured dicts on every rendered line
)
```

- `progress(msg)` — every human-readable log/progress line.
- `on_snapshot(dict)` — the structured RunProgress snapshot whenever a
  line renders (`cars_total`, `cars_done`, `listings_sold`, image
  counters, `images_total`, ...). Use this for programmatic UIs; the CLI's
  `--progress json` mode wraps it into JSON lines.
- Returns `{"summary", "errors", "report", "report_path", "audit",
  "blocked", "interrupted", "error_count"}`; `summary` includes
  `listings_new/changed/unchanged/healed/sold`.

Removals are recorded per-country at `country_done` (scoped + checkpointed
immediately); interrupted partial walks record nothing; a run that walks
zero countries (e.g. blocked pre-flight) leaves the store untouched.
