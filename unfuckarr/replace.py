"""Replacing a heavily re-encoded file with a better release, safely.

`research` asks the *arr to look for something better and leaves the rest to
it. On this library that asks for nothing: every Radarr profile has upgrades
off and every Sonarr profile has its cutoff at the lowest quality it allows
(measured 2026-09-22, still true 2026-11-03), so the *arr refuses every
release the search finds and says so to nobody. 1,542 episodes sat at
`very_low_bitrate` with no way out.

So this does the part the *arr will not. It runs the same interactive search
the *arr's UI runs, reads the releases *and* the reasons each was refused,
and overrules only the refusals that say "you already have this quality".
Anything refused for another reason — wrong episode, no seeders, blocklisted,
a language the profile does not want — stays refused. Of what is left it grabs
the smallest release that is clearly better, judged on bitrate per pixel
rather than on the quality name, because the quality name is what got the
library here: "HDTV-1080p" says nothing about whether the release is 4 Mbps
or 0.6.

Then nothing happens to the old file until something better has arrived:

1. Before the grab, a copy of the original goes into unfuckarr's recycle bin
   (`recycle.keep_copy`, a hard link — no space, no time). Neither *arr here
   has a recycle bin, and a same-quality release passes the *arr's own import
   check, so the *arr may import the download and remove the old file the
   minute it finishes. The copy is what makes that safe.
2. When the *arr holds the download instead ("Not an upgrade for existing
   episode file(s)", which Sonarr 4.0.20 leaves `importPending` with a
   warning, not `importBlocked` — `held`), `advance` opens it, checks it the way every other file
   is checked, and only if it is genuinely better asks the *arr to import it
   (Manual Import), which replaces the old file the *arr's own way round.
   A download that fails the check is removed and blocklisted, and the
   original was never touched.
3. When the *arr imported it by itself, `advance` checks what it imported. A
   good one drops the copy; a worse one keeps it, and says so.

The copy is dropped only when the replacement passed or the original is still
where it was. Every other ending leaves it in the bin.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any, Callable

from . import db, intake, recycle
from .clients.arr import ArrClient, ArrError
from .config import Settings
from .probe import MediaInfo

log = logging.getLogger(__name__)

# How much more picture a codec gets out of a bit than H.264 does — near enough
# to rank releases by, which is all it is for. The finding itself is
# codec-blind (under a megabit at 720p and up), and taking that at face value
# trades like for like: the first live search offered, for a 0.71 Mbps HEVC
# episode, a 1.5 Mbps x264 one that clears the finding and is the same picture.
CODEC_EFFICIENCY = {
    "h264": 1.0, "hevc": 2.0, "av1": 2.5, "vp9": 1.8,
    "mpeg4": 0.7, "msmpeg4v3": 0.6, "mpeg2video": 0.5, "vc1": 0.9,
}

# Release titles name the codec, when they name it at all, in these ways. A
# title that names none counts as H.264: the least efficient guess anyone would
# make for a modern release, so an unnamed codec can only make a release look
# *less* of an improvement than it is, never more.
_TITLE_CODECS = (
    (re.compile(r"(?i)\b(x\.?265|h\.?265|hevc)\b"), "hevc"),
    (re.compile(r"(?i)\bav1\b"), "av1"),
    (re.compile(r"(?i)\bvp9\b"), "vp9"),
    (re.compile(r"(?i)\b(xvid|divx)\b"), "mpeg4"),
    (re.compile(r"(?i)\b(x\.?264|h\.?264|avc)\b"), "h264"),
)

# A release has to promise at least twice the picture of the file it replaces,
# as estimated from its size. Twice, because the estimate is coarse — the size
# includes the audio, and the runtime is the old file's — and a margin is what
# keeps a sidegrade from being downloaded on a guess.
MIN_GAIN = 2.0
# The download itself, measured, has to come in at 1.5 times. Lower than the
# promise on purpose: the promise has already been checked, and this is
# here to catch a release that lied about its size or its content, not to
# re-litigate a choice made on good evidence.
MIN_MEASURED_GAIN = 1.5
# And the replacement must clear the finding with room to spare, or it is
# grabbed, imported, and flagged for the same thing the next night. The
# finding's floor is 1.0 Mbps (`checks/hygiene.py`).
MIN_RAW_MBPS = 1.25
# The replacement's running time against the file's. A broadcast episode and
# its WEB release differ by a minute or two; an extended cut against the
# ordinary one is ten or more, and losing those minutes is not an upgrade.
DURATION_TOLERANCE = (0.90, 1.15)
# A release the indexers say has fewer seeders than this is slow at best.
# Usenet has no such thing and always passes.
MIN_SEEDERS = 3

# Every grab costs bandwidth and indexer goodwill, and a third failure for the
# same episode says the indexers do not have a better copy.
MAX_ATTEMPTS = 3
# A download that has not finished after this long is not going to.
EXPIRE_DAYS = 3
# A grab that is in neither the queue nor the history this soon may simply not
# have reached the download client yet.
VANISH_GRACE_SECONDS = 30 * 60
# A manual import takes seconds. One whose download is still in the queue an
# hour later did not take, and an open row blocks the episode for good.
IMPORT_GRACE_SECONDS = 60 * 60

OPEN_STATES = ("grabbed", "importing")

# The refusals that mean only "you already have this or better": the ones a
# profile with upgrades off or its cutoff at the floor puts on every release.
# Substring, lower case, short — the *arrs reword these between versions, and
# a miss here can only make a release *ineligible*, never the other way.
# Verified against Sonarr 4.0.20, 2026-11-03: "Existing file on disk is of
# equal or higher preference: HDTV-720p v1" and "Existing file on disk has a
# equal or higher Custom Format score: 0".
UPGRADE_REFUSALS = (
    "equal or higher",
    "meets cutoff",
    "cutoff has already been met",
    "does not allow upgrades",
    "not an upgrade",
)
# ...except when it is about something already in the queue: then a download
# for this episode is already on its way and a second one would race it.
QUEUE_REFUSAL = "queue"


# -- measuring -------------------------------------------------------------

def codec_of_title(title: str) -> str:
    for pattern, codec in _TITLE_CODECS:
        if pattern.search(title or ""):
            return codec
    return "h264"


def efficiency(codec: str | None) -> float:
    return CODEC_EFFICIENCY.get((codec or "").lower(), 1.0)


def tier(width: int, height: int) -> int:
    """The resolution class of a picture, as the *arrs name them.

    Width counts, because a 2.39:1 film at 1920x800 is a 1080p release, and
    reading its height alone would let a 720p one "match" it.
    """
    effective = max(height or 0, round((width or 0) * 9 / 16))
    for standard in (2160, 1080, 720, 576, 480, 360):
        if effective >= standard * 0.9:
            return standard
    return effective


def measure(info: MediaInfo | None = None,
            summary: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Bitrate, H.264-equivalent bitrate, duration and tier of a file.

    From a probe when there is one, else from the stored summary. The bitrate
    is the whole file's — size over duration — because that is what the
    finding measures and what a release's size can be compared with.
    """
    if info is not None:
        v = info.video
        size, duration = info.size, info.duration
        width, height = (v.width, v.height) if v else (0, 0)
        codec = v.codec_name if v else ""
    elif summary:
        v = summary.get("video") or {}
        size, duration = summary.get("size") or 0, summary.get("duration") or 0
        width, height = v.get("width") or 0, v.get("height") or 0
        codec = v.get("codec") or ""
    else:
        return None
    if not size or not duration:
        return None
    mbps = size * 8 / duration / 1_000_000
    return {"mbps": round(mbps, 3), "equiv": round(mbps * efficiency(codec), 3),
            "duration": float(duration), "tier": tier(width, height),
            "codec": codec}


