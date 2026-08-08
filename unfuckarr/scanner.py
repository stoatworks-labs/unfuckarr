"""Library enumeration, the per-file check, and the scan loop.

The scan is deliberately conservative about acting. Two brakes exist because
the failure mode that matters is not "missed a broken file" — it is "the array
was unmounted at 3am and unfuckarr deleted the library":

* ``max_actions_per_scan`` caps how many files one pass may touch.
* ``abort_if_failure_ratio_over`` stops the pass entirely when most of the
  library suddenly looks broken, which is what a missing mount looks like.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from . import db, recycle
from .checks import CheckResult, Finding
from .checks import compat as compat_checks
from .checks import hygiene as hygiene_checks
from .checks import integrity as integrity_checks
from .clients.arr import ArrClient, ArrError
from .clients.emby import EmbyClient, EmbyError
from .config import VIDEO_EXTENSIONS, Settings
from .probe import MediaInfo, ProbeError, probe
from .remediation import Decision, Remediator, decide
from .state import ScanProgress, bus, clear_task, publish_scan, set_task, state

log = logging.getLogger(__name__)


# -- enumeration ----------------------------------------------------------

def walk_video_files(root: str) -> Iterable[str]:
    """Every video file under ``root``, skipping the noise.

    ``.grab``/``incomplete`` hold partial downloads, and Emby/Plex metadata
    directories hold trailers and theme videos that are not library items.
    """
    skip_dirs = {"@eaDir", ".grab", "incomplete", "extrafanart", "extrathumbs",
                 ".unfuckarr", "lost+found", ".Trash", ".recycle", "recycle"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if d not in skip_dirs and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                yield os.path.join(dirpath, name)


def collect_library(s: Settings) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the file inventory from the *arrs plus any extra roots.

    Returns the rows and a list of problems worth showing in the UI rather
    than raising — one unreachable Radarr should not stop a Sonarr sweep.
    """
    rows: dict[str, dict[str, Any]] = {}
    problems: list[str] = []

    for cfg, flavour in ((s.sonarr, "sonarr"), (s.radarr, "radarr")):
        if not cfg.enabled or not cfg.url:
            continue
        try:
            for item in ArrClient(cfg, flavour).library():
                rows[item["path"]] = item
        except ArrError as exc:
            problems.append(f"{flavour}: {exc}")
            log.warning("%s enumeration failed: %s", flavour, exc)

    for root in s.extra_library_paths:
        if not root or not os.path.isdir(root):
            if root:
                problems.append(f"library path not found: {root}")
            continue
        for path in walk_video_files(root):
            rows.setdefault(path, {
                "path": path, "arr_id": None, "arr_parent_id": None,
                "arr_episode_ids": None, "title": Path(path).stem,
                "expected_runtime": 0, "size": 0, "quality": "",
                "source": "folder", "library": Path(root).name or root,
            })

    return list(rows.values()), problems


