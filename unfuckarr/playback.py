"""Investigating a file when it fails to play in Emby.

Emby has no "playback failed" event. Measured on the live server (4.10.2,
2026-10-22): eight months of activity log, 1,650 entries, and not one was a
playback failure — only starts, stops, logins and plugin updates. A failure
shows up as a play that stops seconds after it started, and 104 of the 719
plays there lasted under thirty seconds. Most of those were someone browsing,
which is why this module is shaped the way it is:

    webhook → Tracker.stop()       a *suspect*: stopped before it got anywhere
            → investigate()        Emby's transcoder log, a re-check of the
                                   file, a decode of the place it stopped
            → decide()             exactly as for any other check
            → Remediator.apply()   within its own daily cap

Invariant 23 applies unchanged: only the file can condemn itself. A short play
is a reason to look, and it is recorded as information, never as a fault — it
cannot by itself delete, blocklist or re-search anything.

The four plays that prompted this, 2026-10-22 04:03–04:32, were Blu-ray images
played in Chrome. Chrome cannot play an ISO, so Emby transcodes it, and Emby's
own ffmpeg cannot open one either: "Invalid data found when processing input",
in under a second, three retries each. The fix — converting the disc — already
existed; those discs were simply waiting their turn at two a night.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import db, disc
from .checks import CheckResult, Finding
from .clients.emby import EmbyClient, EmbyError
from .config import IntegrityConfig, Settings, apply_path_mappings
from .probe import MediaInfo, ProbeError, decode_check_full
from .remediation import Decision, Remediator, decide
from .scanner import check_file, persist_result
from .state import bus, state

log = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000

# A play that sat for longer than this before stopping was abandoned — paused
# and walked away from — not a failure, however little of it was watched.
ABANDONED_SECONDS = 600

# Suspects waiting for the worker. A storm past this is not a run of broken
# files; it is Emby, the network or the mount, and dropping the excess costs
# nothing the next scan will not find.
QUEUE_LIMIT = 50

# What a webhook event that arrived with no start is measured from: Emby's
# transcoder logs written in the last this-many seconds before the stop.
LOG_LOOKBACK_SECONDS = 600


# -- the webhook ----------------------------------------------------------

@dataclass
class Play:
    """One ``playback.*`` event, reduced to what the investigation needs."""

    event: str
    item_id: str = ""
    title: str = ""
    emby_path: str = ""
    media_source_id: str = ""
    play_session_id: str = ""
    session_id: str = ""
    user: str = ""
    device: str = ""
    client: str = ""
    # Seconds into the file, or None when Emby did not say.
    position: float | None = None
    completed: bool = False

    @property
    def key(self) -> str:
        """What ties a stop to its start: one play of one item on one device."""
        return self.play_session_id or self.device_key

    @property
    def device_key(self) -> str:
        """The same play when Emby has given it a new PlaySessionId since.

        Emby Web retries a play that fails, direct play and then a transcode
        twice, and takes a new PlaySessionId for each attempt, while the start
        went out under the first. Live, 2026-10-24: both Sinbad discs started
        under one id and stopped under the third. Empty when Emby sent no
        session or no item, because ":" would tie unrelated plays together.
        """
        if not (self.session_id and self.item_id):
            return ""
        return f"{self.session_id}:{self.item_id}"


def webhook_payload(body: bytes, content_type: str = "") -> dict[str, Any] | None:
    """The JSON document out of an Emby webhook request.

    Emby offers ``application/json`` or ``multipart/form-data`` for a webhook,
    and the multipart form carries the same JSON in a field called ``data``.
    Parsed with the standard library rather than adding python-multipart for
    one field.
    """
    if content_type.lower().startswith("multipart/"):
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") == "data":
                body = part.get_payload(decode=True) or b""
                break
        else:
            return None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def parse(payload: dict[str, Any]) -> Play | None:
    """A ``Play`` from Emby's webhook JSON, or None for any other event.

    The shape is Emby's notification model: ``Item`` (with ``Path``),
    ``Session`` (``DeviceName``, ``Client``, ``Id``) and ``PlaybackInfo``
    (``PositionTicks``, ``PlayedToCompletion``, ``PlaySessionId`` and the
    ``MediaSource`` actually played, which is the file that matters when an
    item has more than one version).
    """
    event = str(payload.get("Event") or "")
    if not event.startswith("playback."):
        return None
    item = payload.get("Item") or {}
    session = payload.get("Session") or {}
    user = payload.get("User") or {}
    info = payload.get("PlaybackInfo") or {}
    source = info.get("MediaSource") or {}

    ticks = info.get("PositionTicks")
    position = (ticks / TICKS_PER_SECOND
                if isinstance(ticks, (int, float)) and ticks >= 0 else None)
    return Play(
        event=event,
        item_id=str(item.get("Id") or ""),
        title=_title(item),
        emby_path=str(source.get("Path") or item.get("Path") or ""),
        media_source_id=str(source.get("Id") or info.get("MediaSourceId") or ""),
        play_session_id=str(info.get("PlaySessionId") or ""),
        session_id=str(session.get("Id") or session.get("DeviceId") or ""),
        user=str(user.get("Name") or ""),
        device=str(session.get("DeviceName") or ""),
        client=str(session.get("Client") or ""),
        position=position,
        completed=bool(info.get("PlayedToCompletion")),
    )


def _title(item: dict[str, Any]) -> str:
    name = str(item.get("Name") or "")
    series = item.get("SeriesName")
    if not series:
        return name
    season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
    if isinstance(season, int) and isinstance(episode, int):
        return f"{series} S{season:02d}E{episode:02d} – {name}"
    return f"{series} – {name}"


@dataclass
class Suspect:
    """A play that stopped before it got anywhere."""

    play: Play
    started: float | None       # wall clock, ours; None when no start arrived
    stopped: float
    seconds: float              # how far it got


class Tracker:
    """Which plays are in progress, so that a stop knows how far it got.

    How far, not where: a film resumed at forty minutes that dies at once
    stops at 40:05, and that is a five-second play. Without its start, a stop
    is measured from the beginning of the file, which misses a failed resume
    and never invents one.
    """

    def __init__(self, clock=time.time) -> None:
        # key -> (when it started, where it started, Play.device_key)
        self._open: dict[str, tuple[float, float, str]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def start(self, play: Play) -> None:
        now = self._clock()
        with self._lock:
            # Bounded by age: a stop Emby never sent must not hold memory for
            # the life of the process.
            for key in [k for k, (t, _, _) in self._open.items()
                        if now - t > 86400]:
                del self._open[key]
            self._open[play.key] = (now, play.position or 0.0, play.device_key)

    def _opened(self, play: Play) -> tuple[float, float] | None:
        """The start this stop ends: by its PlaySessionId, or else the latest
        start of the same item on the same device (`Play.device_key`)."""
        with self._lock:
            opened = self._open.pop(play.key, None)
            if opened is None and play.device_key:
                same = [k for k, v in self._open.items()
                        if v[2] == play.device_key]
                if same:
                    opened = self._open.pop(max(same,
                                                key=lambda k: self._open[k][0]))
        return (opened[0], opened[1]) if opened else None

    def stop(self, play: Play, max_seconds: float) -> Suspect | None:
        now = self._clock()
        opened = self._opened(play)
        # Emby calls a play complete in two cases: it reached the end, or Emby
        # never knew where the end was. An item with no runtime, which is
        # every disc image its ffmpeg cannot open, is marked watched on any
        # stop and reported at position 0. Only the first is a reason to stop
        # looking. Live, 2026-10-24: two Blu-ray images that died in a second
        # each arrived as completed and were never investigated.
        no_end = play.completed and not play.position
        if play.completed and not no_end:
            return None
        wall = now - opened[0] if opened else None
        if wall is not None and wall > ABANDONED_SECONDS:
            return None
        # That 0 is Emby's, not the player's, so it says nothing about how far
        # the play got. The wall clock does.
        position = None if no_end else play.position
        if position is not None:
            reached = max(0.0, position - (opened[1] if opened else 0.0))
        elif wall is not None:
            reached = wall
        else:
            return None                  # nothing to measure it by
        if reached >= max_seconds:
            return None
        return Suspect(play=play, started=opened[0] if opened else None,
                       stopped=now, seconds=reached)


# -- Emby's transcoder log ------------------------------------------------

@dataclass
class Transcoder:
    """What one of Emby's ``ffmpeg-*`` logs says happened."""

    log: str
    finished: bool              # ffmpeg reached its closing summary
    error: str | None           # the line that says what went wrong
    last_words: str             # the last thing ffmpeg said, whatever it was
    item_id: str = ""
    play_session_id: str = ""

    @property
    def failed(self) -> bool:
        return not self.finished and self.error is not None


