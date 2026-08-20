"""The long-lived service: scheduler, watch-folder wiring, single-scan lock.

Everything here runs on background threads. The web layer only ever calls into
this module — it never touches the scanner directly — so "is a scan already
running" has exactly one answer.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import config, db, quality, recycle, transcode
from .clients.arr import ArrClient, ArrError
from .clients.emby import EmbyClient, EmbyError
from .config import Settings, WatchFolder
from .probe import probe
from .remediation import Remediator, decide
from .scanner import Scanner, check_file, persist_result
from .state import bus, clear_task, set_task, state
from .watcher import WatchManager

log = logging.getLogger(__name__)


class Service:
    def __init__(self) -> None:
        self.remediator = Remediator(config.get)
        self.scanner = Scanner(config.get, self.remediator)
        self.watcher = WatchManager(config.get, self._on_watch_ready)
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._sched_stop = threading.Event()
        self._sched_thread: threading.Thread | None = None
        self._watch_pool = threading.Semaphore(2)
        self._estimate_lock = threading.Lock()
        self._shrink_thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        db.init()
        config.load()
        self._restore_last_scan()
        self._reconcile_interrupted_work()
        self.watcher.start()
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(target=self._scheduler, daemon=True,
                                              name="unfuckarr-scheduler")
        self._sched_thread.start()
        self._shrink_thread = threading.Thread(target=self._shrink_worker,
                                               daemon=True,
                                               name="unfuckarr-shrink")
        self._shrink_thread.start()
        self.refresh_services()
        s = config.get()
        if s.schedule.scan_enabled and s.schedule.scan_at_startup:
            self.start_scan("startup")
        db.log("service_started", "info")

    def stop(self) -> None:
        self._sched_stop.set()
        self.scanner.request_stop()
        self.watcher.stop()
        if self._sched_thread is not None:
            self._sched_thread.join(timeout=5)
        db.log("service_stopped", "info")

    def _reconcile_interrupted_work(self) -> None:
        """Clear up after a transcode that was killed part-way.

        Nothing can be in flight at startup — the process has only just begun —
        so any job still marked running, and any ``*.unfuckarr.*`` left beside
        a known media file, is a leftover by definition. That is a safe rule
        and it needs to exist: `is_temp_output` deliberately makes the scanner
        and the watcher ignore these files, which stops them being acted on and
        equally stops anyone noticing them. Live, four had accumulated to
        **199 GB** inside the library, and Emby had indexed each as a separate
        film and pulled artwork for it.
        """
        stale = db.q("SELECT id, kind, path FROM jobs WHERE state IN "
                     "('running','queued')")
        for job in stale:
            db.ex("UPDATE jobs SET state='failed', finished=?, error=? WHERE id=?",
                  (time.time(), "interrupted by a restart", job["id"]))
        if stale:
            db.log("jobs_reconciled", "warn",
                   detail={"interrupted": len(stale),
                           "paths": [j["path"] for j in stale if j["path"]][:10]})

        def sweep() -> None:
            paths = [r["path"] for r in db.q("SELECT path FROM files")]
            leftovers = transcode.abandoned_outputs(paths)
            freed = 0
            for path in leftovers:
                try:
                    freed += os.path.getsize(path)
                    os.unlink(path)
                except OSError as exc:
                    log.warning("could not remove leftover %s: %s", path, exc)
            if leftovers:
                db.log("abandoned_outputs_removed", "warn", detail={
                    "count": len(leftovers), "bytes": freed,
                    "paths": leftovers[:10],
                })

        # On a background thread: it is a listdir per media directory, and a
        # slow array must not hold up the web server coming back.
        threading.Thread(target=sweep, daemon=True,
                         name="unfuckarr-leftovers").start()

    # -- continuous shrinking ---------------------------------------------

    def _shrink_worker(self) -> None:
        """Work through the shrink backlog for as long as the service runs.

        A library of thousands of candidates is not a per-scan job. At a
        quarter of an hour or more each, a nightly batch of five takes years,
        and a nightly batch large enough to matter is a scan that runs all
        day and blocks everything behind it. So shrinking is a worker that is
        simply always running, and is *paced* rather than *rationed* — see
        governor.py, which holds it to a share of the GPU's encode engine so
        that leaving it on permanently is not the reason a film stutters.

        The scan's job is now only to decide *what* is a candidate; this
        decides when, and in what order.
        """
        from .remediation import Decision, shrink_blocked

        # Nothing at all until the first scan has had a chance to populate the
        # backlog, and so that a restart does not immediately start encoding.
        self._sched_stop.wait(30)

        while not self._sched_stop.is_set():
            wait = 60.0
            try:
                s = config.get()
                reason = self._shrink_idle_reason(s, shrink_blocked)
                if reason is not None:
                    # Nothing to do, and nothing wrong. Checking every five
                    # minutes is often enough for a state that only changes
                    # when someone edits the settings or a scan finishes.
                    self._sched_stop.wait(300)
                    continue

                row = self._next_shrink_candidate()
                if row is None:
                    self._sched_stop.wait(300)
                    continue

                self._shrink_one(row, Decision(
                    "shrink",
                    "measuring how small this can be at full perceptual quality"))
                # Straight on to the next one: the governor is what paces
                # this, not a delay here.
                wait = 0.0
            except Exception as exc:  # noqa: BLE001 - the worker must not die
                log.exception("shrink worker failed")
                db.log("shrink_worker_error", "error", detail=str(exc))
                wait = 300.0
            if wait:
                self._sched_stop.wait(wait)

    @staticmethod
    def _shrink_idle_reason(s: Settings, shrink_blocked) -> str | None:
        """Why the worker has nothing to do, or None when it should work."""
        if not s.shrink.continuous:
            return "continuous shrinking is off"
        if s.policy.oversize_action != "shrink":
            return f"the policy is {s.policy.oversize_action!r}"
        if state.paused:
            return "paused"
        blocked = shrink_blocked(s)
        if blocked is not None:
            return blocked
        from .remediation import _window_wait
        return _window_wait(s.shrink.only_between_hours)

    @staticmethod
    def _next_shrink_candidate() -> dict[str, Any] | None:
        """The fattest file still waiting to be measured.

        Fattest first because the order decides which savings land this month
        and which land next year. `shrink_priority` is how far above the
        reference bitrate for its resolution the file sits; NULLs sort last
        under DESC in SQLite, so a row that has never been checked falls to
        the back rather than to the front.
        """
        # `hygiene` as well as `unmeasured`, because `decide` deliberately
        # prefers a shrink over a hygiene flag — the re-encode rewrites every
        # byte and carries the tag fixes with it. Selecting on status alone
        # missed that: status precedence puts a file with untidy metadata
        # under `hygiene`, so on the live library 1,489 perfectly good
        # candidates were invisible to the worker while `decide` would have
        # shrunk every one of them.
        #
        # `corrupt` and `incompatible` stay out, and that also matches
        # `decide`: something is wrong with those files, and repairing them
        # comes first. They become candidates once repaired.
        row = db.q1(
            "SELECT * FROM files "
            " WHERE status IN ('unmeasured', 'hygiene') "
            "   AND shrunk IS NULL AND shrink_skipped IS NULL "
            "   AND COALESCE(shrink_attempts, 0) = 0 "
            "   AND EXISTS (SELECT 1 FROM findings d WHERE d.path = files.path "
            "               AND d.code = 'not_measured' AND d.resolved IS NULL) "
            " ORDER BY shrink_priority DESC, size DESC LIMIT 1")
        return {k: row[k] for k in row.keys()} if row else None

    def _shrink_one(self, record: dict[str, Any], decision) -> None:
        path = record["path"]
        if not Path(path).exists():
            db.ex("UPDATE files SET status='missing' WHERE path=?", (path,))
            return
        s = config.get()
        result, info = check_file(
            path, s, expected_runtime=record.get("expected_runtime"),
            already_shrunk=bool(record.get("shrunk") or record.get("shrink_skipped")))
        try:
            stat = Path(path).stat()
            persist_result(path, result, info, stat.st_size, stat.st_mtime)
        except OSError:
            pass
        # The file may have changed since the scan flagged it — repaired,
        # replaced by the *arr, or no longer a candidate at all.
        if not result.unmeasured:
            return
        outcome = self.remediator.apply(record, result, info, decision)
        bus.publish("remediated", {"path": path, **outcome})

    def _restore_last_scan(self) -> None:
        """Carry the last scan time across a restart.

        Without this a container restart reports "No scan yet" on a library
        that has been scanned for months, and — worse — `_recompute_next_scan`
        falls back to `time.time()`, so every restart pushes the next
        scheduled scan out by a full interval. A daily restart would mean the
        schedule never fires at all.
        """
        row = db.q1("SELECT MAX(finished) f FROM scans WHERE finished IS NOT NULL")
        if row and row["f"]:
            state.last_scan_finished = row["f"]

    def reload(self) -> None:
        """Called after settings are saved."""
        self.watcher.start()
        self.refresh_services()
        self._recompute_next_scan()
        bus.publish("settings", None)

    # -- scans ------------------------------------------------------------

    @property
    def scanning(self) -> bool:
        return state.scan.running

    def start_scan(self, trigger: str = "manual",
                   paths: list[str] | None = None) -> bool:
        """Returns False when a scan is already running."""
        if not self._scan_lock.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self.scanner.run(trigger=trigger, paths=paths)
            except Exception:  # noqa: BLE001
                log.exception("scan crashed")
                db.log("scan_crashed", "error")
            finally:
                self._recompute_next_scan()
                self._scan_lock.release()

        self._scan_thread = threading.Thread(target=run, daemon=True,
                                             name="unfuckarr-scan")
        self._scan_thread.start()
        return True

    def stop_scan(self) -> None:
        self.scanner.request_stop()

    def _scheduler(self) -> None:
        self._recompute_next_scan()
        while not self._sched_stop.wait(30.0):
            s = config.get()
            if state.paused or not s.schedule.scan_enabled:
                state.next_scan_at = None
                continue
            due = state.next_scan_at
            if due is not None and time.time() >= due and not self.scanning:
                self.start_scan("scheduled")
            # Retention runs on the same tick; it is cheap and needs no
            # scan to have happened.
            try:
                recycle.sweep(s.policy.recycle_bin_days)
            except Exception:  # noqa: BLE001
                log.exception("recycle sweep failed")

    def _recompute_next_scan(self) -> None:
        s = config.get()
        if not s.schedule.scan_enabled:
            state.next_scan_at = None
        else:
            base = state.last_scan_finished or time.time()
            state.next_scan_at = base + s.schedule.scan_interval_hours * 3600
        bus.publish("state", state.snapshot())

    # -- watch folders ----------------------------------------------------

    def _on_watch_ready(self, path: str, folder: WatchFolder) -> None:
        """A settled file in a watch folder. Check it and act immediately.

        Handed to a thread so the settle loop keeps running while a transcode
        of the previous arrival is still going.
        """
        threading.Thread(target=self._check_arrival, args=(path,), daemon=True,
                         name="unfuckarr-arrival").start()

    def _check_arrival(self, path: str) -> None:
        with self._watch_pool:
            s = config.get()
            row = db.q1("SELECT * FROM files WHERE path = ?", (path,))
            if row is None:
                # Not in the inventory yet — the *arr may not have imported it.
                # Record it so the result has somewhere to live.
                db.ex("INSERT OR IGNORE INTO files (path, library, source, title) "
                      "VALUES (?,?,?,?)",
                      (path, "Watch", "watch", Path(path).stem))
                row = db.q1("SELECT * FROM files WHERE path = ?", (path,))
            record = {k: row[k] for k in row.keys()}

            set_task(f"watch:{path}", kind="probing", path=path,
                     title=record.get("title") or Path(path).name,
                     detail="new arrival", progress=-1, started=time.time())
            emby = EmbyClient(s.emby) if s.emby.enabled else None
            try:
                result, info = check_file(
                    path, s, expected_runtime=record.get("expected_runtime"),
                    emby=emby)
            except Exception as exc:  # noqa: BLE001
                log.exception("arrival check failed for %s", path)
                db.log("arrival_check_failed", "error", path, str(exc))
                clear_task(f"watch:{path}")
                return
            finally:
                clear_task(f"watch:{path}")

            try:
                stat = Path(path).stat()
                persist_result(path, result, info, stat.st_size, stat.st_mtime)
            except OSError:
                persist_result(path, result, info, 0, 0.0)

            decision = decide(result, s)
            db.log("arrival_checked", "info", path,
                   {"status": result.status, "action": decision.action,
                    "reason": decision.reason})
            bus.publish("arrival", {"path": path, "status": result.status,
                                    "action": decision.action})
            if decision.action != "none":
                outcome = self.remediator.apply(record, result, info, decision)
                bus.publish("remediated", {"path": path, **outcome})

    # -- single-file operations from the UI -------------------------------

    def recheck(self, path: str, act: bool = True) -> dict[str, Any]:
        s = config.get()
        row = db.q1("SELECT * FROM files WHERE path = ?", (path,))
        record = ({k: row[k] for k in row.keys()} if row
                  else {"path": path, "source": "folder", "title": Path(path).name})
        emby = EmbyClient(s.emby) if s.emby.enabled else None
        result, info = check_file(
            path, s, expected_runtime=record.get("expected_runtime"), emby=emby,
            already_shrunk=bool(record.get("shrunk") or record.get("shrink_skipped")))
        try:
            stat = Path(path).stat()
            persist_result(path, result, info, stat.st_size, stat.st_mtime)
        except OSError:
            pass
        decision = decide(result, s)
        out: dict[str, Any] = {"result": result.as_dict(),
                               "decision": decision.as_dict()}
        if act and decision.action != "none":
            out["outcome"] = self.remediator.apply(record, result, info, decision)
        return out

    def force_action(self, path: str, action: str) -> dict[str, Any]:
        """Run a specific action against a file regardless of policy."""
        from .remediation import Decision

        s = config.get()
        row = db.q1("SELECT * FROM files WHERE path = ?", (path,))
        if row is None:
            raise FileNotFoundError(path)
        record = {k: row[k] for k in row.keys()}
        result, info = check_file(
            path, s, expected_runtime=record.get("expected_runtime"),
            # An explicit request should see the file as it is, not as the
            # shrink bookkeeping has already labelled it.
            already_shrunk=action != "shrink" and bool(record.get("shrunk")))
        return self.remediator.apply(
            record, result, info,
            Decision(action, f"{action} requested from the web UI", force=True),
        )

    def estimate_shrink(self, path: str) -> bool:
        """Measure what a shrink would save, and change nothing.

        This is the honest answer to "what would automatic shrinking do to my
        library" — it runs the same search the real action runs, on one file,
        and reports the CRF, the score and the projected saving without
        encoding anything. It costs the same minutes the search always costs,
        so it runs on a background thread and reports through the event
        stream rather than holding an HTTP request open for them.
        """
        if not self._estimate_lock.acquire(blocking=False):
            return False

        def run() -> None:
            s = config.get()
            key = f"estimate:{path}"
            try:
                set_task(key, kind="analysing", path=path,
                         title=Path(path).name, progress=-1,
                         started=time.time(), detail="measuring a shrink")
                info = probe(path, s.ffprobe_path)
                metric = quality.resolve_metric(s.shrink, s.ffmpeg_path)
                if metric is None:
                    payload = {"path": path, "ok": False,
                               "reason": "no quality metric available — "
                                         "neither libvmaf nor ssim was found"}
                else:
                    plan = quality.search(info, s.shrink, s.transcode,
                                          ffmpeg=s.ffmpeg_path, metric=metric)
                    payload = {"path": path, **plan.as_dict()}
                db.log("shrink_estimate", "info", path, payload)
            except Exception as exc:  # noqa: BLE001 - a worker must not die
                log.exception("shrink estimate failed for %s", path)
                payload = {"path": path, "ok": False, "reason": str(exc)}
                db.log("shrink_estimate", "warn", path, payload)
            finally:
                clear_task(key)
                self._estimate_lock.release()
            bus.publish("shrink_estimate", payload)

        threading.Thread(target=run, daemon=True,
                         name="unfuckarr-estimate").start()
        return True

    # -- service health ---------------------------------------------------

    def refresh_services(self) -> dict[str, Any]:
        s = config.get()
        out: dict[str, Any] = {}
        for name, cfg, flavour in (("sonarr", s.sonarr, "sonarr"),
                                   ("radarr", s.radarr, "radarr")):
            if not cfg.enabled or not cfg.url:
                out[name] = {"configured": False}
                continue
            try:
                out[name] = {"configured": True, **ArrClient(cfg, flavour).ping()}
            except ArrError as exc:
                out[name] = {"configured": True, "ok": False, "error": str(exc)}
        if s.emby.enabled and s.emby.url:
            try:
                out["emby"] = {"configured": True, **EmbyClient(s.emby).ping()}
            except EmbyError as exc:
                out["emby"] = {"configured": True, "ok": False, "error": str(exc)}
        else:
            out["emby"] = {"configured": False}
        state.services = out
        bus.publish("services", out)
        return out


service = Service()
