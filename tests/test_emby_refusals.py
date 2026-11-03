"""Emby's silent refusals: finding out why, and what to do when nothing small will.

Measured on the live library, 2026-11-03: 328 files Emby would not direct play,
every one with an empty `TranscodeReasons`, and every one flagged "nothing in
the file looks wrong". Asking again with the profile relaxed named the cause
for 323. These tests pin the asking, the plans each cause earns, the re-encode
that answers the rest, and the brakes that keep a re-encode to once a file.
"""

from __future__ import annotations

import os

import pytest

from unfuckarr import db, transcode
from unfuckarr.checks import CheckResult, Finding
from unfuckarr.checks import hygiene as hygiene_checks
from unfuckarr.clients.emby import EmbyClient
from unfuckarr.probe import MediaInfo, Stream, probe
from unfuckarr.remediation import MAX_FIX_ATTEMPTS, Decision, Remediator
from unfuckarr.scanner import _verdict_without_reasons, needs_check

from .conftest import needs_ffmpeg


# -- a pretend Emby ---------------------------------------------------------

class PickyEmby(EmbyClient):
    """An Emby that plays a file only when the named parts are relaxed.

    `needs` is the set of profile parts the file fails on. A profile direct
    plays when every one of them accepts anything, which is how the real
    server behaves part by part (measured: relaxing the audio alone turned 226
    refusals into direct play).
    """

    def __init__(self, settings, needs: set[str], size: int | None = None,
                 reasons: list[str] | None = None):
        super().__init__(settings.emby)
        self.needs = needs
        self.size = size
        self.reasons = reasons or []
        self.asked: list[dict] = []
        self.refreshed: list[tuple] = []

    def build_index(self, max_age: float = 600.0):
        return {self.path: {"id": "item1", "name": "X", "media_source_id": "ms1"}}

    def device_profile(self, cfg):
        return {"Name": "t",
                "DirectPlayProfiles": [{"Container": "mkv", "Type": "Video",
                                        "VideoCodec": "h264", "AudioCodec": "ac3"}],
                "CodecProfiles": [{"Type": "Video", "Codec": "h264",
                                   "Conditions": [{"Property": "Height"}]}],
                "SubtitleProfiles": [{"Format": "srt", "Method": "External"}]}

    def playback_info(self, item_id, profile, media_source_id=None):
        self.asked.append(profile)
        direct = profile["DirectPlayProfiles"][0]
        relaxed = {
            "container": direct.get("Container") == "",
            "video": direct.get("VideoCodec") == "" and not profile["CodecProfiles"],
            "audio": direct.get("AudioCodec") == "",
            "subtitles": len(profile["SubtitleProfiles"]) > 1,
        }
        plays = all(relaxed.get(n, False) for n in self.needs)
        return {"Size": self.size, "Path": "/remote/x.mkv",
                "SupportsDirectPlay": plays, "SupportsDirectStream": False,
                "TranscodeReasons": [] if plays else self.reasons}

    def refresh_item(self, item_id, remote_path=None):
        self.refreshed.append((item_id, remote_path))


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "x.mkv"
    path.write_bytes(b"\0" * 1000)
    return str(path)


def _ask(settings, clip, **kw) -> tuple[bool, CheckResult, PickyEmby]:
    emby = PickyEmby(settings, size=os.path.getsize(clip), **kw)
    emby.path = clip
    result = CheckResult(path=clip)
    answered = emby.check_direct_play(clip, settings.emby_compat, result)
    return answered, result, emby


def _verdict(result: CheckResult) -> Finding:
    return next(f for f in result.findings if f.code == "no_direct_play")


def test_a_silent_refusal_is_explained_by_asking_again(settings, clip):
    answered, result, _ = _ask(settings, clip, needs={"audio"})
    f = _verdict(result)
    assert answered
    assert f.data == {"reasons": ["AudioCodecNotSupported"], "inferred": True}
    assert "audio codec not supported" in f.detail


def test_a_file_with_two_problems_names_both(settings, clip):
    """An AVI is the container and the codec at once. Relaxing one part at a
    time finds neither — which is what the first measurement did to the five
    files it could not explain — so the parts are put back one at a time."""
    _, result, _ = _ask(settings, clip, needs={"container", "video"})
    assert _verdict(result).data["reasons"] == ["ContainerNotSupported",
                                               "VideoCodecNotSupported"]


def test_a_refusal_nothing_relaxes_says_so_and_guesses_nothing(settings, clip):
    _, result, _ = _ask(settings, clip, needs={"something_else"})
    f = _verdict(result)
    assert f.data == {"reasons": [], "inferred": True}
    assert f.detail == "Emby would transcode this: Emby did not say why"


