"""Check-engine behaviour, against real files rendered by ffmpeg."""

from __future__ import annotations

import pytest

from unfuckarr.checks import CheckResult, Finding
from unfuckarr.checks import compat, hygiene, integrity
from unfuckarr.probe import ProbeError, probe
from unfuckarr.scanner import check_file

from .conftest import needs_ffmpeg


# -- status precedence ----------------------------------------------------

def test_status_is_corrupt_when_both_corrupt_and_incompatible():
    """A file that is both must read as corrupt: that is the finding that
    decides what happens to it."""
    r = CheckResult(path="x")
    r.add(Finding("compat", "bad_container", "error", ""))
    r.add(Finding("integrity", "decode_errors", "error", ""))
    assert r.status == "corrupt"


def test_warnings_alone_are_hygiene_not_failure():
    r = CheckResult(path="x")
    r.add(Finding("hygiene", "audio_missing_language", "warning", ""))
    assert r.status == "hygiene"


def test_info_findings_do_not_fail_a_file():
    r = CheckResult(path="x")
    r.add(Finding("emby", "not_in_emby", "info", ""))
    assert r.status == "ok"


# -- integrity ------------------------------------------------------------

@needs_ffmpeg
def test_good_file_passes(video_factory, settings):
    path = video_factory("good.mkv", seconds=12,
                         extra=["-metadata:s:a:0", "language=eng"])
    result, info = check_file(str(path), settings)
    assert result.status == "ok", [f.code for f in result.findings]
    assert info is not None and info.video.codec_name == "h264"


def test_zero_length_file(tmp_path, settings):
    p = tmp_path / "empty.mkv"
    p.write_bytes(b"")
    result, info = integrity.check(str(p), settings.integrity,
                                   settings.ffprobe_path, settings.ffmpeg_path)
    assert [f.code for f in result.findings] == ["zero_length"]
    assert info is None


def test_garbage_file_fails_probe(tmp_path, settings):
    p = tmp_path / "junk.mkv"
    p.write_bytes(b"definitely not a matroska file")
    result, _ = integrity.check(str(p), settings.integrity,
                                settings.ffprobe_path, settings.ffmpeg_path)
    codes = {f.code for f in result.findings}
    assert "probe_failed" in codes
    assert result.status == "corrupt"


