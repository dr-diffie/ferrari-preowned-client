# Future-Proofing Runbook

What to do when Ferrari changes the site. The library is built to make every
failure **typed, evidenced, and diagnosable** — this document turns that into
a procedure.

## How you'll notice a change

The library never guesses, so changes surface as typed errors:

| Signal | What likely changed | Where to look |
|---|---|---|
| `KeystreamError` on many VINs | encryption keystream rotated | §1 |
| `ContractChangedError` (countries/search) | search API payload/shape changed | §2 |
| `ContractChangedError` (detail/gallery) | SSR page structure changed | §3 |
| `BlockedError` (403) | WAF rules tightened | §4 |
| `ThrottledError` at safe pacing | throttle window shortened | §4 |
| 200-but-empty ads at `limit=44` | page-size cap changed | §2 |

Every error carries `.evidence`; the transport `ledger` shows the exact
request sequence that led there.

## §1 Keystream rotation — self-heals in one call

The VIN encryption is a static XOR keystream. If it rotates:

```python
client = FerrariPreownedClient()
client.refresh_keystream()   # one en-US/US search call re-derives it
```

`refresh_keystream` uses the site's own oracle behavior (plaintext VINs for
US/CA cars in en-US/fr-CA/en-CA locales — the frontend's `canDisplayVin`
logic) and majority-vote derivation (tolerates the occasional bad data row;
observed 43/44 agreement in the wild).

**If the oracle itself is gone** (no plaintext VINs in any locale/country
combination): fall back to `decrypt_vins=False`, keep blobs as opaque ids,
and use detail-page `chassis` + model/year as identity. Then re-reverse the
encryption: blobs are URL-encoded base64, `[16B header][32B nonce][payload]`;
capture a listing response, save the JS bundle that handles VINs
(`chunks/f8cce*.js` historically), and diff against the contract.

**After any change:** update `KEYSTREAM_V1`/`BLOB_HEADER` in
`contract.py`, bump `CONTRACT_VERSION`, add the new fixtures to
`tests/fixtures/`, extend `test_offline_contract.py`.

## §2 Search API contract drift

Symptom: `ContractChangedError` from `parse_countries`/`parse_listings`, or
200-with-empty-ads, or 405 on POST.

1. Open the site in a normal browser, DevTools → Network.
2. Load a listing page, click "Load more" — capture the `/api/v1/search`
   request (URL, headers, **exact JSON body**) and response.
3. Diff the body against `client.py`'s `search()` payload. Typical drift:
   new required field, renamed field, type change (`page` int vs string).
4. Diff the response against `tests/fixtures/dk_page0.json` (use
   `parsing.parse_listings` on the new capture to find the exact break).
5. Update `contract.py` + `parsing.py`, bump versions, add fixture.

**Page-size re-verification** (if `limit=44` returns empty): binary-search
`limit` in {11, 22, 44, 55} with `page=0` on a country with ≥55 ads, one
request each, 30 s apart. The largest value that returns ads is the new cap.
Never test `limit > total`.

## §3 SSR page structure drift

Symptom: `ContractChangedError` from `parse_detail_page` / `parse_next_data`
("no __NEXT_DATA__ script" = framework rewrite).

1. `GET` one known-good detail page (URL from a fresh listing).
2. Check `__NEXT_DATA__` exists; if not, the site left Next.js —
   re-reverse from the new framework's equivalent (look for a large JSON
   island with `"ads"`).
3. If it exists, walk `props.pageProps.initialProps.adDetailsData.ads[0]`
   and compare keys against `dk_detail_ad_full.json` (saved full fixture).
4. Update `parsing.parse_detail_page` field mappings; bump versions.

## §4 WAF / throttle behavior drift

- **403 on everything:** stop. Do not rotate identities (see stealth.md).
  Next day, try one page GET (cheapest request). If pages work but the API
  doesn't, the WAF now challenges POSTs — at that point the honest options
  are (a) slower pacing, (b) an authorized API, or (c) a `curl_cffi`
  transport variant matching a real browser TLS fingerprint (a deliberate,
  documented addition — not mid-run evasion).
- **202 at safe pacing:** raise `min_interval` (try 10–15 s), rerun a small
  country. Update `contract.DEFAULT_MIN_INTERVAL` if the new floor holds.

## §5 Maintenance schedule (proactive)

- **Weekly canary:** run `examples/snapshot_demo.py --countries DK` (29 ads,
  2 requests). Any typed error = drift caught early.
- **Monthly:** re-run the full offline test suite (fixtures are frozen
  snapshots; parser changes must keep them green).
- **On every Next.js buildId change** (visible in page HTML): re-capture
  detail + listing fixtures, diff ad keys, update parsers if needed.

## Evidence files to keep current

| Artifact | Where | Purpose |
|---|---|---|
| `tests/fixtures/*.json` | this repo | frozen responses; offline tests |
| `tests/fixtures/dk_detail.html` | this repo | full SSR page |
| `docs/internal/PREOWNED-API-RECON.md` | modena-maestro-v2 repo | the RE evidence + session ledger |
| `bundles/` (JS chunks) | session scratch | frontend source of truth for `canDisplayVin` etc. |

## The contract module is the single point of truth

Every verified constant lives in `ferrari_preowned_client/contract.py`.
A site change should touch that file (+ parsers + tests), nothing else.
`CONTRACT_VERSION` rides along in every record's provenance, so stored data
can always be mapped back to the contract that produced it.