# -- choosing ----------------------------------------------------------------

def _refusals(release: dict[str, Any]) -> list[str]:
    out = []
    for r in release.get("rejections") or []:
        out.append(r if isinstance(r, str) else str((r or {}).get("reason") or r))
    return out


def choose(releases: list[dict[str, Any]], current: dict[str, Any], *,
           episode_count: int = 1, sonarr: bool = True,
           tried: set[str] | None = None) -> tuple[dict[str, Any] | None, Counter]:
    """The release to grab, or None — and why each of the others was not.

    Pure, for the same reason `remediation.decide` is. ``current`` is
    `measure` of the file on disk; ``tried`` holds the guids and titles of
    releases grabbed for this file before, which are never grabbed again
    whatever happened to them.

    Of the releases that pass, the smallest wins, with weakly seeded torrents
    last. Every one that passes is already a clear improvement, so the rest is
    cost: the smallest is the quickest to arrive and the least space, and the
    library's own shrink can do the rest later at a measured quality.
    """
    tried = tried or set()
    skipped: Counter = Counter()
    good: list[tuple[tuple, dict[str, Any], float]] = []
    duration = current["duration"]
    for r in releases:
        title = r.get("title") or ""
        refusals = _refusals(r)
        other = [x for x in refusals
                 if QUEUE_REFUSAL in x.lower()
                 or not any(m in x.lower() for m in UPGRADE_REFUSALS)]
        if other:
            # The kind of refusal, not its detail: "Episode wasn't requested:
            # 43x1-9" and "...: 49x1-9" are one reason in a summary.
            skipped[f"refused: {other[0].split(':')[0][:60]}"] += 1
            continue
        if r.get("downloadAllowed") is False:
            skipped["the *arr cannot match it to the library"] += 1
            continue
        if r.get("guid") in tried or title in tried:
            skipped["grabbed for this file before"] += 1
            continue
        if sonarr:
            if r.get("fullSeason"):
                skipped["season pack"] += 1
                continue
            eps = r.get("mappedEpisodeNumbers") or r.get("episodeNumbers") or []
            if len(eps) != episode_count:
                skipped["covers a different number of episodes"] += 1
                continue
        quality = ((r.get("quality") or {}).get("quality") or {})
        resolution = int(quality.get("resolution") or 0)
        if not resolution:
            skipped["unknown resolution"] += 1
            continue
        if resolution < current["tier"]:
            skipped["lower resolution than the file"] += 1
            continue
        size = int(r.get("size") or 0)
        if not size or not duration:
            skipped["no size"] += 1
            continue
        mbps = size * 8 / duration / 1_000_000
        if mbps < MIN_RAW_MBPS:
            skipped["would still be heavily re-encoded"] += 1
            continue
        equiv = mbps * efficiency(codec_of_title(title))
        if equiv < MIN_GAIN * current["equiv"]:
            skipped["not enough better"] += 1
            continue
        weak = (r.get("protocol") == "torrent"
                and (r.get("seeders") or 0) < MIN_SEEDERS)
        good.append(((weak, size), r, equiv))
    if not good:
        return None, skipped
    good.sort(key=lambda g: g[0])
    best = dict(good[0][1])
    best["_est_equiv"] = round(good[0][2], 3)
    return best, skipped