def test_reasons_emby_gives_are_not_second_guessed(settings, clip):
    _, result, emby = _ask(settings, clip, needs={"audio"},
                           reasons=["AudioCodecNotSupported"])
    assert _verdict(result).data == {"reasons": ["AudioCodecNotSupported"]}
    assert len(emby.asked) == 1


def test_a_verdict_about_a_file_since_replaced_is_set_aside(settings, clip):
    """Westworld S04E04, 2026-11-03: fixed at 04:42, and at 18:26 Emby still
    had one DTS track and the old size on record — so it refused, and the
    file was flagged as one nothing could be done for."""
    emby = PickyEmby(settings, needs={"audio"}, size=12_099_700_816)
    emby.path = clip
    result = CheckResult(path=clip)
    answered = emby.check_direct_play(clip, settings.emby_compat, result)
    assert not answered
    assert [f.code for f in result.findings] == ["emby_stale"]
    assert result.findings[0].severity == "info"
    assert emby.refreshed == [("item1", "/remote/x.mkv")]


def test_a_set_aside_verdict_is_asked_again_next_pass(settings, clip):
    row = {"path": clip, "status": "ok", "last_checked": 1e12,
           "checked_signature": None, "last_result": None,
           "convert_skipped": None, "convert_attempts": 0, "converted": None}
    stat = os.stat(clip)
    row["checked_signature"] = f"{stat.st_size}:{stat.st_mtime}"
    assert not needs_check(row, settings)
    row["last_result"] = '{"findings": [{"code": "emby_stale"}]}'
    assert needs_check(row, settings)


def test_an_inferred_reason_still_runs_the_local_table(settings):
    """It names the part, not the track; the table names the track."""
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("emby", "no_direct_play", "error", "",
                  {"reasons": ["AudioCodecNotSupported"], "inferred": True}))
    assert _verdict_without_reasons(r)
    stated = CheckResult(path="/media/x.mkv")
    stated.add(Finding("emby", "no_direct_play", "error", "",
                       {"reasons": ["AudioCodecNotSupported"]}))
    assert not _verdict_without_reasons(stated)


# -- the plans the causes earn ---------------------------------------------

def _info(audio=(), subs=(), container="mkv") -> MediaInfo:
    streams = [Stream(index=0, codec_type="video", codec_name="h264",
                      width=1920, height=1080, fps=25.0)]
    for t in audio:
        codec, channels, default = t[:3]
        streams.append(Stream(index=len(streams), codec_type="audio",
                              codec_name=codec, channels=channels,
                              is_default=default,
                              language=t[3] if len(t) > 3 else "eng"))
    for t in subs:
        codec, default, forced = t[:3]
        streams.append(Stream(index=len(streams), codec_type="subtitle",
                              codec_name=codec, is_default=default,
                              is_forced=forced,
                              language=t[3] if len(t) > 3 else "eng"))
    return MediaInfo(path=f"/media/x.{container}", input_url=f"/media/x.{container}",
                     container=container, duration=3600.0, size=6_000_000_000,
                     streams=streams)


def _refused(info, *reasons, inferred=True) -> CheckResult:
    r = CheckResult(path=info.path)
    r.add(Finding("emby", "no_direct_play", "error", "",
                  {"reasons": list(reasons), "inferred": inferred}))
    return r


def test_emby_naming_the_audio_promotes_a_playable_default(settings):
    """Vikings: DTS-HD default, AC-3 5.1 beside it. Emby's verdict alone, with
    no local finding, has to be enough to get the AC-3 promoted."""
    info = _info(audio=[("dts", 6, True), ("ac3", 6, False)])
    p = transcode.plan(info, _refused(info, "AudioCodecNotSupported"),
                       settings.transcode, settings.emby_compat)
    assert p.set_default_audio == 1
    assert p.video_action == "copy" and p.audio_action == "copy"


def test_emby_naming_the_subtitles_unflags_the_image_track_only(settings):
    """Dark Angel: one DVD subtitle flagged default and forced, burned in on
    every play. The text track's flags are nobody's problem and stay."""
    info = _info(audio=[("aac", 2, True)],
                 subs=[("subrip", True, False), ("dvd_subtitle", True, True)])
    p = transcode.plan(info, _refused(info, "SubtitleCodecNotSupported"),
                       settings.transcode, settings.emby_compat)
    assert p.clear_subtitle_flags == [1]
    cmd = transcode.build_command(info.path, "/tmp/out.mkv", info, p,
                                  settings.transcode)
    assert "-disposition:s:1" in cmd and "-disposition:s:0" not in cmd
    assert not transcode.changes_nothing(p, info)


