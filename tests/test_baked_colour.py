"""Dolby Vision baked in: the purple and green already in the pixels.

The stream shapes and release titles below are the ones the live library
showed on 2026-11-15 (ffprobe 8.0.1 inside `unfk`, sceneNames from Sonarr and
Radarr): Strange New Worlds S04E09, the shrink's output of a profile 5 source
(10-bit, limited range, untagged); Bridgerton S02E03, one of the nineteen
8-bit full-range files some earlier tool left; and the files beside them that
looked right and must stay unflagged — Futurama S11E05 and The Wire's 2160p
upscales (untagged 10-bit, no DV in the name), and Sherlock S02 (8-bit full
range at 2160p, but tagged PQ).
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from unfuckarr import db, replace, scanner
from unfuckarr.checks import CheckResult, Finding, colour, efficiency
from unfuckarr.playback import _not_for_this
from unfuckarr.probe import MediaInfo, Stream
from unfuckarr.remediation import Decision, decide

from .conftest import make_video, needs_ffmpeg
from .test_replace import FakeArr, _remediator, release

MB = 1024 * 1024

SNW_RELEASE = "Star.Trek.Strange.New.Worlds.S04E09.DV.2160p.WEB.H265-CAKES"
BRIDGERTON_RELEASE = "Bridgerton.S02E03.DV.2160p.WEB.h265-EDITH"

# SNW S04E09 after the shrink: no record, no RPU, no tags, limited range.
SHRUNK = {"pix_fmt": "yuv420p10le", "color_range": "tv"}
# Bridgerton S02E03 and eighteen more: 8-bit, full range, nothing else.
EIGHT_BIT_FULL = {"pix_fmt": "yuvj420p", "color_range": "pc"}
# Sherlock S02E01, an upscale: the same shape, but it says what it is.
SHERLOCK = {"pix_fmt": "yuvj420p", "color_range": "pc",
            "color_space": "bt2020nc", "color_transfer": "smpte2084",
            "color_primaries": "bt2020"}
BT709 = {"pix_fmt": "yuv420p", "color_range": "tv", "color_space": "bt709",
         "color_transfer": "bt709", "color_primaries": "bt709"}
PROFILE_5 = {"pix_fmt": "yuv420p10le", "color_range": "tv",
             "side_data_list": [{"side_data_type": "DOVI configuration record",
                                 "dv_profile": 5,
                                 "dv_bl_signal_compatibility_id": 0}]}


def media(raw: dict, *, codec: str = "hevc", width: int = 3840,
          height: int = 2160, bit_depth: int | None = None,
          dovi_in_stream: bool | None = None) -> MediaInfo:
    """A probed file. ``dovi_in_stream`` defaults to what `probe` would have
    found: False for a stream it reads the first frame of, None otherwise."""
    depth = bit_depth or (10 if "10" in raw.get("pix_fmt", "") else 8)
    video = Stream(index=0, codec_type="video", codec_name=codec,
                   width=width, height=height, bit_depth=depth,
                   pix_fmt=raw.get("pix_fmt", ""), fps=23.976, raw=dict(raw))
    audio = Stream(index=1, codec_type="audio", codec_name="eac3", channels=6)
    info = MediaInfo(path="/media/tv/Show/S04E09.mkv", container="mkv",
                     duration=3000, size=4000 * MB, streams=[video, audio])
    from unfuckarr.probe import _could_hide_dovi
    info.dovi_in_stream = (dovi_in_stream if dovi_in_stream is not None
                           else (False if _could_hide_dovi(info) else None))
    return info


def checked(info: MediaInfo, settings, release: str | None = None) -> CheckResult:
    result = CheckResult(path=info.path)
    colour.check(info, settings.emby_compat, result, release=release)
    return result


# -- the release title ----------------------------------------------------

@pytest.mark.parametrize("title", [
    SNW_RELEASE,
    "star.trek.strange.new.worlds.s04e10.dv.2160p.web.h265-cakes",
    "House of the Dragon S01E03 Second of His Name 2160p HMAX WEB-DL DDP5 1 DoVi HEVC-NTb",
    "Andor.S02E11.GERMAN.DL.DV.2160P.WEB.H265.REPACK-RiLE-xpost",
    "Some.Film.2025.2160p.WEB-DL.Dolby.Vision.H.265-GRP",
    "Some.Film.2025.2160p.WEB-DL.DolbyVision.H.265-GRP",
])
def test_a_title_naming_dolby_vision_and_no_hdr_is_trash_s_format(title):
    assert colour.names_dv_without_hdr(title)


@pytest.mark.parametrize("title", [
    "Andor.S02E10.Make.It.Stop.REPACK.2160p.DSNP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-BLOOM",
    "Star.Wars.Andor.2022.S02E07.Messenger.2160p.HDR10.DV.Hybrid.DSNP.Webrip.x265",
    "Some.Show.S01E01.2160p.WEB.DV.HDR10Plus.H265-GRP",
    "Bridgerton.S02E03.HDR.2160p.WEB.h265-EDITH",
    "Futurama.S11E05.2160p.WEB.H265-SuccessfulCrab[TGx]-xpost",
    "Some.Film.1999.DVDRip.XviD-GRP",
    "",
    None,
])
def test_hdr_in_the_title_or_no_dolby_vision_is_not(title):
    assert not colour.names_dv_without_hdr(title)


# -- recognising it -------------------------------------------------------

def test_the_shrink_output_is_caught_by_its_release_alone():
    """SNW S04E09: nothing in the stream tells it from a plain SDR encode."""
    assert colour.baked(media(SHRUNK), SNW_RELEASE) == ["release"]
    assert colour.baked(media(SHRUNK)) == []


def test_the_eight_bit_full_range_shape_needs_no_release():
    assert colour.baked(media(EIGHT_BIT_FULL)) == ["shape"]
    assert colour.baked(media(EIGHT_BIT_FULL), BRIDGERTON_RELEASE) == ["release", "shape"]


def test_a_newer_ffmpeg_saying_yuv420p_and_pc_is_the_same_shape():
    """FFmpeg is retiring the yuvj formats; the range is what matters."""
    assert colour.baked(media({"pix_fmt": "yuv420p", "color_range": "pc"})) == ["shape"]


def test_the_shape_is_2160p_by_width_too():
    """A Knight of the Seven Kingdoms S01E01 is 3840x1920."""
    assert colour.baked(media(EIGHT_BIT_FULL, height=1920)) == ["shape"]
    assert colour.baked(media(EIGHT_BIT_FULL, width=1920, height=1080)) == []


def test_the_shape_is_hevc_and_eight_bit_only():
    assert colour.baked(media(EIGHT_BIT_FULL, codec="h264")) == []
    assert colour.baked(media({"pix_fmt": "yuv420p10le", "color_range": "pc"})) == []
    assert colour.baked(media({"pix_fmt": "yuv420p", "color_range": "tv"})) == []


def test_any_colour_tag_means_somebody_knew_what_colour_it_was():
    """Sherlock S02 is the 8-bit full-range shape, tagged PQ; a DV-named
    release whose file is tagged BT.709 was tone-mapped on purpose."""
    assert colour.baked(media(SHERLOCK)) == []
    assert colour.baked(media(BT709), SNW_RELEASE) == []
    assert colour.baked(media({**SHRUNK, "color_primaries": "bt709"}), SNW_RELEASE) == []


def test_unknown_tags_are_no_tags():
    raw = {**SHRUNK, "color_space": "unknown", "color_transfer": "unknown",
           "color_primaries": "unknown"}
    assert colour.baked(media(raw), SNW_RELEASE) == ["release"]


def test_a_file_that_still_has_its_dolby_vision_is_the_other_finding():
    """The RPU can still be applied while it is there."""
    assert colour.baked(media(PROFILE_5), SNW_RELEASE) == []
    assert colour.baked(media(SHRUNK, dovi_in_stream=True), SNW_RELEASE) == []


def test_could_not_look_is_not_nothing_left():
    """A 10-bit untagged HEVC stream could carry the RPU in its frames; when
    the first frame could not be read, it is not called baked."""
    unread = media(SHRUNK)
    unread.dovi_in_stream = None
    assert colour.baked(unread, SNW_RELEASE) == []


def test_plain_untagged_sdr_is_left_alone():
    """Futurama S11E05 and The Wire's upscales: untagged 10-bit HEVC whose
    release says nothing about Dolby Vision."""
    assert colour.baked(media(SHRUNK),
                        "Futurama.S11E05.2160p.WEB.H265-SuccessfulCrab[TGx]-xpost") == []
    assert colour.baked(media(SHRUNK), None) == []
    assert colour.baked(media({"pix_fmt": "yuv420p"}, codec="h264",
                              width=1920, height=1080), None) == []


def test_a_disc_image_is_never_one():
    info = media(EIGHT_BIT_FULL)
    info.disc_kind = "bluray"
    assert colour.baked(info, SNW_RELEASE) == []


# -- the finding ----------------------------------------------------------

def test_it_is_an_error_that_says_which_signal(settings):
    result = checked(media(SHRUNK), settings, SNW_RELEASE)
    [f] = result.findings
    assert (f.category, f.code, f.severity) == ("compat", colour.BAKED, "error")
    assert f.data == {"signals": ["release"], "release": SNW_RELEASE}
    assert SNW_RELEASE in f.detail and "purple and green" in f.detail
    assert result.status == "incompatible"


def test_the_shape_finding_names_the_shape(settings):
    [f] = checked(media(EIGHT_BIT_FULL), settings).findings
    assert f.data == {"signals": ["shape"], "release": None}
    assert "8-bit full-range HEVC at 3840x2160" in f.detail


def test_dolby_vision_clients_does_not_soften_it(settings):
    """Once the cast is in the pixels a Dolby Vision client has nothing to
    apply either."""
    settings.emby_compat.dolby_vision_clients = True
    [f] = checked(media(EIGHT_BIT_FULL), settings).findings
    assert f.severity == "error"


def test_it_follows_the_compat_switch(settings):
    settings.emby_compat.enabled = False
    assert not checked(media(EIGHT_BIT_FULL), settings).findings


def test_profile_5_still_reads_as_profile_5(settings):
    [f] = checked(media(PROFILE_5), settings, SNW_RELEASE).findings
    assert f.code == colour.CODE


# -- what is done about it ------------------------------------------------

def baked_result(*extra: Finding) -> CheckResult:
    r = CheckResult(path="/media/tv/Show/S04E09.mkv")
    r.add(Finding("compat", colour.BAKED, "error", "", {"signals": ["release"]}))
    r.add(*extra)
    return r


def test_the_default_flags_it(settings):
    assert settings.policy.baked_colour_action == "unfixable"
    assert decide(baked_result(), settings).action == "flag"


def test_research_and_replace_are_the_ways_out(settings):
    settings.policy.unfixable_action = "research"
    assert decide(baked_result(), settings).action == "research"
    settings.policy.baked_colour_action = "replace"
    d = decide(baked_result(), settings)
    assert d.action == "replace" and colour.BAKED in d.findings


def test_replacing_low_bitrate_files_does_not_replace_these(settings):
    settings.policy.low_bitrate_action = "replace"
    assert decide(baked_result(), settings).action == "flag"


def test_it_is_never_sent_to_the_transcoder(settings):
    settings.policy.incompatible_action = "transcode"
    for extra in (Finding("emby", "no_direct_play", "error", ""),
                  Finding("compat", "default_audio_unplayable", "error", "")):
        d = decide(baked_result(extra), settings)
        assert d.action == "flag" and not d.reencode


def test_it_is_never_shrunk(settings):
    """Not the shrink's damage this time, but an encode spent on a picture
    that is waiting to be replaced. Even with HDR allowed."""
    settings.policy.oversize_action = "shrink"
    settings.efficiency.allow_hdr = True
    result = baked_result(Finding("efficiency", "not_measured", "info", ""))
    assert decide(result, settings).action == "flag"
    result = CheckResult(path="x")
    efficiency.check(media(SHRUNK), settings.efficiency, result,
                     release=SNW_RELEASE)
    [f] = result.findings
    assert f.code == "dolby_vision_not_shrunk" and f.data["baked"]
    assert not result.unmeasured


def test_corruption_still_comes_first(settings):
    d = decide(baked_result(Finding("integrity", "zero_length", "error", "")), settings)
    assert d.action == "redownload"


def test_a_failed_play_is_not_a_reason_to_grab(settings):
    d = _not_for_this(Decision("replace", "x", [colour.BAKED]), baked_result())
    assert d.action == "flag" and "colour is baked in" in d.reason


# -- choosing a replacement ----------------------------------------------

# Mission: Impossible – The Final Reckoning, 6.3 Mbps HEVC at 2160p: not a
# small file, and its fault is not its size.
CURRENT = {"mbps": 6.3, "equiv": 12.6, "duration": 10_140.0, "tier": 2160,
           "codec": "hevc"}


def film(title: str, gb: float, resolution: int = 2160, **kw):
    return release(title, gb, resolution, eps=(), **kw)


def test_a_colour_replacement_needs_no_bitrate_gain():
    hdr = film("Mission.Impossible.2025.2160p.WEB-DL.HDR.H.265-GRP", 12.0)  # 9.5 Mbps
    assert replace.choose([hdr], CURRENT, sonarr=False)[0] is None
    choice, _ = replace.choose([hdr], CURRENT, sonarr=False, colour=True)
    assert choice["title"] == hdr["title"]


def test_profile_5_is_not_a_cure_for_profile_5():
    choice, skipped = replace.choose(
        [film("Mission.Impossible.2025.2160p.PMTP.WEB-DL.DV.H.265-GRP", 12.0)],
        CURRENT, sonarr=False, colour=True)
    assert choice is None
    assert skipped["Dolby Vision with no HDR fallback"] == 1


def test_the_floor_and_the_resolution_still_hold():
    choice, skipped = replace.choose([
        film("Mission.Impossible.2025.1080p.WEB-DL.H.264-GRP", 8.0, 1080),
        film("Mission.Impossible.2025.2160p.WEB.x265-TINY", 1.0),   # 0.8 Mbps
    ], CURRENT, sonarr=False, colour=True)
    assert choice is None
    assert skipped["lower resolution than the file"] == 1
    assert skipped["would still be heavily re-encoded"] == 1


@pytest.fixture
def baked_row(tmp_path, settings):
    settings.policy.baked_colour_action = "replace"
    settings.policy.recycle_bin_path = str(tmp_path / "bin")
    path = tmp_path / "tv" / "SNW S04E09.mkv"
    path.parent.mkdir()
    path.write_bytes(b"x" * 1000)
    probe = {"size": 3_000_000_000, "duration": 3300.0,
             "video": {"codec": "hevc", "width": 3832, "height": 1600}}
    db.ex("INSERT INTO files (path, library, source, arr_id, arr_parent_id, "
          "arr_episode_ids, title, probe, release) VALUES (?,?,?,?,?,?,?,?,?)",
          (str(path), "TV", "sonarr", 21, 2014, json.dumps([88001]), "SNW",
           json.dumps(probe), SNW_RELEASE))
    return {"path": str(path), "source": "sonarr", "arr_id": 21,
            "arr_parent_id": 2014, "arr_episode_ids": [88001], "title": "SNW",
            "last_research": None, "probe": json.dumps(probe),
            "release": SNW_RELEASE}


def test_a_colour_grab_records_no_bitrate_to_beat(settings, baked_row):
    """So that `judge` asks of the download what was asked of the release:
    its colour, not twice the picture."""
    arr = FakeArr([
        release("Star.Trek.Strange.New.Worlds.S04E09.DV.2160p.WEB.H265-CAKES", 9.0, 2160),
        release("Star.Trek.Strange.New.Worlds.S04E09.HDR.2160p.WEB.h265-ETHEL", 6.0, 2160),
    ])
    out = _remediator(settings, arr).apply(
        baked_row, baked_result(), None,
        Decision("replace", "x", [colour.BAKED]))
    assert out["ok"] and "for its colour" in out["message"], out
    assert arr.grabbed == [("Star.Trek.Strange.New.Worlds.S04E09.HDR.2160p.WEB.h265-ETHEL", 4)]
    rec = db.q1("SELECT * FROM replacements")
    assert rec["old_mbps"] is None and rec["old_tier"] == 2160
    assert os.path.exists(baked_row["path"]) and arr.deleted == []


def test_the_ui_s_replace_button_replaces_for_colour_too(settings, baked_row):
    """A forced decision carries no findings; the check says what it is."""
    arr = FakeArr([release("Star.Trek.Strange.New.Worlds.S04E09.HDR.2160p.WEB.h265-ETHEL",
                           6.0, 2160)])
    out = _remediator(settings, arr).apply(
        baked_row, baked_result(), None,
        Decision("replace", "replace requested from the web UI", force=True))
    assert out["ok"] and "for its colour" in out["message"], out
    assert db.q1("SELECT old_mbps FROM replacements")["old_mbps"] is None


def test_none_found_says_why_in_colour_terms(settings, baked_row):
    arr = FakeArr([release("Star.Trek.Strange.New.Worlds.S04E09.DV.2160p.WEB.H265-CAKES",
                           9.0, 2160)])
    out = _remediator(settings, arr).apply(
        baked_row, baked_result(), None, Decision("replace", "x", [colour.BAKED]))
    assert out["ok"] and "without Dolby Vision profile 5" in out["message"]
    assert arr.grabbed == []


# -- judging what arrives -------------------------------------------------

def _judge(settings, monkeypatch, info: MediaInfo, title: str | None):
    def fake_check(path, s, **kw):
        result = CheckResult(path=path)
        colour.check(info, s.emby_compat, result, release=kw.get("release"))
        return result, info
    monkeypatch.setattr(scanner, "check_file", fake_check)
    return replace.judge("/downloads/x.mkv", {"title": title}, settings)


def test_a_replacement_with_the_colour_baked_in_is_turned_away(settings, monkeypatch):
    ok, why, _ = _judge(settings, monkeypatch, media(SHRUNK), SNW_RELEASE)
    assert not ok and "baked in" in why and "release names" in why
    ok, why, _ = _judge(settings, monkeypatch, media(EIGHT_BIT_FULL),
                        "Bridgerton.S02E03.2160p.WEB.x265-SOMEONE")
    assert not ok and "8-bit full-range" in why


def test_it_is_asked_of_the_file_whatever_the_compat_switch(settings, monkeypatch):
    settings.emby_compat.enabled = False
    ok, _, _ = _judge(settings, monkeypatch, media(EIGHT_BIT_FULL), None)
    assert not ok


def test_an_honest_replacement_passes(settings, monkeypatch):
    hdr10 = {"pix_fmt": "yuv420p10le", "color_space": "bt2020nc",
             "color_transfer": "smpte2084", "color_primaries": "bt2020"}
    ok, why, _ = _judge(settings, monkeypatch, media(hdr10),
                        "Bridgerton.S02E03.HDR.2160p.WEB.h265-EDITH")
    assert ok, why
    ok, why, _ = _judge(settings, monkeypatch, media(SHRUNK),
                        "Andor.S02E01.2160p.10bit.DSNP.WEB-DL.DDP5.1.HEVC-Vyndros")
    assert ok, why


# -- where the release title comes from -----------------------------------

ARR_ROUTES = {
    "/api/v3/series": [{"id": 5, "title": "SNW", "runtime": 55}],
    "/api/v3/episodefile": [
        {"id": 21, "path": "/tv/SNW/S04E09.mkv", "sceneName": SNW_RELEASE},
        {"id": 22, "path": "/tv/SNW/S04E10.mkv"}],
    "/api/v3/episode": [
        {"id": 1, "episodeFileId": 21, "seasonNumber": 4, "episodeNumber": 9},
        {"id": 2, "episodeFileId": 22, "seasonNumber": 4, "episodeNumber": 10}],
    "/api/v3/movie": [
        {"id": 1, "title": "MI", "movieFile": {"id": 9, "path": "/m/mi.mkv",
                                               "sceneName": "MI.2025.DV.H.265-X"}},
        {"id": 2, "title": "ES", "movieFile": {"id": 10, "path": "/m/es.mkv"}}],
}


def test_both_arrs_hand_over_the_scene_name(monkeypatch):
    """Both leave `sceneName` out when they have none (277 of 412 Radarr
    files on the live install)."""
    from unfuckarr.clients.arr import ArrClient
    from unfuckarr.config import ArrConfig

    def handler(request):
        return httpx.Response(200, json=ARR_ROUTES[request.url.path])
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **kw: real(transport=httpx.MockTransport(handler), **kw))
    cfg = ArrConfig(enabled=True, url="http://arr:8989", api_key="k")
    assert [i["release"] for i in ArrClient(cfg, "sonarr").library()] == [SNW_RELEASE, None]
    assert [i["release"] for i in ArrClient(cfg, "radarr").library()] == [
        "MI.2025.DV.H.265-X", None]


def test_the_inventory_keeps_it_and_the_scan_passes_it_on(tmp_path, settings,
                                                         monkeypatch):
    path = tmp_path / "S04E09.mkv"
    path.write_bytes(b"x")
    scanner.sync_inventory([{"path": str(path), "source": "sonarr", "library": "TV",
                             "arr_id": 21, "arr_parent_id": 5, "title": "SNW",
                             "release": SNW_RELEASE}])
    row = db.q1("SELECT release FROM files WHERE path=?", (str(path),))
    assert row["release"] == SNW_RELEASE
    # A new grab replaces it on the next enumeration.
    scanner.sync_inventory([{"path": str(path), "source": "sonarr", "library": "TV",
                             "arr_id": 23, "arr_parent_id": 5, "title": "SNW",
                             "release": "SNW.S04E09.HDR.2160p.WEB.h265-ETHEL"}])
    assert db.q1("SELECT release FROM files WHERE path=?",
                 (str(path),))["release"].endswith("ETHEL")


@needs_ffmpeg
def test_a_recheck_reads_the_release_from_the_row(tmp_path, settings, monkeypatch):
    from unfuckarr import service as service_mod

    path = make_video(tmp_path / "S04E09.mkv", seconds=4)
    db.ex("INSERT INTO files (path, library, source, title, release) "
          "VALUES (?,?,?,?,?)", (str(path), "TV", "sonarr", "SNW", SNW_RELEASE))
    seen = {}
    real = service_mod.check_file

    def spy(*a, **kw):
        seen["release"] = kw.get("release")
        return real(*a, **kw)
    monkeypatch.setattr(service_mod, "check_file", spy)
    service = service_mod.Service.__new__(service_mod.Service)  # act=False needs no workers
    service.recheck(str(path), act=False)
    assert seen["release"] == SNW_RELEASE


# -- the real thing -------------------------------------------------------

@pytest.fixture
def small_clips_are_whole(monkeypatch):
    """A three-second clip is under the size floor that calls a file a
    sample; that floor is not what these tests are about."""
    from unfuckarr.checks import integrity
    monkeypatch.setattr(integrity, "TINY_FILE_BYTES", 0)


def _clip(path, size: str, extra: list[str]):
    try:
        return make_video(path, seconds=3, vcodec="libx265", size=size,
                          extra=["-x265-params", "log-level=error", *extra])
    except Exception:  # noqa: BLE001
        pytest.skip("this ffmpeg has no libx265")


@needs_ffmpeg
@pytest.mark.usefixtures("small_clips_are_whole")
def test_a_real_8_bit_full_range_4k_hevc_file_is_found(tmp_path, settings):
    """The shape, written and read by a real ffmpeg: what an encode to
    full-range 8-bit with no colour description looks like to ffprobe."""
    path = _clip(tmp_path / "baked.mkv", "3840x2160", ["-pix_fmt", "yuvj420p"])
    result, info = scanner.check_file(str(path), settings)
    assert info.video.pix_fmt == "yuvj420p" and info.video.bit_depth == 8
    [f] = [f for f in result.findings if f.code == colour.BAKED]
    assert f.data["signals"] == ["shape"]
    assert decide(result, settings).action == "flag"


@needs_ffmpeg
@pytest.mark.usefixtures("small_clips_are_whole")
def test_real_files_of_the_other_shapes_are_not(tmp_path, settings):
    tagged = _clip(tmp_path / "tagged.mkv", "3840x2160",
                   ["-pix_fmt", "yuvj420p", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-colorspace", "bt709"])
    hd = _clip(tmp_path / "hd.mkv", "1920x1080", ["-pix_fmt", "yuvj420p"])
    limited = _clip(tmp_path / "limited.mkv", "3840x2160", [])
    for path in (tagged, hd, limited):
        result, _ = scanner.check_file(str(path), settings)
        assert colour.BAKED not in {f.code for f in result.findings}, path.name


@needs_ffmpeg
@pytest.mark.usefixtures("small_clips_are_whole")
def test_a_real_shrink_output_is_found_by_its_release(tmp_path, settings):
    """10-bit, limited, untagged: the frame read runs and finds no RPU, and
    only the release says what it was."""
    path = _clip(tmp_path / "shrunk.mkv", "640x360", ["-pix_fmt", "yuv420p10le"])
    result, info = scanner.check_file(str(path), settings, release=SNW_RELEASE)
    if info.video.raw.get("color_transfer"):
        pytest.skip("this x265 tags its output")
    assert info.dovi_in_stream is False
    assert colour.BAKED in {f.code for f in result.findings}
    for other in (None, "Star.Trek.Strange.New.Worlds.S04E03.HDR.2160p.WEB.h265-ETHEL"):
        result, _ = scanner.check_file(str(path), settings, release=other)
        assert colour.BAKED not in {f.code for f in result.findings}

    ok, why, _ = replace.judge(str(path), {"title": SNW_RELEASE}, settings)
    assert not ok and "baked in" in why