# -- bookkeeping -------------------------------------------------------------

def episode_ids(row: dict[str, Any]) -> list[int]:
    """The row's episode ids, whether it came decoded or straight off SQLite."""
    raw = row.get("arr_episode_ids")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return [int(e) for e in (raw or [])]


def arr_key(row: dict[str, Any]) -> str | None:
    """What a replacement is *for*: the episode or film, not the path.

    The path is the one thing a successful replacement changes, so counting
    attempts by it would start the count again every time one landed.
    """
    source = row.get("source")
    if source == "sonarr":
        eps = sorted(episode_ids(row))
        return f"sonarr:{','.join(map(str, eps))}" if eps else None
    if source == "radarr" and row.get("arr_parent_id"):
        return f"radarr:{int(row['arr_parent_id'])}"
    return None


def owns(source: str, download_id: str) -> bool:
    """Whether a download in the queue is one of ours that is still open.

    `intake` asks before it judges a blocked import, because ours are blocked
    on purpose and the replacement pass decides what happens to them: two
    owners for one queue item is the race invariant 24 is about.
    """
    return db.q1("SELECT 1 FROM replacements WHERE source=? AND download_id=? "
                 "AND state IN ('grabbed','importing')",
                 (source, download_id)) is not None


def blocked(row: dict[str, Any], s: Settings) -> str | None:
    """Why this file should not have a replacement grabbed now, or None.

    Asked by the scan before it spends a capped slot and again inside the
    job, exactly as `research_blocked` is (invariant 25).
    """
    key = arr_key(row)
    if key is None or not row.get("arr_parent_id"):
        return "no Sonarr or Radarr entry owns this file"
    if s.policy.recycle_bin_days <= 0:
        return ("the recycle bin is off, and the *arr may import a replacement "
                "by itself — with no copy kept, the original would go before "
                "anything had checked what replaced it")
    if db.q1("SELECT 1 FROM replacements WHERE arr_key=? AND state IN "
             "('grabbed','importing')", (key,)):
        return "a replacement is already on its way"
    attempts = db.q1("SELECT COUNT(*) n FROM replacements WHERE arr_key=? "
                     "AND state <> 'imported'", (key,))["n"]
    if attempts >= MAX_ATTEMPTS:
        return (f"{attempts} replacements already tried and none was better — "
                "the indexers do not seem to have one")
    last = row.get("last_research")
    if last:
        days = (time.time() - float(last)) / 86400
        if days < s.policy.research_after_days:
            return (f"searched {days:.0f} days ago; waiting "
                    f"{s.policy.research_after_days} between searches")
    return None


