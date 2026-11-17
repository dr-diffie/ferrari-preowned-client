"""Verified contract constants for the Ferrari Pre-Owned website.

Every value in this module was verified against the live site during the
2026-09-24 reverse-engineering session. If the site changes, this is the
single file that needs updating (see docs/future-proofing.md).
"""

from __future__ import annotations

import datetime

#: Package / contract version. Bump the contract part whenever a value in
#: this module changes so downstream provenance records can be migrated.
__version__ = "1.0.0"

CONTRACT_VERSION = "preowned-api-2026-09-24"

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

BASE_URL = "https://preowned.ferrari.com"
SEARCH_ENDPOINT = "/api/v1/search"
PLAYLIST_ENDPOINT = "/api/v1/thron/playlist-content"

#: Listing page URL template (SEO slugs are decorative for routing; the
#: server routes on the id-blob tail, but SEO segments must be present).
LISTING_URL_TEMPLATE = (
    "/{locale}/r/{region_slug}/{fixed_string}/{country_slug}/rfc"
)

#: Detail page URL template. The trailing segment is the encrypted id blob.
DETAIL_URL_TEMPLATE = (
    "/{locale}/a/{region_slug}/{fixed_string}/{country_slug}"
    "/{dealer_slug}/{model_slug}/{id_blob}"
)

#: Fixed path segment per locale ("ferrari-gebraucht" for de, ...).
FIXED_STRINGS = {
    "de-DE": "ferrari-gebraucht",
    "en-US": "ferrari-used",
    "fr-FR": "ferrari-occasion",
    "it-IT": "ferrari-usati",
    "en-GB": "ferrari-used",
    "es-ES": "ferrari-usados",
    "fr-CA": "ferrari-occasion",
    "en-CA": "ferrari-used",
}
DEFAULT_FIXED_STRING = "ferrari-used"

#: Region code -> SEO slug used in URLs. Ground truth: sitemap-listing.xml
#: (2026-09-26) — note MEA is 'naher-osten-afrika' and GC 'gro-china',
#: NOT what the German region names suggest.
REGION_SEO = {
    "EU": "europa",
    "NA": "nordamerika",
    "LA": "lateinamerika",
    "AP": "asien-pazifik",
    "MEA": "naher-osten-afrika",
    "GC": "gro-china",
}

# ---------------------------------------------------------------------------
# Search payload contract
# ---------------------------------------------------------------------------

#: Locales verified live (others exist on the site but are unverified).
SUPPORTED_LOCALES = ("de-DE", "en-US", "fr-CA", "en-CA")

#: Locales for which the frontend displays plaintext VINs for US/CA cars
#: (recovered from ``canDisplayVin`` in the site's JS bundles). Used as the
#: keystream self-heal oracle.
ORACLE_LOCALES = ("en-US", "fr-CA", "en-CA")
ORACLE_COUNTRIES = ("US", "CA")

#: Maximum page size that reliably returns ads. Verified: 11 (frontend
#: default) and 44 work; 55+ can 504; ``limit > total`` returns 200 with an
#: EMPTY ad list, so the effective cap is ``min(limit, total)``.
MAX_LIMIT = 44

#: Currencies/units observed in payloads.
DEFAULT_CURRENCY = "EUR"
DEFAULT_UNIT_LENGTH = "km"

# ---------------------------------------------------------------------------
# Pacing / safety
# ---------------------------------------------------------------------------

#: Minimum spacing between API calls (seconds). Rapid bursts (~10 calls in
#: under 60s) triggered an HTTP 202 empty-body throttle during testing.
DEFAULT_MIN_INTERVAL = 6.0

#: Extra random jitter added to every interval (seconds, uniform 0..jitter).
DEFAULT_JITTER = 4.0

#: Occasionally insert a longer "human dwell" pause (seconds).
DWELL_EVERY = (8, 14)      # every N requests (uniform inclusive range)
DWELL_SECONDS = (25.0, 90.0)

#: HTTP statuses that mean "back off or stop" (never retry these blindly).
THROTTLE_STATUS = 202      # empty body, rate limited
BLOCK_STATUS = (403, 405)  # WAF challenge / contract mismatch
TRANSIENT_STATUS = (500, 502, 503, 504)

#: Default per-run request budget.
DEFAULT_REQUEST_BUDGET = 500

#: CloudFront page cache TTL (max-age) — pages are cheap & cached.
PAGE_CACHE_TTL = 300

# ---------------------------------------------------------------------------
# Blob encryption contract (see decrypt.py)
# ---------------------------------------------------------------------------

#: Verified static XOR keystream (17 bytes) for VIN payloads.
KEYSTREAM_V1 = bytes.fromhex("e2c0552c1685b254748871cc87c5c03a2f")

#: Constant 16-byte blob header shared by every vin/id blob.
BLOB_HEADER = bytes.fromhex("2258e2c5b720567246b0ab107c31acae")

#: Payload offset inside the decoded blob.
BLOB_PAYLOAD_OFFSET = 48

#: Marker that identifies encrypted values (base64 prefix).
BLOB_MARKER = "Iljixbcg"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

UA_PRODUCT_TOKEN = f"ferrari-preowned-client/{__version__}"

RECORDED_AT = datetime.date(2026, 9, 24)