# Lines ffmpeg writes in Emby's log carry a wall-clock stamp; the request,
# the media source JSON and the user policy above them do not.
_STAMP = re.compile(r"^\d\d:\d\d:\d\d\.\d{3} ")
# The closing summary of a run that got to the end of what it was asked for.
# A successful transcode logs "Could not find codec parameters" for a PGS
# subtitle on the live server, so a log is never judged by its error-looking
# lines alone: only one that never reached this is a failure.
_FINISHED = re.compile(r"^(video:\s*\d+\s*kB|encoded \d+ frames)|muxing overhead")
_ERROR = re.compile(
    r"invalid data|error|failed|could not|cannot|no such file|permission denied"
    r"|not supported|unsupported|unknown (de|en)coder|corrupt|truncat", re.I)
_BANNER = ("ffmpeg version", "built with", "Execution Date", "configuration:",
           "lib", "EXIT")


def read_log(name: str, text: str) -> Transcoder:
    lines = text.splitlines()
    head = "\n".join(lines[:60])
    item = re.search(r"/videos/([^/?\s]+)/", head)
    session = re.search(r"PlaySessionId=([0-9A-Za-z-]+)", head)

    said: list[str] = []
    for line in lines:
        if line.startswith("======= ExtraData"):
            break                       # a base64 blob follows
        body = _STAMP.sub("", line).strip() if _STAMP.match(line) else line.strip()
        if body and not body.startswith(_BANNER):
            said.append(body)
    stamped = [_STAMP.sub("", l).strip() for l in lines if _STAMP.match(l)]
    stamped = [l for l in stamped if l and not l.startswith(_BANNER)]

    finished = any(_FINISHED.search(l) for l in said)
    # Only ffmpeg's last words count, plus the reasons straight before them
    # ("…: Invalid data found" then "Conversion failed!"). An error-looking
    # line further up is a warning ffmpeg carried on past — a transcode the
    # viewer stopped part-way has one and ends on a progress line.
    last: list[str] = []
    for line in reversed(stamped):
        if not _ERROR.search(line) or len(last) == 3:
            break
        last.insert(0, line)
    return Transcoder(log=name, finished=finished,
                      error=" — ".join(last) if last else None,
                      last_words=stamped[-1] if stamped else "",
                      item_id=item.group(1) if item else "",
                      play_session_id=session.group(1) if session else "")