def _tried(key: str) -> set[str]:
    out: set[str] = set()
    for r in db.q("SELECT guid, title FROM replacements WHERE arr_key=?", (key,)):
        out.update(x for x in (r["guid"], r["title"]) if x)
    return out


# -- starting one ------------------------------------------------------------

def start(row: dict[str, Any], info: MediaInfo | None, client: ArrClient,
          s: Settings) -> dict[str, Any]:
    """Search, choose, keep a copy, grab. Returns the job's outcome.

    Touches nothing in the library. The only file written is the copy in the
    recycle bin, and it is taken away again if the grab does not happen.
    """
    path = row["path"]
    why = blocked(row, s)
    if why is not None:
        return {"ok": True, "message": why}

    summary = None
    if info is None and row.get("probe"):
        try:
            summary = (json.loads(row["probe"]) if isinstance(row["probe"], str)
                       else row["probe"])
        except ValueError:
            summary = None
    current = measure(info, summary)
    if current is None:
        return {"ok": True, "message": "no measurement of the file to compare "
                                       "releases against"}

    key = arr_key(row)
    parent = int(row["arr_parent_id"])
    eps = episode_ids(row)
    sonarr = row.get("source") == "sonarr"
    try:
        releases = client.releases(parent, eps)
    except ArrError as exc:
        db.log("arr_search_failed", "error", path, str(exc))
        return {"ok": False, "message": f"search failed: {exc}"}

    # The cooldown starts at the search, whatever it finds: it rate-limits the
    # searching, and a search that finds nothing is the case it most needs
    # to cover.
    now = time.time()
    db.ex("UPDATE files SET last_research=?, "
          "research_attempts=COALESCE(research_attempts,0)+1 WHERE path=?",
          (now, path))

    choice, skipped = choose(releases, current, episode_count=max(1, len(eps)),
                             sonarr=sonarr, tried=_tried(key))
    if choice is None:
        message = (f"none of {len(releases)} releases is clearly better than "
                   f"{current['mbps']:.2f} Mbps {current['codec']} at "
                   f"{current['tier']}p")
        db.log("replacement_none_found", "info", path,
               {"releases": len(releases), "skipped": dict(skipped.most_common(8)),
                "current": current})
        return {"ok": True, "message": message}

    held = recycle.keep_copy(
        path, f"kept while a replacement downloads ({choice.get('title')})",
        s.policy.recycle_bin_path, s.policy.recycle_bin_days,
        max(0, s.policy.recycle_bin_max_gb) * 1024 ** 3)
    if held is None:
        message = "could not keep a copy of the original, so nothing was grabbed"
        db.log("replacement_not_grabbed", "warn", path, message)
        return {"ok": False, "message": message}

    try:
        client.grab(str(choice["guid"]), int(choice["indexerId"]))
    except (ArrError, KeyError, TypeError, ValueError) as exc:
        recycle.discard(held, "the grab it was kept for did not happen")
        db.log("replacement_grab_failed", "error", path, str(exc))
        return {"ok": False, "message": f"grab failed: {exc}"}

    download_id = None
    try:
        download_id = client.find_grab(parent, eps, choice.get("title") or "", now)
    except ArrError:
        pass  # named on a later pass; the title is enough to find it by

    db.ex("INSERT INTO replacements (arr_key, path, source, arr_parent_id, "
          "arr_episode_ids, title, guid, indexer, protocol, size, download_id, "
          "held_copy, old_mbps, old_duration, old_tier, est_mbps, state, "
          "message, grabbed, updated) "
          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (key, path, row.get("source"), parent, json.dumps(eps) if eps else None,
           choice.get("title"), choice.get("guid"), choice.get("indexer"),
           choice.get("protocol"), choice.get("size"), download_id, held,
           current["equiv"], current["duration"], current["tier"],
           choice["_est_equiv"], "grabbed", "waiting for the download", now, now))
    db.bump("replacements_grabbed")
    message = (f"grabbed {choice.get('title')} — about "
               f"{choice['_est_equiv'] / max(current['equiv'], 0.01):.1f}× the "
               "picture; this file stays until it has arrived and been checked")
    db.log("replacement_grabbed", "info", path, {
        "release": choice.get("title"), "size": choice.get("size"),
        "protocol": choice.get("protocol"), "est_equiv_mbps": choice["_est_equiv"],
        "current": current, "skipped": dict(skipped.most_common(8))})
    return {"ok": True, "message": message}


