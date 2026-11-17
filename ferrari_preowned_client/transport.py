"""HTTP transport: pacing, jitter, dwell, budget, ledger, stop-on-block.

This is the stealth core. Every request the library makes goes through
:class:`Transport`, which enforces:

* **Pacing** — a configurable minimum interval between requests plus
  uniform jitter, and occasional longer "human dwell" pauses. Defaults are
  derived from the verified throttle behavior (202-empty after rapid
  bursts; comfortable at >=5-8s spacing).
* **Budget** — a hard cap on requests per run; exceeding raises
  :class:`BudgetExceededError` instead of silently continuing.
* **Stop-on-block** — 403/405 raise :class:`BlockedError` immediately (no
  retry, no identity rotation); 202-empty raises :class:`ThrottledError`;
  5xx retry with backoff only when transient and within retry limits.
* **Ledger** — every request and response is recorded (method, URL,
  status, bytes, latency, cache header) for audit and post-mortem.

The transport is proxy-aware: when a :class:`~ferrari_preowned_client.proxies.ProxyPool`
is attached, requests are routed through the sticky proxy and outcomes are
reported back to the pool for health tracking.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

from . import contract
from .errors import (
    BlockedError, BudgetExceededError, FerrariPreownedError, ServerError,
    ThrottledError, TransportError,
)
from .personas import Persona, sample as sample_persona


@dataclass
class LedgerEntry:
    ts: str
    method: str
    url: str
    status: Optional[int]
    bytes: int
    latency: float
    proxy: Optional[str]
    cache: Optional[str]
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TransportConfig:
    min_interval: float = contract.DEFAULT_MIN_INTERVAL
    jitter: float = contract.DEFAULT_JITTER
    request_budget: int = contract.DEFAULT_REQUEST_BUDGET
    dwell_every: tuple[int, int] = contract.DWELL_EVERY
    dwell_seconds: tuple[float, float] = contract.DWELL_SECONDS
    timeout: float = 60.0
    max_retries: int = 3
    retry_backoff: float = 8.0


class Transport:
    """Paced, budgeted, ledgered HTTP transport with optional proxies."""

    def __init__(self, *, config: Optional[TransportConfig] = None,
                 persona: Optional[Persona] = None,
                 pool=None, rng: Optional[random.Random] = None,
                 on_dwell: Optional[Callable[[float], None]] = None):
        self.config = config or TransportConfig()
        self.persona = persona or sample_persona(rng)
        self.pool = pool
        self.rng = rng or random.Random()
        self.on_dwell = on_dwell
        self._session = requests.Session()
        self._last_request_at: float = 0.0
        self._requests_made = 0
        self._next_dwell_at: int = self._draw_dwell_every()
        self.ledger: list[LedgerEntry] = []

    # ------------------------------------------------------------------ api

    @property
    def requests_made(self) -> int:
        return self._requests_made

    def get(self, url: str, *, locale: str, referer: Optional[str] = None,
            params: Optional[dict] = None) -> requests.Response:
        headers = self.persona.page_headers(locale, referer=referer)
        return self._request("GET", url, headers=headers, params=params)

    def post_json(self, url: str, payload: dict[str, Any], *, locale: str,
                  referer: str) -> requests.Response:
        headers = self.persona.api_headers(locale, referer=referer)
        return self._request("POST", url, headers=headers, payload=payload)

    def close(self) -> None:
        self._session.close()

    def ledger_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.ledger]

    # ------------------------------------------------------------- internals

    def _draw_dwell_every(self) -> int:
        lo, hi = self.config.dwell_every
        return self.rng.randint(lo, hi)

    def _pace(self) -> None:
        """Sleep so that requests are spaced >= min_interval + jitter."""
        now = time.monotonic()
        wait = self._last_request_at + self._next_delay - now
        self._last_request_at = now + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)
        # Dwells: every N requests, take one long human-like pause.
        if self._requests_made and self._requests_made >= self._next_dwell_at:
            lo, hi = self.config.dwell_seconds
            pause = self.rng.uniform(lo, hi)
            if self.on_dwell:
                self.on_dwell(pause)
            time.sleep(pause)
            self._next_dwell_at = self._requests_made + self._draw_dwell_every()

    @property
    def _next_delay(self) -> float:
        return self.config.min_interval + self.rng.uniform(0.0, self.config.jitter)

    def _request(self, method: str, url: str, *, headers: dict[str, str],
                 payload: Optional[dict[str, Any]] = None,
                 params: Optional[dict] = None) -> requests.Response:
        if self._requests_made >= self.config.request_budget:
            raise BudgetExceededError(
                f"request budget of {self.config.request_budget} exhausted",
                evidence={"requests_made": self._requests_made},
            )
        self._pace()

        proxies = None
        if self.pool is not None:
            proxy_url = self.pool.pick()
            if proxy_url is None:
                raise TransportError("no working proxy available in pool")
            proxies = {"http": proxy_url, "https": proxy_url}
        else:
            proxy_url = None
        attempt = 0
        while True:
            t0 = time.monotonic()
            entry_kw = {"method": method, "url": url, "proxy": proxy_url}
            try:
                if method == "POST":
                    resp = self._session.post(
                        url, json=payload, headers=headers, params=params,
                        timeout=self.config.timeout, proxies=proxies)
                else:
                    resp = self._session.get(
                        url, headers=headers, params=params,
                        timeout=self.config.timeout, proxies=proxies)
                latency = time.monotonic() - t0
                self._record(entry_kw, resp, latency)
                self._requests_made += 1
                if proxy_url and self.pool is not None:
                    self.pool.release(proxy_url, ok=True, latency=latency)
                try:
                    return self._handle_status(resp)
                except ServerError as exc:
                    # transient 5xx: retry with backoff (docstring promise
                    # the old code never kept — a single 500 used to skip
                    # a whole country walk)
                    attempt += 1
                    if attempt > self.config.max_retries:
                        raise
                    delay = exc.evidence.get("retry_after") \
                        if getattr(exc, "evidence", None) else None
                    if not delay:
                        delay = self.config.retry_backoff * (2 ** (attempt - 1))
                    time.sleep(float(delay))
                    continue
            except requests.RequestException as exc:
                latency = time.monotonic() - t0
                self._requests_made += 1
                if proxy_url and self.pool is not None:
                    self.pool.release(proxy_url, ok=False)
                self.ledger.append(LedgerEntry(
                    ts=_iso_now(), status=None, bytes=0, latency=latency,
                    cache=None, error=str(exc)[:200], **entry_kw))
                attempt += 1
                if attempt > self.config.max_retries:
                    raise TransportError(
                        f"{method} {url} failed after {attempt} attempts: {exc}",
                        evidence={"proxy": proxy_url},
                    ) from exc
                time.sleep(self.config.retry_backoff * (2 ** (attempt - 1)))

    def _handle_status(self, resp: requests.Response) -> requests.Response:
        status = resp.status_code
        if status == contract.THROTTLE_STATUS and not resp.text.strip():
            raise ThrottledError(
                f"rate limited (202 empty) after {self._requests_made} requests",
                retry_after=_retry_after(resp),
            )
        if status in contract.BLOCK_STATUS:
            raise BlockedError(
                f"blocked with HTTP {status} — WAF challenge or contract change",
                evidence={"url": resp.url, "body_head": resp.text[:200]},
            )
        if status >= 500:
            raise ServerError(
                f"server error {status} from {resp.url}",
                evidence={"retry_after": _retry_after(resp), "body_head": resp.text[:200]},
            )
        if status == 404:
            resp.status_code = 404  # pass through; callers treat as missing
        return resp

    def _record(self, entry_kw, resp: requests.Response, latency: float) -> None:
        self.ledger.append(LedgerEntry(
            ts=_iso_now(), status=resp.status_code, bytes=len(resp.content or b""),
            latency=latency, cache=resp.headers.get("X-Cache"),
            error=None, **entry_kw))


def _retry_after(resp: requests.Response) -> Optional[float]:
    v = resp.headers.get("Retry-After")
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
