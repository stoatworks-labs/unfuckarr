"""The intake side: downloads the *arr has finished but will not import.

Everything else in unfuckarr works on files that are already in the library.
This module works on the queue — the short window between "the download
client says it is done" and "Sonarr has moved it into the library" — because
that window has a failure mode nothing else here can see: the download
completes, the *arr refuses to import it, and the item sits in the queue for
ever. No file ever appears, so the scanner has nothing to check; the *arr's
own Failed Download Handling never fires, because nothing *failed*; and the
episode simply stays missing until somebody notices.

**What this is not.** It is deliberately not a general queue cleaner. Stalled
torrents, slow torrents and stuck metadata are someone else's job (Cleanuparr
does them well, on a strike model, and two tools removing from one queue race
each other — the loser's DELETE hits a queue id that no longer exists, or
worse, blocklists the replacement the winner just grabbed). This handles the
one class those tools get wrong, and it gets it right by a different method.

**The method, and the whole point of the module.** A blocked import has two
completely different causes that look identical from the queue:

* *The release is unusable.* It unpacked to nothing, it is still in a rar, it
  is a sample, the video is broken. Another copy is the fix, so: remove,
  blocklist, re-search.
* *The release is fine and the* arr *cannot place it.* It could not match the
  series, the path is wrong, the destination is read-only. Another copy fixes
  nothing — the next one blocks for exactly the same reason — and blocklisting
  throws away a good release and burns the indexer.

Cleanuparr, decluttarr and the *arr's own settings tell these apart by
matching the status message. That is a guess, and it is wrong in the
expensive direction. Live on this system, 2026-09-03: three complete,
perfectly importable 2 GB usenet downloads sat in `importBlocked` carrying

    "Found matching series via grab history, but release was matched to
     series by ID. Automatic import is not possible."

which is Sonarr saying *I know exactly what this is and I want a human to
confirm it*. Cleanuparr had already recorded two `failedimport` strikes
against them. A third would have deleted 6 GB of good media and blocklisted
three clean releases.

unfuckarr does not have to guess, because opening media files is the thing it
already does. `triage` reads the queue record and decides only whether the
item is worth looking at; `inspect` then goes and looks, and **only evidence
from the files themselves can produce a `bad_release`**. A status message can
send us to look. It can never, on its own, delete anything. That is invariant
23, and it is the reason this module exists rather than a pattern list.

The corollary matters as much: when the path does not resolve, when the
directory cannot be read, when ffprobe will not run — the answer is
`unrecognised`, which flags and acts on nothing. "I could not look" is not
"the release is bad" (invariant 17, in its natural home).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from . import db
from .clients.arr import ArrClient, ArrError
from .config import VIDEO_EXTENSIONS, Settings, apply_path_mappings
from .probe import MediaInfo, ProbeError, probe
from .state import bus

log = logging.getLogger(__name__)

Kind = Literal["working", "manual", "bad_release", "unrecognised"]

# Archive parts. A release still in these has not been unpacked — either the
# client's post-processing is not configured or it failed — and the *arr
# genuinely cannot import it.
ARCHIVE_EXTENSIONS = {
    ".rar", ".zip", ".7z", ".tar", ".gz", ".bz2", ".arj", ".cab", ".ace",
}

# Parts of a multipart rar: .r00, .r01, ... and .part01.rar.
def _is_archive(name: str) -> bool:
    lower = name.lower()
    suffix = Path(lower).suffix
    if suffix in ARCHIVE_EXTENSIONS:
        return True
    # .r00 … .r99
    return (len(suffix) == 4 and suffix[1] == "r" and suffix[2:].isdigit())


# Everything a release directory is allowed to contain without it counting as
# content. Nothing here is a reason to keep a release, and their presence
# alone (with no video) is what "unpacked to nothing" looks like.
JUNK_EXTENSIONS = {
    ".nfo", ".sfv", ".txt", ".srt", ".sub", ".idx", ".ass", ".ssa", ".jpg",
    ".jpeg", ".png", ".md5", ".par2", ".url", ".exe", ".lnk", ".website",
    ".nzb", ".torrent", ".db", ".ds_store",
}

# States the *arr uses while it is still working. None of them is a fault and
# none of them is ours to touch — an import that is merely pending is an
# import that has not happened *yet*.
WORKING_STATES = {"downloading", "importPending", "importing", "ignored"}

# States that mean the *arr has stopped and is waiting for someone. These are
# the only ones this module looks at at all.
BLOCKED_STATES = {"importBlocked", "importFailed", "failedPending", "failed"}

# Status messages that are unambiguously about the *arr's own bookkeeping
# rather than the release. Matching one of these means we do not even look at
# the files: another copy cannot fix it, so there is no decision to make.
#
# Substring, case-insensitive, and deliberately short — the *arrs reword these
# between versions and a long exact string silently stops matching. Every
# entry here can only ever make us *less* likely to act, so a false positive
# costs a flag and never a deletion.
ARR_SIDE_MARKERS: tuple[tuple[str, str], ...] = (
    # Verified live against Sonarr 4.0.19.2979, 2026-09-03. The full message
    # is "Found matching series via grab history, but release was matched to
    # series by ID. Automatic import is not possible. See the FAQ for
    # details." Sonarr knows what the file is; it wants a human to confirm.
    ("matched to series by id", "Sonarr matched this by ID and wants a manual import"),
    ("matched to movie by id", "Radarr matched this by ID and wants a manual import"),
    ("automatic import is not possible",
     "the *arr will not import this automatically"),
    # Cannot work out what it is. A re-grab of the same title parses the same
    # way, so this is a naming problem for a human, not a bad release.
    ("unable to determine", "the *arr cannot work out what this is"),
    ("not found in the database", "the *arr has no matching item"),
    ("unable to parse", "the *arr cannot parse the release name"),
    # Already have something as good or better. The release is fine; there is
    # simply nothing to do with it, and blocklisting it is actively wrong.
    ("not an upgrade", "the library already has this or better"),
    ("already imported", "already imported"),
    ("existing file", "the library already has this"),
    # Filesystem and permissions. This is the mount-has-gone-away shape, and
    # it is the single most dangerous thing to misread: every item in the
    # queue fails at once, and a tool that blocklists on it empties the
    # library's future in one pass.
    ("access to the path", "a permissions or path problem, not the release"),
    ("permission denied", "a permissions or path problem, not the release"),
    ("is denied", "a permissions or path problem, not the release"),
    ("could not find a path", "the *arr cannot see the download's path"),
    ("no such file or directory", "the *arr cannot see the download's path"),
    ("destination already exists", "something is already at the destination"),
    ("disk is full", "the destination is full"),
    ("not enough free space", "the destination is full"),
)


@dataclass
class QueueItem:
    """One *arr queue record, normalised across Sonarr and Radarr."""

    source: str                       # sonarr | radarr
    download_id: str                  # the client's id/hash — stable
    queue_id: int
    title: str
    state: str                        # trackedDownloadState
    tracked_status: str               # trackedDownloadStatus
    status: str                       # status
    error_message: str = ""
    messages: list[str] = field(default_factory=list)
    protocol: str = ""
    indexer: str = ""
    download_client: str = ""
    size: int = 0
    sizeleft: int = 0
    added: str = ""
    output_path: str = ""             # as the *arr sees it
    local_path: str = ""              # after path mapping
    arr_parent_id: int | None = None
    arr_episode_ids: list[int] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.state in BLOCKED_STATES

    @property
    def all_text(self) -> str:
        return " ".join([self.error_message, *self.messages]).lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "download_id": self.download_id,
            "queue_id": self.queue_id, "title": self.title, "state": self.state,
            "tracked_status": self.tracked_status, "status": self.status,
            "error_message": self.error_message, "messages": list(self.messages),
            "protocol": self.protocol, "indexer": self.indexer,
            "download_client": self.download_client, "size": self.size,
            "sizeleft": self.sizeleft, "added": self.added,
            "output_path": self.output_path, "local_path": self.local_path,
            "arr_parent_id": self.arr_parent_id,
            "arr_episode_ids": list(self.arr_episode_ids),
        }


@dataclass
class Verdict:
    kind: Kind
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.kind == "bad_release"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason,
                "evidence": dict(self.evidence)}


def normalise(record: dict[str, Any], source: str,
              path_mappings: list[dict[str, str]]) -> QueueItem | None:
    """One raw queue record into a `QueueItem`.

    Returns None for a record with no download id. That is not defensive
    padding: a queue row the client has not acknowledged yet has no id, and it
    is the id — not the queue row's own `id`, which changes when the *arr
    restarts — that this module tracks an item by across passes.
    """
    download_id = (record.get("downloadId") or "").strip()
    if not download_id:
        return None

    messages: list[str] = []
    for block in record.get("statusMessages") or []:
        for message in block.get("messages") or []:
            if message:
                messages.append(str(message))

    episode_ids: list[int] = []
    if record.get("episodeId"):
        episode_ids = [int(record["episodeId"])]
    # Sonarr v4 returns a season pack as one record per episode, but some
    # versions carry the whole list; take it when it is there.
    for eid in record.get("episodeIds") or []:
        if int(eid) not in episode_ids:
            episode_ids.append(int(eid))

    output_path = record.get("outputPath") or ""
    return QueueItem(
        source=source,
        download_id=download_id,
        queue_id=int(record.get("id") or 0),
        title=record.get("title") or "",
        state=record.get("trackedDownloadState") or "",
        tracked_status=record.get("trackedDownloadStatus") or "",
        status=record.get("status") or "",
        error_message=record.get("errorMessage") or "",
        messages=messages,
        protocol=record.get("protocol") or "",
        indexer=record.get("indexer") or "",
        download_client=record.get("downloadClient") or "",
        size=int(record.get("size") or 0),
        sizeleft=int(record.get("sizeleft") or 0),
        added=record.get("added") or "",
        output_path=output_path,
        local_path=(apply_path_mappings(output_path, path_mappings)
                    if output_path else ""),
        arr_parent_id=record.get("seriesId") or record.get("movieId"),
        arr_episode_ids=episode_ids,
    )


def triage(item: QueueItem) -> Verdict:
    """Decide from the queue record alone whether the files are worth opening.

    Pure — no filesystem, no subprocess — for the same reason
    `remediation.decide` is: the policy is the part that has to be testable
    without a library attached.

    It can return `working`, `manual` or `unrecognised`. It can **never**
    return `bad_release`; only `inspect` can, and only on evidence. That
    asymmetry is the invariant.
    """
    if not item.blocked:
        return Verdict("working", f"the *arr is still working on this "
                                  f"({item.state or 'no state'})")

    text = item.all_text
    for marker, reason in ARR_SIDE_MARKERS:
        if marker in text:
            return Verdict("manual", reason, {"matched": marker})

    if item.state in ("failed", "failedPending"):
        # The download client itself reported failure, so there is usually no
        # complete file to open. This is the one case where the *arr's own
        # Failed Download Handling normally acts first; when it has not, the
        # files are still the evidence — `inspect` will find nothing
        # importable and say so.
        return Verdict("unrecognised",
                       "the download client reported this as failed",
                       {"state": item.state})

    return Verdict("unrecognised",
                   "the *arr has blocked the import and has not said why in "
                   "terms we recognise",
                   {"state": item.state,
                    "messages": item.messages[:4]})


@dataclass
class Contents:
    """What is actually in a finished download."""

    videos: list[str] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    total_bytes: int = 0
    unreadable: str = ""

    @property
    def empty(self) -> bool:
        return not (self.videos or self.archives or self.other)


def read_contents(local_path: str, max_entries: int = 5000) -> Contents:
    """List a finished download without opening anything.

    A file path is allowed as well as a directory: usenet single-file grabs
    and some torrent clients hand the *arr the file itself.
    """
    out = Contents()
    if not local_path:
        out.unreadable = "the *arr reported no output path"
        return out
    try:
        if os.path.isfile(local_path):
            entries: Iterable[Path] = [Path(local_path)]
        elif os.path.isdir(local_path):
            entries = (p for p in Path(local_path).rglob("*") if p.is_file())
        else:
            out.unreadable = (f"{local_path} does not exist — check the path "
                              f"mappings for this *arr")
            return out
        for n, p in enumerate(entries):
            if n >= max_entries:
                break
            name = p.name
            if name.startswith("."):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.total_bytes += size
            suffix = p.suffix.lower()
            if suffix in VIDEO_EXTENSIONS:
                out.videos.append(str(p))
            elif _is_archive(name):
                out.archives.append(str(p))
            elif suffix in JUNK_EXTENSIONS:
                continue
            else:
                out.other.append(str(p))
    except OSError as exc:
        out.unreadable = f"could not read {local_path}: {exc}"
    return out


# How little of the release may have arrived as video before the download is
# called incomplete. Deliberately far below anything a real release reaches: a
# finished grab is video plus a few MB of par2 and an nfo, so it clears this
# by an order of magnitude. It is here to catch "a 40 MB file where 2 GB was
# expected", not to judge packaging.
MIN_VIDEO_SHARE = 0.05


def looks_like_sample(path: str) -> bool:
    """Whether this video is the release's sample rather than the release.

    Name only, and that is the point. An earlier version also called a video
    a sample when it was a small fraction of the release size, which is the
    same arithmetic that condemns a **season pack**: Sonarr opens one queue
    record per episode and puts the *pack's* total size on each one, so every
    episode of a twenty-part pack is 5% of "its" release and every one of them
    reads as a sample. Size belongs in the completeness test below, measured
    against everything in the directory rather than one file.

    Scene releases name the sample, and they have for twenty years: a
    `Sample/` directory or `*-sample.mkv`. That is a fact about the release,
    not an inference from it.
    """
    lower = path.lower()
    return ("sample" in Path(lower).name
            or f"{os.sep}sample{os.sep}" in f"{os.sep}{lower.strip(os.sep)}{os.sep}")


def inspect(item: QueueItem, settings: Settings,
            triaged: Verdict | None = None) -> Verdict:
    """Open what arrived and decide what it is.

    The only route to a `bad_release`. Every path that cannot see the files
    ends in `unrecognised`, which flags and acts on nothing.
    """
    verdict = triaged if triaged is not None else triage(item)
    if verdict.kind in ("working", "manual"):
        return verdict

    contents = read_contents(item.local_path)
    evidence: dict[str, Any] = {
        "local_path": item.local_path,
        "output_path": item.output_path,
        "videos": len(contents.videos),
        "archives": len(contents.archives),
        "other": len(contents.other),
        "bytes": contents.total_bytes,
    }

    if contents.unreadable:
        # Cannot look, so cannot condemn. On this system that is not a corner
        # case: the Emby path mapping has been wrong for 17,569 of 17,715
        # files, and a queue mapping is exactly as easy to get wrong.
        return Verdict("unrecognised", contents.unreadable, evidence)

    if contents.empty:
        if item.size > 0:
            # The *arr says gigabytes arrived and this container sees an empty
            # directory. Between "the release evaporated" and "the path
            # mapping lands somewhere that merely exists", the second is far
            # likelier and the first is not worth a blocklist to find out.
            return Verdict(
                "unrecognised",
                f"the *arr recorded {item.size / 1e9:.1f} GB for this download "
                f"and {item.local_path} is empty — that is usually a path "
                f"mapping pointing somewhere that happens to exist",
                evidence)
        return Verdict("bad_release",
                       "the download is empty — there is nothing to import",
                       evidence)

    if not contents.videos:
        if contents.archives:
            return Verdict(
                "bad_release",
                f"still packed: {len(contents.archives)} archive part(s) and "
                "no video. The download client did not unpack it",
                evidence)
        return Verdict(
            "bad_release",
            f"no video file among {len(contents.other)} file(s) — nothing "
            "here is importable",
            evidence)

    # There is video. Whether it is *the* video is the remaining question, and
    # from here on only a measurement answers it.
    sizes = {p: _size_of(p) for p in contents.videos}
    video_bytes = sum(sizes.values())
    evidence["video_bytes"] = video_bytes

    # Completeness, measured against every video present rather than the
    # largest — see `looks_like_sample` for why the distinction is what keeps
    # a season pack out of this branch.
    release_size = item.size
    if release_size > 0 and video_bytes < release_size * MIN_VIDEO_SHARE:
        return Verdict(
            "bad_release",
            f"only {video_bytes / 1e6:.0f} MB of video arrived where the *arr "
            f"expected {release_size / 1e9:.1f} GB — this download is not the "
            f"release",
            evidence)

    # Everything that is left is a sample by name, so judge the biggest thing
    # that is not one.
    real = {p: n for p, n in sizes.items() if not looks_like_sample(p)}
    if not real:
        largest = max(sizes, key=lambda p: sizes[p])
        evidence["largest"] = largest
        return Verdict("bad_release",
                       "the only video in this download is the sample",
                       evidence)

    largest = max(real, key=lambda p: real[p])
    largest_size = real[largest]
    evidence["largest"] = largest
    evidence["largest_bytes"] = largest_size

    info: MediaInfo | None = None
    try:
        info = probe(largest, settings.ffprobe_path)
    except ProbeError as exc:
        # ffprobe refusing the file *is* evidence, and it is the evidence this
        # application exists to gather. A release whose only video will not
        # open is a release another copy fixes.
        evidence["probe_error"] = str(exc)
        return Verdict("bad_release",
                       f"the only video in this download will not open: {exc}",
                       evidence)

    evidence["duration"] = round(info.duration, 1)
    evidence["container"] = info.container
    if info.video is not None:
        evidence["codec"] = info.video.codec_name

    if info.video is None:
        return Verdict("bad_release",
                       "the largest video file has no video stream in it",
                       evidence)

    if info.duration and info.duration < settings.integrity.min_duration_seconds:
        return Verdict("bad_release",
                       f"the only video is {info.duration:.0f}s long, under "
                       f"the {settings.integrity.min_duration_seconds}s floor",
                       evidence)

    # A complete, openable, full-length video that the *arr will not take.
    # Whatever is wrong is on the *arr's side of the line, and another copy of
    # this release will block in exactly the same way.
    return Verdict(
        "manual",
        "the download is complete and the video opens cleanly — the *arr is "
        "refusing it for a reason another copy will not fix. This wants a "
        "manual import, not a blocklist",
        evidence)


def _size_of(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# -- the pass -------------------------------------------------------------


@dataclass
class PassResult:
    """What one sweep of the queue found and did."""

    queued: int = 0
    blocked: int = 0
    ripe: int = 0                 # blocked for longer than the timer
    inspected: int = 0
    bad: int = 0
    manual: int = 0
    unrecognised: int = 0
    acted: int = 0
    aborted: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queued": self.queued, "blocked": self.blocked, "ripe": self.ripe,
            "inspected": self.inspected, "bad": self.bad,
            "manual": self.manual, "unrecognised": self.unrecognised,
            "acted": self.acted, "aborted": self.aborted,
            "errors": list(self.errors),
        }


def abort_reason(blocked: int, queued: int, cfg) -> str | None:
    """Whether this queue looks broken rather than unlucky.

    The direct analogue of `Policy.abort_if_failure_ratio_over`, and it exists
    for the identical reason: when SABnzbd stops, when qBittorrent loses its
    session, when the downloads mount goes away, *every* item fails its import
    in the same pass. A tool that reads that as a run of bad releases empties
    the library's future in one sweep, and blocklisting is the one thing here
    that is not undoable by putting a file back.

    The floor is the part the file-side brake does not need. A library has
    thousands of files, so a ratio is meaningful on its own; a queue often has
    three, and two blocked out of three is a ratio of 0.67 that means nothing
    at all.
    """
    if queued < max(1, cfg.abort_ratio_min_items):
        return None
    ratio = blocked / queued
    if ratio > cfg.abort_if_blocked_ratio_over:
        return (f"{blocked} of {queued} queued downloads are blocked "
                f"({ratio:.0%}) — that is the download client or the mount, "
                f"not a run of bad releases. Flagged, nothing removed")
    return None


class IntakeWatcher:
    """Sweeps the queue, records what it finds, and acts within the brakes."""

    def __init__(self, settings_getter):
        self._settings = settings_getter

    # -- clients ----------------------------------------------------------

    def _clients(self) -> list[tuple[str, ArrClient]]:
        s = self._settings()
        out: list[tuple[str, ArrClient]] = []
        if s.sonarr.enabled and s.sonarr.url:
            out.append(("sonarr", ArrClient(s.sonarr, "sonarr")))
        if s.radarr.enabled and s.radarr.url:
            out.append(("radarr", ArrClient(s.radarr, "radarr")))
        return out

    def _mappings(self, source: str) -> list[dict[str, str]]:
        s = self._settings()
        return (s.sonarr if source == "sonarr" else s.radarr).path_mappings

    # -- the sweep --------------------------------------------------------

    def run_pass(self, force: bool = False) -> PassResult:
        """One sweep. ``force`` acts even when the policy says flag."""
        from .state import state as app_state

        s = self._settings()
        cfg = s.intake
        out = PassResult()
        if not cfg.enabled and not force:
            return out

        items: list[QueueItem] = []
        # Only the *arrs that actually answered. An unreachable Sonarr must
        # not look like an empty Sonarr: `_mark_gone` would then retire every
        # item it was tracking, losing `blocked_since` on all of them, and the
        # min-blocked timer would restart from zero the moment it came back.
        answered: set[str] = set()
        for source, client in self._clients():
            try:
                records = client.queue()
            except ArrError as exc:
                out.errors.append(f"{source}: {exc}")
                db.log("intake_queue_failed", "warn", None,
                       {"source": source, "error": str(exc)})
                continue
            answered.add(source)
            mappings = self._mappings(source)
            for record in records:
                item = normalise(record, source, mappings)
                if item is not None:
                    items.append(item)

        out.queued = len(items)
        now = time.time()
        seen: set[tuple[str, str]] = set()
        for item in items:
            seen.add((item.source, item.download_id))
            self._upsert(item, now)

        # Anything we knew about that is no longer in the queue has been
        # imported, removed by hand, or removed by something else. Mark it,
        # do not delete it: "did we already act on this" has to stay
        # answerable, and a verdict someone disagreed with should still be
        # there to look at.
        self._mark_gone(seen, answered, now)

        blocked = [i for i in items if i.blocked]
        out.blocked = len(blocked)

        aborted = abort_reason(len(blocked), len(items), cfg)
        if aborted:
            out.aborted = aborted
            db.log("intake_aborted", "warn", None, aborted)

        ripe = [i for i in blocked if self._blocked_long_enough(i, cfg, now)]
        out.ripe = len(ripe)

        acted = 0
        cap = max(0, cfg.max_actions_per_pass)
        for item in ripe:
            row = self._row(item)
            if row is not None and row["acted"]:
                continue

            verdict = self._verdict_for(item, row, s)
            if verdict.kind == "bad_release":
                out.bad += 1
            elif verdict.kind == "manual":
                out.manual += 1
            elif verdict.kind == "unrecognised":
                out.unrecognised += 1
            if row is None or row["verdict"] != verdict.kind:
                out.inspected += 1

            self._store_verdict(item, verdict, now)

            if not verdict.actionable:
                continue
            if aborted or app_state.paused:
                continue
            if not (force or cfg.action == "fix"):
                continue
            if acted >= cap:
                db.log("intake_capped", "warn", None,
                       {"cap": cap, "remaining": out.bad - acted})
                break
            if self._act(item, verdict):
                acted += 1

        out.acted = acted
        level = "warn" if (out.bad or out.errors or out.aborted) else "info"
        db.log("intake_pass", level, None, out.as_dict())
        bus.publish("intake", out.as_dict())
        return out

    # -- persistence ------------------------------------------------------

    @staticmethod
    def _row(item: QueueItem):
        return db.q1("SELECT * FROM intake WHERE source=? AND download_id=?",
                     (item.source, item.download_id))

    def _upsert(self, item: QueueItem, now: float) -> None:
        """Record the item, keeping `blocked_since` across queue-id churn.

        `blocked_since` is cleared the moment an item goes back to a working
        state, so a download that blocks, retries, imports halfway and blocks
        again starts its timer from the second block rather than the first.
        """
        row = self._row(item)
        blocked_since: float | None
        if item.blocked:
            blocked_since = (row["blocked_since"] if row and row["blocked_since"]
                             else now)
        else:
            blocked_since = None
        payload = (
            item.queue_id, item.title, item.protocol, item.indexer,
            item.download_client, item.arr_parent_id,
            json.dumps(item.arr_episode_ids), item.output_path,
            item.local_path, item.size, item.state,
            json.dumps(item.messages), blocked_since, now,
        )
        if row is None:
            db.ex(
                "INSERT INTO intake (queue_id, title, protocol, indexer, "
                "download_client, arr_parent_id, arr_episode_ids, output_path, "
                "local_path, size, state, messages, blocked_since, last_seen, "
                "source, download_id, first_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*payload, item.source, item.download_id, now))
        else:
            db.ex(
                "UPDATE intake SET queue_id=?, title=?, protocol=?, indexer=?, "
                "download_client=?, arr_parent_id=?, arr_episode_ids=?, "
                "output_path=?, local_path=?, size=?, state=?, messages=?, "
                "blocked_since=?, last_seen=?, gone=NULL "
                "WHERE source=? AND download_id=?",
                (*payload, item.source, item.download_id))

    @staticmethod
    def _mark_gone(seen: set[tuple[str, str]], answered: set[str],
                   now: float) -> None:
        for row in db.q("SELECT source, download_id FROM intake "
                        "WHERE gone IS NULL"):
            if row["source"] not in answered:
                continue
            if (row["source"], row["download_id"]) not in seen:
                db.ex("UPDATE intake SET gone=? WHERE source=? AND download_id=?",
                      (now, row["source"], row["download_id"]))

    @staticmethod
    def _blocked_long_enough(item: QueueItem, cfg, now: float) -> bool:
        row = db.q1("SELECT blocked_since FROM intake WHERE source=? "
                    "AND download_id=?", (item.source, item.download_id))
        since = row["blocked_since"] if row else None
        if not since:
            return False
        return (now - since) >= cfg.min_blocked_minutes * 60

    def _verdict_for(self, item: QueueItem, row, s: Settings) -> Verdict:
        """Reuse the stored verdict unless something has changed.

        `inspect` runs ffprobe, so re-deciding an unchanged item every ten
        minutes would be a subprocess per blocked download per pass, for ever,
        to reach the answer already on the row.
        """
        if row is not None and row["verdict"] and row["state"] == item.state:
            try:
                evidence = json.loads(row["evidence"] or "{}")
            except (TypeError, ValueError):
                evidence = {}
            return Verdict(row["verdict"], row["reason"] or "", evidence)

        triaged = triage(item)
        extra = [p.strip().lower() for p in s.intake.never_act_phrases
                 if p.strip()]
        text = item.all_text
        for phrase in extra:
            if phrase in text:
                return Verdict("manual",
                               f"matches a phrase you told unfuckarr never to "
                               f"act on ({phrase!r})", {"matched": phrase})
        return inspect(item, s, triaged)

    @staticmethod
    def _store_verdict(item: QueueItem, verdict: Verdict, now: float) -> None:
        db.ex("UPDATE intake SET verdict=?, reason=?, evidence=?, last_seen=? "
              "WHERE source=? AND download_id=?",
              (verdict.kind, verdict.reason, json.dumps(verdict.evidence),
               now, item.source, item.download_id))

    # -- acting -----------------------------------------------------------

    def _act(self, item: QueueItem, verdict: Verdict) -> bool:
        """Remove, blocklist and let the *arr re-search. Returns True on success."""
        s = self._settings()
        cfg = s.intake
        client = None
        for source, candidate in self._clients():
            if source == item.source:
                client = candidate
                break
        if client is None:
            return False

        job_id = db.ex(
            "INSERT INTO jobs (kind, path, state, message, created) "
            "VALUES (?,?,?,?,?)",
            ("intake", item.local_path or item.title, "running",
             verdict.reason, time.time()))
        try:
            client.remove_from_queue(
                item.queue_id,
                blocklist=cfg.blocklist,
                remove_from_client=cfg.remove_from_client,
                # False so the *arr blocklists *and* searches in one call —
                # invariant 2. A separate search would give the indexer a
                # window in which to hand back the release being rejected.
                skip_redownload=False)
        except ArrError as exc:
            db.ex("UPDATE jobs SET state='failed', finished=?, error=? WHERE id=?",
                  (time.time(), str(exc), job_id))
            db.log("intake_remove_failed", "error", item.local_path or None,
                   {"title": item.title, "error": str(exc)})
            return False

        steps = ["removed from the queue"]
        if cfg.remove_from_client:
            steps.append("removed from the download client")
        if cfg.blocklist:
            steps.append("release blocklisted, replacement search queued")
        db.ex("UPDATE intake SET acted=?, outcome=? WHERE source=? AND download_id=?",
              (time.time(), "; ".join(steps), item.source, item.download_id))
        db.ex("UPDATE jobs SET state='done', progress=1.0, finished=?, message=? "
              "WHERE id=?", (time.time(), "; ".join(steps), job_id))
        db.bump("intake_removed")
        if cfg.blocklist:
            db.bump("redownloads")
        db.log("intake_removed", "warn", item.local_path or None, {
            "title": item.title, "source": item.source,
            "reason": verdict.reason, "evidence": verdict.evidence,
            "steps": steps,
        })
        bus.publish("intake_removed", {"title": item.title,
                                       "reason": verdict.reason})
        return True

    # -- housekeeping -----------------------------------------------------

    @staticmethod
    def sweep(keep_days: int = 30) -> None:
        """Age out rows for downloads that left the queue long ago."""
        if keep_days <= 0:
            return
        cutoff = time.time() - keep_days * 86400
        db.ex("DELETE FROM intake WHERE gone IS NOT NULL AND gone < ?", (cutoff,))


__all__ = [
    "QueueItem", "Verdict", "Contents", "PassResult", "IntakeWatcher",
    "normalise", "triage", "inspect", "read_contents", "looks_like_sample",
    "abort_reason", "BLOCKED_STATES", "WORKING_STATES", "ARR_SIDE_MARKERS",
]

