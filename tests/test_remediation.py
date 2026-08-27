"""Remediation: the transcode plan, the recycle bin, and the *arr calls."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from unfuckarr import db, recycle, transcode
from unfuckarr.checks import CheckResult, Finding
from unfuckarr.clients.arr import ArrClient
from unfuckarr.probe import MediaInfo, Stream, probe
from unfuckarr.remediation import Decision, Remediator
from unfuckarr.scanner import check_file

from .conftest import needs_ffmpeg


# -- planning -------------------------------------------------------------

@needs_ffmpeg
def test_plan_copies_a_stream_that_already_passes(video_factory, settings):
    """A compatible H.264 stream in the wrong container is a remux, not a
    re-encode. Getting this wrong turns 20 seconds of work into 6 hours."""
    path = video_factory("ok.mkv", seconds=6)
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    result.add(Finding("compat", "bad_container", "error", ""))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.video_action == "copy"
    assert p.audio_action == "copy"
    assert p.is_remux


@needs_ffmpeg
def test_plan_re_encodes_a_bad_video_codec(video_factory, settings):
    path = video_factory("m2v.mkv", seconds=6, vcodec="mpeg2video",
                         extra=["-b:v", "1M"])
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    result.add(Finding("compat", "bad_video_codec", "error", ""))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.video_action == "encode"


def _two_audio(defaults: tuple[bool, bool]) -> MediaInfo:
    """A 5.1 main track and a 2.0 commentary, built without ffmpeg because
    the question is what the planner does with the dispositions, not whether
    ffmpeg can mux them."""
    return MediaInfo(
        path="/media/x.mkv", input_url="/media/x.mkv", container="mkv",
        duration=3600.0, size=6_000_000_000,
        streams=[
            Stream(index=0, codec_type="video", codec_name="h264",
                   width=1920, height=1080, fps=25.0),
            Stream(index=1, codec_type="audio", codec_name="ac3", channels=6,
                   language="eng", is_default=defaults[0]),
            Stream(index=2, codec_type="audio", codec_name="ac3", channels=2,
                   language="eng", is_default=defaults[1]),
        ],
    )


def test_multiple_default_audio_is_actually_fixed(settings):
    """It sat next to `no_default_audio` in the hygiene check and nowhere in
    the planner, so the remux copied both flags through and the finding
    survived every time. Live 2026-08-26: 33 of 39 `transcode_did_not_fix`
    events in nine hours were this one code."""
    info = _two_audio((True, True))
    result = CheckResult(path=info.path)
    result.add(Finding("hygiene", "multiple_default_audio", "warning", ""))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.set_default_audio == 0        # the 5.1 track, not the commentary
    cmd = transcode.build_command(info.path, "/tmp/out.mkv", info, p,
                                  settings.transcode)
    assert ["-disposition:a:0", "default"] == cmd[cmd.index("-disposition:a:0"):
                                                  cmd.index("-disposition:a:0") + 2]
    assert ["-disposition:a:1", "0"] == cmd[cmd.index("-disposition:a:1"):
                                            cmd.index("-disposition:a:1") + 2]


def test_multiple_default_audio_chooses_among_the_flagged_tracks(settings):
    """Only the ambiguity needs settling. Promoting a track the file never
    marked default is a different decision, and not one the finding asked for."""
    info = _two_audio((False, True))    # only the 2.0 commentary is default
    result = CheckResult(path=info.path)
    result.add(Finding("hygiene", "multiple_default_audio", "warning", ""))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.set_default_audio == 1


def test_all_subtitles_forced_is_cleared(settings):
    info = MediaInfo(
        path="/media/x.mkv", input_url="/media/x.mkv", container="mkv",
        duration=3600.0, size=6_000_000_000,
        streams=[
            Stream(index=0, codec_type="video", codec_name="h264",
                   width=1920, height=1080, fps=25.0),
            Stream(index=1, codec_type="audio", codec_name="ac3", channels=6,
                   language="eng", is_default=True),
            Stream(index=2, codec_type="subtitle", codec_name="subrip",
                   language="eng", is_forced=True),
            Stream(index=3, codec_type="subtitle", codec_name="subrip",
                   language="fre", is_forced=True),
        ],
    )
    result = CheckResult(path=info.path)
    result.add(Finding("hygiene", "all_subtitles_forced", "warning", ""))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.clear_forced_subtitles
    cmd = transcode.build_command(info.path, "/tmp/out.mkv", info, p,
                                  settings.transcode)
    assert "-disposition:s:0" in cmd and "-disposition:s:1" in cmd


def test_every_fixable_code_is_one_the_planner_acts_on(settings):
    """`decide` flags a hygiene warning whose code is not in HYGIENE_FIXABLE
    rather than rewriting the file for nothing. If a code is listed here and
    the planner does nothing with it, that promise is broken silently and the
    pointless remuxes come straight back."""
    info = _two_audio((True, True))
    info.streams.append(Stream(index=3, codec_type="subtitle",
                               codec_name="subrip", is_forced=True))
    for code in transcode.HYGIENE_FIXABLE:
        p = transcode.TranscodePlan(reason="t")
        transcode.apply_hygiene_fixes(p, info, {code})
        assert (p.fix_language_tags or p.set_default_audio is not None
                or p.clear_forced_subtitles), code


def test_local_table_runs_when_emby_refuses_without_reasons(settings, monkeypatch):
    """The fallback that makes the verdict actionable. Without it a refusal
    with no reasons produces `video_action == "copy"` and a stream copy."""
    from unfuckarr import scanner as scanner_mod

    info = _two_audio((True, False))
    info.container = "avi"
    info.streams[0].codec_name = "mpeg4"

    class StubEmby:
        def check_direct_play(self, path, cfg, result):
            result.add(Finding("emby", "no_direct_play", "error", "",
                               {"reasons": []}))
            return True

    def fake_integrity(path, cfg, **kw):
        return CheckResult(path=path), info
    monkeypatch.setattr(scanner_mod.integrity_checks, "check", fake_integrity)

    settings.emby.enabled = True
    settings.emby.use_playback_info = True
    result, _ = scanner_mod.check_file(info.path, settings, emby=StubEmby())
    codes = {f.code for f in result.findings}
    assert "no_direct_play" in codes
    assert "bad_video_codec" in codes, codes
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.video_action == "encode"


@needs_ffmpeg
def test_repair_plan_never_re_encodes(video_factory, settings):
    path = video_factory("r.mkv", seconds=6)
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    result.add(Finding("integrity", "decode_errors", "error", ""))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat,
                       repair=True)
    assert p.video_action == "copy" and p.audio_action == "copy" and p.is_remux


@needs_ffmpeg
def test_emby_transcode_reasons_drive_the_plan(video_factory, settings):
    """Emby's own verdict names the stream; the planner must honour it even
    when the local codec table was happy."""
    path = video_factory("emby.mkv", seconds=6)
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    result.add(Finding("emby", "no_direct_play", "error", "",
                       {"reasons": ["VideoCodecNotSupported"]}))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.video_action == "encode"


@needs_ffmpeg
def test_pgs_subtitles_are_dropped_for_mp4(video_factory, settings):
    """Copying PGS into MP4 fails the whole job, so it must be dropped."""
    from unfuckarr.probe import MediaInfo, Stream
    info = MediaInfo(path="x.mkv", container="mkv", duration=60, streams=[
        Stream(index=0, codec_type="video", codec_name="h264"),
        Stream(index=1, codec_type="audio", codec_name="aac"),
        Stream(index=2, codec_type="subtitle", codec_name="hdmv_pgs_subtitle"),
    ])
    settings.transcode.container = "mp4"
    p = transcode.plan(info, CheckResult(path="x"), settings.transcode,
                       settings.emby_compat)
    assert p.drop_subtitles == [0]


@needs_ffmpeg
def test_build_command_is_well_formed(video_factory, settings):
    path = video_factory("cmd.mkv", seconds=6)
    info = probe(str(path), settings.ffprobe_path)
    p = transcode.plan(info, CheckResult(path=str(path)), settings.transcode,
                       settings.emby_compat, repair=True)
    cmd = transcode.build_command(str(path), "/tmp/out.mkv", info, p,
                                  settings.transcode, ffmpeg="ffmpeg")
    assert cmd[0] == "ffmpeg" and cmd[-1] == "/tmp/out.mkv"
    assert "-nostdin" in cmd          # or a stalled ffmpeg eats stdin and hangs
    assert "-c:v" in cmd


@needs_ffmpeg
def test_vaapi_uploads_rather_than_assuming_a_hardware_decode(video_factory,
                                                              settings):
    """The GPU must not be asked to filter frames it never decoded.

    Verified against a real Radeon 880M: with -hwaccel_output_format vaapi, an
    MPEG-2 source (no recent AMD part decodes MPEG-2) falls back to a software
    decode and scale_vaapi then dies mid-stream with "Failed to inject frame
    into filter network". Uploading explicitly works whatever the decoder did.
    """
    path = video_factory("va.mkv", seconds=4)
    info = probe(str(path), settings.ffprobe_path)
    settings.transcode.hwaccel = "vaapi"
    settings.transcode.video_codec = "hevc"
    result = CheckResult(path=str(path))
    result.add(Finding("compat", "bad_video_codec", "error", "needs encoding"))
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    assert p.video_action == "encode"
    cmd = transcode.build_command(str(path), "/tmp/out.mkv", info, p,
                                  settings.transcode, ffmpeg="ffmpeg")
    joined = " ".join(cmd)
    assert "-c:v hevc_vaapi" in joined
    assert "hwupload" in joined
    assert "-hwaccel_output_format" not in joined
    assert "scale_vaapi" not in joined


# -- end to end -----------------------------------------------------------

@needs_ffmpeg
def test_incompatible_file_is_transcoded_and_verified(video_factory, settings):
    path = video_factory("legacy.avi", seconds=8, vcodec="mpeg2video",
                         acodec="ac3", extra=["-b:v", "1M"])
    db.ex("INSERT INTO files (path, library, source, title) VALUES (?,?,?,?)",
          (str(path), "Movies", "folder", "Legacy"))
    row = {"path": str(path), "source": "folder", "title": "Legacy",
           "arr_id": None, "arr_parent_id": None}

    result, info = check_file(str(path), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("transcode", "incompatible"))

    assert out["ok"], out
    new = Path(out["path"])
    assert new.exists() and new.suffix == ".mkv"
    assert not path.exists(), "the original should have been recycled"

    recheck, _ = check_file(str(new), settings)
    assert recheck.status == "ok", [f.code for f in recheck.findings]


@needs_ffmpeg
def test_a_bad_transcode_output_is_discarded(video_factory, settings, monkeypatch):
    """ffmpeg exiting 0 having written rubbish must not replace the source."""
    path = video_factory("keep.mkv", seconds=8)
    row = {"path": str(path), "source": "folder", "title": "Keep",
           "arr_id": None, "arr_parent_id": None}
    result, info = check_file(str(path), settings)
    result.add(Finding("compat", "bad_container", "error", ""))

    def fake_run(cmd, *a, **k):
        Path(cmd[-1]).write_bytes(b"garbage")
        return True, "faked"
    monkeypatch.setattr(transcode, "run", fake_run)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("transcode", "incompatible"))
    assert not out["ok"]
    assert path.exists(), "the source must survive a bad output"


@needs_ffmpeg
def test_a_failed_repair_falls_through_to_redownload(video_factory, settings,
                                                     monkeypatch):
    path = video_factory("fail.mkv", seconds=8)
    row = {"path": str(path), "source": "folder", "title": "Fail",
           "arr_id": None, "arr_parent_id": None}
    db.ex("INSERT INTO files (path) VALUES (?)", (str(path),))
    result, info = check_file(str(path), settings)
    result.add(Finding("integrity", "decode_errors", "error", ""))

    monkeypatch.setattr(transcode, "run", lambda *a, **k: (False, "ffmpeg died"))
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("repair", "container damage"))

    assert out["action"] == "redownload"
    assert not path.exists()
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 1
    # The replacement is a different file: attempts start over.
    assert db.q1("SELECT fix_attempts FROM files WHERE path=?",
                 (str(path),))["fix_attempts"] == 0


@needs_ffmpeg
def test_a_transcode_that_does_not_fix_it_is_not_repeated_for_ever(
        video_factory, settings, monkeypatch):
    """A transcode that leaves the finding in place would otherwise be redone
    on every scan, indefinitely, with nothing saying why."""
    from unfuckarr.remediation import MAX_FIX_ATTEMPTS

    path = video_factory("loop.mkv", seconds=8)
    db.ex("INSERT INTO files (path, title) VALUES (?,?)", (str(path), "Loop"))
    row = {"path": str(path), "source": "folder", "title": "Loop",
           "arr_id": None, "arr_parent_id": None, "fix_attempts": 0}

    # Whatever comes out, the checker keeps saying it is incompatible.
    from unfuckarr import scanner as scanner_mod
    real_check = scanner_mod.check_file

    def always_bad(p, s, **kw):
        result, info = real_check(p, s, **kw)
        result.add(Finding("compat", "bad_container", "error", "still wrong"))
        return result, info
    monkeypatch.setattr(scanner_mod, "check_file", always_bad)

    rem = Remediator(lambda: settings)
    result, info = check_file(str(path), settings)
    result.add(Finding("compat", "bad_container", "error", ""))

    current = str(path)
    for _ in range(MAX_FIX_ATTEMPTS):
        out = rem.apply(row, result, info, Decision("transcode", "incompatible"))
        assert out["ok"], out
        current = out["path"]
        row = {**row, "path": current,
               "fix_attempts": db.q1("SELECT fix_attempts FROM files WHERE path=?",
                                     (current,))["fix_attempts"]}

    # The cap is now reached, so the next attempt refuses rather than running.
    out = rem.apply(row, result, info, Decision("transcode", "incompatible"))
    assert out["action"] == "flag"
    assert "not trying again" in out["message"]


@needs_ffmpeg
def test_output_missing_at_verify_counts_attempts_and_stops(
        video_factory, settings, monkeypatch):
    """The live loop of 2026-08: the remux 'completes', the temp output is
    gone by verify time, and nothing counted the attempt — so the cycle
    re-ran on every scan, for ever."""
    from unfuckarr.remediation import MAX_FIX_ATTEMPTS

    path = video_factory("gone.mkv", seconds=6)
    db.ex("INSERT INTO files (path, title) VALUES (?,?)", (str(path), "Gone"))

    # ffmpeg reports success but the output is not there any more — what a
    # race with another job, or an *arr moving files, looks like.
    monkeypatch.setattr(transcode, "run", lambda *a, **k: (True, "completed in 1s"))

    rem = Remediator(lambda: settings)
    result, info = check_file(str(path), settings)
    result.add(Finding("compat", "bad_container", "error", ""))

    def row():
        return {"path": str(path), "source": "folder", "title": "Gone",
                "arr_id": None, "arr_parent_id": None,
                "fix_attempts": db.q1("SELECT fix_attempts FROM files WHERE path=?",
                                      (str(path),))["fix_attempts"]}

    for expected in range(1, MAX_FIX_ATTEMPTS + 1):
        out = rem.apply(row(), result, info, Decision("transcode", "incompatible"))
        assert not out["ok"]
        assert path.exists(), "the source must be untouched"
        assert row()["fix_attempts"] == expected

    # The cap is reached, so the next attempt refuses rather than running.
    out = rem.apply(row(), result, info, Decision("transcode", "incompatible"))
    assert out["action"] == "flag"
    assert "not trying again" in out["message"]


@needs_ffmpeg
def test_hygiene_remux_that_leaves_its_warning_counts_attempts(
        video_factory, settings, monkeypatch):
    """hygiene_action=transcode plus a warning a remux cannot clear (image-only
    subtitles, an odd frame rate). The re-check coming back 'hygiene' is not
    success when hygiene was the reason for the transcode — treating it as
    success is the other half of the 2026-08 loop."""
    from unfuckarr.remediation import MAX_FIX_ATTEMPTS

    from unfuckarr import scanner as scanner_mod
    real_check = scanner_mod.check_file

    def still_grubby(p, s, **kw):
        result, info = real_check(p, s, **kw)
        result.add(Finding("hygiene", "image_subtitles_only", "warning", ""))
        return result, info
    monkeypatch.setattr(scanner_mod, "check_file", still_grubby)

    path = video_factory("tidy.mkv", seconds=6)
    db.ex("INSERT INTO files (path, title) VALUES (?,?)", (str(path), "Tidy"))
    rem = Remediator(lambda: settings)

    result, info = check_file(str(path), settings)
    result.add(Finding("hygiene", "image_subtitles_only", "warning", ""))
    decision = Decision("transcode", "stream metadata needs tidying",
                        ["image_subtitles_only"])

    current = str(path)
    for expected in range(1, MAX_FIX_ATTEMPTS + 1):
        row = {"path": current, "source": "folder", "title": "Tidy",
               "arr_id": None, "arr_parent_id": None,
               "fix_attempts": db.q1("SELECT fix_attempts FROM files WHERE path=?",
                                     (current,))["fix_attempts"]}
        out = rem.apply(row, result, info, decision)
        assert out["ok"], out
        current = out["path"]
        assert db.q1("SELECT fix_attempts FROM files WHERE path=?",
                     (current,))["fix_attempts"] == expected

    row = {"path": current, "source": "folder", "title": "Tidy",
           "arr_id": None, "arr_parent_id": None,
           "fix_attempts": MAX_FIX_ATTEMPTS}
    out = rem.apply(row, result, info, decision)
    assert out["action"] == "flag"
    assert "not trying again" in out["message"]


def test_own_temp_outputs_are_not_library_files(tmp_path):
    """A scan or a watch event during a long remux sees the half-written
    *.unfuckarr.mkv next to its source. Treating it as media spawns a second
    job that races the first for the same file."""
    from unfuckarr.config import WatchFolder
    from unfuckarr.scanner import walk_video_files
    from unfuckarr.watcher import _Handler

    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "film.mkv").write_bytes(b"x")
    (tmp_path / "m" / "film.unfuckarr.mkv").write_bytes(b"x")

    assert [Path(p).name for p in walk_video_files(str(tmp_path))] == ["film.mkv"]

    touched: list[str] = []
    handler = _Handler(WatchFolder(path=str(tmp_path)), touched.append)
    handler._consider(str(tmp_path / "m" / "film.unfuckarr.mkv"))
    handler._consider(str(tmp_path / "m" / "film.mkv"))
    assert [Path(p).name for p in touched] == ["film.mkv"]


def test_remediator_refuses_to_act_on_its_own_temp_output(settings):
    out = Remediator(lambda: settings).apply(
        {"path": "/media/film.unfuckarr.mkv"}, CheckResult(path="x"), None,
        Decision("transcode", "stream metadata needs tidying"))
    assert out["action"] == "none"
    assert db.q1("SELECT COUNT(*) n FROM jobs")["n"] == 0


def test_fix_attempts_column_is_added_to_an_existing_database(tmp_path):
    """The column was added after 1.0.0, and CREATE TABLE IF NOT EXISTS will
    not add it to a database that already exists."""
    import sqlite3
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(db.SCHEMA)
    conn.execute("ALTER TABLE files DROP COLUMN fix_attempts")   # the 1.0.0 shape
    conn.execute("INSERT INTO files (path, status) VALUES ('/old.mkv', 'ok')")
    conn.commit()
    conn.close()

    db.reset_for_tests(p)
    # The existing row survives the migration.
    assert db.q1("SELECT status FROM files WHERE path='/old.mkv'")["status"] == "ok"
    cols = {r["name"] for r in db.connect().execute("PRAGMA table_info(files)")}
    assert "fix_attempts" in cols


# -- recycle bin ----------------------------------------------------------

def test_recycled_file_can_be_restored(tmp_path, settings):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x" * 4096)
    stored = recycle.store(str(src), "test", "", 14)
    assert stored and Path(stored).exists() and not src.exists()

    row = db.q1("SELECT * FROM recycle")
    recycle.restore(row["id"])
    assert src.exists()
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 0


def test_zero_retention_unlinks_immediately(tmp_path, settings):
    src = tmp_path / "gone.mkv"
    src.write_bytes(b"x" * 100)
    assert recycle.store(str(src), "test", "", 0) is None
    assert not src.exists()
    # Still recorded, so the activity log can show what happened.
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 1


def test_orphaned_bin_files_are_swept_too(tmp_path, settings):
    """The sweep walks the database, so a file whose row is gone was never
    looked at again. Live that was 139 GB, 126 GB of it three identical copies
    of one 42 GB disc image."""
    import time

    bin_dir = tmp_path / "bin"
    dated = bin_dir / "2026-08-08"
    dated.mkdir(parents=True)
    orphan = dated / "movies_Film__Film BR-DISK.1.2.iso"
    orphan.write_bytes(b"x" * 2048)
    old_enough = time.time() - 30 * 86400
    os.utime(orphan, (old_enough, old_enough))

    # A file the database *does* know about, from today, must survive.
    keeper = dated / "movies_Other__Other.mkv"
    keeper.write_bytes(b"y" * 2048)
    db.ex("INSERT INTO recycle (original, stored, size, deleted, reason) "
          "VALUES (?,?,?,?,?)",
          ("/media/Other.mkv", str(keeper), 2048, time.time(), "test"))

    removed = recycle.sweep(14, str(bin_dir))
    assert removed == 1
    assert not orphan.exists()
    assert keeper.exists()


def test_a_recent_orphan_is_left_alone(tmp_path, settings):
    """An in-flight recycle writes the file before it writes the row."""
    bin_dir = tmp_path / "bin"
    dated = bin_dir / "2026-08-19"
    dated.mkdir(parents=True)
    fresh = dated / "movies_New__New.mkv"
    fresh.write_bytes(b"z" * 1024)

    assert recycle.sweep(14, str(bin_dir)) == 0
    assert fresh.exists()


def test_unrelated_directories_in_the_bin_are_not_touched(tmp_path, settings):
    """Only dated directories are ours to empty."""
    import time

    bin_dir = tmp_path / "bin"
    other = bin_dir / "please-do-not-delete"
    other.mkdir(parents=True)
    theirs = other / "notes.txt"
    theirs.write_bytes(b"hello")
    old_enough = time.time() - 30 * 86400
    os.utime(theirs, (old_enough, old_enough))

    assert recycle.sweep(14, str(bin_dir)) == 0
    assert theirs.exists()


def test_abandoned_transcode_outputs_are_found(tmp_path):
    """A transcode killed part-way leaves its output behind for ever: the
    scanner and the watcher both ignore it by design, so nothing notices.
    Live, four had reached 199 GB inside the library, with 507 metadata files
    Emby had written for them as if they were separate films."""
    movie = tmp_path / "Batman (1989)"
    movie.mkdir()
    real = movie / "Batman (1989).mkv"
    real.write_bytes(b"real")
    leftover = movie / "Batman (1989).unfuckarr.mkv"
    leftover.write_bytes(b"half a transcode")
    artwork = movie / "Batman (1989).unfuckarr-banner.jpg"
    artwork.write_bytes(b"emby wrote this for the temp file")

    found = set(transcode.abandoned_outputs([str(real)]))
    assert found == {str(leftover), str(artwork)}
    assert str(real) not in found


def test_sweep_only_removes_expired_entries(tmp_path, settings):
    import time
    src = tmp_path / "old.mkv"
    src.write_bytes(b"x" * 100)
    stored = recycle.store(str(src), "test", "", 14)
    db.ex("UPDATE recycle SET deleted = ?", (time.time() - 20 * 86400,))
    assert recycle.sweep(14) == 1
    assert not Path(stored).exists()

    src2 = tmp_path / "new.mkv"
    src2.write_bytes(b"x" * 100)
    stored2 = recycle.store(str(src2), "test", "", 14)
    assert recycle.sweep(14) == 0
    assert Path(stored2).exists()


def test_two_files_with_the_same_name_do_not_collide(tmp_path, settings):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "video.mkv").write_bytes(b"x" * 100)
        recycle.store(str(tmp_path / d / "video.mkv"), "test", "", 14)
    stored = [r["stored"] for r in db.q("SELECT stored FROM recycle")]
    assert len(set(stored)) == 2
    assert all(Path(s).exists() for s in stored)


# -- *arr client ----------------------------------------------------------

def _arr(monkeypatch, handler) -> ArrClient:
    from unfuckarr.config import ArrConfig
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def client_factory(*a, **kw):
        return real(transport=transport, **kw)
    monkeypatch.setattr(httpx, "Client", client_factory)
    return ArrClient(ArrConfig(enabled=True, url="http://arr:7878", api_key="k"),
                     "radarr")


def test_radarr_library_is_normalised(monkeypatch):
    def handler(request):
        assert request.headers["X-Api-Key"] == "k"
        return httpx.Response(200, json=[{
            "id": 12, "title": "Some Film", "runtime": 100,
            "movieFile": {"id": 99, "path": "/movies/Some Film/film.mkv",
                          "size": 5_000_000_000,
                          "quality": {"quality": {"name": "Bluray-1080p"}}},
        }, {"id": 13, "title": "No File", "runtime": 90}])
    client = _arr(monkeypatch, handler)
    lib = client.library()
    assert len(lib) == 1, "a movie with no file must not appear"
    assert lib[0]["expected_runtime"] == 6000       # minutes → seconds
    assert lib[0]["arr_id"] == 99 and lib[0]["arr_parent_id"] == 12


def test_blocklist_picks_the_newest_grab(monkeypatch):
    posted = []

    def handler(request):
        if request.url.path.endswith("/history/movie"):
            return httpx.Response(200, json=[
                {"id": 1, "eventType": "grabbed", "date": "2026-01-01T00:00:00Z"},
                {"id": 2, "eventType": "grabbed", "date": "2026-06-01T00:00:00Z"},
                {"id": 3, "eventType": "downloadFolderImported", "date": "2026-07-01T00:00:00Z"},
            ])
        posted.append(request.url.path)
        return httpx.Response(200, json={})
    client = _arr(monkeypatch, handler)
    assert client.blocklist_last_grab(12) is True
    assert posted == ["/api/v3/history/failed/2"]


def test_blocklist_reports_false_with_no_grab_history(monkeypatch):
    """A hand-imported file has no grab. That is not an error — the caller
    falls back to a plain search."""
    def handler(request):
        return httpx.Response(200, json=[])
    client = _arr(monkeypatch, handler)
    assert client.blocklist_last_grab(12) is False


def test_arr_errors_are_wrapped(monkeypatch):
    from unfuckarr.clients.arr import ArrError

    def handler(request):
        return httpx.Response(401, text="unauthorised")
    client = _arr(monkeypatch, handler)
    with pytest.raises(ArrError, match="rejected the API key"):
        client.library()
