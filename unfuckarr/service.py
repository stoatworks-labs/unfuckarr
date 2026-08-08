"""The long-lived service: scheduler, watch-folder wiring, single-scan lock.

Everything here runs on background threads. The web layer only ever calls into
this module — it never touches the scanner directly — so "is a scan already
running" has exactly one answer.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import config, db, recycle
from .clients.arr import ArrClient, ArrError
from .clients.emby import EmbyClient, EmbyError
from .config import Settings, WatchFolder
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

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        db.init()
        config.load()
        self.watcher.start()
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(target=self._scheduler, daemon=True,
                                              name="unfuckarr-scheduler")
        self._sched_thread.start()
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
        result, info = check_file(path, s,
                                  expected_runtime=record.get("expected_runtime"),
                                  emby=emby)
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
        result, info = check_file(path, s,
                                  expected_runtime=record.get("expected_runtime"))
        return self.remediator.apply(
            record, result, info,
            Decision(action, f"{action} requested from the web UI"),
        )

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
