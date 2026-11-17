"""Optional proxy pool with smart, undetectable routing.

Philosophy (see docs/proxies.md for the full reasoning):

* **Optional** — with no configuration the library runs proxy-free, exactly
  like the verified baseline (plain direct requests, which worked 100%).
* **Pre-checked** — every proxy is validated *before* it enters the routing
  pool, with a two-stage check: (1) cheap reachability (connect to the
  proxy itself), (2) real-world check (fetch the actual target site
  through it and require a 200 + expected content). A proxy that fails
  either stage never sees a real request routed through it.
* **Sticky routing** — once a working proxy is chosen for a logical
  "session", subsequent requests stick to it. Rapidly jumping IPs between
  requests looks robotic; a session that consistently comes from one IP
  looks like a user behind a VPN.
* **Gentle rotation** — between snapshot runs (not between requests), or
  after a proxy shows degradation (errors/slow), with cooldown periods
  before a dropped proxy may be re-tested.
* **Health bookkeeping** — consecutive-failure counters, exponential
  cooldowns, and a monotonic "last latency" per proxy so the router can
  avoid sick proxies without hammering them.

The pool never uses anonymity tricks beyond what the proxies themselves
provide; it never rotates in response to a WAF block (that would be
evasion, see docs/stealth.md) — a block raises
:class:`~ferrari_preowned_client.errors.BlockedError` and stops.
"""

from __future__ import annotations

import random
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from . import contract

#: Seconds a proxy is benched after a failure (doubles per consecutive miss).
COOLDOWN_BASE = 120.0
COOLDOWN_MAX = 3600.0
#: A proxy is marked dead after this many consecutive failures.
DEAD_THRESHOLD = 4


@dataclass
class ProxyHealth:
    failures: int = 0
    last_failure: float = 0.0
    last_success: float = 0.0
    last_latency: Optional[float] = None
    dead: bool = False

    @property
    def cooldown_remaining(self) -> float:
        if self.dead or self.failures == 0:
            return 0.0
        cd = min(COOLDOWN_BASE * (2 ** (self.failures - 1)), COOLDOWN_MAX)
        elapsed = time.monotonic() - self.last_failure
        return max(0.0, cd - elapsed)

    def record_success(self, latency: float) -> None:
        self.failures = 0
        self.last_success = time.monotonic()
        self.last_latency = latency

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure = time.monotonic()
        if self.failures >= DEAD_THRESHOLD:
            self.dead = True


class ProxyPool:
    """A pool of HTTP(S) proxies with pre-checks and sticky routing.

    Parameters
    ----------
    proxies:
        Iterable of proxy URLs (``http://host:port``; auth via
        ``http://user:pass@host:port`` is supported by ``requests``).
    check_url:
        URL fetched through the proxy during the deep pre-check. Defaults
        to the Ferrari listing page (CloudFront-cached, cheap, realistic).
    """

    def __init__(self, proxies, *, check_url: Optional[str] = None,
                 rng: Optional[random.Random] = None):
        self._entries: list[tuple[str, ProxyHealth]] = []
        for p in proxies:
            url = str(p).strip()
            if url and not url.startswith(("http://", "https://", "socks5://", "socks4://")):
                url = "http://" + url
            if url and url not in {e for e, _ in self._entries}:
                self._entries.append((url, ProxyHealth()))
        self._check_url = check_url or (contract.BASE_URL + "/de-DE/r/europa/ferrari-gebraucht/d-nemark/rfc")
        self._rng = rng or random.Random()
        self._sticky: Optional[str] = None
        self._lock = threading.Lock()

    # -- introspection ------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def stats(self) -> list[dict]:
        with self._lock:
            return [
                {"proxy": url, "failures": h.failures, "dead": h.dead,
                 "last_latency": h.last_latency,
                 "cooldown_remaining": round(h.cooldown_remaining, 1)}
                for url, h in self._entries
            ]

    # -- selection ----------------------------------------------------------

    def pick(self) -> Optional[str]:
        """Pick the current sticky proxy or the best available one."""
        with self._lock:
            if self._sticky:
                for url, h in self._entries:
                    if url == self._sticky and not h.dead and h.cooldown_remaining == 0:
                        return url
            candidates = [
                (url, h) for url, h in self._entries
                if not h.dead and h.cooldown_remaining == 0
            ]
            if not candidates:
                return None
            # Prefer previously-successful (known latency), then random.
            known = [(u, h) for u, h in candidates if h.last_latency]
            pool = known or candidates
            url = self._rng.choice(pool)[0]
            self._sticky = url
            return url

    def release(self, url: str, *, ok: bool, latency: Optional[float] = None) -> None:
        """Report the outcome of a request routed through ``url``."""
        with self._lock:
            for u, h in self._entries:
                if u == url:
                    if ok:
                        h.record_success(latency or 0.0)
                    else:
                        h.record_failure()
                        if self._sticky == u:
                            self._sticky = None
                    return

    def rotate(self) -> Optional[str]:
        """Force selection of a different sticky proxy (between runs)."""
        with self._lock:
            self._sticky = None
            candidates = [
                (url, h) for url, h in self._entries
                if not h.dead and h.cooldown_remaining == 0
                and url != self._sticky
            ]
            if not candidates:
                return None
            url = self._rng.choice(candidates)[0]
            self._sticky = url
            return url

    # -- pre-checks ---------------------------------------------------------

    def precheck(self, *, timeout: float = 10.0, session_factory=None) -> list[str]:
        """Two-stage pre-check of every proxy; returns working URLs.

        Stage 1 (cheap): TCP connect to the proxy address itself.
        Stage 2 (deep): GET ``check_url`` through the proxy and require a
        200 response whose body looks like the target site.

        This method performs network I/O. It is intended to be called once
        before a snapshot run; it never fetches anything Ferrari-specific
        beyond one cached page per proxy.
        """
        import requests

        working = []
        for url, health in list(self._entries):
            host_port = _host_port(url)
            if not _tcp_reachable(host_port, timeout=timeout):
                health.record_failure()
                continue
            try:
                sess = session_factory() if session_factory else requests.Session()
                t0 = time.monotonic()
                r = sess.get(self._check_url, proxies={"http": url, "https": url},
                             timeout=timeout, headers={
                                 "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
                                 "Accept": "text/html,*/*;q=0.8",
                             })
                latency = time.monotonic() - t0
                ok = r.status_code == 200 and "ferrari" in r.text.lower()[:4000]
            except Exception:
                ok, latency = False, None
            if ok:
                health.record_success(latency or 0.0)
                working.append(url)
            else:
                health.record_failure()
        return working

    def working(self) -> list[str]:
        with self._lock:
            return [u for u, h in self._entries if not h.dead]


def _host_port(url: str) -> tuple[str, int]:
    p = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    return p.hostname or "127.0.0.1", p.port or 80


def _tcp_reachable(host_port: tuple[str, int], *, timeout: float) -> bool:
    import socket
    host, port = host_port
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def load_proxies_file(path) -> list[str]:
    """Load proxies from a file OR accept a bare proxy URL.

    Two accepted forms:
    - a path to a proxies.txt file (one URL per line, CRLF tolerated)
    - a single proxy URL (``http://user:pass@host:port``) — the typical
      Docker/production case with one sticky ISP proxy; passing it
      directly avoids a secrets file
    """
    if "://" in str(path):
        return [str(path).strip()]
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#"):
                out.append(line)
    return out
