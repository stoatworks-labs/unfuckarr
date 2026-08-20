"""Deciding and carrying out what happens to a bad file.

The decision is separate from the action so the policy can be tested without a
filesystem, and so the UI can show what *would* happen when a check is set to
flag-only.

Order of preference throughout: fix the file in place if we can, and only fall
back to deleting it and asking the *arr for another copy when we cannot. A
redownload costs the user bandwidth and may return something worse.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, governor, quality, recycle, transcode
from .checks import CheckResult
from .checks.compat import resolve as resolve_profile
from .checks.integrity import looks_repairable
from .clients.arr import ArrClient, ArrError
from .config import Settings
from .probe import MediaInfo, ProbeError, probe
from .state import bus, clear_task, set_task

log = logging.getLogger(__name__)

Action = str  # none | flag | transcode | repair | shrink | redownload

# A transcode that does not clear the finding would otherwise be repeated on
# every scan for ever. Two goes, then the file is flagged and left alone.
MAX_FIX_ATTEMPTS = 2

# A shrink gets one attempt, not two. Nothing is wrong with the file, so a
# failure costs nothing to leave alone — and the failure modes here (the search
# cannot reach the target, the saving is not there) are properties of the
# content and will be just as true next time, for another few hours of CPU.
MAX_SHRINK_ATTEMPTS = 1


@dataclass
class Decision:
    action: Action
    reason: str
    findings: list[str] = field(default_factory=list)
    # Set only by an explicit request from the UI. It reopens a file the
    # shrink search has already written off — the settings may have changed
    # since — but it never overrides the "already shrunk once" refusal, which
    # is about generation loss and does not become untrue.
    force: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "reason": self.reason,
                "findings": self.findings}


def decide(result: CheckResult, settings: Settings) -> Decision:
    """Map a check result onto an action, honouring the configured policy."""
    policy = settings.policy
    errors = result.errors
    codes = [f.code for f in errors]

    if result.error and not errors:
        return Decision("none", f"check did not complete: {result.error}")

    integrity = [f for f in errors if f.category == "integrity"]
    compat = [f for f in errors if f.category in ("compat", "emby")]
    # Efficiency findings are warnings, but they are not hygiene: a large file
    # is not untidy metadata, and `hygiene_action` must never be what decides
    # to re-encode one.
    warnings = [f for f in result.findings
                if f.severity == "warning" and f.category != "efficiency"]
    # `not_measured` is info severity, not a warning: there is nothing wrong
    # with the file. It is the *absence of an answer* that drives the action.
    unmeasured = result.unmeasured

    if integrity:
        action = policy.corrupt_action
        if action == "redownload" and policy.try_repair_before_redownload \
                and looks_repairable(result):
            return Decision(
                "repair",
                "container damage only — trying a remux before re-downloading",
                codes,
            )
        if action == "transcode":
            return Decision("repair", "corrupt; remuxing", codes)
        return Decision(action, "file is corrupt", codes)

    if compat:
        action = policy.incompatible_action
        return Decision(action, "Emby cannot direct play this", codes)

    # Before hygiene: a shrink re-encodes the whole file and carries the
    # hygiene fixes with it, so letting a flag-only hygiene finding answer
    # first would mask it. When shrinking is not what happens, this falls
    # through and hygiene decides as it always did.
    if unmeasured and policy.oversize_action == "shrink":
        blocked = shrink_blocked(settings)
        if blocked is None:
            return Decision("shrink",
                            "measuring how small this can be at full "
                            "perceptual quality",
                            [f.code for f in unmeasured])
        log.debug("not shrinking %s: %s", result.path, blocked)

    if warnings:
        action = policy.hygiene_action
        if action == "transcode" and _is_disc(result):
            # Hygiene findings on a disc image are real — a Blu-ray playlist
            # genuinely has no language tags and no default audio track — but
            # the cheap fix is not available. Tidying metadata on an ordinary
            # file is a remux measured in seconds; on a 90 GB disc image it is
            # a full conversion of the whole disc, and doing that to fix a
            # language tag is a bad trade nobody asked for. Flag it, and let
            # the shrink path be what converts a disc, since that at least
            # reclaims the space to pay for the work.
            return Decision("flag",
                            "stream metadata needs tidying, but this is a disc "
                            "image and rewriting one to fix tags is not worth "
                            "the work — shrinking it would fix both",
                            [f.code for f in warnings])
        if action != "none":
            return Decision(action, "stream metadata needs tidying",
                            [f.code for f in warnings])

    if unmeasured and policy.oversize_action != "none":
        return Decision("flag", "not measured for a saving",
                        [f.code for f in unmeasured])

    return Decision("none", "file is fine")


def _is_disc(result: CheckResult) -> bool:
    """Whether the checked file was a disc image.

    Read off the stored probe summary rather than plumbing MediaInfo into
    `decide`, which is deliberately a pure function of the result and the
    policy so it can be tested without a filesystem.
    """
    probe = result.probe or {}
    return bool(probe.get("disc"))


def shrink_blocked(settings: Settings) -> str | None:
    """Why a shrink cannot go ahead, or None when it can.

    Separate from ``decide`` so the settings page and the file drawer can say
    *why* nothing is being shrunk, which is otherwise invisible: the finding is
    raised, the policy says shrink, and nothing happens.
    """
    if not settings.shrink.enabled:
        return "shrinking is switched off"
    if not settings.transcode.enabled:
        return "transcoding is switched off, and a shrink is a transcode"
    if not settings.transcode.replace_original:
        # `replace_original` off means "leave the new file beside the old
        # one", which for a repair is a reasonable way to keep a human in the
        # loop. For a shrink it is self-defeating: the point is to use less
        # space, and two copies uses more. Refuse rather than quietly ignore
        # the setting and swap the file out anyway.
        return ("replace_original is off, and a shrink that leaves both files "
                "in place uses more space than it saves")
    codec = settings.shrink.codec
    profile = resolve_profile(settings.emby_compat)
    if settings.emby_compat.enabled and codec not in profile.video:
        # Shrinking into a codec the library's own target profile rejects
        # trades a size win for a file Emby has to transcode on every play,
        # which the compat check would then, correctly, flag as a fault.
        return (f"{codec} is not in the target Emby profile "
                f"({', '.join(sorted(profile.video))}) — shrinking into it "
                "would make the file incompatible")
    return None


def _window_wait(window: str) -> str | None:
    """None when now is inside the configured shrink window."""
    if not window:
        return None
    try:
        start_s, _, end_s = window.partition("-")
        start, end = int(start_s), int(end_s)
    except ValueError:
        return None
    hour = time.localtime().tm_hour
    inside = start <= hour < end if start < end else (hour >= start or hour < end)
    if inside:
        return None
    return f"outside the {window} shrink window (it is {hour:02d}:00)"


class Remediator:
    """Carries out decisions. One instance, shared by the scanner and watcher."""

    def __init__(self, settings_getter):
        self._settings = settings_getter
        self._transcode_sema: threading.Semaphore | None = None
        self._sema_size = 0
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _semaphore(self, size: int) -> threading.Semaphore:
        with self._lock:
            if self._transcode_sema is None or self._sema_size != size:
                self._transcode_sema = threading.Semaphore(size)
                self._sema_size = size
            return self._transcode_sema

    def cancel(self, path: str) -> bool:
        ev = self._cancel.get(path)
        if ev is None:
            return False
        ev.set()
        return True

    # -- entry point ------------------------------------------------------

    def apply(self, file_row: dict[str, Any], result: CheckResult,
              info: MediaInfo | None, decision: Decision) -> dict[str, Any]:
        path = file_row["path"]
        if transcode.is_temp_output(path):
            # Our own in-flight output, reached through a watch event, a scan,
            # or the UI before the owning job renamed it into place. Acting on
            # it means racing that job for the file.
            db.log("own_temp_skipped", "info", path, decision.reason)
            return {"action": "none", "ok": True,
                    "message": "unfuckarr's own temporary output — not touched"}
        job_id = db.ex(
            "INSERT INTO jobs (kind, path, state, message, created) VALUES (?,?,?,?,?)",
            (decision.action, path, "queued", decision.reason, time.time()),
        )
        try:
            if decision.action in ("none", "flag"):
                self._set_job(job_id, "done", 1.0, decision.reason)
                return {"action": decision.action, "ok": True,
                        "message": decision.reason}
            if decision.action in ("transcode", "repair"):
                return self._transcode(job_id, file_row, result, info, decision)
            if decision.action == "shrink":
                return self._shrink(job_id, file_row, result, info, decision)
            if decision.action == "redownload":
                return self._redownload(job_id, file_row, decision.reason)
            self._set_job(job_id, "failed", 0, f"unknown action {decision.action}")
            return {"action": decision.action, "ok": False,
                    "message": "unknown action"}
        except Exception as exc:  # noqa: BLE001 - a worker must not die
            log.exception("remediation failed for %s", path)
            self._set_job(job_id, "failed", 0, str(exc), error=str(exc))
            db.log("remediation_failed", "error", path, str(exc))
            return {"action": decision.action, "ok": False, "message": str(exc)}
        finally:
            clear_task(f"remediate:{path}")

    def _set_job(self, job_id: int, state: str, progress: float,
                 message: str, error: str | None = None) -> None:
        now = time.time()
        if state == "running":
            db.ex("UPDATE jobs SET state=?, progress=?, message=?, started=? WHERE id=?",
                  (state, progress, message, now, job_id))
        elif state in ("done", "failed", "cancelled"):
            db.ex("UPDATE jobs SET state=?, progress=?, message=?, finished=?, error=? "
                  "WHERE id=?", (state, progress, message, now, error, job_id))
        else:
            db.ex("UPDATE jobs SET state=?, progress=?, message=? WHERE id=?",
                  (state, progress, message, job_id))
        bus.publish("job", {"id": job_id, "state": state, "progress": progress,
                            "message": message})

    # -- transcode / repair ----------------------------------------------

    def _transcode(self, job_id: int, file_row: dict[str, Any],
                   result: CheckResult, info: MediaInfo | None,
                   decision: Decision) -> dict[str, Any]:
        s = self._settings()
        path = file_row["path"]
        if not s.transcode.enabled:
            self._set_job(job_id, "done", 1.0, "transcoding is disabled — flagged only")
            return {"action": "flag", "ok": True, "message": "transcoding disabled"}

        attempts = file_row.get("fix_attempts") or 0
        if attempts >= MAX_FIX_ATTEMPTS:
            msg = (f"already transcoded {attempts} time(s) without clearing the "
                   "problem — not trying again")
            self._set_job(job_id, "done", 1.0, msg)
            return {"action": "flag", "ok": True, "message": msg}

        if info is None:
            try:
                info = probe(path, s.ffprobe_path)
            except ProbeError as exc:
                # Cannot plan without stream info; a corrupt file that ffprobe
                # will not read is a redownload, not a transcode.
                self._set_job(job_id, "failed", 0, f"cannot probe: {exc}")
                return self._redownload_row(file_row, f"unprobeable: {exc}")

        repair = decision.action == "repair"
        plan = transcode.plan(info, result, s.transcode, s.emby_compat, repair=repair)
        dst = transcode.output_path(path, plan.container)

        if not transcode.free_space_ok(dst, info.size or file_row.get("size") or 0):
            msg = "not enough free space for the output"
            self._set_job(job_id, "failed", 0, msg)
            db.log("transcode_skipped", "warn", path, msg)
            return {"action": "flag", "ok": False, "message": msg}

        cmd = transcode.build_command(path, dst, info, plan, s.transcode,
                                      ffmpeg=s.ffmpeg_path)
        cancel = threading.Event()
        self._cancel[path] = cancel

        sema = self._semaphore(max(1, s.transcode.max_concurrent))
        with sema:
            self._set_job(job_id, "running", 0.0, plan.describe)
            set_task(f"remediate:{path}", kind="transcoding", path=path,
                     title=file_row.get("title") or Path(path).name,
                     detail=plan.describe, progress=0.0, started=time.time())
            db.log("transcode_started", "info", path,
                   {"plan": plan.describe, "reason": decision.reason})

            def on_progress(frac: float, eta: float | None) -> None:
                set_task(f"remediate:{path}", progress=frac, eta=eta)
                db.ex("UPDATE jobs SET progress=? WHERE id=?", (frac, job_id))

            ok, message = transcode.run(
                cmd, info.duration, on_progress=on_progress,
                stall_timeout=s.transcode.stall_timeout_seconds,
                nice_level=s.transcode.nice_level, cancel=cancel,
            )
            self._cancel.pop(path, None)

        if not ok:
            Path(dst).unlink(missing_ok=True)
            self._set_job(job_id, "failed", 0, message, error=message)
            detail: dict[str, Any] = {"message": message}
            if not cancel.is_set():
                # A cancel says nothing about the file. Anything else counts
                # against the cap — a failure that repeats deterministically
                # would otherwise be retried on every scan, for ever.
                detail["attempts"] = self._count_attempt(path, file_row)
            db.log("transcode_failed", "error", path, detail)
            # A repair that failed means the damage is real. Fall through to a
            # redownload rather than leaving a broken file flagged as "tried".
            if repair and s.policy.corrupt_action == "redownload":
                return self._redownload_row(file_row, f"remux failed: {message}")
            return {"action": "flag", "ok": False, "message": message}

        # Verify the output before letting it replace anything. An ffmpeg that
        # exits 0 having written a file with no video stream is rare but real.
        verify = self._verify_output(dst, info, s)
        if verify is not None:
            Path(dst).unlink(missing_ok=True)
            self._set_job(job_id, "failed", 0, f"output failed verification: {verify}")
            db.log("transcode_output_bad", "error", path,
                   {"message": verify,
                    "attempts": self._count_attempt(path, file_row)})
            if repair and s.policy.corrupt_action == "redownload":
                return self._redownload_row(file_row, f"remux produced a bad file: {verify}")
            return {"action": "flag", "ok": False, "message": verify}

        final = dst
        if s.transcode.replace_original:
            try:
                recycled = recycle.store(
                    path, f"replaced by transcode ({plan.describe})",
                    s.policy.recycle_bin_path, s.policy.recycle_bin_days)
            except OSError as exc:
                Path(dst).unlink(missing_ok=True)
                msg = f"could not recycle the original: {exc}"
                self._set_job(job_id, "failed", 0, msg, error=msg)
                return {"action": "flag", "ok": False, "message": msg}
            try:
                final = transcode.replace(path, dst)
            except OSError as exc:
                # The verified output vanished before the swap — something
                # else (an *arr import, another process) took it.
                self._restore_original(path, recycled)
                msg = f"output disappeared before it could replace the original: {exc}"
                self._set_job(job_id, "failed", 0, msg, error=msg)
                db.log("transcode_output_bad", "error", path,
                       {"message": msg,
                        "attempts": self._count_attempt(path, file_row)})
                return {"action": "flag", "ok": False, "message": msg}
            self._move_db_row(path, final)
            self._notify_arr_rescan(file_row)
            self._confirm_fixed(path, final, file_row, decision)

        self._set_job(job_id, "done", 1.0, f"{plan.describe} → {Path(final).name}")
        db.log("transcode_done", "info", final,
               {"from": path, "plan": plan.describe, "message": message})
        return {"action": "transcode", "ok": True, "path": final,
                "message": plan.describe}

    def _verify_output(self, dst: str, source: MediaInfo,
                       s: Settings) -> str | None:
        """Returns a failure reason, or None when the output is good."""
        try:
            out = probe(dst, s.ffprobe_path)
        except ProbeError as exc:
            return f"output is unprobeable: {exc}"
        if out.video is None:
            return "output has no video stream"
        src_v = source.video
        if src_v is not None and (out.video.width, out.video.height) != \
                (src_v.width, src_v.height):
            # Nothing here ever asks for a resolution change, so one is a
            # defect. `hevc_vaapi` on Mesa/AMD pads 1080 to the 1088 CTB
            # boundary and does not signal a conformance window, so the
            # output decodes eight rows taller than the source with padding
            # at the bottom — and the container metadata still says 1080,
            # which is why ffprobe does not give it away. Verified by
            # decoding a frame and counting bytes: 3,133,440 against the
            # source's 3,110,400.
            return (f"output is {out.video.width}x{out.video.height} against a "
                    f"{src_v.width}x{src_v.height} source — the encoder changed "
                    "the picture size, which nothing asked it to do")
        if not out.audio and source.audio:
            return "output lost its audio"
        if source.duration > 0:
            drift = abs(out.duration - source.duration) / source.duration
            # 2% covers container rounding and a trimmed trailing GOP.
            if drift > 0.02:
                return (f"output is {out.duration:.0f}s against a "
                        f"{source.duration:.0f}s source")
        # Only a floor for "ffmpeg wrote a header and nothing else". The
        # duration check above is what actually proves the content is there;
        # a legitimate transcode can be many times smaller than its source.
        if out.size < 64 * 1024:
            return f"output is only {out.size} bytes"
        return None

    def _count_attempt(self, path: str, file_row: dict[str, Any]) -> int:
        """One more transcode that did not leave a verified fix behind.

        The counter is the brake: at MAX_FIX_ATTEMPTS `_transcode` refuses to
        run again. Every path that ends a transcode without a confirmed fix
        must come through here — an attempt that is never counted is retried
        on every scan, for ever.
        """
        attempts = (file_row.get("fix_attempts") or 0) + 1
        file_row["fix_attempts"] = attempts
        db.ex("UPDATE files SET fix_attempts=? WHERE path=?", (attempts, path))
        return attempts

    def _confirm_fixed(self, old: str, final: str, file_row: dict[str, Any],
                       decision: Decision) -> None:
        """Re-check the replacement and record the result.

        Two reasons this is not optional. The UI would otherwise show a file
        as "not checked" immediately after a successful transcode, and — more
        importantly — a transcode that does *not* clear the finding would be
        re-transcoded on every subsequent scan, for ever, with nothing in the
        log to say why. Surfacing that here turns an infinite loop into one
        warning.
        """
        # Imported here: scanner imports this module, so a top-level import
        # would be circular.
        from .scanner import check_file, persist_result

        s = self._settings()
        try:
            result, info = check_file(
                final, s, expected_runtime=file_row.get("expected_runtime"))
            stat = os.stat(final)
            persist_result(final, result, info, stat.st_size, stat.st_mtime)
        except Exception as exc:  # noqa: BLE001 - never fail the job over this
            log.warning("post-transcode check failed for %s: %s", final, exc)
            return

        # "hygiene" is only success when hygiene was not what we were fixing.
        # A hygiene-triggered remux whose warnings survive has fixed nothing,
        # and calling it fixed is exactly the infinite loop this method exists
        # to prevent.
        still_open = set(decision.findings) & {f.code for f in result.findings}
        if result.status in ("ok", "hygiene") and not still_open:
            return

        attempts = self._count_attempt(final, file_row)
        db.log("transcode_did_not_fix", "warn", final, {
            "was": old,
            "status": result.status,
            "findings": sorted({f.code for f in result.errors} | still_open),
            "attempts": attempts,
            "note": ("giving up on transcoding this file" if attempts >= MAX_FIX_ATTEMPTS
                     else "will be retried on the next scan"),
        })

    def _restore_original(self, path: str, recycled: str | None) -> None:
        """Undo the recycle when the swap could not go through."""
        if not recycled or os.path.exists(path):
            return
        row = db.q1("SELECT id FROM recycle WHERE stored=? ORDER BY id DESC LIMIT 1",
                    (recycled,))
        if row is None:
            return
        try:
            recycle.restore(row["id"])
        except (OSError, FileNotFoundError) as exc:
            log.error("could not put %s back after a failed swap: %s", path, exc)

    def _move_db_row(self, old: str, new: str) -> None:
        if old == new:
            db.ex("UPDATE files SET status='unknown', last_checked=NULL WHERE path=?",
                  (old,))
            return
        db.ex("UPDATE files SET path=?, status='unknown', last_checked=NULL "
              "WHERE path=?", (new, old))
        db.ex("UPDATE findings SET path=? WHERE path=?", (new, old))

    def _notify_arr_rescan(self, file_row: dict[str, Any]) -> None:
        """The *arr still has the old size and quality on record."""
        client = self._arr_for(file_row)
        parent = file_row.get("arr_parent_id")
        if client is None or not parent:
            return
        try:
            client.rescan(int(parent))
        except ArrError as exc:
            db.log("arr_rescan_failed", "warn", file_row["path"], str(exc))

    # -- shrink -----------------------------------------------------------

    def _shrink(self, job_id: int, file_row: dict[str, Any],
                result: CheckResult, info: MediaInfo | None,
                decision: Decision) -> dict[str, Any]:
        """Re-encode an intact file to a measured quality target.

        The shape is the same as ``_transcode`` — plan, run, verify, recycle,
        replace — with two extra gates that exist only here, because here
        nothing is wrong with the file and the only justification for touching
        it is that the result is both smaller *and* indistinguishable:

        1. the quality search must find a CRF that meets the target, and its
           projected saving must clear ``min_saving_pct``, before a single
           frame of the real encode is run; and
        2. the finished file must actually be that much smaller *and* still
           score at the target when measured against the original.

        Anything short of both, the output is deleted and the original is left
        exactly as it was. That is the normal, expected outcome for a lot of
        files and is not a failure.
        """
        s = self._settings()
        path = file_row["path"]

        blocked = shrink_blocked(s)
        if blocked is not None:
            self._set_job(job_id, "done", 1.0, blocked)
            return {"action": "flag", "ok": True, "message": blocked}

        if file_row.get("shrunk"):
            msg = "already shrunk once — re-encoding an encode is a second "\
                  "generation of loss for a fraction of the saving"
            self._set_job(job_id, "done", 1.0, msg)
            return {"action": "flag", "ok": True, "message": msg}
        if file_row.get("shrink_skipped") and not decision.force:
            msg = f"already assessed and left alone: {file_row['shrink_skipped']}"
            self._set_job(job_id, "done", 1.0, msg)
            return {"action": "flag", "ok": True, "message": msg}
        if decision.force:
            db.ex("UPDATE files SET shrink_skipped=NULL, shrink_attempts=0 "
                  "WHERE path=?", (path,))
            file_row["shrink_skipped"] = None
            file_row["shrink_attempts"] = 0
        if (file_row.get("shrink_attempts") or 0) >= MAX_SHRINK_ATTEMPTS:
            msg = "a previous shrink of this file failed — not trying again"
            self._set_job(job_id, "done", 1.0, msg)
            return {"action": "flag", "ok": True, "message": msg}

        window = _window_wait(s.shrink.only_between_hours)
        if window is not None:
            # Deliberately not recorded as a skip: nothing has been decided
            # about this file, it is simply not the right time of day.
            self._set_job(job_id, "done", 1.0, window)
            return {"action": "flag", "ok": True, "message": window}

        if info is None:
            try:
                info = probe(path, s.ffprobe_path)
            except ProbeError as exc:
                msg = f"cannot probe: {exc}"
                self._set_job(job_id, "failed", 0, msg)
                return {"action": "flag", "ok": False, "message": msg}

        if info.is_hdr and not s.efficiency.allow_hdr:
            return self._skip_shrink(job_id, path,
                                     "HDR, and allow_hdr is off")

        metric = quality.resolve_metric(s.shrink, s.ffmpeg_path)
        if metric is None:
            # A configuration problem, not a property of the file: do not
            # write it off permanently, because installing an ffmpeg with
            # libvmaf should be enough to make it work on the next scan.
            msg = ("no quality metric available — neither libvmaf nor ssim "
                   "could be found, so there is no way to prove a re-encode "
                   "still looks like the original")
            self._set_job(job_id, "done", 1.0, msg)
            db.log("shrink_no_metric", "warn", path, msg)
            return {"action": "flag", "ok": True, "message": msg}

        cancel = threading.Event()
        self._cancel[path] = cancel
        sema = self._semaphore(max(1, s.transcode.max_concurrent))
        title = file_row.get("title") or Path(path).name

        with sema:
            self._set_job(job_id, "running", 0.0,
                          f"measuring how far {metric.name.upper()} allows")
            set_task(f"remediate:{path}", kind="analysing", path=path,
                     title=title, progress=-1, started=time.time(),
                     detail=f"searching CRF {s.shrink.crf_min}–{s.shrink.crf_max} "
                            f"for {metric.name.upper()} {metric.target:g}")
            db.log("shrink_search_started", "info", path, {
                "metric": metric.name, "target": metric.target,
                "size": info.size,
            })

            qplan = quality.search(info, s.shrink, s.transcode,
                                   ffmpeg=s.ffmpeg_path, metric=metric,
                                   cancel=cancel)
            if not qplan.ok:
                self._cancel.pop(path, None)
                if cancel.is_set():
                    self._set_job(job_id, "cancelled", 0, "cancelled")
                    return {"action": "flag", "ok": False, "message": "cancelled"}
                if qplan.error:
                    # The search could not be carried out — a broken encoder
                    # setting, a missing binary, a source too damaged to read.
                    # That says nothing conclusive about whether the file is
                    # worth shrinking, so it is not recorded as a verdict:
                    # `shrink_skipped` stays empty and fixing the cause makes
                    # the file a candidate again.
                    #
                    # The attempt is still counted, because the continuous
                    # worker picks the fattest candidate every time it looks —
                    # so a failure that repeats deterministically is an
                    # infinite loop on one file. Live, that was 330 identical
                    # failures on a damaged Matroska, one every 55 seconds,
                    # while the rest of the backlog waited.
                    attempts = self._count_shrink_attempt(path, file_row)
                    db.log("shrink_search_failed", "warn", path,
                           {**qplan.as_dict(), "attempts": attempts})
                    self._set_job(job_id, "failed", 0, qplan.reason,
                                  error=qplan.reason)
                    return {"action": "flag", "ok": False,
                            "message": qplan.reason}
                db.log("shrink_declined", "info", path, qplan.as_dict())
                return self._skip_shrink(job_id, path, qplan.reason)

            if qplan.saving_pct < s.shrink.min_saving_pct:
                self._cancel.pop(path, None)
                reason = (
                    f"only about {qplan.saving_pct:.0f}% smaller at "
                    f"{metric.name.upper()} {metric.target:g} "
                    f"({quality.human_size(info.size)} → "
                    f"{quality.human_size(qplan.projected_size)}), "
                    f"under the {s.shrink.min_saving_pct:g}% worth re-encoding for"
                )
                db.log("shrink_declined", "info", path,
                       {**qplan.as_dict(), "reason": reason})
                return self._skip_shrink(job_id, path, reason)

            # ffmpeg is rewriting every byte of this file anyway, so the
            # hygiene tidying rides along for free rather than being left for
            # a second full pass later.
            codes = {f.code for f in result.findings}
            default_audio = None
            if "no_default_audio" in codes and info.audio:
                best = max(info.audio, key=lambda a: (a.channels, -a.index))
                default_audio = info.audio.index(best)
            plan = transcode.TranscodePlan(
                reason="shrink",
                video_action="encode",
                audio_action="copy",
                container=s.transcode.container,
                codec=qplan.codec,
                crf=qplan.crf,
                pix_fmt=quality.pix_fmt_for(info),
                is_shrink=True,
                faststart=s.transcode.container == "mp4",
                drop_subtitles=transcode._subtitles_to_drop(
                    info, s.transcode.container),
                fix_language_tags=bool(codes & {"audio_missing_language",
                                                "subtitle_missing_language"}),
                set_default_audio=default_audio,
            )
            dst = transcode.output_path(path, plan.container)
            if not transcode.free_space_ok(dst, qplan.projected_size):
                self._cancel.pop(path, None)
                msg = "not enough free space for the output"
                self._set_job(job_id, "failed", 0, msg)
                db.log("shrink_skipped", "warn", path, msg)
                return {"action": "flag", "ok": False, "message": msg}

            cmd = transcode.build_command(path, dst, info, plan, s.transcode,
                                          ffmpeg=s.ffmpeg_path)
            self._set_job(job_id, "running", 0.0, plan.describe)
            set_task(f"remediate:{path}", kind="shrinking", detail=plan.describe,
                     progress=0.0)
            db.log("shrink_started", "info", path, {
                "plan": plan.describe, **qplan.as_dict(),
            })

            def on_progress(frac: float, eta: float | None) -> None:
                set_task(f"remediate:{path}", progress=frac, eta=eta)
                db.ex("UPDATE jobs SET progress=? WHERE id=?", (frac, job_id))

            gov = governor.Governor(target=s.shrink.gpu_encode_percent / 100)
            ok, message = transcode.run(
                cmd, info.duration, on_progress=on_progress,
                stall_timeout=s.transcode.stall_timeout_seconds,
                nice_level=s.transcode.nice_level, cancel=cancel,
                governor=gov,
            )
            self._cancel.pop(path, None)

        if not ok:
            Path(dst).unlink(missing_ok=True)
            self._set_job(job_id, "failed", 0, message, error=message)
            detail: dict[str, Any] = {"message": message}
            if not cancel.is_set():
                detail["attempts"] = self._count_shrink_attempt(path, file_row)
            db.log("shrink_failed", "error", path, detail)
            return {"action": "flag", "ok": False, "message": message}

        # The same structural verification a repair gets: the streams have to
        # still be there and the duration has to match.
        bad = self._verify_output(dst, info, s)
        if bad is not None:
            Path(dst).unlink(missing_ok=True)
            self._set_job(job_id, "failed", 0, f"output failed verification: {bad}")
            db.log("shrink_output_bad", "error", path, {
                "message": bad, "attempts": self._count_shrink_attempt(path, file_row),
            })
            return {"action": "flag", "ok": False, "message": bad}

        try:
            new_size = os.path.getsize(dst)
        except OSError as exc:
            Path(dst).unlink(missing_ok=True)
            self._set_job(job_id, "failed", 0, str(exc), error=str(exc))
            return {"action": "flag", "ok": False, "message": str(exc)}

        realised = (1 - new_size / info.size) * 100 if info.size else 0.0
        if realised < s.shrink.min_saving_pct:
            # The projection came from short samples; rate control over two
            # hours does not have to agree with it. When it does not, the
            # original wins — there is nothing wrong with it.
            Path(dst).unlink(missing_ok=True)
            reason = (f"finished only {realised:.0f}% smaller "
                      f"({quality.human_size(info.size)} → "
                      f"{quality.human_size(new_size)}), against a projected "
                      f"{qplan.saving_pct:.0f}% — original kept")
            db.log("shrink_declined", "warn", path,
                   {**qplan.as_dict(), "realised_pct": round(realised, 1),
                    "reason": reason})
            return self._skip_shrink(job_id, path, reason)

        set_task(f"remediate:{path}", kind="analysing", progress=-1,
                 detail="checking the result against the original")
        try:
            mean, worst = quality.verify(info.source, dst, info, s.shrink,
                                         metric, cancel=cancel,
                                         ffmpeg=s.ffmpeg_path)
        except quality.QualityError as exc:
            # Unable to prove the output is good is not the same as knowing it
            # is bad, but it is not a licence to replace the original either.
            Path(dst).unlink(missing_ok=True)
            msg = f"could not verify the result: {exc}"
            self._set_job(job_id, "failed", 0, msg, error=msg)
            db.log("shrink_output_bad", "error", path, {
                "message": msg, "attempts": self._count_shrink_attempt(path, file_row),
            })
            return {"action": "flag", "ok": False, "message": msg}

        if mean < metric.target or worst < metric.target - metric.tolerance:
            Path(dst).unlink(missing_ok=True)
            reason = (f"finished file measured {metric.name.upper()} "
                      f"{mean:.1f} (worst sample {worst:.1f}) against a target "
                      f"of {metric.target:g} — original kept")
            db.log("shrink_declined", "warn", path,
                   {**qplan.as_dict(), "measured": round(mean, 2),
                    "worst": round(worst, 2), "reason": reason})
            return self._skip_shrink(job_id, path, reason)

        # Only now is the original allowed to go.
        try:
            recycled = recycle.store(
                path, f"replaced by shrink ({plan.describe}, "
                      f"{metric.name.upper()} {mean:.1f})",
                s.policy.recycle_bin_path, s.policy.recycle_bin_days)
        except OSError as exc:
            Path(dst).unlink(missing_ok=True)
            msg = f"could not recycle the original: {exc}"
            self._set_job(job_id, "failed", 0, msg, error=msg)
            return {"action": "flag", "ok": False, "message": msg}

        try:
            final = transcode.replace(path, dst)
        except OSError as exc:
            self._restore_original(path, recycled)
            msg = f"output disappeared before it could replace the original: {exc}"
            self._set_job(job_id, "failed", 0, msg, error=msg)
            return {"action": "flag", "ok": False, "message": msg}

        self._move_db_row(path, final)
        # Written before the re-check, so that the re-check — and every scan
        # after it — already knows this file is finished with.
        db.ex("UPDATE files SET shrunk=?, shrunk_from=?, shrink_score=?, "
              "shrink_metric=?, shrink_skipped=NULL WHERE path=?",
              (time.time(), info.size, mean, metric.name, final))
        self._notify_arr_rescan(file_row)
        self._recheck_after_shrink(final, file_row)

        saved = info.size - new_size
        summary = (f"{quality.human_size(info.size)} → "
                   f"{quality.human_size(new_size)} "
                   f"({realised:.0f}% smaller), {metric.name.upper()} "
                   f"{mean:.1f} at CRF {qplan.crf}")
        self._set_job(job_id, "done", 1.0, summary)
        db.log("shrink_done", "info", final, {
            "from": path, "saved": saved, "realised_pct": round(realised, 1),
            "crf": qplan.crf, "metric": metric.name, "score": round(mean, 2),
            "worst": round(worst, 2), "estimated_metric": metric.is_estimate,
            "search_seconds": round(qplan.seconds),
        })
        return {"action": "shrink", "ok": True, "path": final,
                "saved": saved, "message": summary}

    def _skip_shrink(self, job_id: int, path: str, reason: str) -> dict[str, Any]:
        """Record, permanently, that this file is not worth shrinking.

        Permanent because the reasons are properties of the content — this
        much grain does not compress, this file is already efficient — and
        they will be just as true on the next scan, after another few hours of
        CPU spent finding that out again.
        """
        db.ex("UPDATE files SET shrink_skipped=? WHERE path=?", (reason, path))
        self._set_job(job_id, "done", 1.0, reason)
        return {"action": "flag", "ok": True, "message": reason}

    def _count_shrink_attempt(self, path: str, file_row: dict[str, Any]) -> int:
        attempts = (file_row.get("shrink_attempts") or 0) + 1
        file_row["shrink_attempts"] = attempts
        db.ex("UPDATE files SET shrink_attempts=? WHERE path=?", (attempts, path))
        return attempts

    def _recheck_after_shrink(self, final: str, file_row: dict[str, Any]) -> None:
        """Re-check the replacement so the UI is not left showing 'unknown'.

        Unlike ``_confirm_fixed`` there is nothing here to confirm — the
        quality and the saving were both measured before the swap, and the
        `shrunk` marker already guarantees the file is never revisited. This
        exists purely so the file list is honest immediately afterwards.
        """
        from .scanner import check_file, persist_result

        s = self._settings()
        try:
            result, info = check_file(
                final, s, expected_runtime=file_row.get("expected_runtime"),
                already_shrunk=True)
            stat = os.stat(final)
            persist_result(final, result, info, stat.st_size, stat.st_mtime)
        except Exception as exc:  # noqa: BLE001 - never fail the job over this
            log.warning("post-shrink check failed for %s: %s", final, exc)

    # -- redownload -------------------------------------------------------

    def _redownload(self, job_id: int, file_row: dict[str, Any],
                    reason: str) -> dict[str, Any]:
        self._set_job(job_id, "running", 0.0, reason)
        out = self._redownload_row(file_row, reason)
        self._set_job(job_id, "done" if out["ok"] else "failed",
                      1.0 if out["ok"] else 0.0, out["message"],
                      error=None if out["ok"] else out["message"])
        return out

    def _redownload_row(self, file_row: dict[str, Any], reason: str) -> dict[str, Any]:
        """Delete the file and ask the *arr for a replacement.

        Deleting through the *arr is what keeps its database honest; we only
        fall back to the recycle bin directly when no *arr owns the file.
        """
        s = self._settings()
        path = file_row["path"]
        client = self._arr_for(file_row)
        arr_id = file_row.get("arr_id")
        parent = file_row.get("arr_parent_id")
        episode_ids = file_row.get("arr_episode_ids") or None

        steps: list[str] = []

        # Take our own copy first: the *arr's delete is permanent as far as we
        # are concerned, and the whole point of automatic action is that it is
        # reversible.
        recycled = None
        if s.policy.recycle_bin_days > 0 and os.path.exists(path):
            try:
                recycled = recycle.store(path, f"redownload: {reason}",
                                         s.policy.recycle_bin_path,
                                         s.policy.recycle_bin_days)
                steps.append("moved to recycle bin")
            except OSError as exc:
                return {"action": "redownload", "ok": False,
                        "message": f"could not recycle the file: {exc}"}

        if client is not None and arr_id:
            try:
                # The file may already be gone (we recycled it); the *arr's
                # delete then just clears its own record, which is what we want.
                client.delete_file(int(arr_id))
                steps.append(f"removed from {client.flavour}")
            except ArrError as exc:
                db.log("arr_delete_failed", "error", path, str(exc))
                steps.append(f"{client.flavour} delete failed: {exc}")
        elif recycled is None and os.path.exists(path):
            # No *arr and no recycle bin: unlink, because a broken file left in
            # place is what the user asked us to remove.
            try:
                os.unlink(path)
                steps.append("deleted from disk")
            except OSError as exc:
                return {"action": "redownload", "ok": False,
                        "message": f"could not delete: {exc}"}

        if client is not None and parent:
            blocklisted = False
            if s.policy.blocklist_on_redownload:
                try:
                    blocklisted = client.blocklist_last_grab(
                        int(parent), [int(e) for e in episode_ids or []])
                    if blocklisted:
                        steps.append("release blocklisted, search queued")
                except ArrError as exc:
                    db.log("arr_blocklist_failed", "warn", path, str(exc))
            if not blocklisted:
                # No grab in history, or blocklisting failed — search anyway,
                # otherwise the episode simply stays missing.
                try:
                    client.search(int(parent), [int(e) for e in episode_ids or []])
                    steps.append("search triggered")
                except ArrError as exc:
                    db.log("arr_search_failed", "error", path, str(exc))
                    steps.append(f"search failed: {exc}")

        # The replacement the *arr fetches is a different file; it deserves a
        # fresh set of fix attempts.
        db.ex("UPDATE files SET status='missing', last_checked=?, fix_attempts=0 "
              "WHERE path=?", (time.time(), path))
        message = "; ".join(steps) or "nothing to do"
        db.log("redownload", "warn", path, {"reason": reason, "steps": steps})
        return {"action": "redownload", "ok": True, "message": message,
                "recycled": recycled}

    def _arr_for(self, file_row: dict[str, Any]) -> ArrClient | None:
        s = self._settings()
        source = file_row.get("source")
        if source == "sonarr" and s.sonarr.enabled:
            return ArrClient(s.sonarr, "sonarr")
        if source == "radarr" and s.radarr.enabled:
            return ArrClient(s.radarr, "radarr")
        return None