def test_a_reencode_of_a_matroska_file_keeps_its_original_audio(settings):
    """The hybrid Allan chose for default audio (2026-10-12) holds here too:
    a playable default goes in front, the DTS stays behind it."""
    info = _info(audio=[("dts", 6, True)])
    p = transcode.plan(info, _refused(info), settings.transcode,
                       settings.emby_compat, reencode=True)
    assert p.reencode and p.video_action == "encode"
    assert p.audio_action == "copy" and p.add_audio is not None
    assert p.add_audio.codec == "ac3"
    assert p.describe.startswith("re-encode to the target profile")


def test_a_reencode_of_a_broadcast_capture_decodes_the_audio_too(settings):
    """The Sky at Night S2018E01: every remux to Matroska died on a packet with
    no timestamp, audio and subtitle packets included. Decoding is what gives
    one a time, so each track is encoded — at its own channels' bitrate, not
    the AAC stereo the transcode setting is sized for."""
    info = _info(audio=[("ac3", 6, True), ("mp2", 2, False)],
                 subs=[("dvb_subtitle", False, False)], container="ts")
    p = transcode.plan(info, _refused(info, "ContainerNotSupported"),
                       settings.transcode, settings.emby_compat, reencode=True)
    assert p.audio_action == "encode" and p.audio_codec == "ac3"
    cmd = " ".join(transcode.build_command(info.path, "/tmp/out.mkv", info, p,
                                           settings.transcode))
    assert "-c:a ac3" in cmd
    assert "-b:a:0 640k -ac:a:0 6" in cmd and "-b:a:1 224k -ac:a:1 2" in cmd
    assert "-c:s:0 copy" in cmd


def test_a_text_track_is_converted_for_matroska_not_dropped(settings):
    """The Sandman S02, nine episodes: MP4 to MKV with a `mov_text` track,
    which Matroska cannot hold. It was in the 'can carry' list, so it was
    copied, and every remux died writing the header."""
    info = _info(audio=[("aac", 2, True)], subs=[("mov_text", False, False, "")],
                 container="mp4")
    r = CheckResult(path=info.path)
    r.add(Finding("hygiene", "subtitle_missing_language", "warning", ""))
    p = transcode.plan(info, r, settings.transcode, settings.emby_compat)
    assert p.drop_subtitles == []
    cmd = transcode.build_command(info.path, "/tmp/out.mkv", info, p,
                                  settings.transcode)
    assert cmd[cmd.index("-c:s:0") + 1] == "srt"
    assert "language=eng" in cmd


def test_two_defaults_settle_on_the_default_language(settings):
    """Deep Space Nine S03E21: pl-PL and en-US, both E-AC-3 5.1, both flagged.
    On channels and stream order the Polish dub won."""
    info = _info(audio=[("eac3", 6, True, "pl-PL"), ("eac3", 6, True, "en-US")])
    r = CheckResult(path=info.path)
    r.add(Finding("hygiene", "multiple_default_audio", "warning", ""))
    p = transcode.plan(info, r, settings.transcode, settings.emby_compat)
    assert p.set_default_audio == 1


def test_no_linguistic_content_is_a_language_tag(settings):
    """Goldfinger's isolated score is tagged `zxx`, which is right, and was
    flagged as untagged, which the transcoder could never clear."""
    info = _info(audio=[("flac", 2, True, "zxx"), ("ac3", 6, False, "none")])
    result = CheckResult(path=info.path)
    hygiene_checks.check(info, settings.hygiene, result)
    flagged = next(f for f in result.findings if f.code == "audio_missing_language")
    assert flagged.data["streams"] == [2]
    p = transcode.plan(info, result, settings.transcode, settings.emby_compat)
    cmd = transcode.build_command(info.path, "/tmp/out.mkv", info, p,
                                  settings.transcode)
    assert cmd[cmd.index("-metadata:s:a:1") + 1] == "language=eng"
    assert "-metadata:s:a:0" not in cmd


# -- once, and only once ----------------------------------------------------

def _row(path, attempts=0):
    db.ex("INSERT OR REPLACE INTO files (path, title, fix_attempts) VALUES (?,?,?)",
          (path, "X", attempts))
    return {"path": path, "source": "folder", "title": "X", "arr_id": None,
            "arr_parent_id": None, "fix_attempts": attempts}


def _capture_run(monkeypatch):
    seen = []

    def fake_run(cmd, *a, **k):
        seen.append(cmd)
        return False, "stopped by the test"
    monkeypatch.setattr(transcode, "run", fake_run)
    return seen


