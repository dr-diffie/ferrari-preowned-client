"""Transport, persona, and proxy-pool behavior — fully mocked, no network."""

from __future__ import annotations

import random
import time
from unittest.mock import patch

import pytest

from ferrari_preowned_client import contract, personas
from ferrari_preowned_client.errors import (
    BlockedError, BudgetExceededError, ThrottledError, TransportError,
)
from ferrari_preowned_client.proxies import ProxyPool, load_proxies_file, ProxyHealth
from ferrari_preowned_client.transport import Transport, TransportConfig


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------

class TestPersonas:
    def test_sample_returns_coherent_persona(self):
        rng = random.Random(42)
        for _ in range(30):
            p = personas.sample(rng)
            assert p.family in ("firefox", "chrome", "safari")
            assert "Mozilla/5.0" in p.user_agent
            if p.family == "firefox":
                assert "Firefox/" in p.user_agent
                assert "sec-ch-ua" not in p.headers  # firefox never sends client hints
            if p.family == "chrome":
                assert "Chrome/" in p.user_agent
                assert "sec-ch-ua" in p.headers

    def test_api_headers_shape(self):
        p = personas.sample(random.Random(1))
        h = p.api_headers("de-DE", referer="https://preowned.ferrari.com/de-DE")
        assert h["Accept"] == "application/json, text/plain, */*"
        assert h["Content-Type"] == "application/json"
        assert h["Origin"] == "https://preowned.ferrari.com"
        assert h["Referer"].startswith("https://preowned.ferrari.com")
        assert "de" in h["Accept-Language"]

    def test_page_headers_shape(self):
        p = personas.sample(random.Random(2))
        h = p.page_headers("en-US", referer=None)
        assert "text/html" in h["Accept"]
        assert h["Accept-Language"].startswith("en")


# ---------------------------------------------------------------------------
# Transport pacing & safety
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, text="", headers=None, url="https://x"):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.content = text.encode()

    def json(self):
        import json
        return json.loads(self.text)


def _fast_config():
    return TransportConfig(min_interval=0.0, jitter=0.0, dwell_every=(10**9, 10**9),
                           dwell_seconds=(0, 0), timeout=5, request_budget=100)


class TestTransport:
    def test_pacing_enforced(self):
        cfg = TransportConfig(min_interval=0.05, jitter=0.0, dwell_every=(10**9, 10**9),
                              dwell_seconds=(0, 0))
        t = Transport(config=cfg, rng=random.Random(0))
        t._last_request_at = time.monotonic()
        with patch.object(t._session, "get", return_value=_Resp(200, "ok")):
            start = time.monotonic()
            t.get("https://x", locale="de-DE")
            elapsed = time.monotonic() - start
            assert elapsed >= 0.04  # pacing slept

    def test_budget_exceeded(self):
        cfg = TransportConfig(min_interval=0, jitter=0, dwell_every=(10**9, 10**9),
                              dwell_seconds=(0, 0), request_budget=2)
        t = Transport(config=cfg, rng=random.Random(0))
        with patch.object(t._session, "get", return_value=_Resp(200, "ok")):
            t.get("https://x", locale="de-DE")
            t.get("https://x", locale="de-DE")
            with pytest.raises(BudgetExceededError):
                t.get("https://x", locale="de-DE")
        assert t.requests_made == 2

    def test_throttle_202_raises(self):
        t = Transport(config=_fast_config(), rng=random.Random(0))
        with patch.object(t._session, "get", return_value=_Resp(202, "")):
            with pytest.raises(ThrottledError):
                t.get("https://x", locale="de-DE")

    def test_block_403_raises(self):
        t = Transport(config=_fast_config(), rng=random.Random(0))
        with patch.object(t._session, "get", return_value=_Resp(403, "blocked")):
            with pytest.raises(BlockedError):
                t.get("https://x", locale="de-DE")

    def test_5xx_raises_server_error(self):
        t = Transport(config=_fast_config(), rng=random.Random(0))
        with patch.object(t._session, "get", return_value=_Resp(503, "oops")):
            with pytest.raises(Exception):
                t.get("https://x", locale="de-DE")

    def test_ledger_records_every_request(self):
        t = Transport(config=_fast_config(), rng=random.Random(0))
        with patch.object(t._session, "get", return_value=_Resp(200, "hello")):
            t.get("https://x", locale="de-DE")
        assert len(t.ledger) == 1
        e = t.ledger[0]
        assert e.status == 200 and e.bytes == 5 and e.method == "GET"

    def test_dwell_fires_eventually(self):
        fired = []
        cfg = TransportConfig(min_interval=0, jitter=0, dwell_every=(1, 1),
                              dwell_seconds=(0.01, 0.01))
        t = Transport(config=cfg, rng=random.Random(0), on_dwell=fired.append)
        with patch.object(t._session, "get", return_value=_Resp(200, "ok")):
            t.get("https://x", locale="de-DE")
            t.get("https://x", locale="de-DE")
        assert fired  # dwell callback invoked after first request