def sync_inventory(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Upsert the inventory. Returns (seen, dropped)."""
    conn = db.connect()
    seen = 0
    with conn:
        for r in rows:
            try:
                stat = os.stat(r["path"])
                size, mtime = stat.st_size, stat.st_mtime
            except OSError:
                size, mtime = r.get("size") or 0, 0.0
            conn.execute(
                """INSERT INTO files
                     (path, library, source, arr_id, arr_parent_id, arr_episode_ids,
                      title, size, mtime, expected_runtime)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     library=excluded.library, source=excluded.source,
                     arr_id=excluded.arr_id, arr_parent_id=excluded.arr_parent_id,
                     arr_episode_ids=excluded.arr_episode_ids,
                     title=excluded.title, size=excluded.size, mtime=excluded.mtime,
                     expected_runtime=excluded.expected_runtime""",
                (r["path"], r.get("library"), r.get("source"), r.get("arr_id"),
                 r.get("arr_parent_id"),
                 json.dumps(r["arr_episode_ids"]) if r.get("arr_episode_ids") else None,
                 r.get("title"), size, mtime, r.get("expected_runtime") or 0),
            )
            seen += 1

    # Files the *arrs no longer list have been deleted or moved elsewhere.
    # Drop them so the dashboard counts stay honest.
    known = {r["path"] for r in rows}
    dropped = 0
    for row in db.q("SELECT path FROM files"):
        if row["path"] not in known:
            db.ex("DELETE FROM files WHERE path=?", (row["path"],))
            db.ex("DELETE FROM findings WHERE path=?", (row["path"],))
            dropped += 1
    return seen, dropped


# -- per-file check -------------------------------------------------------

def check_file(path: str, s: Settings, expected_runtime: int | None = None,
               emby: EmbyClient | None = None) -> tuple[CheckResult, MediaInfo | None]:
    """Run every enabled check against one file."""
    result, info = integrity_checks.check(
        path, s.integrity, ffprobe=s.ffprobe_path, ffmpeg=s.ffmpeg_path,
        expected_runtime=expected_runtime,
    )

    if info is None:
        # ffprobe never got a look, so compat and hygiene have nothing to say.
        if not result.findings and not result.error:
            result.error = "no media information"
        return result, None

    result.probe = info.summary()

    # Emby's own verdict is authoritative; when it answers, the local codec
    # model is skipped so the two cannot contradict each other in the UI.
    answered = False
    if emby is not None and s.emby.enabled and s.emby.use_playback_info:
        try:
            answered = emby.check_direct_play(path, s.emby_compat, result)
        except EmbyError as exc:
            result.add(Finding("emby", "emby_unreachable", "info", str(exc)))
    if not answered:
        compat_checks.check(info, s.emby_compat, result)

    hygiene_checks.check(info, s.hygiene, result)
    return result, info


def persist_result(path: str, result: CheckResult, info: MediaInfo | None,
                   size: int, mtime: float) -> None:
    now = time.time()
    db.ex("UPDATE files SET status=?, last_checked=?, last_result=?, probe=?, "
          "checked_signature=? WHERE path=?",
          (result.status, now, json.dumps(result.as_dict()),
           json.dumps(info.summary()) if info else None,
           f"{size}:{mtime}", path))
    # Findings are replaced wholesale each pass; the previous set is resolved
    # rather than deleted so the activity view can show what changed.
    db.ex("UPDATE findings SET resolved=? WHERE path=? AND resolved IS NULL",
          (now, path))
    for f in result.findings:
        db.ex("INSERT INTO findings (path, category, code, severity, detail, created) "
              "VALUES (?,?,?,?,?,?)",
              (path, f.category, f.code, f.severity, f.detail, now))


def needs_check(row: Any, s: Settings) -> bool:
    """Whether this file is worth probing again this pass."""
    if not s.schedule.skip_unchanged:
        return True
    if row["last_checked"] is None:
        return True
    try:
        stat = os.stat(row["path"])
    except OSError:
        return True                      # gone or unreadable — worth reporting
    signature = f"{stat.st_size}:{stat.st_mtime}"
    if row["checked_signature"] != signature:
        return True
    if row["status"] not in ("ok",):
        return True                      # keep re-reporting a known-bad file
    age_days = (time.time() - row["last_checked"]) / 86400
    return age_days >= s.schedule.recheck_after_days


# -- the scan -------------------------------------------------------------

class Scanner:
    def __init__(self, settings_getter, remediator: Remediator):
        self._settings = settings_getter
        self.remediator = remediator
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self, trigger: str = "manual", paths: list[str] | None = None) -> dict[str, Any]:
        s = self._settings()
        self._stop = False
        scan_id = db.ex("INSERT INTO scans (started, trigger) VALUES (?,?)",
                        (time.time(), trigger))
        state.scan = ScanProgress(running=True, scan_id=scan_id, trigger=trigger,
                                  started=time.time())
        publish_scan()
        db.log("scan_started", "info", detail={"trigger": trigger})

        try:
            return self._run_inner(s, scan_id, paths)
        finally:
            state.scan.running = False
            state.scan.current = ""
            state.last_scan_finished = time.time()
            publish_scan()
            clear_task("scan")
            db.ex("UPDATE scans SET finished=?, total=?, checked=?, ok=?, failed=?, "
                  "actions=?, aborted=? WHERE id=?",
                  (time.time(), state.scan.total, state.scan.checked, state.scan.ok,
                   state.scan.failed, state.scan.actions, state.scan.aborted, scan_id))
            db.prune()

    def _run_inner(self, s: Settings, scan_id: int,
                   paths: list[str] | None) -> dict[str, Any]:
        if paths is None:
            set_task("scan", kind="scanning", detail="enumerating libraries",
                     progress=-1, started=time.time())
            rows, problems = collect_library(s)
            for p in problems:
                db.log("enumeration_problem", "warn", detail=p)
            if not rows and problems:
                # Nothing enumerated and every service errored: acting now
                # would be acting on stale inventory.
                state.scan.aborted = "no library could be enumerated"
                db.log("scan_aborted", "error", detail=state.scan.aborted)
                return {"aborted": state.scan.aborted}
            sync_inventory(rows)
            candidates = db.q("SELECT * FROM files ORDER BY library, path")
        else:
            placeholders = ",".join("?" for _ in paths)
            candidates = db.q(
                f"SELECT * FROM files WHERE path IN ({placeholders})", paths)

        todo = [r for r in candidates if needs_check(r, s)]
        state.scan.total = len(todo)
        publish_scan()

        emby = EmbyClient(s.emby) if s.emby.enabled else None
        if emby is not None:
            try:
                emby.build_index()
            except EmbyError as exc:
                db.log("emby_index_failed", "warn", detail=str(exc))
                emby = None

        pending: list[tuple[dict[str, Any], CheckResult, MediaInfo | None, Decision]] = []
        workers = max(1, s.schedule.max_concurrent_probes)

        # Probing is I/O and subprocess bound, so threads are the right tool;
        # remediation is applied on this thread afterwards so the action cap
        # and abort ratio are evaluated against the whole pass.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for row, result, info in pool.map(
                lambda r: self._check_one(r, s, emby), todo
            ):
                if self._stop:
                    state.scan.aborted = "stopped by user"
                    break
                state.scan.checked += 1
                if result.status == "ok":
                    state.scan.ok += 1
                else:
                    state.scan.failed += 1
                state.scan.current = row["path"]
                publish_scan()

                decision = decide(result, s)
                if decision.action not in ("none",):
                    pending.append((row, result, info, decision))

        if state.scan.aborted:
            return {"aborted": state.scan.aborted}

        return self._remediate(s, pending)

    def _check_one(self, row: Any, s: Settings,
                   emby: EmbyClient | None) -> tuple[dict[str, Any], CheckResult, MediaInfo | None]:
        path = row["path"]
        set_task("scan", kind="probing", path=path,
                 title=row["title"] or Path(path).name,
                 detail=f"{state.scan.checked + 1}/{state.scan.total}",
                 progress=(state.scan.checked / state.scan.total)
                 if state.scan.total else -1)
        try:
            result, info = check_file(
                path, s, expected_runtime=row["expected_runtime"], emby=emby)
        except ProbeError as exc:
            result, info = CheckResult(path=path, error=str(exc)), None
        except Exception as exc:  # noqa: BLE001
            log.exception("check failed for %s", path)
            result, info = CheckResult(path=path, error=str(exc)), None

        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        persist_result(path, result, info, size, mtime)

        record = {k: row[k] for k in row.keys()}
        if record.get("arr_episode_ids"):
            try:
                record["arr_episode_ids"] = json.loads(record["arr_episode_ids"])
            except (TypeError, json.JSONDecodeError):
                record["arr_episode_ids"] = None
        return record, result, info

    def _remediate(self, s: Settings,
                   pending: list[tuple[dict[str, Any], CheckResult,
                                       MediaInfo | None, Decision]]) -> dict[str, Any]:
        checked = max(1, state.scan.checked)
        destructive = [p for p in pending
                       if p[3].action in ("redownload", "transcode", "repair")]
        ratio = len(destructive) / checked

        if ratio > s.policy.abort_if_failure_ratio_over and len(destructive) > 3:
            state.scan.aborted = (
                f"{len(destructive)} of {checked} files failed "
                f"({ratio:.0%}) — that is a mount or configuration problem, "
                "not a media problem. Nothing was changed."
            )
            db.log("scan_aborted", "error", detail=state.scan.aborted)
            publish_scan()
            return {"aborted": state.scan.aborted, "flagged": len(pending)}

        applied = 0
        for row, result, info, decision in pending:
            if self._stop:
                break
            if decision.action != "flag" and applied >= s.policy.max_actions_per_scan:
                db.log("action_cap_reached", "warn", row["path"],
                       {"cap": s.policy.max_actions_per_scan})
                break
            outcome = self.remediator.apply(row, result, info, decision)
            if decision.action != "flag":
                applied += 1
                state.scan.actions = applied
                publish_scan()
            bus.publish("remediated", {"path": row["path"], **outcome})

        recycle.sweep(s.policy.recycle_bin_days)
        db.log("scan_finished", "info", detail={
            "checked": state.scan.checked, "failed": state.scan.failed,
            "actions": applied,
        })
        return {"checked": state.scan.checked, "failed": state.scan.failed,
                "actions": applied}