@needs_ffmpeg
def test_truncated_file_is_detected(video_factory, settings, tmp_path):
    """Half an MP4 has no moov atom, so ffprobe refuses it outright."""
    src = video_factory("whole.mp4", seconds=20, size="640x360")
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(src.read_bytes()[: len(src.read_bytes()) // 2])
    result, _ = integrity.check(str(cut), settings.integrity,
                                settings.ffprobe_path, settings.ffmpeg_path)
    assert result.status == "corrupt"


@needs_ffmpeg
def test_duration_mismatch_against_arr_runtime(video_factory, settings):
    """The *arr says 90 minutes; the file is 10 seconds. That is truncation
    even though every byte present decodes cleanly."""
    settings.integrity.min_duration_seconds = 5   # so `too_short` does not win first
    path = video_factory("short.mkv", seconds=10)
    result, _ = integrity.check(str(path), settings.integrity,
                                settings.ffprobe_path, settings.ffmpeg_path,
                                expected_runtime=5400)
    codes = {f.code for f in result.errors}
    assert "duration_mismatch" in codes


@needs_ffmpeg
def test_nominal_runtime_gap_is_not_corruption(video_factory, settings):
    """The *arr's runtime is the broadcast slot, not the content length. A
    22 min sitcom against a 25 min slot is the single most common file in a
    TV library — calling it corrupt sends a pristine remux to be deleted and
    re-searched, which is what happened live against 2,924 TV files."""
    settings.integrity.min_duration_seconds = 5
    path = video_factory("nominal.mkv", seconds=20)
    result, _ = integrity.check(str(path), settings.integrity,
                                settings.ffprobe_path, settings.ffmpeg_path,
                                expected_runtime=25)
    assert "duration_mismatch" not in {f.code for f in result.errors}
    assert result.status == "ok", "a normal episode must not read as corrupt"
    codes = {f.code for f in result.findings}
    assert "duration_below_expected" in codes, "but the gap is still reported"


@needs_ffmpeg
def test_file_short_of_half_its_runtime_is_still_truncated(video_factory, settings):
    """The relaxation must not blind the check: below half the expected
    runtime nothing explains the gap but truncation."""
    settings.integrity.min_duration_seconds = 5
    path = video_factory("cut.mkv", seconds=10)
    result, _ = integrity.check(str(path), settings.integrity,
                                settings.ffprobe_path, settings.ffmpeg_path,
                                expected_runtime=25)
    assert "duration_mismatch" in {f.code for f in result.errors}
    assert result.status == "corrupt"


@needs_ffmpeg
def test_longer_than_expected_is_not_a_mismatch(video_factory, settings):
    """An extended cut is longer than the *arr's runtime and is not broken."""
    path = video_factory("long.mkv", seconds=20)
    result, _ = integrity.check(str(path), settings.integrity,
                                settings.ffprobe_path, settings.ffmpeg_path,
                                expected_runtime=10)
    assert "duration_mismatch" not in {f.code for f in result.errors}


@needs_ffmpeg
def test_quick_depth_skips_the_decode_pass(video_factory, settings, monkeypatch):
    settings.integrity.depth = "quick"
    called = []
    monkeypatch.setattr(integrity, "decode_check_full",
                        lambda *a, **k: called.append(1))
    video = video_factory("q.mkv", seconds=8)
    integrity.check(str(video), settings.integrity,
                    settings.ffprobe_path, settings.ffmpeg_path)
    assert not called


def test_repairable_only_for_container_damage():
    container = CheckResult(path="x")
    container.add(Finding("integrity", "decode_errors", "error", ""))
    assert integrity.looks_repairable(container)

    gone = CheckResult(path="x")
    gone.add(Finding("integrity", "zero_length", "error", ""))
    assert not integrity.looks_repairable(gone)

    mixed = CheckResult(path="x")
    mixed.add(Finding("integrity", "decode_errors", "error", ""))
    mixed.add(Finding("integrity", "duration_mismatch", "error", ""))
    assert not integrity.looks_repairable(mixed)


# -- compatibility --------------------------------------------------------

@needs_ffmpeg
def test_mpeg2_avi_is_incompatible(video_factory, settings):
    path = video_factory("legacy.avi", seconds=8, vcodec="mpeg2video",
                         acodec="ac3", extra=["-b:v", "1M"])
    result, _ = check_file(str(path), settings)
    codes = {f.code for f in result.errors}
    assert "bad_container" in codes
    assert "bad_video_codec" in codes
    assert result.status == "incompatible"


@needs_ffmpeg
def test_conservative_profile_rejects_hevc(video_factory, settings):
    settings.emby_compat.target_profile = "conservative"
    path = video_factory("hevc.mkv", seconds=6, vcodec="libx265",
                         extra=["-x265-params", "log-level=none"])
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    compat.check(info, settings.emby_compat, result)
    assert "video_codec_not_in_profile" in {f.code for f in result.errors}


@needs_ffmpeg
def test_modern_profile_accepts_hevc(video_factory, settings):
    path = video_factory("hevc2.mkv", seconds=6, vcodec="libx265",
                         extra=["-x265-params", "log-level=none"])
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    compat.check(info, settings.emby_compat, result)
    assert not result.errors, [f.code for f in result.errors]


@needs_ffmpeg
def test_one_playable_audio_track_is_enough(video_factory, settings, tmp_path):
    """A DTS track alongside an AAC track is fine — Emby picks the AAC one."""
    base = video_factory("base.mkv", seconds=6)
    import subprocess
    out = tmp_path / "dual.mkv"
    subprocess.run([settings.ffmpeg_path, "-v", "error", "-y", "-i", str(base),
                    "-map", "0:v", "-map", "0:a", "-map", "0:a",
                    "-c:v", "copy", "-c:a:0", "copy", "-c:a:1", "dts",
                    "-strict", "-2", str(out)], check=True, capture_output=True)
    info = probe(str(out), settings.ffprobe_path)
    result = CheckResult(path=str(out))
    compat.check(info, settings.emby_compat, result)
    assert "bad_audio_codec" not in {f.code for f in result.errors}


@needs_ffmpeg
def test_faststart_detection(video_factory, tmp_path, settings):
    import subprocess
    src = video_factory("fs_src.mkv", seconds=6)
    slow = tmp_path / "slow.mp4"
    fast = tmp_path / "fast.mp4"
    subprocess.run([settings.ffmpeg_path, "-v", "error", "-y", "-i", str(src),
                    "-c", "copy", str(slow)], check=True, capture_output=True)
    subprocess.run([settings.ffmpeg_path, "-v", "error", "-y", "-i", str(src),
                    "-c", "copy", "-movflags", "+faststart", str(fast)],
                   check=True, capture_output=True)
    from unfuckarr.probe import check_faststart
    assert check_faststart(str(fast)) is True
    assert check_faststart(str(slow)) is False


def test_custom_profile_uses_the_configured_lists(settings):
    settings.emby_compat.target_profile = "custom"
    settings.emby_compat.video_codecs = ["av1"]
    p = compat.resolve(settings.emby_compat)
    assert p.video == frozenset({"av1"})


# -- hygiene --------------------------------------------------------------

@needs_ffmpeg
def test_untagged_audio_is_flagged(video_factory, settings):
    path = video_factory("untagged.mkv", seconds=6)
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    hygiene.check(info, settings.hygiene, result)
    assert "audio_missing_language" in {f.code for f in result.findings}


@needs_ffmpeg
def test_tagged_audio_is_not_flagged(video_factory, settings):
    path = video_factory("tagged.mkv", seconds=6,
                         extra=["-metadata:s:a:0", "language=eng"])
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    hygiene.check(info, settings.hygiene, result)
    assert "audio_missing_language" not in {f.code for f in result.findings}


@needs_ffmpeg
def test_single_audio_track_needs_no_default_flag(video_factory, settings):
    """The no-default warning is only meaningful with more than one track."""
    path = video_factory("one.mkv", seconds=6,
                         extra=["-metadata:s:a:0", "language=eng"])
    info = probe(str(path), settings.ffprobe_path)
    result = CheckResult(path=str(path))
    hygiene.check(info, settings.hygiene, result)
    assert "no_default_audio" not in {f.code for f in result.findings}


def test_disabled_checks_produce_nothing(settings):
    settings.hygiene.enabled = False
    result = CheckResult(path="x")
    hygiene.check(None, settings.hygiene, result)
    assert not result.findings


def test_probe_error_on_missing_binary(tmp_path):
    with pytest.raises(ProbeError):
        probe(tmp_path / "nope.mkv", ffprobe="/definitely/not/ffprobe")
