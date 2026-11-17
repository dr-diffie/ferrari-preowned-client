# Proxy Guide

Optional proxy support with pre-checked, sticky, health-tracked routing.

## Quick start

```python
from ferrari_preowned_client import FerrariPreownedClient, ProxyPool, load_proxies_file

pool = ProxyPool(load_proxies_file("free-proxies.txt"))
working = pool.precheck(timeout=12)     # two-stage check, ~1 call per proxy

with FerrariPreownedClient(locale="de-DE", pool=pool) as client:
    ...  # all requests route through the pool
```

Without a pool the client goes direct — the verified baseline (100% success at human pacing).

## File format

One proxy URL per line; `http://`, `https://`, `socks5://`, `socks4://` schemes;
`http://user:pass@host:port` auth supported (via requests). Bare `host:port`
lines are treated as `http://`. Comments (`#`) and CRLF tolerated. Duplicates removed.

## The routing philosophy

**Why sticky, not round-robin.** A WAF profiles *sequences* of requests from an
IP. Ten requests in a minute, each from a different IP, all hitting the same
API = textbook coordinated scraping. One IP making a paced sequence = a user
(via VPN). So:

- `pick()` selects one proxy and **sticks** to it.
- A failure releases the sticky binding; the failed proxy enters an
  **exponential cooldown** (2 min → 4 → 8 → … capped at 1 h).
- 4 consecutive failures mark a proxy **dead** (excluded until pool rebuild).
- `rotate()` switches sticky proxies deliberately — call it **between** snapshot
  runs, never mid-run.

**Why pre-check before routing.** The two-stage pre-check guarantees no real
request is wasted on a dead proxy, and no weird half-response ever reaches
your parsers:

1. **Stage 1 — TCP reachability** to the proxy address (cheap, no target contact).
2. **Stage 2 — real GET** of the Ferrari listing page through the proxy,
   requiring HTTP 200 + site content. This also warms and validates the full
   chain (proxy → CloudFront → origin).

Free proxies are volatile — expect most to fail. That's the pool doing its
job: it benches them with cooldowns instead of routing through them.

## Health bookkeeping

```python
for s in pool.stats():
    print(s["proxy"], s["failures"], s["dead"], s["last_latency"], s["cooldown_remaining"])
```

- `failures` — consecutive failures (reset on any success)
- `last_latency` — measured on the pre-check and on every successful request
- `cooldown_remaining` — seconds until it's eligible again
- `dead` — excluded from routing after 4 consecutive failures

## Choosing proxies

- **HTTP forward proxies** are fine for this site (plain HTTPS CONNECT).
- Prefer a **small pool of stable proxies** over a large list of dead ones —
  the pre-check cost is one request per proxy per pool build.
- Rebuild/re-precheck the pool daily; free-list proxies rot in hours.

## What the pool does NOT do

- No automatic rotation in response to `BlockedError` — a block stops the run
  (see [stealth.md](stealth.md)). Evading a WAF signal by switching identity
  is both adversarial and the loudest possible bot fingerprint.
- No retry-storms: retries are bounded (3) with exponential backoff, and
  failed-through-proxy requests count toward the proxy's failure counter.
