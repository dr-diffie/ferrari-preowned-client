"""Decryption of Ferrari Pre-Owned VIN/id blobs.

Verified during the 2026-09-24 reverse-engineering session:

* ``vin``, ``id`` and the detail-URL tail are URL-encoded base64 blobs with
  the byte layout ``[16B constant header][32B per-listing nonce][payload]``
  where the payload starts at offset 48.
* The payload is XOR-encrypted with a **static, position-only keystream**
  — the 32-byte nonce has no influence on the key material.
* Keystream (17 bytes, covers a full modern VIN):

      e2 c0 55 2c 16 85 b2 54 74 88 71 cc 87 c5 c0 3a 2f

* ``vin`` payload: 17-char VIN (modern cars) or a 7-char legacy chassis
  number (pre-2000 cars without full VINs in the system).
* ``id`` payload: ``VIN + "-" + 13-digit epoch millis``. The VIN part
  decrypts with the same keystream; the epoch suffix does NOT (it appears
  to use additional per-listing key material) — use ``publishDate`` from
  the detail page instead.

Self-heal: if Ferrari rotates the keystream, it can be re-derived from a
single "oracle" search call — the site serves **plaintext VINs** for
US/CA-country listings when the locale is en-US / fr-CA / en-CA (this is
the frontend's own ``canDisplayVin`` logic, recovered from its JS bundles).
Pair the plaintext VIN with the same car's ``id`` blob and XOR:
``KS = id_blob[48:65] xor plaintext_vin``.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Optional

from . import contract
from .errors import KeystreamError

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{7,17}$")
#: Legacy chassis formats: bare numbers ("02528") and classic Ferrari
#: chassis with type suffix ("4507 GT", "4859 GT").
_LEGACY_RE = re.compile(r"^[0-9]{3,8}$|^[0-9]{3,5}\s*[A-Z]{1,3}$")


def is_encrypted(value: object) -> bool:
    """True if ``value`` looks like an encrypted blob."""
    return isinstance(value, str) and value.startswith(contract.BLOB_MARKER)


def decode_blob(value: str) -> bytes:
    """Decode a blob (URL-encoded base64) to raw bytes."""
    s = urllib.parse.unquote(value)
    s = s + "=" * ((-len(s)) % 4)
    try:
        return base64.b64decode(s, validate=False)
    except Exception as exc:  # pragma: no cover - defensive
        raise KeystreamError(f"cannot base64-decode blob: {exc}", sample=value[:40]) from exc


def _check_structure(raw: bytes, value: str) -> None:
    if len(raw) < contract.BLOB_PAYLOAD_OFFSET + 1:
        raise KeystreamError("blob too short", sample=value[:40])
    if raw[:16] != contract.BLOB_HEADER:
        raise KeystreamError(
            "blob header changed — site encryption format was updated",
            sample=value[:40],
        )


def decrypt_vin(value: str, keystream: bytes = contract.KEYSTREAM_V1) -> str:
    """Decrypt a ``vin`` blob to the plaintext VIN (or legacy chassis).

    Raises :class:`KeystreamError` when the blob's structure or decrypted
    payload doesn't match the verified contract — never returns a guess.
    """
    raw = decode_blob(value)
    _check_structure(raw, value)
    payload = _xor(raw[contract.BLOB_PAYLOAD_OFFSET:], keystream)
    text = payload.decode("ascii", errors="strict") if _is_ascii(payload) else ""
    if _VIN_RE.match(text or "") or _LEGACY_RE.match(text or ""):
        return text
    raise KeystreamError(
        "decryption produced a non-VIN payload — keystream likely rotated",
        sample=value[:40],
    )


def decrypt_id_vin(value: str, keystream: bytes = contract.KEYSTREAM_V1) -> str:
    """Decrypt just the VIN portion of an ``id`` blob.

    The id payload is ``VIN-<epoch13>``; only the first 17 bytes are needed
    here. The epoch suffix is NOT decrypted (unstable keystream region).
    """
    raw = decode_blob(value)
    _check_structure(raw, value)
    n = len(raw) - contract.BLOB_PAYLOAD_OFFSET
    if n < 17:
        # Legacy chassis ids are shorter; decrypt what's there.
        payload = _xor(raw[contract.BLOB_PAYLOAD_OFFSET:], keystream)
        text = payload.decode("ascii", "replace")
        if _LEGACY_RE.match(text):
            return text
        raise KeystreamError("short id blob does not decrypt to a chassis", sample=value[:40])
    payload = _xor(raw[contract.BLOB_PAYLOAD_OFFSET:contract.BLOB_PAYLOAD_OFFSET + 17], keystream)
    text = payload.decode("ascii", "replace")
    if _VIN_RE.match(text) or _LEGACY_RE.match(text):
        return text
    raise KeystreamError("id blob VIN part failed validation", sample=value[:40])


def derive_keystream(plaintext_vin: str, id_blob: str) -> bytes:
    """Derive the 17-byte keystream from an oracle pair.

    ``plaintext_vin`` must come from an en-US/fr-CA/en-CA locale response
    for a US/CA car, and ``id_blob`` must be the SAME car's ``id`` value
    from the SAME response (the id's epoch differs between captures, so
    pairs must be captured together).
    """
    raw = decode_blob(id_blob)
    _check_structure(raw, id_blob)
    n = len(raw) - contract.BLOB_PAYLOAD_OFFSET
    if n < 17:
        raise KeystreamError("id blob too short for keystream derivation")
    pv = plaintext_vin.encode("ascii")
    if len(pv) < 17:
        raise KeystreamError("oracle VIN too short (legacy chassis?)")
    ks = bytes(
        raw[contract.BLOB_PAYLOAD_OFFSET + j] ^ pv[j]
        for j in range(17)
    )
    return ks


def verify_keystream(keystream: bytes, pairs: list[tuple[str, str]],
                     *, min_agreement: float = 0.8) -> bool:
    """Check a keystream against (plaintext_vin, id_blob) oracle pairs.

    Uses majority agreement: real-world oracle data can contain a small
    number of inconsistent pairs (observed once: a car whose id-blob
    embedded an outdated VIN after a dealer correction). A keystream is
    accepted when >= ``min_agreement`` of pairs derive it exactly.
    """
    if not pairs:
        return False
    good = 0
    for vin, blob in pairs:
        try:
            if derive_keystream(vin, blob) == keystream[:17]:
                good += 1
        except KeystreamError:
            pass
    return good / len(pairs) >= min_agreement


def classify_payload(text: str) -> Optional[str]:
    """Classify a decrypted payload: ``"vin"``, ``"legacy"``, or ``None``."""
    if _VIN_RE.match(text) and len(text) >= 11:
        return "vin"
    if _LEGACY_RE.match(text):
        return "legacy"
    return None


def derive_keystream_majority(pairs: list[tuple[str, str]]) -> tuple[bytes, float]:
    """Derive the keystream by majority vote over oracle pairs.

    Returns ``(keystream, agreement_ratio)``. Raises when no candidate
    reaches a majority — that means the oracle itself changed shape.
    """
    from collections import Counter
    streams: Counter[bytes] = Counter()
    for vin, blob in pairs:
        try:
            streams[derive_keystream(vin, blob)] += 1
        except KeystreamError:
            continue
    if not streams:
        raise KeystreamError("no oracle pair yielded a keystream")
    ks, votes = streams.most_common(1)[0]
    agreement = votes / len(pairs)
    if agreement < 0.5:
        raise KeystreamError(
            f"oracle pairs disagree: best candidate has {votes}/{len(pairs)}",
        )
    return ks, agreement


def _xor(data: bytes, keystream: bytes) -> bytes:
    return bytes(b ^ keystream[i % len(keystream)] for i, b in enumerate(data))


def _is_ascii(data: bytes) -> bool:
    return all(32 <= b < 127 for b in data)