# -- following it through ----------------------------------------------------

def judge(local_path: str, rec: dict[str, Any],
          s: Settings) -> tuple[bool, str, dict[str, Any]]:
    """Whether a downloaded or imported file really is the better copy.

    The ordinary checks, run on it the way they run on everything, and then
    the three things that make it a *replacement*: the finding is gone, it is
    the same programme (running time), and it is no smaller a picture.
    """
    from .scanner import check_file  # scanner imports remediation imports this

    try:
        result, info = check_file(local_path, s)
    except Exception as exc:  # noqa: BLE001 - a bad download must not kill the pass
        return False, f"could not check it: {exc}", {}
    if info is None:
        return False, f"it would not open: {result.error or 'no media information'}", {}
    broken = [f for f in result.errors if f.category == "integrity"]
    if broken:
        return False, f"it is damaged: {broken[0].detail}", {}
    if any(f.code == "very_low_bitrate" for f in result.findings):
        return False, "it is heavily re-encoded too", {}
    m = measure(info) or {}
    if rec.get("old_duration") and m.get("duration"):
        ratio = m["duration"] / rec["old_duration"]
        if not DURATION_TOLERANCE[0] <= ratio <= DURATION_TOLERANCE[1]:
            return (False, f"it runs {m['duration'] / 60:.0f} min against the "
                    f"file's {rec['old_duration'] / 60:.0f} — a different cut", m)
    if rec.get("old_tier") and m.get("tier", 0) < rec["old_tier"]:
        return False, f"it is {m.get('tier')}p against the file's {rec['old_tier']}p", m
    if rec.get("old_mbps") and m.get("equiv", 0) < MIN_MEASURED_GAIN * rec["old_mbps"]:
        return (False, f"it measures {m.get('equiv', 0):.2f} H.264-equivalent Mbps "
                f"against the file's {rec['old_mbps']:.2f} — not enough better", m)
    return True, "better in every way measured", m


def _event_kind(event: Any) -> str:
    # The v3 API names its history events ("downloadFolderImported"; live,
    # both *arrs, 2026-11-03). The numbers behind them differ between Sonarr
    # and Radarr, so a number is not guessed at: it reads as unknown, and an
    # unknown event leaves the row to the vanish grace.
    return event if isinstance(event, str) else ""


def _update(rec_id: int, **fields: Any) -> None:
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    db.ex(f"UPDATE replacements SET {cols} WHERE id=?", (*fields.values(), rec_id))


def _finish(rec: dict[str, Any], state: str, message: str,
            verified: bool = False) -> None:
    """End one replacement, and decide what happens to the kept copy.

    The copy goes only when the replacement passed, or when the original is
    still in the library exactly as it was — which is the case for every
    ending in which nothing was imported. Anything else, and it stays.
    """
    held = rec.get("held_copy")
    kept = None
    if held and state == "imported_worse":
        copy = db.q1("SELECT stored FROM recycle WHERE id=?", (held,))
        kept = copy["stored"] if copy else None
    elif held:
        if verified:
            recycle.discard(held, "its replacement arrived and passed")
        else:
            copy = db.q1("SELECT stored FROM recycle WHERE id=?", (held,))
            same = False
            try:
                same = (copy is not None and os.path.exists(rec["path"])
                        and os.path.samestat(os.stat(rec["path"]),
                                             os.stat(copy["stored"])))
            except OSError:
                same = False
            if same or (copy is not None and os.path.exists(rec["path"])
                        and os.path.getsize(rec["path"]) == os.path.getsize(copy["stored"])):
                recycle.discard(held, "the original is still in place")
            elif copy is not None:
                kept = copy["stored"]
    _update(rec["id"], state=state, message=message, finished=time.time())
    level = "info" if state == "imported" else "warn"
    db.log(f"replacement_{state}", level, rec["path"],
           {"release": rec.get("title"), "message": message,
            **({"original_kept_at": kept} if kept else {})})


