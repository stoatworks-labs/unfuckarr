"""Replacing a heavily re-encoded file with a release unfuckarr has checked.

Two questions run through every test here, the same two `test_research.py`
asks of a plain re-search: is the right thing grabbed, and is the old file
still there until something better has arrived. The second has a sharper edge
than it does for a re-search, because this path does grab, and a grab the *arr
imports by itself removes the old file at once — so the copy in the recycle
bin is under test as much as the choice is.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from unfuckarr import db, replace
from unfuckarr.checks import CheckResult, Finding
from unfuckarr.probe import MediaInfo, Stream
from unfuckarr.remediation import Decision, Remediator, decide


def reencoded() -> CheckResult:
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("hygiene", "very_low_bitrate", "warning", "0.71 Mbps at 720p"))
    return r


def media(size: int, duration: float, width: int = 1280, height: int = 720,
          codec: str = "hevc", path: str = "/media/x.mkv") -> MediaInfo:
    return MediaInfo(path=path, size=size, duration=duration, streams=[
        Stream(index=0, codec_type="video", codec_name=codec,
               width=width, height=height, fps=25.0),
        Stream(index=1, codec_type="audio", codec_name="aac", channels=2,
               language="eng", is_default=True)])


# -- the decision ----------------------------------------------------------

def test_the_default_leaves_low_bitrate_to_unfixable_action(settings):
    assert settings.policy.low_bitrate_action == "unfixable"
    assert decide(reencoded(), settings).action == "flag"
    settings.policy.unfixable_action = "research"
    assert decide(reencoded(), settings).action == "research"


def test_replace_is_decided_when_asked(settings):
    settings.policy.low_bitrate_action = "replace"
    assert decide(reencoded(), settings).action == "replace"


def test_only_low_bitrate_is_replaced(settings):
    """Image-only subtitles have no size to judge a release by."""
    settings.policy.low_bitrate_action = "replace"
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("hygiene", "image_subtitles_only", "warning", ""))
    assert decide(r, settings).action == "flag"


def test_a_fixable_tidy_still_goes_first(settings):
    """The cheap remux clears the tag; the next pass replaces the file."""
    settings.policy.low_bitrate_action = "replace"
    settings.policy.hygiene_action = "transcode"
    r = reencoded()
    r.add(Finding("hygiene", "audio_missing_language", "warning", ""))
    assert decide(r, settings).action == "transcode"


def test_hygiene_none_silences_it(settings):
    settings.policy.low_bitrate_action = "replace"
    settings.policy.hygiene_action = "none"
    assert decide(reencoded(), settings).action == "none"


def test_replace_can_never_be_a_delete(settings):
    """The literal type is the enforcement, as for hygiene_action."""
    from unfuckarr.config import Policy
    allowed = Policy.model_fields["low_bitrate_action"].annotation.__args__
    assert "redownload" not in allowed and "delete" not in allowed


# -- choosing --------------------------------------------------------------

# The file the first live search was run for: Have I Got News for You
# S49E07, HEVC 720p, 224 MB over 42 minutes.
CURRENT = {"mbps": 0.711, "equiv": 1.422, "duration": 2522.0, "tier": 720,
           "codec": "hevc"}

UPGRADE = "Existing file on disk is of equal or higher preference: HDTV-720p v1"
CF = "Existing file on disk has a equal or higher Custom Format score: 0"


def release(title: str, gb: float, resolution: int = 1080, *,
            rejections=(UPGRADE, CF), protocol: str = "usenet", seeders=None,
            eps=(7,), full_season: bool = False, guid: str | None = None):
    return {"title": title, "guid": guid or title, "indexerId": 4,
            "size": int(gb * 1e9), "protocol": protocol, "seeders": seeders,
            "rejections": list(rejections), "downloadAllowed": True,
            "fullSeason": full_season, "episodeNumbers": list(eps),
            "mappedEpisodeNumbers": list(eps),
            "quality": {"quality": {"name": f"WEBDL-{resolution}p",
                                    "resolution": resolution}}}


def test_a_sidegrade_in_a_worse_codec_is_not_an_upgrade():
    """The live case: 1.5 Mbps x264 clears the finding and is the same
    picture as 0.71 Mbps HEVC."""
    choice, skipped = replace.choose(
        [release("HIGNFY.S49E07.720p.HDTV.x264-SNEAkY", 0.48, 720)], CURRENT)
    assert choice is None
    assert skipped["not enough better"] == 1


def test_the_smallest_clear_improvement_wins():
    choice, _ = replace.choose([
        release("A.S49E07.1080p.WEB.h264", 3.2),
        release("B.S49E07.1080p.WEB.h264", 1.2),
        release("C.S49E07.1080p.WEB.x265", 0.6),   # 1.9 Mbps HEVC = 3.8 equiv
    ], CURRENT)
    assert choice["title"] == "C.S49E07.1080p.WEB.x265"


def test_refusals_other_than_already_have_it_stand():
    choice, skipped = replace.choose([
        release("A.S49E07.1080p.WEB.h264", 1.2,
                rejections=(UPGRADE, "Not enough seeders: 0. Minimum seeders: 1")),
        release("B.S49E07.1080p.WEB.h264", 1.3, rejections=("Unable to parse release",)),
        release("C.S49E07.1080p.WEB.h264", 1.4,
                rejections=("Release in queue already meets cutoff: WEBDL-1080p",)),
    ], CURRENT)
    assert choice is None
    assert sum(n for k, n in skipped.items() if k.startswith("refused")) == 3


def test_season_packs_and_other_episodes_are_skipped():
    choice, skipped = replace.choose([
        release("S49.1080p.WEB.h264", 30.0, full_season=True, eps=()),
        release("S49E07E08.1080p.WEB.h264", 2.4, eps=(7, 8)),
    ], CURRENT)
    assert choice is None
    assert skipped["season pack"] == 1
    assert skipped["covers a different number of episodes"] == 1


def test_a_lower_resolution_is_never_an_upgrade():
    choice, skipped = replace.choose(
        [release("A.S49E07.480p.h264", 1.5, 480)], CURRENT)
    assert choice is None and skipped["lower resolution than the file"] == 1


def test_unknown_quality_is_skipped():
    choice, _ = replace.choose([release("A.S49E07", 1.5, 0)], CURRENT)
    assert choice is None


def test_a_release_grabbed_before_is_never_grabbed_again():
    r = release("A.S49E07.1080p.WEB.h264", 1.2, guid="abc")
    choice, skipped = replace.choose([r], CURRENT, tried={"abc"})
    assert choice is None and skipped["grabbed for this file before"] == 1


def test_a_weakly_seeded_torrent_goes_last():
    choice, _ = replace.choose([
        release("A.S49E07.1080p.WEB.h264", 1.2, protocol="torrent", seeders=1),
        release("B.S49E07.1080p.WEB.h264", 1.6, protocol="torrent", seeders=40),
    ], CURRENT)
    assert choice["title"] == "B.S49E07.1080p.WEB.h264"


def test_radarr_releases_have_no_episodes():
    r = release("Film.2004.1080p.BluRay.x264", 8.0, eps=())
    choice, _ = replace.choose([r], {**CURRENT, "duration": 7200.0},
                               sonarr=False)
    assert choice is not None


@pytest.mark.parametrize("w,h,expected", [
    (1920, 800, 1080), (1280, 720, 720), (1440, 1080, 1080),
    (720, 576, 576), (3840, 1600, 2160), (1916, 1036, 1080)])
def test_tier_reads_the_width_too(w, h, expected):
    assert replace.tier(w, h) == expected


def test_codec_from_the_title():
    assert replace.codec_of_title("Show.S01E01.1080p.WEB.H.265-GRP") == "hevc"
    assert replace.codec_of_title("Show.S01E01.1080p.WEB.x264-GRP") == "h264"
    assert replace.codec_of_title("Show S01E01 1080p AV1 Opus") == "av1"
    # Unnamed counts as the least efficient guess.
    assert replace.codec_of_title("Show S01E01 1080p WEB-DL") == "h264"


# -- starting one ----------------------------------------------------------

class FakeArr:
    flavour = "sonarr"

    def __init__(self, releases=None, grab_error=None):
        self._releases = releases or []
        self.grab_error = grab_error
        self.grabbed: list[tuple[str, int]] = []
        self.deleted: list[int] = []
        self.removed: list[tuple[int, dict]] = []
        self.imported: list[dict] = []
        self.queue_records: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        self.candidates: list[dict] = []
        self.command: dict = {"status": "started"}
        self.cfg = type("Cfg", (), {"path_mappings": []})()

    def releases(self, entity_id, episode_ids=None):
        return self._releases

    def grab(self, guid, indexer_id):
        if self.grab_error:
            from unfuckarr.clients.arr import ArrError
            raise ArrError(self.grab_error)
        self.grabbed.append((guid, indexer_id))
        return {}

    def find_grab(self, entity_id, episode_ids, title, since):
        return "dl-1"

    def queue(self):
        return self.queue_records

    def history_for_download(self, download_id):
        return self.history.get(download_id, [])

    def manual_import_candidates(self, download_id, entity_id):
        return self.candidates

    def manual_import(self, item, entity_id, episode_ids, download_id):
        self.imported.append(item)
        return 99

    def command_status(self, command_id):
        return self.command

    def remove_from_queue(self, queue_id, **kw):
        self.removed.append((queue_id, kw))

    def local_path(self, remote):
        return remote

    def delete_file(self, file_id):  # pragma: no cover - must never run
        self.deleted.append(file_id)


@pytest.fixture
def row(tmp_path, settings):
    settings.policy.low_bitrate_action = "replace"
    settings.policy.recycle_bin_path = str(tmp_path / "bin")
    path = tmp_path / "tv" / "HIGNFY S49E07.mkv"
    path.parent.mkdir()
    path.write_bytes(b"x" * 224_085)
    probe = {"size": 224_085_646, "duration": 2521.96,
             "video": {"codec": "hevc", "width": 1280, "height": 720}}
    db.ex("INSERT INTO files (path, library, source, arr_id, arr_parent_id, "
          "arr_episode_ids, title, probe) VALUES (?,?,?,?,?,?,?,?)",
          (str(path), "TV", "sonarr", 11, 1014, json.dumps([67221]), "HIGNFY",
           json.dumps(probe)))
    return {"path": str(path), "source": "sonarr", "arr_id": 11,
            "arr_parent_id": 1014, "arr_episode_ids": [67221], "title": "HIGNFY",
            "last_research": None, "probe": json.dumps(probe)}


def _remediator(settings, arr):
    rem = Remediator(lambda: settings)
    rem._arr_for = lambda file_row: arr
    return rem


def _bin_files(settings):
    root = settings.policy.recycle_bin_path
    out = []
    for dirpath, _, names in os.walk(root):
        out += [os.path.join(dirpath, n) for n in names]
    return out


def test_a_grab_keeps_the_file_and_a_copy_of_it(settings, row):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    out = _remediator(settings, arr).apply(
        row, reencoded(), None, Decision("replace", "x"))
    assert out["ok"], out
    assert arr.grabbed == [("HIGNFY.S49E07.1080p.WEB.h264", 4)]
    # The two things that make this safe.
    assert os.path.exists(row["path"])
    assert arr.deleted == []
    copies = _bin_files(settings)
    assert len(copies) == 1
    assert os.path.samefile(copies[0], row["path"])      # a hard link
    rec = db.q1("SELECT * FROM replacements")
    assert rec["state"] == "grabbed" and rec["download_id"] == "dl-1"
    assert rec["held_copy"] is not None
    assert db.q1("SELECT last_research FROM files WHERE path=?",
                 (row["path"],))["last_research"] > 0


def test_nothing_better_grabs_nothing_and_keeps_no_copy(settings, row):
    arr = FakeArr([release("HIGNFY.S49E07.720p.HDTV.x264", 0.48, 720)])
    out = _remediator(settings, arr).apply(
        row, reencoded(), None, Decision("replace", "x"))
    assert out["ok"] and "none of 1 releases" in out["message"]
    assert arr.grabbed == [] and _bin_files(settings) == []
    # Still counted against the cooldown, so the slot is not spent on it again.
    assert db.q1("SELECT last_research FROM files WHERE path=?",
                 (row["path"],))["last_research"] > 0


def test_a_failed_grab_takes_its_copy_away(settings, row):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)],
                  grab_error="indexer said no")
    out = _remediator(settings, arr).apply(
        row, reencoded(), None, Decision("replace", "x"))
    assert not out["ok"]
    assert _bin_files(settings) == []
    assert db.q1("SELECT COUNT(*) n FROM replacements")["n"] == 0
    assert os.path.exists(row["path"])


def test_no_grab_without_a_recycle_bin(settings, row):
    """The *arr may import by itself and remove the original at once."""
    settings.policy.recycle_bin_days = 0
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    out = _remediator(settings, arr).apply(
        row, reencoded(), None, Decision("replace", "x"))
    assert arr.grabbed == [] and "recycle bin is off" in out["message"]


def test_one_replacement_at_a_time(settings, row):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2),
                   release("HIGNFY.S49E07.1080p.WEB.x264-OTHER", 1.5)])
    rem = _remediator(settings, arr)
    rem.apply(row, reencoded(), None, Decision("replace", "x"))
    row["last_research"] = None    # cooldown aside, an open grab still blocks
    out = rem.apply(row, reencoded(), None, Decision("replace", "x"))
    assert len(arr.grabbed) == 1 and "already on its way" in out["message"]


def test_attempts_are_capped_per_episode(settings, row):
    for i in range(replace.MAX_ATTEMPTS):
        db.ex("INSERT INTO replacements (arr_key, path, source, state, grabbed, "
              "updated) VALUES (?,?,?,?,?,?)",
              ("sonarr:67221", f"/old/{i}.mkv", "sonarr", "rejected",
               time.time(), time.time()))
    assert "none was better" in replace.blocked(row, settings)


def test_the_scan_shares_the_research_cap(settings, monkeypatch, row):
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    settings.policy.max_researches_per_scan = 3
    asked = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: asked.append(a[0]["path"]) or {"ok": True})
    pending = [({**row, "path": f"/media/{i}.mkv", "arr_episode_ids": [i]},
                reencoded(), None, Decision("replace", "x")) for i in range(10)]
    state.scan = ScanProgress(running=True, checked=200)
    out = Scanner(lambda: settings, rem)._remediate(settings, pending,
                                                    population=200)
    assert len(asked) == 3 and out["researches"] == 3


# -- following it through --------------------------------------------------

def _grabbed(settings, row, arr):
    _remediator(settings, arr).apply(row, reencoded(), None,
                                     Decision("replace", "x"))
    return dict(db.q1("SELECT * FROM replacements"))


def _queued(state: str = "importBlocked", message: str =
            "Not an upgrade for existing episode file(s)"):
    return {"id": 5, "downloadId": "dl-1", "title": "HIGNFY.S49E07.1080p.WEB.h264",
            "trackedDownloadState": state, "trackedDownloadStatus": "warning",
            "status": "completed", "seriesId": 1014, "episodeId": 67221,
            "statusMessages": [{"messages": [message]}]}


def _candidate(path: str):
    return {"path": path, "size": 1_200_000_000, "episodes": [{"id": 67221}],
            "quality": {"quality": {"id": 3}}, "languages": [{"id": 1}],
            "rejections": [{"reason": "Not an upgrade for existing episode file(s)"}]}


def _judge(monkeypatch, verdict: bool, why: str = "better in every way measured"):
    seen = []

    def fake(local, rec, s):
        seen.append(local)
        return verdict, why, {"equiv": 3.5}
    monkeypatch.setattr(replace, "judge", fake)
    return seen


def test_a_held_download_that_passes_is_imported_and_the_copy_goes(
        settings, row, monkeypatch, tmp_path):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.queue_records = [_queued()]
    arr.candidates = [_candidate(str(tmp_path / "dl" / "ep.mkv"))]
    seen = _judge(monkeypatch, True)

    replace.advance(settings, lambda source: arr)
    assert seen == [str(tmp_path / "dl" / "ep.mkv")]
    assert len(arr.imported) == 1 and arr.removed == []
    assert db.q1("SELECT state FROM replacements")["state"] == "importing"
    assert len(_bin_files(settings)) == 1          # not until it has landed

    # The *arr imports it: gone from the queue, an import in the history.
    arr.queue_records = []
    arr.history["dl-1"] = [{"eventType": "downloadFolderImported",
                            "data": {"importedPath": "/tv/HIGNFY S49E07 WEBDL.mkv"}}]
    replace.advance(settings, lambda source: arr)
    rec = db.q1("SELECT * FROM replacements")
    assert rec["state"] == "imported"
    assert _bin_files(settings) == []
    assert db.totals().get("files_replaced") == 1
    assert arr.deleted == []


def test_a_held_download_that_fails_is_thrown_back_and_nothing_changes(
        settings, row, monkeypatch, tmp_path):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.queue_records = [_queued()]
    arr.candidates = [_candidate(str(tmp_path / "dl" / "ep.mkv"))]
    _judge(monkeypatch, False, "it is heavily re-encoded too")

    replace.advance(settings, lambda source: arr)
    assert arr.imported == []
    queue_id, how = arr.removed[0]
    assert queue_id == 5
    # Blocklisted so it is never grabbed again; no re-search, because the
    # next attempt is unfuckarr's to choose.
    assert how["blocklist"] and how["skip_redownload"]
    rec = db.q1("SELECT * FROM replacements")
    assert rec["state"] == "rejected" and "re-encoded too" in rec["message"]
    assert os.path.exists(row["path"])
    assert _bin_files(settings) == []               # original still in place
    assert arr.deleted == []


def test_a_sample_alone_is_not_the_episode(settings, row, monkeypatch, tmp_path):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.queue_records = [_queued()]
    sample = _candidate(str(tmp_path / "dl" / "sample.mkv"))
    sample["rejections"] = [{"reason": "Sample"}]
    arr.candidates = [sample]
    _judge(monkeypatch, True)
    replace.advance(settings, lambda source: arr)
    assert arr.imported == [] and arr.removed
    assert db.q1("SELECT state FROM replacements")["state"] == "rejected"


def test_a_download_still_running_is_left_alone(settings, row, monkeypatch):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.queue_records = [_queued("downloading", "")]
    _judge(monkeypatch, True)
    replace.advance(settings, lambda source: arr)
    assert arr.imported == [] and arr.removed == []
    assert db.q1("SELECT state FROM replacements")["state"] == "grabbed"


def test_one_the_arr_imported_itself_is_checked_after(settings, row, monkeypatch):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.history["dl-1"] = [{"eventType": "downloadFolderImported",
                            "data": {"importedPath": row["path"]}}]
    seen = _judge(monkeypatch, True)
    replace.advance(settings, lambda source: arr)
    assert seen == [row["path"]]
    assert db.q1("SELECT state FROM replacements")["state"] == "imported"
    assert _bin_files(settings) == []


def test_a_worse_one_the_arr_imported_keeps_the_original(
        settings, row, monkeypatch):
    """The *arr removed the old file the moment the download finished. The
    copy is now the only one, and it stays."""
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    copy = _bin_files(settings)[0]
    os.unlink(row["path"])                      # what the *arr's import did
    new = row["path"].replace(".mkv", " WEBDL.mkv")
    with open(new, "wb") as f:
        f.write(b"y" * 10)
    arr.history["dl-1"] = [{"eventType": "downloadFolderImported",
                            "data": {"importedPath": new}}]
    _judge(monkeypatch, False, "it is damaged: decode errors")
    replace.advance(settings, lambda source: arr)
    rec = db.q1("SELECT * FROM replacements")
    assert rec["state"] == "imported_worse"
    assert os.path.exists(copy)
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 1
    assert db.totals().get("files_replaced", 0) == 0


def test_a_failed_download_leaves_the_original(settings, row, monkeypatch):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.history["dl-1"] = [{"eventType": "downloadFailed"}]
    replace.advance(settings, lambda source: arr)
    assert db.q1("SELECT state FROM replacements")["state"] == "failed"
    assert os.path.exists(row["path"]) and _bin_files(settings) == []


def test_a_grab_that_vanishes_is_given_up_after_a_grace(settings, row):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    replace.advance(settings, lambda source: arr)
    assert db.q1("SELECT state FROM replacements")["state"] == "grabbed"
    db.ex("UPDATE replacements SET grabbed=?",
          (time.time() - replace.VANISH_GRACE_SECONDS - 60,))
    replace.advance(settings, lambda source: arr)
    assert db.q1("SELECT state FROM replacements")["state"] == "vanished"
    assert os.path.exists(row["path"]) and _bin_files(settings) == []


def test_a_stuck_download_expires(settings, row):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.queue_records = [_queued("downloading", "")]
    db.ex("UPDATE replacements SET grabbed=?",
          (time.time() - replace.EXPIRE_DAYS * 86400 - 60,))
    replace.advance(settings, lambda source: arr)
    assert db.q1("SELECT state FROM replacements")["state"] == "expired"
    assert arr.removed and os.path.exists(row["path"])


def test_a_failed_manual_import_is_recorded(settings, row, monkeypatch, tmp_path):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.queue_records = [_queued()]
    arr.candidates = [_candidate(str(tmp_path / "dl" / "ep.mkv"))]
    _judge(monkeypatch, True)
    replace.advance(settings, lambda source: arr)
    arr.command = {"status": "failed", "message": "Destination already exists"}
    replace.advance(settings, lambda source: arr)
    assert db.q1("SELECT state FROM replacements")["state"] == "failed"
    assert os.path.exists(row["path"])


# -- judging what arrived --------------------------------------------------

def _fake_check(monkeypatch, info: MediaInfo | None, *findings: Finding):
    from unfuckarr import scanner

    def fake(path, s, **kw):
        r = CheckResult(path=path)
        for f in findings:
            r.add(f)
        return r, info
    monkeypatch.setattr(scanner, "check_file", fake)


REC = {"old_duration": 2522.0, "old_tier": 720, "old_mbps": 1.422}


def test_judge_passes_a_real_improvement(settings, monkeypatch):
    _fake_check(monkeypatch, media(1_200_000_000, 2520.0, 1920, 1080, "h264"))
    ok, why, m = replace.judge("/dl/ep.mkv", REC, settings)
    assert ok, why
    assert m["tier"] == 1080


def test_judge_refuses_another_heavy_re_encode(settings, monkeypatch):
    _fake_check(monkeypatch, media(200_000_000, 2520.0),
                Finding("hygiene", "very_low_bitrate", "warning", ""))
    ok, why, _ = replace.judge("/dl/ep.mkv", REC, settings)
    assert not ok and "re-encoded too" in why


def test_judge_refuses_a_damaged_file(settings, monkeypatch):
    _fake_check(monkeypatch, media(1_200_000_000, 2520.0, 1920, 1080, "h264"),
                Finding("integrity", "decode_errors", "error", "errors at 40%"))
    ok, why, _ = replace.judge("/dl/ep.mkv", REC, settings)
    assert not ok and "damaged" in why


def test_judge_refuses_a_different_cut(settings, monkeypatch):
    """The 42-minute extended episode is not replaced by the 29-minute one."""
    _fake_check(monkeypatch, media(900_000_000, 29 * 60.0, 1920, 1080, "h264"))
    ok, why, _ = replace.judge("/dl/ep.mkv", REC, settings)
    assert not ok and "different cut" in why


def test_judge_refuses_a_smaller_picture(settings, monkeypatch):
    rec = {**REC, "old_tier": 1080}
    _fake_check(monkeypatch, media(1_200_000_000, 2520.0, 1280, 720, "h264"))
    ok, why, _ = replace.judge("/dl/ep.mkv", rec, settings)
    assert not ok and "720p" in why


def test_judge_refuses_a_release_that_lied_about_its_size(settings, monkeypatch):
    _fake_check(monkeypatch, media(450_000_000, 2520.0, 1280, 720, "h264"))
    ok, why, _ = replace.judge("/dl/ep.mkv", REC, settings)
    assert not ok and "not enough better" in why


def test_judge_refuses_what_will_not_open(settings, monkeypatch):
    _fake_check(monkeypatch, None)
    ok, why, _ = replace.judge("/dl/ep.mkv", REC, settings)
    assert not ok and "would not open" in why


# -- the queue watcher keeps its hands off ---------------------------------

def test_intake_leaves_our_grab_to_the_replacement_pass(settings, row):
    from unfuckarr import intake
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    item = intake.normalise(_queued("importBlocked", "something unrecognised"),
                            "sonarr", [])
    verdict = intake.IntakeWatcher(lambda: settings)._verdict_for(item, None, settings)
    assert verdict.kind == "manual" and verdict.evidence.get("replacement")


def test_a_finished_grab_is_no_longer_ours(settings, row):
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    db.ex("UPDATE replacements SET state='rejected'")
    assert not replace.owns("sonarr", "dl-1")


# -- keeping a copy --------------------------------------------------------

def test_keep_copy_is_a_hard_link_and_leaves_the_file(tmp_path):
    from unfuckarr import recycle
    f = tmp_path / "ep.mkv"
    f.write_bytes(b"abc")
    rid = recycle.keep_copy(str(f), "test", str(tmp_path / "bin"), 14)
    stored = db.q1("SELECT stored FROM recycle WHERE id=?", (rid,))["stored"]
    assert f.exists() and os.path.samefile(stored, f)
    assert recycle.discard(rid, "done")
    assert f.exists() and not os.path.exists(stored)


def test_keep_copy_refuses_when_the_bin_is_off(tmp_path):
    from unfuckarr import recycle
    f = tmp_path / "ep.mkv"
    f.write_bytes(b"abc")
    assert recycle.keep_copy(str(f), "test", str(tmp_path / "bin"), 0) is None
    assert f.exists()


def test_a_held_copy_outlives_the_size_limit_and_the_sweep(settings, row):
    """While the download is open the copy may be all there is: the *arr can
    import our grab by itself and remove the original at any moment."""
    from unfuckarr import recycle
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    rec = _grabbed(settings, row, arr)
    bin_ = settings.policy.recycle_bin_path
    db.ex("UPDATE recycle SET deleted=? WHERE id=?",
          (time.time() - 30 * 86400, rec["held_copy"]))
    assert recycle.enforce_limit(1, bin_) == (0, 0)
    assert recycle.sweep(1, bin_) == 0
    assert len(_bin_files(settings)) == 1

    db.ex("UPDATE replacements SET state='rejected'")     # ended: ordinary again
    assert recycle.sweep(1, bin_) == 1
    assert _bin_files(settings) == []


def test_a_manual_import_that_never_takes_is_thrown_back(
        settings, row, monkeypatch, tmp_path):
    """Left open, the row would block the episode for good."""
    arr = FakeArr([release("HIGNFY.S49E07.1080p.WEB.h264", 1.2)])
    _grabbed(settings, row, arr)
    arr.queue_records = [_queued()]
    arr.candidates = [_candidate(str(tmp_path / "dl" / "ep.mkv"))]
    _judge(monkeypatch, True)
    replace.advance(settings, lambda source: arr)
    arr.command = {"status": "completed"}
    replace.advance(settings, lambda source: arr)
    assert db.q1("SELECT state FROM replacements")["state"] == "importing"

    db.ex("UPDATE replacements SET updated=?",
          (time.time() - replace.IMPORT_GRACE_SECONDS - 60,))
    replace.advance(settings, lambda source: arr)
    rec = db.q1("SELECT * FROM replacements")
    assert rec["state"] == "rejected" and "did not take" in rec["message"]
    assert arr.removed and os.path.exists(row["path"])
    assert _bin_files(settings) == []               # original still in place
