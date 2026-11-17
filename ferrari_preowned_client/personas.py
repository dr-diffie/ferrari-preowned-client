"""Browser personas: coherent, realistic header sets for stealth.

A *persona* is a complete browser identity — User-Agent plus every header a
real browser would send, with values that are internally consistent (an
Safari UA never sends ``Sec-Fetch-Site`` Chrome spellings, a Firefox UA
never sends ``sec-ch-ua`` client hints, etc.).

Why personas instead of random-UA-per-request: the WAF sees a cookieless
session as a sequence of requests. If request N has a Firefox UA and
request N+1 a Chrome UA *from the same IP within seconds*, that is itself a
strong bot signal. The library therefore:

* picks ONE persona per run (sticky) by default;
* keeps it consistent across every request of the run;
* rotates only between runs or when explicitly asked.

A second noise axis is *referer realism*: API calls carry the referer of a
real page the "user" would have been on, and detail-page GETs are preceded
by an (optional) referer that looks like natural browsing. We also send
``Upgrade-Insecure-Requests`` and ``Sec-Fetch-*``/``Sec-Fetch-Mode`` style
headers where the real browsers do, plus a plausible ``Accept-Encoding``.

The personas below are templates keyed by (family, platform). ``sample()``
builds a fully-specified header set with randomized-but-coherent versions.
Version numbers are drawn from plausible ranges (not the bleeding edge, not
ancient) so the traffic blends with real-world telemetry.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# Plausible version pools (kept deliberately modest).
_FF_VERSIONS = [str(v) for v in range(128, 149)]
_CHROME_VERSIONS = [str(v) for v in range(120, 157)]
_SAFARI_VERSIONS = [str(v) for v in range(15, 19)]
_WIN_BUILD = ["10.0; Win64; x64", "10.0; Win32; x64", "11.0; Win64; x64"]
_LINUX_UCI = ["Ubuntu; Ubuntu 24.04 LTS", "X11; Ubuntu 22.04",
              "Fedora; Linux x86_64", "Debian; Linux x86_64"]

_FIREFOX_L10N = [
    ("de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "de-DE"),
    ("en-US,en;q=0.9,de;q=0.8", "en-US"),
    ("en-GB,en;q=0.9,en-US;q=0.8", "en-GB"),
    ("fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7", "fr-FR"),
    ("it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7", "it-IT"),
]

_CHROME_L10N = [
    ("de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "de-DE"),
    ("en-US,en;q=0.9", "en-US"),
    ("en-GB,en;q=0.9", "en-GB"),
]


@dataclass(frozen=True)
class Persona:
    """A complete, coherent browser identity."""

    family: str                    # "firefox" | "chrome" | "safari"
    user_agent: str
    headers: dict[str, str]        # baseline headers (no referer/origin)

    def api_headers(self, locale: str, referer: str) -> dict[str, str]:
        """Headers for a JSON API call made from ``referer``."""
        h = dict(self.headers)
        h["Accept"] = "application/json, text/plain, */*"
        h["Content-Type"] = "application/json"
        h["Origin"] = "https://preowned.ferrari.com"
        h["Referer"] = referer
        h["Accept-Language"] = _language_header_for_locale(locale)
        return h

    def page_headers(self, locale: str, referer: str | None = None) -> dict[str, str]:
        """Headers for an HTML page GET (the stealthiest request type)."""
        h = dict(self.headers)
        h["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        )
        h["Accept-Language"] = _language_header_for_locale(locale)
        if referer:
            h["Referer"] = referer
        return h


def _language_header_for_locale(locale: str) -> str:
    """Map a site locale to a realistic Accept-Language value."""
    m = {
        "de-DE": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "en-US": "en-US,en;q=0.9",
        "en-CA": "en-CA,en;q=0.9,en-US;q=0.8",
        "fr-CA": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
    }
    return m.get(locale, "en-US,en;q=0.9")


def _firefox_persona(rng: random.Random) -> Persona:
    ver = rng.choice(_FF_VERSIONS)
    plat = rng.choice(["X11; Linux x86_64", f"Windows {rng.choice(_WIN_BUILD)}",
                        "Macintosh; Intel Mac OS X 10.15"])
    ua = f"Mozilla/5.0 ({plat}; rv:{ver}.0) Gecko/20100101 Firefox/{ver}.0"
    headers = {
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }
    return Persona("firefox", ua, headers)


def _chrome_persona(rng: random.Random) -> Persona:
    ver = rng.choice(_CHROME_VERSIONS)
    plat = rng.choice(["X11; Linux x86_64", f"Windows {rng.choice(_WIN_BUILD)}",
                        "Macintosh; Intel Mac OS X 10_15_7"])
    ua = (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{ver}.0.0.0 Safari/537.36")
    ch_ua = f'"Chromium";v="{ver}", "Not_A Brand";v="24", "Google Chrome";v="{ver}"'
    headers = {
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"' if "Windows" in plat
                              else ('"macOS"' if "Macintosh" in plat else '"Linux"'),
        "Connection": "keep-alive",
    }
    return Persona("firefox" if False else "chrome", ua, headers)


def _safari_persona(rng: random.Random) -> Persona:
    ver = rng.choice(_SAFARI_VERSIONS)
    wk = 6000 + rng.randint(0, 200)
    ua = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          f"AppleWebKit/{wk}.1.15 (KHTML, like Gecko) Version/{ver}.0 Safari/60{rng.randint(0, 9)}.{wk}")
    headers = {
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
    return Persona("safari", ua, headers)


_BUILDERS = (lambda rng: _firefox_persona(rng),
             lambda rng: _chrome_persona(rng),
             lambda rng: _safari_persona(rng))

#: Weighted persona mix: Firefox-heavy because our verified baseline traffic
#: was Firefox-shaped and most Ferrari site visitors are European.
_MIX = ({"firefox": 45, "chrome": 45, "safari": 10})


def sample(rng: random.Random | None = None) -> Persona:
    """Draw a random coherent persona.

    ``rng`` may be supplied for reproducible test runs.
    """
    rng = rng or random.Random()
    families = list(_MIX.keys())
    weights = list(_MIX.values())
    family = rng.choices(families, weights=weights, k=1)[0]
    builder = {"firefox": _firefox_persona, "chrome": _chrome_persona,
               "safari": _safari_persona}[family]
    return builder(rng)


__all__ = ["Persona", "sample"]