def advance(s: Settings, client_for: Callable[[str], ArrClient | None]) -> dict[str, int]:
    """Move every open replacement along one step. Safe to call often.

    Run from the queue worker, every `intake.poll_minutes`. That cadence
    matters for one reason outside this application: Cleanuparr runs beside
    it on lilnasx every 30 minutes and removes a download after three
    failed-import strikes. It only watches qBittorrent, so a usenet grab is
    never at risk; a torrent one is held for an hour or more before it would
    be, and this looks at it within ten minutes.
    """
    open_rows = [dict(r) for r in db.q(
        "SELECT * FROM replacements WHERE state IN ('grabbed','importing')")]
    out = Counter()
    if not open_rows:
        return dict(out)
    queues: dict[str, dict[str, intake.QueueItem]] = {}
    for rec in open_rows:
        client = client_for(rec["source"])
        if client is None:
            continue
        try:
            if rec["source"] not in queues:
                mappings = client.cfg.path_mappings
                queues[rec["source"]] = {
                    i.download_id: i for i in
                    (intake.normalise(r, rec["source"], mappings)
                     for r in client.queue()) if i is not None}
            out[_step(rec, client, queues[rec["source"]], s)] += 1
        except ArrError as exc:
            log.warning("replacement %s: %s", rec["id"], exc)
            out["error"] += 1
    if out:
        db.log("replacement_pass", "info", None, dict(out))
    return dict(out)


def _step(rec: dict[str, Any], client: ArrClient,
          queue: dict[str, intake.QueueItem], s: Settings) -> str:
    now = time.time()
    eps = episode_ids(rec)
    if not rec.get("download_id"):
        found = client.find_grab(rec["arr_parent_id"], eps, rec.get("title") or "",
                                 rec["grabbed"])
        if found:
            rec["download_id"] = found
            _update(rec["id"], download_id=found)
        elif now - rec["grabbed"] > VANISH_GRACE_SECONDS:
            _finish(rec, "vanished", "the *arr has no record of the grab")
            return "vanished"
        else:
            return "waiting"

    item = queue.get(rec["download_id"])
    if item is None:
        return _left_queue(rec, client, s, now)

    if rec["state"] == "importing":
        status = client.command_status(rec["command_id"]) if rec.get("command_id") else {}
        if status.get("status") == "failed":
            _finish(rec, "failed", "the *arr's manual import failed: "
                    + str(status.get("message") or status.get("exception") or "")[:200])
            return "failed"
        if now - rec["updated"] > IMPORT_GRACE_SECONDS:
            # Thrown back rather than left: once the row ends, intake would
            # judge the still-blocked download, and its `bad_release` path
            # re-searches — the race `owns` exists to stop.
            return _reject(rec, client, item, "the manual import did not take: "
                           "the download was still in the queue an hour later")
        return "importing"

    # A held download needs its own path: it is what tells the download's
    # files from the library's (`_held_by_arr`). Without one it waits out
    # the expiry rather than being judged on anything else.
    if held(item) and item.local_path:
        return _held_by_arr(rec, client, item, s)

    if now - rec["grabbed"] > EXPIRE_DAYS * 86400:
        try:
            client.remove_from_queue(item.queue_id, blocklist=True,
                                     remove_from_client=True, skip_redownload=True)
        except ArrError as exc:
            log.warning("could not remove expired replacement: %s", exc)
        _finish(rec, "expired", f"still not finished after {EXPIRE_DAYS} days")
        return "expired"
    return "downloading"


def held(item: intake.QueueItem) -> bool:
    """Whether the *arr has finished with a download and will not import it.

    `importBlocked` is not the only way it says so, nor the usual one for
    ours. Sonarr 4.0.20 keeps `importBlocked` for its own trouble with a
    release (matched by id, an unknown series). A download it refuses to
    import over a file of equal or better quality stays `importPending` with
    a warning, retried every minute and refused every time. Live,
    2026-11-03, BattleBots S06E02: "Not an upgrade for existing episode
    file(s). Existing quality: WEBDL-720p. New Quality HDTV-1080p." So that
    counts too, but only for an upgrade refusal: "No files found are
    eligible for import" is the *arr not seeing the file at all, which a
    check from here could only misread as an empty download.
    """
    if item.state in ("failed", "failedPending"):
        return False
    if item.blocked:
        return True
    return (item.state == "importPending"
            and any(m in item.all_text for m in UPGRADE_REFUSALS))


