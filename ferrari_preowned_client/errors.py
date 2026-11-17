"""Exception hierarchy for ferrari-preowned-client.

Design rule: the library never guesses. Anything unexpected, blocked, or
malformed raises a typed error carrying the evidence needed to diagnose it.
"""

from __future__ import annotations

from typing import Any, Optional


class FerrariPreownedError(Exception):
    """Base class for all library errors."""

    def __init__(self, message: str, *, evidence: Optional[dict] = None):
        super().__init__(message)
        self.evidence: dict[str, Any] = evidence or {}


class TransportError(FerrariPreownedError):
    """Network-level failure (DNS, connect, read timeout, TLS)."""


class ThrottledError(FerrariPreownedError):
    """HTTP 202 with empty body — the API's rate-limit signal.

    The library stops immediately when this happens (stop-on-block); it
    never hammers through a throttle. ``retry_after`` carries the advised
    cooldown if the site provided one.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None,
                 evidence: Optional[dict] = None):
        super().__init__(message, evidence=evidence)
        self.retry_after = retry_after


class BlockedError(FerrariPreownedError):
    """HTTP 403/405 — WAF challenge or endpoint contract change.

    Never retried, never bypassed. The library refuses to rotate identities
    in response to a block (see docs/stealth.md for the rationale).
    """


class ServerError(FerrariPreownedError):
    """HTTP 5xx. Retried with backoff only when a Retry-After is absent."""


class ContractChangedError(FerrariPreownedError):
    """The site's response no longer matches the verified contract.

    Raised for structural drift (missing keys, changed shapes). See
    docs/future-proofing.md for the diagnosis runbook.
    """


class KeystreamError(FerrariPreownedError):
    """VIN decryption failed — the static keystream no longer applies.

    Use :meth:`FerrariPreownedClient.refresh_keystream` to re-derive it
    from the en-US oracle, or run with ``decrypt=False`` to keep blobs as
    opaque identifiers.
    """

    def __init__(self, message: str, *, sample: Optional[str] = None,
                 evidence: Optional[dict] = None):
        super().__init__(message, evidence=evidence)
        self.sample = sample


class BudgetExceededError(FerrariPreownedError):
    """The configured request budget was exhausted before finishing."""


class ProxyError(FerrariPreownedError):
    """A proxy from the pool failed (connect, read, or target block)."""


class NoWorkingProxyError(ProxyError):
    """Every proxy in the pool failed its pre-check."""
