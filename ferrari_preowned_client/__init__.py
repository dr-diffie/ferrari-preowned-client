"""ferrari-preowned-client — stealthy, verified client for preowned.ferrari.com.

Reverse-engineered and verified against the live site on 2026-09-24:
search API, country filters, SSR detail pages, THRON galleries, and VIN
blob decryption with a self-healing keystream oracle.
"""

from . import contract, decrypt, parsing, personas, proxies
from .client import FerrariPreownedClient
from .engine import run_pipeline  # v1.1 engine: progress, errors, reports
from .errors import (
    BlockedError, BudgetExceededError, ContractChangedError,
    FerrariPreownedError, KeystreamError, NoWorkingProxyError, ProxyError,
    ServerError, ThrottledError, TransportError,
)
from .export import EXPORT_COLUMNS, export, export_csv, export_json, export_xlsx
from .images import ImageConfig, ImageDownloader
from .models import (
    Country, Dealer, Gallery, GalleryItem, ImageSet, Listing,
    ListingDetail, Provenance, SnapshotSummary,
)
from .personas import Persona
from .pipeline import Progress, run_full_snapshot
from .proxies import ProxyPool, load_proxies_file
from .smart import SmartStore, run_smart_snapshot
from .snapshot import SnapshotResult, run_snapshot
from .transport import LedgerEntry, Transport, TransportConfig

__version__ = contract.__version__
__all__ = [
    "FerrariPreownedClient", "Persona", "ProxyPool", "load_proxies_file",
    "Transport", "TransportConfig", "LedgerEntry",
    "SnapshotResult", "run_snapshot",
    "run_full_snapshot", "run_smart_snapshot", "run_pipeline", "Progress",
    "SmartStore", "RunProgress", "ErrorLog", "audit_images",
    "export", "export_json", "export_csv", "export_xlsx", "EXPORT_COLUMNS",
    "ImageDownloader", "ImageConfig",
    "Listing", "ListingDetail", "Country", "Dealer", "Gallery",
    "GalleryItem", "ImageSet", "Provenance", "SnapshotSummary",
    "FerrariPreownedError", "TransportError", "ThrottledError",
    "BlockedError", "ServerError", "ContractChangedError", "KeystreamError",
    "BudgetExceededError", "ProxyError", "NoWorkingProxyError",
    "contract", "decrypt", "parsing", "personas", "proxies",
]