def _log_time(entry: dict[str, Any]) -> float | None:
    stamp = str(entry.get("DateCreated") or "")
    try:
        return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def find_transcoder(emby: EmbyClient, suspect: Suspect) -> list[Transcoder]:
    """Emby's ffmpeg logs for this play, oldest first.

    Matched on the item in the request URL, within a minute either side of the
    play; a matching PlaySessionId narrows that further when Emby sent one.
    Emby retries a failed transcode — three logs a second apart for each of
    the live failures — so there is usually more than one.
    """
    play = suspect.play
    began = (suspect.started if suspect.started is not None
             else suspect.stopped - LOG_LOOKBACK_SECONDS)
    since, until = began - 60, suspect.stopped + 60
    candidates = []
    for entry in emby.log_files():
        name = str(entry["Name"])
        if not name.startswith("ffmpeg-"):
            continue
        created = _log_time(entry)
        if created is not None and since <= created <= until:
            candidates.append((created, name))
    found = []
    for _, name in sorted(candidates)[-10:]:
        t = read_log(name, emby.log_text(name))
        if t.item_id == play.item_id:
            found.append(t)
    if play.play_session_id:
        wanted = play.play_session_id.replace("-", "").lower()
        same = [t for t in found
                if t.play_session_id.replace("-", "").lower() == wanted]
        if same:
            return same
    return found


# -- the investigation ----------------------------------------------------

