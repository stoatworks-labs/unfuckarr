"""Dolby Vision with no fallback: found, never re-encoded, never let in.

The stream records below are the ones ffprobe 8.0.1 printed for the live
library on 2026-11-15 — Slow Horses S01E01 (profile 5), an ordinary profile
8.1 episode, and House of the Dragon S03E07, whose container had lost its
Dolby Vision record while every frame still carried the RPU.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unfuckarr import db, quality, replace, transcode
from unfuckarr.checks import CheckResult, Finding, colour, efficiency
from unfuckarr.probe import MediaInfo, Stream, _could_hide_dovi
from unfuckarr.remediation import Decision, Remediator, decide
from unfuckarr.scanner import check_file

from .conftest import needs_ffmpeg

MB = 1024 * 1024


def dovi_record(profile: int, compat: int) -> dict:
    return {"side_data_type": "DOVI configuration record",
            "dv_version_major": 1, "dv_version_minor": 0,
            "dv_profile": profile, "dv_level": 6,
            "rpu_present_flag": 1, "el_present_flag": 0, "bl_present_flag": 1,
            "dv_bl_signal_compatibility_id": compat}


PROFILE_5 = {"pix_fmt": "yuv420p10le", "color_range": "tv",
             "side_data_list": [dovi_record(5, 0)]}
PROFILE_8_1 = {"pix_fmt": "yuv420p10le", "color_range": "tv",
               "color_space": "bt2020nc", "color_transfer": "smpte2084",
               "color_primaries": "bt2020",
               "side_data_list": [dovi_record(8, 1)]}
HDR10 = {"pix_fmt": "yuv420p10le", "color_space": "bt2020nc",
         "color_transfer": "smpte2084", "color_primaries": "bt2020"}
UNTAGGED_10BIT = {"pix_fmt": "yuv420p10le"}


def media(raw: dict, *, codec: str = "hevc", height: int = 2160,
          dovi_in_stream: bool | None = None) -> MediaInfo:
    video = Stream(index=0, codec_type="video", codec_name=codec,
                   width=3840, height=height, bit_depth=10,
                   pix_fmt=raw.get("pix_fmt", ""), fps=23.976, raw=dict(raw))
    audio = Stream(index=1, codec_type="audio", codec_name="eac3", channels=6,
                   raw={"bit_rate": "640000"})
    return MediaInfo(path="/media/tv/Show/S01E01.mkv", container="mkv",
                     duration=3000, size=8000 * MB, streams=[video, audio],
                     dovi_in_stream=dovi_in_stream)


def checked(info: MediaInfo, settings) -> CheckResult:
    result = CheckResult(path=info.path)
    colour.check(info, settings.emby_compat, result)
    return result


# -- recognising it -------------------------------------------------------

def test_profile_5_is_hdr_although_it_carries_no_colour_tags():
    """The bug that let the shrink at these files: the stream-level record is
    called "DOVI configuration record", and only "dolby vision" was matched.
    A profile 5 stream has no transfer, primaries or matrix, so the record is
    the only sign it is HDR at all."""
    assert media(PROFILE_5).is_hdr


def test_profile_5_has_no_fallback_and_profile_8_1_does():
    assert media(PROFILE_5).dovi_without_fallback
    assert not media(PROFILE_8_1).dovi_without_fallback
    assert not media(HDR10).dovi_without_fallback
    assert not media(UNTAGGED_10BIT).dovi_without_fallback


def test_a_compatibility_id_of_zero_counts_only_on_an_untagged_base_layer():
    """Profile 8 with compatibility 0 is malformed. Untagged, a non-DV player
    sees what it sees on profile 5; with PQ tags it plays as HDR10."""
    untagged = {"pix_fmt": "yuv420p10le", "side_data_list": [dovi_record(8, 0)]}
    tagged = {**HDR10, "side_data_list": [dovi_record(8, 0)]}
    assert media(untagged).dovi_without_fallback
    assert not media(tagged).dovi_without_fallback


def test_the_rpu_in_the_frames_is_found_when_the_record_is_gone():
    hidden = media(UNTAGGED_10BIT, dovi_in_stream=True)
    assert hidden.dovi is None
    assert hidden.dovi_without_fallback
    assert hidden.is_hdr
    # Nobody looked, or the frame said no: an ordinary 10-bit encode.
    assert not media(UNTAGGED_10BIT, dovi_in_stream=None).dovi_without_fallback
    assert not media(UNTAGGED_10BIT, dovi_in_stream=False).dovi_without_fallback


def test_only_a_stream_that_could_hide_one_has_its_first_frame_read():
    assert _could_hide_dovi(media(UNTAGGED_10BIT))
    assert not _could_hide_dovi(media(PROFILE_5))       # the record already says
    assert not _could_hide_dovi(media(HDR10))           # tagged, so not profile 5
    assert not _could_hide_dovi(media(UNTAGGED_10BIT, codec="h264"))
    eight_bit = media({"pix_fmt": "yuv420p"})
    eight_bit.video.bit_depth = 8
    assert not _could_hide_dovi(eight_bit)


# -- the finding ----------------------------------------------------------

def test_profile_5_is_flagged_as_an_error(settings):
    result = checked(media(PROFILE_5), settings)
    [f] = result.findings
    assert (f.category, f.code, f.severity) == ("compat", colour.CODE, "error")
    assert f.data == {"dv_profile": 5, "record": True}
    assert "purple and green" in f.detail
    assert result.status == "incompatible"


def test_a_missing_record_says_so(settings):
    [f] = checked(media(UNTAGGED_10BIT, dovi_in_stream=True), settings).findings
    assert f.code == colour.CODE and f.data["record"] is False
    assert "record missing" in f.detail


def test_profile_8_1_and_hdr10_are_not_flagged(settings):
    assert not checked(media(PROFILE_8_1), settings).findings
    assert not checked(media(HDR10), settings).findings


def test_dolby_vision_clients_turns_the_error_into_a_note(settings):
    settings.emby_compat.dolby_vision_clients = True
    result = checked(media(PROFILE_5), settings)
    [f] = result.findings
    assert f.severity == "info"
    assert result.status == "ok"


def test_the_check_follows_the_compat_switch(settings):
    settings.emby_compat.enabled = False
    assert not checked(media(PROFILE_5), settings).findings


@needs_ffmpeg
def test_the_check_runs_although_emby_said_yes(video_factory, settings, monkeypatch):
    """Emby says yes to these files; that yes is how the purple picture
    reaches the screen. The codec table is skipped on a yes (invariant 7);
    this check must not be."""
    from unfuckarr import scanner as scanner_mod

    path = video_factory("dv.mkv", seconds=4)
    real_integrity = scanner_mod.integrity_checks.check

    def as_profile_5(*a, **kw):
        result, info = real_integrity(*a, **kw)
        info.video.raw.update(PROFILE_5)
        return result, info
    monkeypatch.setattr(scanner_mod.integrity_checks, "check", as_profile_5)

    class YesEmby:
        def check_direct_play(self, path, cfg, result):
            return True
    settings.emby.enabled = True
    settings.emby.use_playback_info = True
    result, _ = check_file(str(path), settings, emby=YesEmby())
    assert colour.CODE in {f.code for f in result.findings}


# -- what is done about it ------------------------------------------------

def dv_result(*extra: Finding) -> CheckResult:
    r = CheckResult(path="/media/tv/Show/S01E01.mkv")
    r.add(Finding("compat", colour.CODE, "error", "", {"dv_profile": 5}))
    r.add(*extra)
    return r


def test_it_is_never_sent_to_the_transcoder(settings):
    """Emby's silent "no" would normally be re-encoded into the profile
    (invariant 29). Here that re-encode is exactly the damage."""
    settings.policy.incompatible_action = "transcode"
    d = decide(dv_result(Finding("emby", "no_direct_play", "error", "")), settings)
    assert d.action == "flag"
    assert not d.reencode


def test_a_fixable_audio_problem_beside_it_does_not_open_the_transcoder(settings):
    d = decide(dv_result(Finding("compat", "default_audio_unplayable", "error", "")),
               settings)
    assert d.action == "flag"


def test_research_asks_for_a_release_with_a_base_layer(settings):
    settings.policy.unfixable_action = "research"
    assert decide(dv_result(), settings).action == "research"


def test_corruption_still_comes_first(settings):
    d = decide(dv_result(Finding("integrity", "zero_length", "error", "")), settings)
    assert d.action == "redownload"


def test_it_is_never_measured_for_a_shrink_even_with_hdr_allowed(settings):
    settings.efficiency.allow_hdr = True
    result = CheckResult(path="x")
    efficiency.check(media(PROFILE_5), settings.efficiency, result)
    assert [f.code for f in result.findings] == ["dolby_vision_not_shrunk"]
    assert not result.unmeasured


def test_profile_8_1_is_still_an_ordinary_hdr_file(settings):
    result = CheckResult(path="x")
    efficiency.check(media(PROFILE_8_1), settings.efficiency, result)
    assert [f.code for f in result.findings] == ["hdr_not_shrunk"]


def _as_profile_5(info: MediaInfo) -> MediaInfo:
    info.video.raw.update(PROFILE_5)
    return info


@needs_ffmpeg
def test_the_transcoder_refuses_a_video_encode_whoever_asks(video_factory, settings,
                                                             monkeypatch):
    """The guard behind `decide`: a decision that reaches `_transcode` anyway
    (made before the check existed, or escalated from an Emby refusal) must
    not run an encode."""
    path = video_factory("legacy.avi", seconds=4, vcodec="mpeg2video",
                         acodec="ac3", extra=["-b:v", "1M"])
    db.ex("INSERT INTO files (path, library, source, title) VALUES (?,?,?,?)",
          (str(path), "TV", "folder", "Show"))
    row = {"path": str(path), "source": "folder", "title": "Show",
           "arr_id": None, "arr_parent_id": None}
    result, info = check_file(str(path), settings)
    assert transcode.plan(info, result, settings.transcode,
                          settings.emby_compat).video_action == "encode"

    def must_not_run(*a, **k):
        raise AssertionError("ffmpeg ran on a profile 5 file")
    monkeypatch.setattr(transcode, "run", must_not_run)

    out = Remediator(lambda: settings).apply(
        row, result, _as_profile_5(info), Decision("transcode", "incompatible"))
    assert out["action"] == "flag" and "refused" in out["message"]
    assert path.exists()
    attempts = db.q("SELECT fix_attempts FROM files WHERE path=?", (str(path),))[0]
    assert not attempts["fix_attempts"], "nothing ran, so nothing is counted"


@needs_ffmpeg
def test_the_shrink_refuses_it_even_with_hdr_allowed(video_factory, settings,
                                                     monkeypatch):
    path = video_factory("dv.mkv", seconds=4)
    db.ex("INSERT INTO files (path, library, source, title) VALUES (?,?,?,?)",
          (str(path), "TV", "folder", "Show"))
    row = {"path": str(path), "source": "folder", "title": "Show",
           "arr_id": None, "arr_parent_id": None}
    settings.efficiency.allow_hdr = True
    result, info = check_file(str(path), settings)

    def must_not_search(*a, **k):
        raise AssertionError("the quality search ran on a profile 5 file")
    monkeypatch.setattr(quality, "resolve_metric", must_not_search)

    out = Remediator(lambda: settings).apply(
        row, result, _as_profile_5(info), Decision("shrink", "measuring"))
    assert out["action"] == "flag" and "Dolby Vision" in out["message"]
    skipped = db.q("SELECT shrink_skipped FROM files WHERE path=?", (str(path),))[0]
    assert "Dolby Vision" in skipped["shrink_skipped"]


def test_a_profile_5_replacement_is_turned_away(settings, monkeypatch):
    """Even with `dolby_vision_clients` on: the file it would replace plays
    everywhere."""
    from unfuckarr import scanner as scanner_mod

    settings.emby_compat.dolby_vision_clients = True
    info = media(PROFILE_5)

    def fake_check(path, s, **kw):
        result = CheckResult(path=path)
        colour.check(info, s.emby_compat, result)
        return result, info
    monkeypatch.setattr(scanner_mod, "check_file", fake_check)
    ok, why, _ = replace.judge("/downloads/x.mkv", {}, settings)
    assert not ok and "Dolby Vision" in why


def test_a_stream_copy_to_mp4_keeps_the_dolby_vision_record(settings):
    """ffmpeg 8.0.1's MP4 muxer drops the record on a copy without
    `-strict unofficial` (measured live); Matroska keeps it unasked."""
    info = media(PROFILE_8_1)
    plan = transcode.TranscodePlan(reason="compatibility", container="mp4")
    plan.video_action = "copy"
    cmd = transcode.build_command("/in.mkv", "/out.mp4", info, plan,
                                  settings.transcode)
    i = cmd.index("-strict")
    assert cmd[i + 1] == "unofficial"
    plan.container = "mkv"
    assert "-strict" not in transcode.build_command(
        "/in.mkv", "/out.mkv", info, plan, settings.transcode)
    plan.container = "mp4"
    assert "-strict" not in transcode.build_command(
        "/in.mkv", "/out.mp4", media(HDR10), plan, settings.transcode)


# -- the real thing -------------------------------------------------------

@needs_ffmpeg
def test_an_ordinary_10_bit_hevc_file_is_read_and_left_alone(tmp_path, settings):
    """`_could_hide_dovi` sends exactly this kind of file to the frame read;
    the read must come back "no", not "could not tell"."""
    from unfuckarr.probe import probe
    from .conftest import make_video

    try:
        path = make_video(tmp_path / "x265.mkv", seconds=2, vcodec="libx265",
                          extra=["-pix_fmt", "yuv420p10le"])
    except Exception:  # noqa: BLE001
        pytest.skip("this ffmpeg has no 10-bit libx265")
    info = probe(path, settings.ffprobe_path)
    if info.video.raw.get("color_transfer"):
        pytest.skip("this x265 tags its output, so the frame read is not asked")
    assert info.dovi_in_stream is False
    assert not info.dovi_without_fallback
    assert not checked(info, settings).findings
