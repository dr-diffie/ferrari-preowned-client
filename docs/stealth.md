# Stealth Design

How this library stays quiet, and why it refuses certain "stealth" tricks.

## What the site sees

The stack in front of preowned.ferrari.com:

1. **AWS CloudFront** (CDN + cache, 5-minute TTL on pages)
2. **AWS WAF** (`aws-waf-token` cookie; JS challenge capability)
3. nginx behind it

What was verified live on 2026-09-24 with plain `python-requests`:

- Page GETs: 200 every time (cached, no WAF interaction)
- Search POSTs with the frontend-exact payload: 200 every time at human pacing
- No CAPTCHA, no TLS/JA3 fingerprint blocking observed
- The only throttle: HTTP 202 with empty body after rapid bursts (~10 requests under ~60 s)

## The library's stealth model

**1. Personas, not random UAs.** One coherent browser identity per run (Firefox/Chrome/Safari mix with matching `Sec-Fetch-*` / `sec-ch-ua` / `Accept-Encoding` headers). A UA that changes mid-session is itself a bot signal; a stable one is indistinguishable from a user. Run-to-run variety comes from the persona draw, plus plausible (not bleeding-edge) version pools.

**2. Referer realism.** API calls carry the referer of the listing page a user would be on; detail GETs carry the listing referer — the exact pattern the real frontend produces (verified by capturing its network traffic).

**3. Pacing.** Minimum interval 6s + up to 4s jitter between requests, plus occasional 25–90 s "dwell" pauses every 8–14 requests — the rhythm of a person reading listings, not a script. Defaults are derived from measured throttle behavior; you can go slower, never faster without re-verifying.

**4. Page GETs over API calls where possible.** Detail data comes from the CloudFront-cached HTML page — the cheapest, most common, least suspicious request type on the whole site.

**5. Volume limits.** A hard request budget (default 500) bounds every run. The search API is only used for listing pages (`limit=44`, the verified safe maximum that still returns ads).

**6. CloudFront cache discipline.** Where the cache serves us (`X-Cache: Hit`), we benefit; we never try to bust it.

## What the library deliberately does NOT do

These are the lines we don't cross — some for ethics, some because they would *increase* detectability:

- **No identity rotation on block.** A 403 raises `BlockedError` and stops. Rotating UA/IP/proxy to evade a WAF block is (a) adversarial escalation against an explicit signal, and (b) exactly the pattern WAFs score highest. If you get blocked, you were too fast — slow down tomorrow.
- **No CAPTCHA solving, no token injection, no headless browsers** for the production path. Verified unnecessary: the site serves everything to plain HTTP at human pacing.
- **No request parallelism.** One request in flight, always. Concurrency is the loudest bot signal a crawler can emit.
- **No fingerprint spoofing beyond headers** (no TLS/JA3 munging). Not needed — verified. If Ferrari ever starts JA3-blocking, the documented path is to add a `curl_cffi` transport *variant*, not to mutate mid-run.

## If you need more throughput

The honest answer: you don't, for a daily global snapshot. 3,000 listings = ~70 search calls + optional detail GETs ≈ one hour at safe pacing, once per day. If you truly need more, the supported axis is **time-shifting** (run countries in slices across hours), not speed.

## Detection canaries to watch

Your monitoring (outside this library) should alert on:

- `ThrottledError` in a run → your pacing config is too aggressive for current site state
- `BlockedError` → stop for the day; review `transport.ledger` for what preceded it
- `ContractChangedError` → the site changed; see [future-proofing.md](future-proofing.md)
