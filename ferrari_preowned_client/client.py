"""High-level client facade for the Ferrari Pre-Owned site.

Typical use::

    from ferrari_preowned_client import FerrariPreownedClient

    client = FerrariPreownedClient(locale="de-DE")
    countries = client.countries()               # 61 options w/ counts
    for listing in client.country_listings("DK"):
        print(listing.vin, listing.car_name, listing.price)
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Iterator, Optional

import requests

from . import contract, decrypt, parsing
from .errors import (
    BlockedError, ContractChangedError, FerrariPreownedError, KeystreamError,
    ThrottledError,
)
from .models import Country, Gallery, Listing, ListingDetail
from .personas import Persona, sample as sample_persona
from .proxies import ProxyPool
from .transport import Transport, TransportConfig
from .snapshot import SnapshotResult, run_snapshot


class FerrariPreownedClient:
    """Facade over the verified Ferrari Pre-Owned contract.

    Parameters
    ----------
    locale:
        Site locale (``de-DE`` default). Oracle locales (``en-US`` etc.)
        activate plaintext-VIN responses for US/CA cars.
    persona:
        Pre-built browser persona; a random coherent one is drawn when
        omitted (sticky for the lifetime of the client).
    pool:
        Optional :class:`ProxyPool` for proxied runs.
    decrypt_vins:
        Decrypt VIN blobs with the verified keystream (default). When
        ``False`` (or after a rotation), blobs stay opaque identifiers.
    strict_decryption:
        Raise :class:`KeystreamError` on VIN blobs that fail decryption
        instead of continuing with ``vin=None`` (fail-closed vs. degrade).
    config:
        Transport tuning (pacing, budget, timeouts).
    seed:
        RNG seed for reproducible persona/pacing in tests.
    """

    def __init__(self, *, locale: str = "de-DE",
                 persona: Optional[Persona] = None,
                 pool: Optional[ProxyPool] = None,
                 decrypt_vins: bool = True,
                 strict_decryption: bool = False,
                 config: Optional[TransportConfig] = None,
                 seed: Optional[int] = None):
        if locale not in contract.SUPPORTED_LOCALES:
            raise ValueError(
                f"unsupported locale {locale!r}; supported: {contract.SUPPORTED_LOCALES}"
            )
        self.locale = locale
        self.rng = random.Random(seed)
        self.persona = persona or sample_persona(self.rng)
        self.decrypt_vins = decrypt_vins
        self.strict_decryption = strict_decryption
        self.pool = pool
        self.transport = Transport(
            config=config, persona=self.persona, pool=pool, rng=self.rng)
        self._keystream = contract.KEYSTREAM_V1
        self._countries_cache: Optional[list[Country]] = None
        self._model_catalog: dict[str, str] = {}
        self._dealer_catalog: dict[str, str] = {}

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "FerrariPreownedClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ endpoints

    def countries(self, *, refresh: bool = False) -> list[Country]:
        """All 61 country options with region and SEO slugs."""
        if self._countries_cache is not None and not refresh:
            return self._countries_cache
        url = contract.BASE_URL + contract.SEARCH_ENDPOINT
        payload = {
            "locale": self.locale, "page": 0, "limit": 0,
            "ads": [], "filters": ["countries"],
        }
        resp = self.transport.post_json(url, payload, locale=self.locale, referer=self._home_referer())
        data = resp.json()
        countries = parsing.parse_countries(data, source_url=resp.url)
        self._countries_cache = countries
        return countries

    def search(self, country: str, *, page: int = 0,
               limit: int = contract.MAX_LIMIT,
               region: Optional[str] = None,
               currency: str = contract.DEFAULT_CURRENCY,
               unit_length: str = contract.DEFAULT_UNIT_LENGTH) -> tuple[list[Listing], tuple[int, int, int]]:
        """One search page for a country. Returns (listings, pagination)."""
        region = region or self._region_for(country)
        url = contract.BASE_URL + contract.SEARCH_ENDPOINT
        payload = {
            "regions": region, "countries": [country],
            "pageLimit": [str(page)], "currency": currency,
            "unitLength": unit_length, "locale": self.locale,
            "page": str(page), "limit": int(limit), "isOffbrand": False,
        }
        referer = self._listing_referer(region, country)
        resp = self.transport.post_json(url, payload, locale=self.locale, referer=referer)
        data = resp.json()
        listings = parsing.parse_listings(
            data, keystream=self._keystream, decrypt_vins=self.decrypt_vins,
            source_url=resp.url, raw_body=resp.text)
        self._absorb_model_catalog(data)
        self._absorb_dealer_catalog(data)
        self._check_decryption_health(listings)
        return listings, parsing.parse_pagination(data)

    def _absorb_model_catalog(self, data: dict) -> None:
        """Learn the model catalog (name/slug -> slugSeo) from any search.

        Detail URLs use the CATALOG slugSeo (e.g. 'ferrari-purosangue'),
        not the slugified carName (which yields 'purosangue' and 404s).
        The catalog rides along in every country-scoped search response.
        """
        opts = ((data.get("results") or {}).get("filters") or {}).get("models", {}).get("options")
        if not opts:
            return
        for o in opts:
            if isinstance(o, dict) and o.get("slugSeo"):
                key = (o.get("slug") or "").upper() or _slugify(o.get("name") or "")
                self._model_catalog[key] = o["slugSeo"]
                # also index by name for ads whose carName differs (e.g.
                # 'Purosangue' vs catalog 'Ferrari Purosangue')
                nm = _slugify(o.get("name") or "")
                if nm:
                    self._model_catalog.setdefault(nm, o["slugSeo"])

    def _absorb_dealer_catalog(self, data: dict) -> None:
        """Learn the dealer catalog (code/slug/name -> slugSeo) from any
        country-scoped search response.

        Detail URLs use the CATALOG dealer slugSeo, which can differ from
        our slugified dealer name in both directions — e.g. the BE dealer
        'Francorchamps Motors Brussels S.A.' -> 'francorchamps-motors-
        brussels-s-a-' (trailing dash our slugify strips). Verified against
        the 305 detail-404s of the 2026-09-25 global run (19 countries).
        """
        opts = ((data.get("results") or {}).get("filters") or {}).get(
            "dealers", {}).get("options")
        if not opts:
            return
        for o in opts:
            if isinstance(o, dict) and o.get("slugSeo"):
                # primary key: dealer code when the ad carries one
                code = (o.get("slug") or "").strip()
                if code:
                    self._dealer_catalog[code] = o["slugSeo"]
                nm = _slugify(o.get("name") or "")
                if nm:
                    self._dealer_catalog.setdefault(nm, o["slugSeo"])

    def country_listings(self, country: str, *, limit: int = contract.MAX_LIMIT,
                         region: Optional[str] = None,
                         max_pages: Optional[int] = None) -> Iterator[Listing]:
        """Iterate all listings for a country, paging automatically.

        Stops when a page comes back empty or the advertised total is
        reached; pagination metadata is validated against the ad count.
        """
        listings, pag = self.search(country, page=0, limit=limit, region=region)
        total = pag[0]
        yield from listings
        pages = pag[1]
        if max_pages is not None:
            pages = min(pages, max_pages)
        seen = {l.ad_id_blob for l in listings}
        page = 1
        while page < pages:
            listings, pag2 = self.search(country, page=page, limit=limit, region=region)
            if not listings:
                break
            new = [l for l in listings if l.ad_id_blob not in seen]
            for l in new:
                seen.add(l.ad_id_blob)
                yield l
            if total and len(seen) >= total:
                break
            page += 1

    def detail(self, listing: Listing) -> ListingDetail:
        """Fetch and parse the detail page SSR for a listing.

        Uses the constructed URL pattern (verified): SEO segments +
        encrypted id-blob tail. The model segment is the slugified
        ``carName`` (e.g. "575m-maranello"), NOT the internal model code.
        Page GETs are CloudFront-cached and the stealthiest request type.
        """
        if listing.country_slug is None:
            raise FerrariPreownedError("listing lacks country for URL build")
        url = parsing.build_detail_url(
            locale=self.locale,
            region_slug=listing.region_slug or "",
            country_slug_seo=self._country_seo(listing.country_slug),
            dealer_slug=self._dealer_slug(listing),
            model_slug=self._model_slug(listing),
            id_blob=listing.ad_id_blob,
        )
        referer = self._listing_referer(listing.region_slug or "", listing.country_slug)
        resp = self.transport.get(url, locale=self.locale, referer=referer)
        if resp.status_code == 404:
            raise FerrariPreownedError(
                f"detail page gone (404): {url}",
                evidence={"ad_id_blob": listing.ad_id_blob[:30]},
            )
        return parsing.parse_detail_page(
            resp.text, keystream=self._keystream, decrypt_vins=self.decrypt_vins,
            source_url=url)

    def gallery(self, listing: Listing | ListingDetail) -> Gallery:
        """Fetch the full THRON gallery for a (EU) listing.

        Prefers the detail-page hero gallery id; falls back to the card
        gallery id (which yields only 4 images).
        """
        gid = None
        if isinstance(listing, ListingDetail):
            gid = listing.images.thron_hero_gallery_id
        if not gid:
            gid = listing.images.thron_card_gallery_id
        if not gid:
            raise FerrariPreownedError(
                "listing has no THRON gallery id (US homenet listings need none)",
                evidence={"car": listing.car_name},
            )
        url = contract.BASE_URL + contract.PLAYLIST_ENDPOINT
        resp = self.transport.get(
            url, locale=self.locale, referer=self._home_referer(),
            params={"xcontentId": gid})
        data = resp.json()
        if not isinstance(data, list):
            raise ContractChangedError("playlist response is no longer a list")
        return parsing.parse_gallery(data, gallery_id=gid, source_url=resp.url)

    def refresh_keystream(self) -> bytes:
        """Re-derive the VIN keystream from the en-US oracle (self-heal).

        Makes one search call in an oracle locale (en-US) for an oracle
        country (US). Plaintext VINs in that response, paired with the
        same cars' id blobs, yield the keystream via XOR. Requires >= 1
        US/CA car to be listed (always true in practice).
        """
        url = contract.BASE_URL + contract.SEARCH_ENDPOINT
        payload = {
            "regions": "NA", "countries": ["US"], "pageLimit": ["0"],
            "currency": "USD", "unitLength": "mi", "locale": "en-US",
            "page": "0", "limit": 11, "isOffbrand": False,
        }
        resp = self.transport.post_json(
            url, payload, locale="en-US",
            referer=contract.BASE_URL + "/en-US/r/north-america/ferrari-used/usa/rfc")
        data = resp.json()
        ads = (data.get("results") or {}).get("ads") or []
        pairs = []
        for ad in ads:
            vin, idb = ad.get("vin"), ad.get("id")
            if isinstance(vin, str) and not decrypt.is_encrypted(vin) and decrypt.is_encrypted(idb):
                if len(vin) == 17:
                    pairs.append((vin, idb))
        if not pairs:
            raise KeystreamError(
                "oracle call yielded no plaintext/id pairs — oracle itself changed",
                evidence={"ads": len(ads)},
            )
        ks, agreement = decrypt.derive_keystream_majority(pairs)
        if agreement < 0.8:
            # Degrade loudly rather than accept a shaky keystream.
            raise KeystreamError(
                f"oracle agreement only {agreement:.0%} — keystream not stable enough to trust",
                evidence={"pairs": len(pairs)},
            )
        self._keystream = ks
        return ks

    def snapshot(self, *, countries: Optional[Iterable[str]] = None,
                 limit: int = contract.MAX_LIMIT,
                 include_details: bool = False,
                 include_galleries: bool = False,
                 checkpoint_path: Optional[str] = None,
                 progress=None) -> "SnapshotResult":
        """Full snapshot run (see snapshot.run_snapshot)."""
        return run_snapshot(
            self, countries=countries, limit=limit,
            include_details=include_details, include_galleries=include_galleries,
            checkpoint_path=checkpoint_path, progress=progress)

    # ------------------------------------------------------------- internals

    def _home_referer(self) -> str:
        return f"{contract.BASE_URL}/{self.locale}"

    def _listing_referer(self, region_slug: str, country_slug: str) -> str:
        region = contract.REGION_SEO.get(region_slug, (region_slug or "europa").lower())
        fixed = contract.FIXED_STRINGS.get(self.locale, contract.DEFAULT_FIXED_STRING)
        cseo = self._country_seo(country_slug)
        return (f"{contract.BASE_URL}/{self.locale}/r/{region}/{fixed}/{cseo}/rfc")

    def _country_seo(self, country_slug: str) -> str:
        """Country segment for /r/ and /a/ URLs.

        Prefers the country catalog's slugSeo (from countries()); falls
        back to slugify-with-dashes (Ferrari drops umlauts to '-'):
        'Dänemark' -> 'd-nemark', not 'danemark'. Ground truth:
        sitemap-listing.xml.
        """
        if self._countries_cache:
            for c in self._countries_cache:
                if c.slug == country_slug:
                    return c.slug_seo or _slugify_dash(c.name or c.slug)
        return _slugify_dash(country_slug)

    def _dealer_slug(self, listing: Listing) -> str:
        """Detail URLs use the dealer catalog's slugSeo when known.

        Catalog keys: dealer code (preferred) or slugified name. Falls
        back to slugify-with-dashes. Ferrari's slugs drop umlauts to '-'
        ('Gohm Böblingen' -> 'gohm-b-blingen') and keep trailing dashes
        ('...S.A.' -> '...-s-a-').
        """
        d = listing.dealer
        c = self._dealer_catalog
        # dealer code rides in the raw ad as 'dealerCode'
        code = (listing.raw or {}).get("dealerCode")
        if code and str(code) in c:
            return c[str(code)]
        name = (d.name if d else None) or "ferrari-dealer"
        k = _slugify_dash(name)
        if k in c:
            return c[k]
        return k or "ferrari-dealer"

    def _model_slug(self, listing: Listing) -> str:
        """Detail URLs use the model catalog's slugSeo (e.g. 'ferrari-purosangue').

        Resolution order (verified against real DK URLs):
        1. catalog by model slug code (F175 -> ferrari-purosangue)
        2. catalog by slugified model name
        3. catalog by slugified carName
        4. fallback: slugified carName (works for e.g. '575M Maranello')
        """
        c = self._model_catalog
        if listing.car_id and listing.car_id.upper() in c:
            return c[listing.car_id.upper()]
        for k in (_slugify(listing.model_name or ""),
                  _slugify(listing.car_name or ""),
                  _slugify_dash(listing.model_name or ""),
                  _slugify_dash(listing.car_name or "")):
            if k and k in c:
                return c[k]
        if listing.car_name:
            k = _slugify(listing.car_name)
            # 'Purosangue' ads still match catalog 'Ferrari Purosangue'
            for cat_key, seo in c.items():
                if cat_key and (k.endswith(cat_key) or cat_key.endswith(k)):
                    return seo
        return _slugify(listing.car_name or listing.model_name or "ferrari")

    def counts(self) -> dict[str, int]:
        """Per-country listing counts via one all-countries probe call.

        The bare filters call returns counts of 0 (counts only populate in
        country-scoped responses), so a single search call listing every
        country collects them all in one request.
        """
        codes = [c.slug for c in self.countries()]
        url = contract.BASE_URL + contract.SEARCH_ENDPOINT
        payload = {
            "regions": "", "countries": codes, "pageLimit": ["0"],
            "currency": contract.DEFAULT_CURRENCY,
            "unitLength": contract.DEFAULT_UNIT_LENGTH,
            "locale": self.locale, "page": "0", "limit": 1,
            "isOffbrand": False,
        }
        resp = self.transport.post_json(
            url, payload, locale=self.locale, referer=self._home_referer())
        data = resp.json()
        out: dict[str, int] = {}
        for o in ((data.get("results") or {}).get("filters", {})
                  .get("countries", {}).get("options", [])):
            if isinstance(o, dict) and o.get("slug"):
                out[o["slug"]] = int(o.get("count") or 0)
        return out

    def _region_for(self, country: str) -> str:
        if self._countries_cache:
            for c in self._countries_cache:
                if c.slug == country:
                    return c.region or "EU"
        return "EU"

    def _check_decryption_health(self, listings: list[Listing]) -> None:
        if not self.decrypt_vins or not listings:
            return
        blobs = [l for l in listings if l.vin_blob and decrypt.is_encrypted(l.vin_blob)]
        failures = [l for l in blobs if l.vin is None and l.legacy_chassis is None]
        if blobs and failures and self.strict_decryption:
            raise KeystreamError(
                f"{len(failures)}/{len(blobs)} VIN blobs failed decryption",
                sample=failures[0].vin_blob[:40] if failures[0].vin_blob else None,
            )


def _slugify(name: str) -> str:
    """Slugify like the site: lowercase, non-alnum -> '-', collapsed.

    NOTE: this ASCII-transliterates umlauts ('ä' -> 'a') because
    ``str.isalnum()`` accepts them. The site's SEO slugs instead DROP
    umlauts to a dash ('Dänemark' -> 'd-nemark') — use :func:`_slugify_dash`
    for URL segments.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in (name or "").lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "ferrari"


def _slugify_dash(name: str) -> str:
    """Slugify the way Ferrari's SEO URLs do (sitemap ground truth):

    * strip diacritics' base char entirely — 'ä' becomes '-' ('Dänemark'
      -> 'd-nemark', 'Böblingen' -> 'b-blingen', 'Österreich' -> '-sterreich')
    * lowercase, non-alnum -> '-', collapse repeats
    * KEEP trailing dashes ('S.A.' -> 's-a-', 'V.A.E.' -> 'v-a-e-')
      and leading dashes ('Österreich' -> '-sterreich') — the site does.
    """
    s = (name or "").lower()
    out = []
    for ch in s:
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif ch in "äöüßàáâèéêìíîòóôùúûñçå":
            out.append("-")          # umlaut/diacritic -> dash
        else:
            out.append("-")          # space, punctuation
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


__all__ = ["FerrariPreownedClient", "SnapshotResult"]