def decode_where_it_stopped(info: MediaInfo | None, position: float | None,
                            cfg: IntegrityConfig,
                            ffmpeg: str = "ffmpeg") -> Finding | None:
    """Decode the stretch of the file where the play stopped.

    The integrity check samples the start, middle and end. A play that dies on
    resuming at forty minutes died somewhere none of those reach, and this is
    the one check that knows where to look. Skipped where the check has
    already been: a play that stopped inside its first sample, or a library
    checked at ``full`` depth. Not for a disc image, which the transcoder
    cannot open at all and which converting answers better.
    """
    if info is None or info.is_disc or position is None or not cfg.enabled:
        return None
    span = float(cfg.sample_seconds)
    if cfg.depth == "full" or (cfg.depth == "sample" and position <= span):
        return None
    start = max(0.0, position - span)
    res = decode_check_full(info.source, ffmpeg, start=start, duration=span * 2)
    if res.timed_out or not res.errors:
        return None
    severity = "error" if res.errors >= cfg.max_decode_errors else "warning"
    return Finding(
        "integrity", "playback_decode_errors", severity,
        f"{res.errors} decode error(s) around {_clock(position)}, where Emby "
        f"stopped: " + "; ".join(res.messages[:3]),
        {"count": res.errors, "start": start, "messages": res.messages},
    )


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _not_for_this(decision: Decision, result: CheckResult) -> Decision:
    """Narrow what `decide` offers to what answers a failed play.

    Two of its answers do not. A shrink is about size, and size is not why a
    film will not start: an hours-long encode set off by somebody pressing
    play is the wrong trade. And a remux whose only reason is the decode of
    the stopping place cannot be confirmed — `_confirm_fixed` re-checks the
    replacement with the ordinary samples, which never look there, so it would
    record a fix it had not seen (invariant 9). Both become a flag.
    """
    if decision.action == "shrink":
        return Decision("flag", "nothing in the file explains the failed play "
                                "(it is only waiting to be measured for a saving)",
                        decision.findings)
    if decision.action in ("transcode", "repair"):
        codes = {f.code for f in result.errors if f.category == "integrity"}
        if codes == {"playback_decode_errors"}:
            return Decision("flag", "decode errors where Emby stopped, which a "
                                    "remux could not be shown to have cleared — "
                                    "redownload it from the file's page",
                            decision.findings)
    return decision


