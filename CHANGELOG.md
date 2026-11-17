# Changelog

## 1.3.0 — 2026-11-17

Production-verified release. Third full campaign: 49/49 countries,
323/323 HTTP 200, 0 throttles/blocks, 100% disk audit, 1.4h incremental.

### Progress reporting
- Live `sold N` counter for delisted cars, shown in-line and logged
  per-country as detected.
- Per-country expected car totals from the `counts()` probe:
  `fetch 5/49 (DE 312/594)`.
- Global car denominator `cars D/T` fixed before the walk (put-time,
  never grows mid-run) + observed fetch ETA.
- Duplicate-line suppression, country-tag retention between countries,
  windowed rates (idle queue = honest `0.0/s`).
- `--progress json` CLI mode and `on_snapshot=` library callback:
  structured `progress`/`log`/`done` events.

### Bullet-proofing
- IP-block fail-safes: pre-flight probe (exit 2, store untouched,
  actionable block report with egress IP + guidance), mid-run aborts,
  10×-consecutive image-CDN 403 abort.
- Transport retries 5xx with Retry-After-honoring backoff.
- Dead-upstream 404s cached in store meta (`dead_image_urls`) — zero
  requests burned on later runs.
- Gallery URL dedup + per-thread `.part` names (worker race eliminated).
- Per-country removal recording at `country_done` (scoped, checkpointed,
  crash-safe; partial walks never record; zero-walk runs never touch
  the store).
- Audit: 64-char identity-prefix folder matching, legacy sibling union,
  orphan counting.
- Image plane 2.9× measured speedup with footprint still 3–5× gentler
  than a browser tab; API plane stealth pacing untouched.

## 1.0.0 — 2026-09-28

Initial production release: reverse-engineered API client, VIN
decryption (KS17 static keystream + US/CA oracle self-heal), smart
incremental store, WebP image pipeline, CSV/JSON/XLSX export, CLI,
82 tests, two full campaigns (29.7h global run + heal campaign,
99.97% disk audit).
