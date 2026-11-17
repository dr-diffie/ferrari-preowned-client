"""Run engine: accurate progress, typed error handling, reports, audits.

v1.1 redesign, driven by the pain points observed during the first
14-hour global run:

* **Progress was confusing** — the image denominator grew as workers
  *popped* items, ``ok`` mixed downloads with resume-skips, and there was
  no ETA. Now: totals are incremented at *queue* time (denominator = all
  known work), counters are split (downloaded / skipped / failed), every
  line carries rate + ETA, and a heartbeat renders even when nothing
  completes (long compression), so the run never *looks* stuck.
* **Errors were silent** — they accumulated in lists you had to grep for.
  Now: every error is logged the moment it happens with full context,
  collected into a typed registry, and written to ``run_report.json``
  (together with run summary, request-ledger stats, and the disk audit).
  SIGINT/SIGTERM trigger a graceful stop: store saved, report written,
  non-zero exit.
* **No end-of-run verification** — now a post-run audit compares the
  store's expected image files against disk and reports anything missing
  (the next smart run then auto-completes them via resume-safe queueing).
"""

from __future__ import annotations

import hashlib
import json
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .models import Gallery, GalleryItem, Listing, ListingDetail


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

class RunProgress:
    """Thread-safe, honest progress: put-time totals, rate, ETA, heartbeat."""

    def __init__(self, *, render_interval: float = 20.0):
        self._lock = threading.Lock()
        self.phase = "init"
        self.started = time.monotonic()
        # fetch plane
        self.countries_done = 0
        self.countries_total = 0
        self.current_country = ""
        self.listings_new = 0
        self.listings_changed = 0
        self.listings_unchanged = 0
        self.listings_healed = 0
        # image plane — totals incremented at QUEUE time
        self.galleries_total = 0
        self.galleries_done = 0
        self.images_total = 0
        self.images_downloaded = 0
        self.images_skipped = 0
        self.images_failed = 0
        # live sold/removed + per-country expected totals (from counts())
        self.listings_sold = 0
        self.expected_by_country: dict[str, int] = {}
        self.cars_by_country: dict[str, int] = {}
        self.sold_by_country: dict[str, int] = {}
        # global car denominator: known BEFORE the walk starts (sum of
        # the counts() probe) — put-time total, never grows mid-run
        self.cars_total = 0
        self.cars_done = 0
        self._render_interval = render_interval
        self._last_render = 0.0
        self._last_line = ""          # duplicate-line suppression
        self._last_done_imgs = 0      # windowed rate, not cumulative
        self._last_rate_t = 0.0
        self._stop = False

    # ---- fetch plane updates ------------------------------------------

    def country_start(self, slug: str) -> None:
        with self._lock:
            self.current_country = slug

    def country_done(self) -> None:
        # NOTE: current_country is intentionally KEPT — renders between
        # countries must not lose the tag (the line still describes the
        # country just finished until the next one starts)
        with self._lock:
            self.countries_done += 1

    def country_expected(self, slug: str, n: int) -> None:
        """Pre-known car total for a country (from the counts() probe)."""
        with self._lock:
            self.expected_by_country[slug] = int(n)

    def country_cars_done(self, slug: str, n: int) -> None:
        with self._lock:
            self.cars_by_country[slug] = int(n)

    def country_sold(self, slug: str, n: int) -> None:
        with self._lock:
            self.listings_sold += n
            self.sold_by_country[slug] = n

    def set_cars_total(self, n: int) -> None:
        """Global expected car count for the whole run (counts() sum)."""
        with self._lock:
            self.cars_total = int(n)

    def car_done(self) -> None:
        with self._lock:
            self.cars_done += 1

    def classified(self, kind: str) -> None:
        with self._lock:
            if kind == "new":
                self.listings_new += 1
            elif kind == "changed":
                self.listings_changed += 1
            elif kind == "healed":
                self.listings_healed += 1
            else:
                self.listings_unchanged += 1

    # ---- image plane updates ------------------------------------------

    def gallery_queued(self, n_images: int) -> None:
        """Called when a gallery enters the queue (put-time total)."""
        with self._lock:
            self.galleries_total += 1
            self.images_total += n_images

    def gallery_result(self, downloaded: int, skipped: int, failed: int) -> None:
        with self._lock:
            self.galleries_done += 1
            self.images_downloaded += downloaded
            self.images_skipped += skipped
            self.images_failed += failed

    # ---- queries --------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "elapsed_s": round(time.monotonic() - self.started),
                "countries_done": self.countries_done,
                "countries_total": self.countries_total,
                "current_country": self.current_country,
                "listings_new": self.listings_new,
                "listings_changed": self.listings_changed,
                "listings_unchanged": self.listings_unchanged,
                "listings_healed": self.listings_healed,
                "listings_sold": self.listings_sold,
                "cars_total": self.cars_total,
                "cars_done": self.cars_done,
                "galleries_total": self.galleries_total,
                "galleries_done": self.galleries_done,
                "images_total": self.images_total,
                "images_downloaded": self.images_downloaded,
                "images_skipped": self.images_skipped,
                "images_failed": self.images_failed,
            }

    def render(self, *, force: bool = False) -> Optional[str]:
        """One human-friendly line. Throttled unless ``force``.

        Identical consecutive lines are suppressed even on force (two
        workers + the heartbeat used to print the same line twice), and
        the rate is windowed (delta since the last rendered line), so an
        idle queue shows 0.0/s instead of a stale cumulative average.
        """
        with self._lock:
            now = time.monotonic()
            if not force and (now - self._last_render) < self._render_interval:
                return None
            elapsed = now - self.started
            s = self._render_locked(elapsed)
            if s == self._last_line:
                return None  # nothing changed — print nothing at all
            self._last_line = s
            self._last_render = now
        return s

    def _render_locked(self, elapsed: float) -> str:
        done_imgs = (self.images_downloaded + self.images_skipped
                     + self.images_failed)
        cc = self.current_country
        cc_part = ""
        if cc:
            exp = self.expected_by_country.get(cc)
            seen = self.cars_by_country.get(cc)
            cc_part = f" ({cc}"
            if exp:
                cc_part += f" {seen or 0}/{exp}"
            cc_part += ")"
        fetch = f"{self.phase} {self.countries_done}/{self.countries_total}{cc_part}"
        if self.cars_total:
            # '+' marks over-delivery: the counts() probe occasionally
            # underestimates what search actually returns (~0.9%)
            over = "+" if self.cars_done > self.cars_total else ""
            fetch += f" cars {self.cars_done}/{self.cars_total}{over}"
            # fetch ETA, simple observed form: wall-time per walked car
            # so far (self-correcting: includes pacing + dwells). Only
            # shown once enough cars make the average meaningful.
            remaining_cars = self.cars_total - self.cars_done
            if self.cars_done >= 20 and remaining_cars > 0:
                sec_per_car = elapsed / self.cars_done
                fetch += f" ETA {_fmt_dur(sec_per_car * remaining_cars)}"
        cars = (f"new {self.listings_new} chg {self.listings_changed} "
                f"same {self.listings_unchanged}"
                + (f" heal {self.listings_healed}"
                   if self.listings_healed else "")
                + (f" sold {self.listings_sold}"
                   if self.listings_sold else ""))
        if self.phase == "fetch" and not self.images_total:
            return (f"[{_fmt_dur(elapsed)}] {fetch} | {cars}")
        # windowed rate: images settled since the LAST rendered line —
        # an idle queue (waiting on the paced fetch plane) shows 0.0/s,
        # never a stale cumulative average
        dt = elapsed - self._last_rate_t
        d_img = done_imgs - self._last_done_imgs
        rate = (d_img / dt) if dt > 0.5 else 0.0
        self._last_done_imgs = done_imgs
        self._last_rate_t = elapsed
        remaining = self.images_total - done_imgs
        eta = _fmt_dur(remaining / rate) if rate > 0 and remaining > 0 else "—"
        pct = (100.0 * done_imgs / self.images_total
               if self.images_total else 0.0)
        return (f"[{_fmt_dur(elapsed)}] {fetch} | {cars} | "
                f"imgs {done_imgs}/{self.images_total} ({pct:.0f}%) "
                f"dl {self.images_downloaded} skip {self.images_skipped} "
                f"fail {self.images_failed} | {rate:.1f}/s ETA {eta}")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class ErrorLog:
    """Typed, immediately-logged error registry."""

    def __init__(self, log_fn: Callable[[str], None]):
        self._log_fn = log_fn
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []

    def add(self, kind: str, subject: str,
            message: str, **context: Any) -> None:
        entry: dict[str, Any] = {
            "ts": _utcnow_iso(),
            "kind": kind,           # search | detail | gallery | image | audit | fatal
            "subject": subject,     # country / VIN / URL
            "message": message[:300],
        }
        if context:
            entry["context"] = {k: str(v)[:120] for k, v in context.items()}
        with self._lock:
            self._entries.append(entry)
        # ALWAYS visible immediately — never silently accumulate
        self._log_fn(f"[error/{kind}] {subject}: {message[:160]}")

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def count(self, kind: Optional[str] = None) -> int:
        with self._lock:
            if kind is None:
                return len(self._entries)
            return sum(1 for e in self._entries if e["kind"] == kind)

    def summary(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for e in self._entries:
                out[e["kind"]] = out.get(e["kind"], 0) + 1
            return out


# ---------------------------------------------------------------------------
# Run report
# ---------------------------------------------------------------------------

def write_run_report(path: "str | Path", *, mode: str, summary: dict,
                     errors: ErrorLog, ledger_stats: dict,
                     audit: Optional[dict], wall_seconds: float,
                     interrupted: bool = False,
                     block: Optional[dict] = None) -> dict:
    """Write the machine-readable run report (JSON)."""
    report = {
        "run": {
            "mode": mode,
            "finished_at": _utcnow_iso(),
            "wall_seconds": round(wall_seconds),
            "interrupted": interrupted,
        },
        "summary": summary,
        "errors": {
            "total": errors.count(),
            "by_kind": errors.summary(),
            "entries": errors.entries(),
        },
        "requests": ledger_stats,
        "audit": audit,
        "block": block,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)
    return report


def ledger_stats(transport) -> dict:
    """Aggregate the request ledger: totals, statuses, throttle events."""
    entries = transport.ledger_dicts()
    statuses: dict[str, int] = {}
    for e in entries:
        st = str(e.get("status") or "network-error")
        statuses[st] = statuses.get(st, 0) + 1
    return {
        "total": len(entries),
        "statuses": statuses,
        "throttle_events": statuses.get("202", 0),
        "blocked_events": statuses.get("403", 0) + statuses.get("405", 0),
    }


# ---------------------------------------------------------------------------
# Post-run audit
# ---------------------------------------------------------------------------

def prune_orphans(images_dir: "str | Path", store,
                  *, apply: bool = False) -> dict:
    """Delete image files no current store URL explains.

    Orphans are leftovers of URL churn (the homenet partner regenerates
    image IDs): old-generation files of LIVE cars. DRY-RUN by default;
    ``apply=True`` deletes.

    Data-safety rules (fail-closed, per project doctrine):
    - a live car whose CURRENT urls are ALL dead upstream keeps every
      file in its folders (old generations are the best data we have;
      deleting them would be permanent loss);
    - folders of cars marked removed (sold) are never touched here —
      counted and reported as ``sold_car_files`` only;
    - folders that cannot be attributed to any store car are counted as
      ``unattributed_files`` and never touched;
    - refuses entirely if >10% of live cars have no recorded URLs at all
      (stale store would mass-delete).
    """
    img_root = Path(images_dir)
    live = [st for st in store._listings.values()  # noqa: SLF001
            if not st.removed]
    with_urls = [st for st in live
                 if (st.image_urls or getattr(st, "homenet_urls", []))]
    if live and len(with_urls) / len(live) < 0.9:
        return {
            "refused": True,
            "reason": (f"{len(live) - len(with_urls)} of {len(live)} live "
                       f"cars have no recorded image URLs — store looks "
                       f"stale; run one smart crawl first"),
        }
    dead = (store.dead_image_urls
            if hasattr(store, "dead_image_urls") else set())

    # per-car classification for the prune decision
    expected: dict[str, set[str]] = {}     # key -> filenames to KEEP
    protected: set[str] = set()            # keys: keep every file
    removed_keys = {k for k, st in store._listings.items()
                    if st.removed}
    for key, st in store._listings.items():
        if st.removed:
            continue
        raw = [u for u in (st.image_urls or [])
               + getattr(st, "homenet_urls", []) if u]
        live_urls = [u for u in raw if u not in dead]
        if raw and not live_urls:
            protected.add(key)  # all current URLs dead upstream
            continue
        names = expected.setdefault(key, set())
        for i, u in enumerate(live_urls):
            digest = hashlib.sha256(u.encode()).hexdigest()[:10]
            names.add(f"{i:03d}_{digest}.webp")

    folders = [d for d in img_root.iterdir() if d.is_dir()] \
        if img_root.exists() else []
    orphans: list[Path] = []
    bytes_freed = 0
    sold_files = unattributed = 0

    def owner_key(folder_name: str):
        key = folder_name.split("_")[0]
        if key in expected or key in protected or key in removed_keys:
            return key
        prefix = folder_name[:64]
        for k in list(expected) + list(protected) + list(removed_keys):
            if prefix.startswith(k[:64]) or k.startswith(prefix):
                return k
        return None

    for folder in folders:
        owner = owner_key(folder.name)
        if owner is None or owner in protected:
            # unknown folders: conservative, count only
            if owner is None:
                unattributed += sum(1 for f in folder.iterdir()
                                    if f.is_file())
            continue
        if owner in removed_keys:
            sold_files += sum(1 for f in folder.iterdir() if f.is_file())
            continue
        for f in folder.iterdir():
            if f.is_file() and f.name not in expected[owner]:
                orphans.append(f)
                bytes_freed += f.stat().st_size

    result = {
        "refused": False,
        "orphan_files": len(orphans),
        "bytes_freed": bytes_freed,
        "sold_car_files_untouched": sold_files,
        "unattributed_files_untouched": unattributed,
        "applied": bool(apply),
        "sample": [str(o.relative_to(img_root)) for o in orphans[:20]],
    }
    if apply:
        for f in orphans:
            f.unlink(missing_ok=True)
        for folder in folders:
            try:
                folder.rmdir()  # drop now-empty dirs only
            except OSError:
                pass
    return result


def audit_images(images_dir: "str | Path", store,
                 error_log: Optional[ErrorLog] = None) -> dict:
    """Verify the store's expected image files exist on disk.

    This is the 'never miss data' guarantee: every gallery URL recorded in
    the store maps to a deterministic filename; anything missing is
    reported (and will be auto-downloaded by the next smart run).
    """
    img_root = Path(images_dir)
    all_folders: list[Path] = []
    if img_root.exists():
        all_folders = [d for d in img_root.iterdir() if d.is_dir()]

    def folders_for(key: str) -> list[Path]:
        # a car's folder is f"{identity_key}_{model}-{year}"[:80]:
        # long blob keys get truncated, and legacy runs may have left
        # sibling folders under old names — match by key prefix and
        # union ALL matching folders (never lose files to a name match).
        prefix = key[:64]
        return [d for d in all_folders
                if d.name.split("_")[0] == key or d.name.startswith(prefix)]

    total_expected = 0
    total_present = 0
    orphan_files = 0
    missing_by_car: dict[str, int] = {}
    no_folder: list[str] = []
    dead_urls = (store.dead_image_urls
                 if hasattr(store, "dead_image_urls") else set())
    for key, st in store._listings.items():  # noqa: SLF001 (internal API)
        urls = (st.image_urls or []) + getattr(st, "homenet_urls", [])
        if st.removed:
            continue  # sold cars: their folders are prune candidates
        urls = [u for u in urls if u and u not in dead_urls]
        if not urls:
            continue
        folders = folders_for(key)
        if not folders:
            no_folder.append(key)
            missing_by_car[key] = len(urls)
            total_expected += len(urls)
            continue
        have = set()
        for folder in folders:
            have |= {f.name for f in folder.iterdir() if f.is_file()}
        expected_names = set()
        for i, u in enumerate(urls):
            digest = hashlib.sha256(u.encode()).hexdigest()[:10]
            name = f"{i:03d}_{digest}.webp"
            expected_names.add(name)
            total_expected += 1
            if name in have:
                total_present += 1
            else:
                missing_by_car[key] = missing_by_car.get(key, 0) + 1
        # files on disk that no current gallery URL explains: leftovers
        # of URL churn (partner regenerates IDs) — data-correct but
        # disk waste, reported so the user can prune consciously
        orphan_files += len(have - expected_names)

    missing_total = total_expected - total_present
    result = {
        "expected_files": total_expected,
        "present_files": total_present,
        "missing_files": missing_total,
        "cars_with_missing": len(missing_by_car),
        "cars_without_folder": len(no_folder),
        "orphan_files": orphan_files,
        "sample_missing": dict(list(missing_by_car.items())[:20]),
        "dead_urls_skipped": len(dead_urls),
    }
    if error_log is not None and missing_total:
        error_log.add(
            "audit", "images",
            f"{missing_total} expected image files missing on disk "
            f"({result['cars_with_missing']} cars) — the next smart run "
            f"auto-completes them",
            cars=result["cars_with_missing"])
    return result


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def run_pipeline(client, target_dir: "str | Path", *, mode: "str" = "smart",
                 countries=None, image_config=None, image_workers: int = 4,
                 store=None, progress: Optional[Callable[[str], None]] = None,
                 on_snapshot=None,
                 report_interval_countries: int = 1) -> dict:
    """Two-plane run engine shared by full/smart modes.

    ``mode='full'`` ignores the store (everything fetched);
    ``mode='smart'`` requires one (classify → skip unchanged).

    ``progress`` receives human-readable log lines; ``on_snapshot`` (used
    by the JSON progress mode) receives the structured RunProgress
    snapshot dict every time a line renders.
    """
    import queue as _queue

    from .errors import ThrottledError
    from .images import ImageConfig, ImageDownloader
    from .pipeline import Progress  # noqa: F401 (legacy export)

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    images_dir = target / "images"
    smart = mode == "smart" and store is not None
    if mode == "smart" and store is None:
        from .smart import SmartStore
        store = SmartStore(target / "smart_store.json")

    def log(msg: str) -> None:
        print(msg, flush=True) if progress is None else progress(msg)

    def emit(line: str) -> None:
        """A rendered progress line: log it, and hand the structured
        snapshot to the JSON-mode consumer (module-friendly events)."""
        log(line)
        if on_snapshot is not None:
            on_snapshot(prog.snapshot())

    prog = RunProgress()
    errors = ErrorLog(log)
    _dead = set()
    if smart and store is not None:
        _dead = set(store._meta.get("dead_image_urls", []))
    downloader = ImageDownloader(client.transport,
                                  config=image_config or ImageConfig(),
                                  dead_urls=_dead)
    downloader._log = log  # noqa: SLF001 — engine owns the logger

    # ---- pre-flight: one cheap probe detects an IP-level block before ---
    # ---- the run starts, so the store is never touched on a blocked run --
    block_info: Optional[dict] = None
    from .errors import BlockedError as _BlockedError
    t0_probe = time.time()
    try:
        probe = client.transport.get(
            "https://preowned.ferrari.com/robots.txt", locale="de-DE")
        if probe.status_code in (403, 405):
            raise _BlockedError(
                f"pre-flight probe got HTTP {probe.status_code}",
                evidence={"url": probe.url, "body_head": probe.text[:200]})
    except _BlockedError as exc:
        block_info = {
            "detected_at": _utcnow_iso(),
            "stage": "pre-flight",
            "message": str(exc),
            "guidance": (
                "Ferrari's CloudFront edge is blocking this egress IP "
                "(datacenter/VPN ranges are blocked). Run from the "
                "residential connection with the VPN OFF. The store was "
                "NOT touched."),
        }
        log("[block] " + block_info["message"])
        log("[block] " + block_info["guidance"])
        errors.add("fatal", "pre-flight", block_info["message"])
        report = write_run_report(
            target / "run_report.json", mode=mode, summary={}, errors=errors,
            ledger_stats=ledger_stats(client.transport), audit=None,
            wall_seconds=time.time() - t0_probe, interrupted=False,
            block=block_info)
        return {
            "target_dir": str(target), "interrupted": False, "blocked": True,
            "summary": {}, "errors": errors.entries(),
            "error_count": errors.count(), "audit": None,
            "report_path": str(target / "run_report.json"),
            "report": report,
        }
    except Exception as exc:
        # network hiccup on probe — log but continue; the fetch plane
        # retries and will classify a real block if it persists
        errors.add("search", "pre-flight", f"probe failed: {exc}")


    listings: dict[str, Listing] = {}
    details: dict[str, Optional[ListingDetail]] = {}
    galleries: dict[str, Gallery] = {}
    state_lock = threading.Lock()
    gallery_q = _queue.Queue()
    _SENTINEL = object()
    interrupted = threading.Event()

    # ---- graceful shutdown: SIGINT/SIGTERM save state + write report ----
    prev_handlers: dict[int, Any] = {}

    def _signal(signum, frame):
        interrupted.set()
        log(f"[signal] {signal.Signals(signum).name} — graceful stop: "
            f"finishing current image, saving state, writing report")
        # unblock queue waiters
        for _ in range(image_workers + 1):
            gallery_q.put(_SENTINEL)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev_handlers[sig] = signal.signal(sig, _signal)
        except (ValueError, OSError):
            pass  # not main thread / unsupported

    # ---- heartbeat: never look stuck -------------------------------------
    def heartbeat() -> None:
        while not interrupted.is_set():
            time.sleep(30.0)
            line = prog.render(force=True)
            if line:
                emit(line)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    # ---- fetch plane ------------------------------------------------------
    def queue_gallery(listing: Listing, urls: list[str],
                      gallery_id: str) -> None:
        gal = Gallery(
            gallery_id=gallery_id,
            items=[GalleryItem(content_id=str(i), content_url=u)
                   for i, u in enumerate(urls)],
        )
        with state_lock:
            galleries[listing.identity_key] = gal
        prog.gallery_queued(len(urls))
        gallery_q.put((listing, gal))

    def abort_blocked(exc: Exception, stage: str) -> None:
        """A single BlockedError anywhere = stop everything, fast."""
        nonlocal block_info
        if block_info is not None:
            return  # already aborting
        block_info = {
            "detected_at": _utcnow_iso(),
            "stage": stage,
            "message": str(exc),
            "guidance": (
                "Blocked mid-run — the WAF/edge refused us. Aborted "
                "immediately to avoid hammering a blocking endpoint. "
                "Check the egress IP (VPN/datacenter ranges are blocked); "
                "store state is checkpointed per country and safe."),
        }
        log("[block] " + str(exc))
        log("[block] aborted at stage: " + stage)
        errors.add("fatal", stage, str(exc))
        interrupted.set()
        for _ in range(image_workers + 1):
            gallery_q.put(_SENTINEL)

    def fetch_worker() -> None:
        prog.phase = "fetch"
        try:
            counts = client.counts()
        except _BlockedError as exc:
            abort_blocked(exc, "counts")
            return
        except Exception as exc:
            errors.add("search", "counts", str(exc))
            for _ in range(image_workers):
                gallery_q.put(_SENTINEL)
            return
        wanted = list(countries) if countries is not None else \
            [c for c, n in counts.items() if n > 0]
        with state_lock:
            prog.countries_total = len(wanted)
        # pre-known car totals straight from the counts() probe
        for slug, n in counts.items():
            if n > 0:
                prog.country_expected(slug, n)
        # the global car denominator is known before walking: sum of
        # expected cars across the countries this run will walk
        prog.set_cars_total(sum(counts.get(c, 0) for c in wanted))
        line = prog.render(force=True)
        if line:
            emit(line)
        log(prog.render(force=True) or "")

        for slug in wanted:
            if interrupted.is_set():
                break
            prog.country_start(slug)
            country_listings = None
            for attempt in range(3):
                if interrupted.is_set():
                    break
                try:
                    country_listings = list(client.country_listings(slug))
                    break
                except ThrottledError as exc:
                    cooldown = max(90.0, float(exc.retry_after or 0.0))
                    log(f"[fetch] throttled on {slug} "
                        f"(attempt {attempt + 1}/3) — cooldown {cooldown:.0f}s")
                    time.sleep(cooldown)
                except _BlockedError as exc:
                    abort_blocked(exc, f"country_listings:{slug}")
                    return
                except Exception as exc:
                    errors.add("search", slug, f"country_listings: {exc}")
                    break
            if country_listings is None or interrupted.is_set():
                prog.country_done()
                continue

            country_keys: list[str] = []
            for listing in country_listings:
                if interrupted.is_set():
                    break
                key = listing.identity_key
                country_keys.append(key)
                prog.car_done()
                with state_lock:
                    listings[key] = listing

                if smart:
                    assert store is not None  # smart ⇒ store (see line ~352)
                    cls = store.classify(listing)
                    st = store.get(key)
                    gallery_urls = st.image_urls if st else None
                    # A car classified 'unchanged' whose store entry has NO
                    # gallery URLs is incompletely recorded (e.g. its detail
                    # 404'd in an older run before the URL fixes). Re-resolve
                    # the detail to learn the gallery — the heal path.
                    if cls == "unchanged" and not gallery_urls \
                            and not listing.images.homenet:
                        prog.classified("healed")
                        try:
                            det = client.detail(listing)
                            with state_lock:
                                details[key] = det
                            store.record_detail(listing, det)
                            gid = (det.images.thron_hero_gallery_id
                                   or det.images.thron_card_gallery_id)
                            if gid:
                                gal = client.gallery(det)
                                with state_lock:
                                    galleries[key] = gal
                                store.record_gallery(listing, gal)
                                queue_gallery(listing, [u for u in
                                                        gal.images if u],
                                              "heal:" + key)
                        except ThrottledError as exc:
                            errors.add("detail", key, f"heal throttled: {exc}",
                                       retry_after=exc.retry_after)
                        except Exception as exc:
                            errors.add("detail", key, f"heal: {exc}")
                        continue
                    prog.classified(cls)
                    if cls == "unchanged":
                        store.record_seen(listing)
                        # resume-safe: queue gallery so MISSING files
                        # complete; existing ones skip locally
                        if listing.images.homenet:
                            store.record_homenet(listing,
                                                 listing.images.homenet)
                            queue_gallery(listing, listing.images.homenet,
                                          "homenet:" + key)
                        elif gallery_urls:
                            queue_gallery(listing, list(gallery_urls),
                                          "store:" + key)
                        continue

                if listing.images.homenet:
                    queue_gallery(listing, listing.images.homenet,
                                  "homenet:" + key)
                    if smart:
                        store.record_seen(listing)
                        store.record_homenet(listing,
                                             listing.images.homenet)
                    continue

                if smart:
                    store.record_seen(listing)
                try:
                    det = client.detail(listing)
                    with state_lock:
                        details[key] = det
                    if smart:
                        store.record_detail(listing, det)
                    gid = det.images.thron_hero_gallery_id or \
                        det.images.thron_card_gallery_id
                    if gid:
                        gal = client.gallery(det)
                        urls = [u for u in gal.images if u]
                        with state_lock:
                            galleries[key] = gal
                        prog.gallery_queued(len(urls))
                        gallery_q.put((listing, gal))
                        if smart:
                            store.record_gallery(listing, gal)
                except ThrottledError as exc:
                    errors.add("detail", key,
                               f"throttled: {exc}",
                               retry_after=exc.retry_after)
                except _BlockedError as exc:
                    abort_blocked(exc, f"detail:{key}")
                    return
                except Exception as exc:
                    errors.add("detail", key, f"{exc}")
            prog.country_cars_done(slug, len(country_listings))
            # live sold/removed: scope the pass to THIS country and
            # checkpoint immediately (crash-safe: a killed run has
            # consistent removal state for every completed country)
            if (smart and store is not None
                    and not interrupted.is_set()):
                # (interrupted mid-country => partial key list: recording
                # removals then would mark half-walked cars as sold)
                removed_now = store.record_removals(
                    country_keys, only_countries={slug})
                if removed_now:
                    prog.country_sold(slug, len(removed_now))
                    log(f"[store] {slug}: {len(removed_now)} listings "
                        f"marked removed (sold/delisted)")
                    store.save()
            prog.country_done()
            line = prog.render(force=True)
            if line:
                emit(line)
            if smart and store is not None:
                store.save()  # per-country checkpoint

        for _ in range(image_workers):
            gallery_q.put(_SENTINEL)

    # ---- image plane ------------------------------------------------------
    def image_worker() -> None:
        while True:
            item = gallery_q.get()
            if item is _SENTINEL:
                gallery_q.task_done()
                return
            listing, gal = item
            if interrupted.is_set():
                prog.gallery_result(0, 0, 0)
                gallery_q.task_done()
                continue
            urls = [u for u in gal.images if u]
            try:
                res = downloader.download_gallery(listing, gal, images_dir)
                failed = res["total"] - res["ok"]
                prog.gallery_result(
                    res["ok"] - res.get("skipped", 0),
                    res.get("skipped", 0),
                    failed)
                if failed:
                    bad = [r for r in res["results"]
                           if r.get("status") == "error"]
                    for r in bad[:3]:
                        errors.add("image", listing.identity_key,
                                   r.get("error") or "download failed",
                                   url=r.get("url"))
            except _BlockedError as exc:
                prog.gallery_result(0, 0, len(urls))
                abort_blocked(exc, "image-cdn")
                gallery_q.task_done()
                return
            except ThrottledError as exc:
                prog.gallery_result(0, 0, len(urls))
                errors.add("image", listing.identity_key,
                           f"throttled, gallery abandoned: {exc}",
                           retry_after=exc.retry_after)
            except Exception as exc:  # never let a worker die
                prog.gallery_result(0, 0, len(urls))
                errors.add("image", listing.identity_key,
                           f"worker exception: {exc}")
            gallery_q.task_done()
            line = prog.render()
            if line:
                emit(line)

    fetch_thread = threading.Thread(target=fetch_worker, name="fetch",
                                    daemon=True)
    image_threads = [threading.Thread(target=image_worker,
                                      name=f"img{i}", daemon=True)
                     for i in range(image_workers)]
    t0 = time.time()
    fetch_thread.start()
    for t in image_threads:
        t.start()
    fetch_thread.join()
    for t in image_threads:
        t.join()

    # ---- finish: audit, exports, report ----------------------------------
    was_interrupted = interrupted.is_set()
    # Removals are recorded per-country at country_done (scoped to that
    # country, checkpointed immediately) — the old finish-time global
    # pass is superseded. A run that walked zero countries records
    # nothing, which is exactly the guarded behavior.

    # persist newly discovered dead upstream URLs so the next run
    # skips them without burning requests
    if smart and store is not None and downloader.new_dead_urls:
        merged = set(store._meta.get("dead_image_urls", [])) | \
            downloader.new_dead_urls
        store._meta["dead_image_urls"] = sorted(merged)
        log(f"[store] {len(downloader.new_dead_urls)} new dead image URLs "
            f"recorded ({len(merged)} total) — skipped next runs")
        store.save()

    prog.phase = "audit" if smart else "export"
    audit = None
    if smart and store is not None:
        audit = audit_images(images_dir, store, error_log=errors)

    prog.phase = "export"
    listing_list = list(listings.values())
    clean_details = {k: v for k, v in details.items() if v is not None}
    from .export import export_csv, export_json, export_xlsx
    export_json(listing_list, clean_details, target / "ferrari_preowned.json")
    export_csv(listing_list, clean_details, target / "ferrari_preowned.csv")
    export_xlsx(listing_list, clean_details,
                target / "ferrari_preowned.xlsx", galleries=galleries)

    prog.phase = "done"
    done_line = prog.render(force=True)
    if done_line:
        emit(done_line)

    # restore signal handlers
    for sig, handler in prev_handlers.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass

    summary = {
        "mode": mode,
        "listings": len(listings),
        "details_fetched": len(details),
        "galleries": len(galleries),
        "listings_new": prog.listings_new,
        "listings_changed": prog.listings_changed,
        "listings_unchanged": prog.listings_unchanged,
        "listings_healed": prog.listings_healed,
        "listings_sold": prog.listings_sold,
        "images_downloaded": downloader.stats["downloaded"],
        "images_skipped": downloader.stats["skipped_existing"],
        "images_failed": downloader.stats["failed"],
        "compression": {
            "bytes_original": downloader.stats["bytes_original"],
            "bytes_final": downloader.stats["bytes_final"],
        },
        "store_stats": store.stats() if (smart and store) else None,
    }
    report = write_run_report(
        target / "run_report.json", mode=mode, summary=summary,
        errors=errors, ledger_stats=ledger_stats(client.transport),
        audit=audit, wall_seconds=time.time() - t0,
        interrupted=was_interrupted, block=block_info)

    return {
        "target_dir": str(target),
        "interrupted": was_interrupted,
        "blocked": block_info is not None,
        "summary": summary,
        "errors": errors.entries(),
        "error_count": errors.count(),
        "audit": audit,
        "report_path": str(target / "run_report.json"),
        "report": report,
    }


__all__ = ["RunProgress", "ErrorLog", "run_pipeline", "write_run_report",
           "ledger_stats", "audit_images"]