class PlaybackWatcher:
    """Takes Emby's webhook, keeps the suspects, investigates them one at a time.

    One worker investigates, because an investigation decodes video and asks
    Emby for its logs, and a viewer working through a broken season sends a
    suspect a minute. A second worker carries out what the investigations
    decide, also one at a time, and the two are kept apart. A conversion is
    tens of minutes, and the next suspect's transcoder log is being pruned
    while it waits, so acting must not hold up investigating. Acting cannot
    run in parallel either: a conversion takes no slot in the remediator (only
    transcodes and shrinks do), so three discs failing in a row, as on
    2026-10-22, would otherwise mean three MakeMKV rips reading the array at
    once.
    """

    def __init__(self, settings_getter, remediator: Remediator) -> None:
        self._settings = settings_getter
        self.remediator = remediator
        self.tracker = Tracker()
        self._queue: queue.Queue[Suspect] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._worker: threading.Thread | None = None
        # Unbounded, because `max_actions_per_day` already bounds what reaches it.
        self._actions: queue.Queue[tuple] = queue.Queue()
        self._actor: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One webhook. Returns at once; the investigation runs on the worker."""
        play = parse(payload)
        if play is None:
            return {"ok": True, "ignored": "not a playback event"}
        s = self._settings()
        if not s.playback.enabled:
            return {"ok": True, "ignored": "playback investigation is switched off"}
        if play.event == "playback.start":
            self.tracker.start(play)
            return {"ok": True}
        if play.event != "playback.stop":
            return {"ok": True, "ignored": play.event}
        suspect = self.tracker.stop(play, s.playback.max_seconds)
        if suspect is None:
            return {"ok": True, "suspect": False}
        try:
            self._queue.put_nowait(suspect)
        except queue.Full:
            db.log("playback_queue_full", "warn", detail={
                "title": play.title, "limit": QUEUE_LIMIT})
            return {"ok": True, "suspect": True, "queued": False}
        self._ensure_worker()
        return {"ok": True, "suspect": True, "queued": True}

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._work, daemon=True,
                                            name="unfuckarr-playback")
            self._worker.start()

    def _ensure_actor(self) -> None:
        with self._worker_lock:
            if self._actor is not None and self._actor.is_alive():
                return
            self._actor = threading.Thread(target=self._act, daemon=True,
                                           name="unfuckarr-playback-act")
            self._actor.start()

    def _act(self) -> None:
        while True:
            item = self._actions.get()
            try:
                self._apply(*item)
            finally:
                self._actions.task_done()

    def _work(self) -> None:
        while True:
            suspect = self._queue.get()
            try:
                self.investigate(suspect)
            except Exception as exc:  # noqa: BLE001 - the worker must not die
                log.exception("playback investigation failed")
                db.log("playback_investigation_failed", "error",
                       detail={"title": suspect.play.title, "error": str(exc)})
            finally:
                self._queue.task_done()

    # -- the work ---------------------------------------------------------

    def investigate(self, suspect: Suspect) -> dict[str, Any]:
        """Look at one failed play, decide, and act within the brakes.

        Returns the row written to ``playback``.
        """
        s = self._settings()
        play = suspect.play
        emby = EmbyClient(s.emby) if s.emby.enabled and s.emby.url else None
        row: dict[str, Any] = {
            "ts": suspect.stopped, "item_id": play.item_id, "path": None,
            "title": play.title, "user": play.user, "device": play.device,
            "client": play.client, "seconds": round(suspect.seconds, 1),
            "position": play.position, "transcoder": None, "error": None,
            "verdict": "", "action": "none", "detail": {},
        }

        path = self._local_path(play, emby, s)
        row["path"] = path
        if path is None:
            row["verdict"] = ("not a file this server knows: no path in the "
                              "webhook, and Emby's index has no item "
                              f"{play.item_id or '(none)'}")
            return self._record(row, "info")
        if not os.path.exists(path):
            row["verdict"] = ("not on disk here — the Emby path mapping may be "
                              "wrong, or the file has gone")
            return self._record(row, "warn")

        quiet = s.playback.quiet_minutes * 60
        if db.q1("SELECT 1 FROM playback WHERE path=? AND ts>? AND verdict<>'repeat'",
                 (path, suspect.stopped - quiet)):
            row["verdict"] = "repeat"
            return self._record(row, "info")

        # The transcoder's log first: Emby prunes it within the day, and it is
        # the only account of what went wrong that does not depend on
        # reproducing it.
        transcoders: list[Transcoder] = []
        if emby is not None:
            try:
                transcoders = find_transcoder(emby, suspect)
            except EmbyError as exc:
                row["detail"]["transcoder_unreadable"] = str(exc)
        failed = [t for t in transcoders if t.failed]
        evidence = failed[-1] if failed else (transcoders[-1] if transcoders else None)
        if evidence is not None:
            row["transcoder"] = evidence.log
            row["error"] = evidence.error if evidence.failed else None
            row["detail"]["transcoder"] = {
                "logs": [t.log for t in transcoders],
                "failed": len(failed), "last_words": evidence.last_words,
            }

        file_row = db.q1("SELECT * FROM files WHERE path=?", (path,))
        record = ({k: file_row[k] for k in file_row.keys()} if file_row
                  else {"path": path, "source": "folder", "title": play.title})
        if record.get("arr_episode_ids"):
            try:
                record["arr_episode_ids"] = json.loads(record["arr_episode_ids"])
            except (TypeError, json.JSONDecodeError):
                record["arr_episode_ids"] = None

        try:
            result, info = check_file(
                path, s, expected_runtime=record.get("expected_runtime"),
                emby=emby,
                already_shrunk=bool(record.get("shrunk") or record.get("shrink_skipped")))
        except ProbeError as exc:
            result, info = CheckResult(path=path, error=str(exc)), None
        window = decode_where_it_stopped(info, play.position, s.integrity,
                                         s.ffmpeg_path)
        if window is not None:
            result.add(window)
        result.add(Finding("emby", "emby_playback_failed", "info",
                           self._describe(suspect, row),
                           {"device": play.device, "client": play.client,
                            "seconds": row["seconds"], "error": row["error"]}))
        if file_row is not None:
            try:
                stat = os.stat(path)
                persist_result(path, result, info, stat.st_size, stat.st_mtime)
            except OSError:
                pass

        decision = _not_for_this(decide(result, s), result)
        row["verdict"] = self._verdict(result, info, decision, evidence, window)
        row["detail"].update({"status": result.status,
                              "decision": decision.as_dict()})

        if decision.action in ("none", "flag"):
            row["action"] = decision.action
            return self._record(row, "warn" if result.errors else "info")

        blocked = self._blocked(s, file_row is not None, record, decision)
        if blocked is not None:
            row["action"] = "flag"
            row["detail"]["not_acted"] = blocked
            return self._record(row, "warn")
        row["action"] = decision.action
        written = self._record(row, "warn")
        # To the action worker, one at a time: see the class docstring.
        self._actions.put((record, result, info, decision))
        self._ensure_actor()
        return written

    def _apply(self, record: dict[str, Any], result: CheckResult,
               info: MediaInfo | None, decision: Decision) -> dict[str, Any]:
        try:
            outcome = self.remediator.apply(record, result, info, decision)
        except Exception as exc:  # noqa: BLE001 - a worker must not die
            log.exception("acting on a failed play of %s", record["path"])
            outcome = {"action": decision.action, "ok": False, "message": str(exc)}
        bus.publish("remediated", {"path": record["path"], **outcome})
        return outcome

    def _local_path(self, play: Play, emby: EmbyClient | None,
                    s: Settings) -> str | None:
        if play.emby_path:
            return apply_path_mappings(play.emby_path, s.emby.path_mappings)
        if emby is not None and play.item_id:
            try:
                return emby.item_path(play.item_id, play.media_source_id or None)
            except EmbyError:
                return None
        return None

    def _blocked(self, s: Settings, known: bool, record: dict[str, Any],
                 decision: Decision) -> str | None:
        """Why the investigation records its decision rather than acting on it."""
        if s.playback.action != "fix":
            return "playback investigation is set to flag; the next scan acts"
        if state.paused:
            return "paused"
        if not known:
            return ("not in unfuckarr's library — no *arr or library path "
                    "covers it, so nothing manages it")
        if decision.action == "convert":
            refused = self.remediator.convert_refused(record)
            if refused is not None:
                return refused
        if decision.action == "research":
            waiting = self.remediator.research_blocked(record)
            if waiting is not None:
                return waiting
        acted = db.q1("SELECT COUNT(*) n FROM playback WHERE ts>? "
                      "AND action NOT IN ('none','flag')",
                      (time.time() - 86400,))["n"]
        if acted >= s.playback.max_actions_per_day:
            db.log("playback_cap_reached", "warn", record["path"],
                   {"cap": s.playback.max_actions_per_day})
            return (f"{acted} action(s) from failed plays in the last day, "
                    f"the cap is {s.playback.max_actions_per_day}")
        return None

    @staticmethod
    def _describe(suspect: Suspect, row: dict[str, Any]) -> str:
        play = suspect.play
        where = f" at {_clock(play.position)}" if play.position else ""
        text = (f"stopped after {suspect.seconds:.0f} s{where} on "
                f"{play.device or play.client or 'an unknown device'}")
        if row.get("error"):
            text += f" — Emby's transcoder: {row['error']}"
        elif row.get("transcoder"):
            text += " — Emby's transcoder did not report an error"
        else:
            text += " — no transcoder log, so the device played the file itself"
        return text

    @staticmethod
    def _verdict(result: CheckResult, info: MediaInfo | None, decision: Decision,
                 evidence: Transcoder | None, window: Finding | None) -> str:
        if (info is not None and info.is_disc) or disc.is_disc_image(result.path):
            if evidence is not None and evidence.failed:
                return ("disc image that Emby's transcoder could not open — "
                        "every client that cannot play the image itself will "
                        "fail the same way until it is converted")
            return "disc image — converting it is the fix for these"
        if result.error and not result.errors:
            return f"could not check the file: {result.error}"
        if window is not None and window.severity == "error":
            return "broken where it stopped: " + window.detail
        if result.errors:
            return f"{result.status}: " + "; ".join(
                f.detail for f in result.errors[:2])
        if evidence is not None and evidence.failed:
            return ("Emby's transcoder failed, but the file reads cleanly "
                    "here — Emby's ffmpeg or the server is the likelier cause")
        if evidence is None:
            return ("nothing wrong with the file, and Emby did not transcode "
                    "it — the device failed to play it itself, or the viewer "
                    "stopped")
        return "nothing wrong found — most likely the viewer stopped"

    @staticmethod
    def _record(row: dict[str, Any], level: str) -> dict[str, Any]:
        detail = json.dumps(row["detail"], default=str)
        row["id"] = db.ex(
            "INSERT INTO playback (ts, item_id, path, title, user, device, "
            "client, seconds, position, transcoder, error, verdict, action, "
            "detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["ts"], row["item_id"], row["path"], row["title"], row["user"],
             row["device"], row["client"], row["seconds"], row["position"],
             row["transcoder"], row["error"], row["verdict"], row["action"],
             detail))
        if row["verdict"] != "repeat":
            db.log("playback_failed", level, row["path"], {
                "title": row["title"], "device": row["device"],
                "seconds": row["seconds"], "error": row["error"],
                "verdict": row["verdict"], "action": row["action"]})
        bus.publish("playback", {k: row[k] for k in (
            "id", "path", "title", "verdict", "action")})
        return row