def _real_mkv(video_factory):
    path = video_factory("x.mkv", seconds=4, acodec="ac3")
    return str(path), probe(str(path))


@needs_ffmpeg
def test_a_refusal_whose_small_fix_has_had_its_go_is_reencoded(
        settings, video_factory, monkeypatch):
    """The Sky at Night: the remux to Matroska failed, so the second go must
    not be the same remux — it is the re-encode."""
    seen = _capture_run(monkeypatch)
    path, info = _real_mkv(video_factory)
    result = _refused(info, "ContainerNotSupported")
    Remediator(lambda: settings).apply(
        _row(path, attempts=1), result, info,
        Decision("transcode", "Emby cannot direct play this", ["no_direct_play"]))
    cmd = seen[0]
    assert cmd[cmd.index("-c:v") + 1] == "libx264"


@needs_ffmpeg
def test_a_refusal_already_fixed_as_far_as_the_file_goes_is_reencoded(
        settings, video_factory, monkeypatch):
    """Emby names the audio, re-read the file, and the default track is
    already AC-3: the small fix is in the file and Emby still says no. A
    stream copy would change nothing (and once looped 162 times); the
    re-encode is what is left."""
    seen = _capture_run(monkeypatch)
    path, info = _real_mkv(video_factory)
    result = _refused(info, "ContainerNotSupported")
    Remediator(lambda: settings).apply(
        _row(path), result, info,
        Decision("transcode", "Emby cannot direct play this", ["no_direct_play"]))
    assert seen and "libx264" in seen[0]
    assert db.q1("SELECT COUNT(*) n FROM activity "
                 "WHERE event='transcode_no_op'")["n"] == 0


@needs_ffmpeg
def test_a_reencode_happens_once_and_is_never_shrunk(settings, video_factory):
    path, info = _real_mkv(video_factory)
    out = Remediator(lambda: settings).apply(
        _row(path), _refused(info), info,
        Decision("transcode", "no single change explains it",
                 ["no_direct_play"], reencode=True))
    assert out["ok"], out
    row = db.q1("SELECT fix_attempts, shrink_skipped FROM files WHERE path=?",
                (out["path"],))
    assert row["fix_attempts"] >= MAX_FIX_ATTEMPTS
    assert "second generation" in row["shrink_skipped"]
    assert probe(out["path"]).video.codec_name == "h264"


@needs_ffmpeg
def test_a_fix_for_a_refusal_is_counted_even_when_the_file_looks_fixed(
        settings, video_factory):
    """The re-check after a transcode does not ask Emby, so it cannot see
    whether a refusal cleared. Counting it is what keeps a fix Emby does not
    accept from being redone every pass — and what sends the next go, if
    there is one, to the re-encode."""
    path = str(video_factory("x.mkv", seconds=4, acodec="pcm_s16le"))
    info = probe(path)
    out = Remediator(lambda: settings).apply(
        _row(path), _refused(info, "AudioCodecNotSupported"), info,
        Decision("transcode", "Emby cannot direct play this", ["no_direct_play"]))
    assert out["ok"], out
    assert out["message"] == "add ac3 mono as the default audio"
    assert probe(out["path"]).video.codec_name == "h264"
    assert db.q1("SELECT fix_attempts FROM files WHERE path=?",
                 (out["path"],))["fix_attempts"] == 1
    assert db.q1("SELECT COUNT(*) n FROM activity "
                 "WHERE event='transcode_awaits_emby'")["n"] == 1


def test_a_broadcast_capture_goes_straight_to_the_reencode(settings):
    """The Sky at Night S2019E11, live after the deploy: its first attempt was a
    remux, which died in ten seconds, because not even its video will copy
    into Matroska. There is no smaller job, so the plan is the re-encode at
    once, whatever the finding."""
    info = _info(audio=[("ac3", 2, True)], subs=[("dvb_subtitle", False, False)],
                 container="ts")
    p = transcode.plan(info, _refused(info, "ContainerNotSupported"),
                       settings.transcode, settings.emby_compat)
    assert p.reencode and p.video_action == "encode"
    assert p.audio_action == "encode" and p.audio_codec == "ac3"

    tidy = CheckResult(path=info.path)
    tidy.add(Finding("hygiene", "audio_missing_language", "warning", ""))
    assert transcode.plan(info, tidy, settings.transcode,
                          settings.emby_compat).reencode


def test_other_containers_still_try_the_remux_first(settings):
    info = _info(audio=[("mp3", 2, True)], container="avi")
    p = transcode.plan(info, _refused(info, "ContainerNotSupported"),
                       settings.transcode, settings.emby_compat)
    assert not p.reencode and p.is_remux