# ---------------------------------------------------------------------------
# Proxy pool
# ---------------------------------------------------------------------------

class TestProxyPool:
    def test_load_file_crlf(self, tmp_path):
        f = tmp_path / "px.txt"
        f.write_bytes(b"http://1.2.3.4:8080\r\n# comment\r\nhttp://5.6.7.8:3128\r\n")
        assert load_proxies_file(f) == ["http://1.2.3.4:8080", "http://5.6.7.8:3128"]

    def test_pool_dedupes_and_normalizes(self):
        pool = ProxyPool(["1.2.3.4:8080", "http://1.2.3.4:8080", "http://9.9.9.9:80"])
        assert pool.size == 2
        urls = [u for u, _ in pool._entries]
        assert "http://1.2.3.4:8080" in urls

    def test_sticky_routing(self):
        pool = ProxyPool([f"http://10.0.0.{i}:80" for i in range(5)], rng=random.Random(7))
        first = pool.pick()
        picks = [pool.pick() for _ in range(10)]
        assert all(p == first for p in picks)

    def test_failure_releases_sticky_and_cooldowns(self):
        pool = ProxyPool(["http://10.0.0.1:80", "http://10.0.0.2:80"], rng=random.Random(7))
        a = pool.pick()
        pool.release(a, ok=False)
        b = pool.pick()
        assert b != a  # sticky released
        # the failed one is cooling down now
        stats = {s["proxy"]: s for s in pool.stats()}
        assert stats[a]["cooldown_remaining"] > 0

    def test_dead_after_threshold(self):
        pool = ProxyPool(["http://10.0.0.1:80", "http://10.0.0.2:80"], rng=random.Random(7))
        a = pool.pick()
        for _ in range(4):
            pool.release(a, ok=False)
        stats = {s["proxy"]: s for s in pool.stats()}
        assert stats[a]["dead"] is True
        assert pool.pick() != a

    def test_rotate_picks_different(self):
        pool = ProxyPool([f"http://10.0.0.{i}:80" for i in range(5)], rng=random.Random(3))
        a = pool.pick()
        b = pool.rotate()
        assert b != a or pool.size == 1

    def test_no_working_proxy(self):
        pool = ProxyPool(["http://10.0.0.1:80"])
        pool._entries[0][1].dead = True
        assert pool.pick() is None

    def test_precheck_skips_unreachable(self):
        # port 9 (discard) on localhost: connection refused instantly
        pool = ProxyPool(["http://127.0.0.1:9"], rng=random.Random(0))
        working = pool.precheck(timeout=0.5)
        assert working == []
        assert pool.stats()[0]["failures"] >= 1


# ---------------------------------------------------------------------------
# Client wiring (mocked transport responses)
# ---------------------------------------------------------------------------

class TestClientWiring:
    def _client(self):
        from ferrari_preowned_client import FerrariPreownedClient
        cfg = TransportConfig(min_interval=0, jitter=0, dwell_every=(10**9, 10**9),
                              dwell_seconds=(0, 0), request_budget=50)
        return FerrariPreownedClient(locale="de-DE", seed=1, config=cfg)

    def test_countries(self, dk_page0):
        import json
        c = self._client()
        with patch.object(c.transport._session, "post",
                          return_value=_Resp(200, json.dumps(dk_page0), url="test")):
            countries = c.countries()
        assert len(countries) == 61
        c.close()

    def test_search_decrypts_vins(self, dk_page0):
        import json
        c = self._client()
        with patch.object(c.transport._session, "post",
                          return_value=_Resp(200, json.dumps(dk_page0), url="test")):
            c.countries()
            listings, pag = c.search("DK")
        assert any(l.vin == "ZFFBT55B000128196" for l in listings)
        c.close()

    def test_strict_decryption_raises_on_rotation(self, dk_page0):
        import json
        from ferrari_preowned_client import FerrariPreownedClient, KeystreamError
        cfg = TransportConfig(min_interval=0, jitter=0, dwell_every=(10**9, 10**9),
                              dwell_seconds=(0, 0), request_budget=50)
        c = FerrariPreownedClient(locale="de-DE", seed=1, config=cfg,
                                  strict_decryption=True)
        c._keystream = bytes(b ^ 0x5A for b in contract.KEYSTREAM_V1)  # simulated rotation
        with patch.object(c.transport._session, "post",
                          return_value=_Resp(200, json.dumps(dk_page0), url="test")):
            with pytest.raises(KeystreamError):
                c.search("DK")
        c.close()