def _held_by_arr(rec: dict[str, Any], client: ArrClient,
                 item: intake.QueueItem, s: Settings) -> str:
    """The download finished and the *arr will not import it. Check it, and
    either have it imported or throw it back."""
    eps = set(episode_ids(rec))
    mine = []
    for c in client.manual_import_candidates(rec["download_id"]):
        local = client.local_path(c.get("path") or "")
        if not _inside(local, item.local_path):
            # Never a file from outside the download. The library's own copy
            # of this episode is what such a list has held, live
            # (`ArrClient.manual_import_candidates`): judged, it fails as the
            # heavily re-encoded file it is, and the good download goes back.
            continue
        refusals = " ".join(str((x or {}).get("reason", x)) if isinstance(x, dict)
                            else str(x) for x in c.get("rejections") or []).lower()
        if "sample" in refusals:
            continue
        if rec["source"] == "sonarr":
            if not eps <= {int(e.get("id")) for e in c.get("episodes") or []}:
                continue
        mine.append((c, local))
    if not mine:
        return _reject(rec, client, item, "nothing in the download is this "
                       + ("episode" if rec["source"] == "sonarr" else "film"))
    best, local = max(mine, key=lambda m: m[0].get("size") or 0)
    ok, why, measured = judge(local, rec, s)
    if not ok:
        return _reject(rec, client, item, why)
    command = client.manual_import(best, rec["arr_parent_id"], sorted(eps),
                                   rec["download_id"])
    _update(rec["id"], state="importing", command_id=command, new_path=local,
            message=f"checked and better ({why}); importing")
    db.log("replacement_accepted", "info", rec["path"],
           {"release": rec.get("title"), "measured": measured})
    return "importing"


def _inside(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` itself or somewhere under it."""
    if not path or not root:
        return False
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _reject(rec: dict[str, Any], client: ArrClient, item: intake.QueueItem,
            why: str) -> str:
    """Throw the download back. Blocklisted so neither we nor the *arr grab
    it again; no re-search, because the next attempt is ours to choose."""
    try:
        client.remove_from_queue(item.queue_id, blocklist=True,
                                 remove_from_client=True, skip_redownload=True)
    except ArrError as exc:
        log.warning("could not remove rejected replacement: %s", exc)
    _finish(rec, "rejected", why)
    return "rejected"


def _left_queue(rec: dict[str, Any], client: ArrClient, s: Settings,
                now: float) -> str:
    """Gone from the queue: imported, failed, or removed by someone else."""
    events = client.history_for_download(rec["download_id"])
    kinds = {_event_kind(e.get("eventType")): e for e in events}
    imported = kinds.get("downloadFolderImported")
    if imported is not None:
        data = imported.get("data") or {}
        local = client.local_path(data.get("importedPath") or "") or rec.get("new_path")
        if rec["state"] == "importing":
            # Ours: checked before it was imported.
            _finish(rec, "imported", "imported after it was checked", verified=True)
            _update(rec["id"], new_path=local)
            db.bump("files_replaced")
            return "imported"
        # The *arr took it by itself. Now check what it took.
        if not local or not os.path.exists(local):
            _finish(rec, "imported_worse", "the *arr imported it, and the "
                    "imported file cannot be found to check")
            return "imported_worse"
        ok, why, _ = judge(local, rec, s)
        _update(rec["id"], new_path=local)
        if ok:
            _finish(rec, "imported", f"the *arr imported it; {why}", verified=True)
            db.bump("files_replaced")
            return "imported"
        _finish(rec, "imported_worse", f"the *arr imported it and {why}")
        return "imported_worse"
    if "downloadFailed" in kinds or "downloadIgnored" in kinds:
        _finish(rec, "failed", "the download failed")
        return "failed"
    if now - rec["grabbed"] > VANISH_GRACE_SECONDS:
        _finish(rec, "vanished", "left the queue without being imported")
        return "vanished"
    return "waiting"
