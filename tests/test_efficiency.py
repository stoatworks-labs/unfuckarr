"""The efficiency check, and the policy routing around it.

The thing being defended here is that "this file is large" never behaves like
"this file is broken". An efficiency finding must not reach a delete, must not
be answered by the hygiene policy, and must not read as a fault anywhere in
the UI.
"""

from __future__ import annotations

import pytest

from unfuckarr.checks import CheckResult, Finding
from unfuckarr.checks import efficiency
from unfuckarr.probe import MediaInfo, Stream
from unfuckarr.remediation import decide, shrink_blocked

MB = 1024 * 1024


def media(*, height=1080, codec="h264", size_mb=4000, duration=7200,
          bit_depth=8, audio_bitrate=640_000, channels=6,
          raw=None) -> MediaInfo:
    video = Stream(index=0, codec_type="video", codec_name=codec,
                   width=height * 16 // 9, height=height, bit_depth=bit_depth,
                   fps=23.976, raw=raw or {})
    audio = Stream(index=1, codec_type="audio", codec_name="eac3",
                   channels=channels, raw={"bit_rate": str(audio_bitrate)})
    return MediaInfo(path="/media/Film/film.mkv", container="mkv",
                     duration=duration, size=size_mb * MB,
                     streams=[video, audio])


def run(info: MediaInfo, settings, **kw) -> CheckResult:
    result = CheckResult(path=info.path)
    efficiency.check(info, settings.efficiency, result, **kw)
    return result


# -- the arithmetic -------------------------------------------------------

def test_audio_is_taken_out_of_the_bitrate():
    """A 30 GB remux with 4 GB of lossless audio is less compressible than its
    overall bitrate suggests, and the audio is stream-copied regardless."""
    info = media(size_mb=4000, duration=7200, audio_bitrate=640_000)
    overall = info.size * 8 / info.duration / 1_000_000
    assert efficiency.video_mbps(info) < overall
    assert efficiency.audio_bytes(info) == pytest.approx(640_000 * 7200 / 8)


def test_audio_without_a_reported_bitrate_is_still_counted():
    """ffprobe reports no per-stream bit_rate for most Matroska audio. Dropping
    those tracks out of the estimate makes every MKV look more compressible
    than it is."""
    info = media()
    info.streams[1].raw = {}
    assert efficiency.audio_bytes(info) > 0


def test_an_unusual_height_lands_on_the_bucket_below_it():
    targets = {"2160": 25.0, "1080": 8.0, "720": 4.0}
    assert efficiency.target_for_height(1600, targets) == 8.0
    assert efficiency.target_for_height(2160, targets) == 25.0
    assert efficiency.target_for_height(240, targets) == 4.0


# -- what is flagged ------------------------------------------------------

def test_a_high_bitrate_remux_is_flagged(settings):
    result = run(media(size_mb=30000, duration=7200), settings)
    assert [f.code for f in result.findings] == ["bitrate_above_target"]
    assert result.status == "oversized"


def test_an_old_codec_is_flagged_below_the_bitrate_target(settings):
    """MPEG-2 at 6 Mbps is under the 1080p target and still worth re-encoding;
    the same 6 Mbps in HEVC is not."""
    old = run(media(codec="mpeg2video", size_mb=5600, duration=7200), settings)
    assert [f.code for f in old.findings] == ["inefficient_codec"]

    modern = run(media(codec="hevc", size_mb=5600, duration=7200), settings)
    assert modern.findings == []


def test_an_already_efficient_codec_is_left_alone(settings):
    """Re-encoding AV1 is a generation of loss for very little."""
    assert run(media(codec="av1", size_mb=30000), settings).findings == []


def test_small_and_short_files_are_not_worth_the_cpu(settings):
    assert run(media(size_mb=100, duration=7200), settings).findings == []
    assert run(media(size_mb=30000, duration=120), settings).findings == []


def test_a_file_already_shrunk_is_never_a_candidate_again(settings):
    """Not just "do not act" — do not keep saying it, either. A permanent
    "oversized" pill on a file nothing will ever touch is noise."""
    assert run(media(size_mb=30000), settings, already_shrunk=True).findings == []


def test_hdr_is_reported_rather_than_silently_skipped(settings):
    """"Why is this 60 GB file not being shrunk" is otherwise unanswerable
    from the UI."""
    info = media(size_mb=60000, raw={"color_transfer": "smpte2084"})
    assert info.is_hdr
    result = run(info, settings)
    assert [f.code for f in result.findings] == ["hdr_not_shrunk"]
    assert all(f.severity == "info" for f in result.findings)
    assert result.status == "oversized"

    settings.efficiency.allow_hdr = True
    assert [f.code for f in run(info, settings).findings] == ["bitrate_above_target"]


def test_hlg_and_dolby_vision_are_hdr_too():
    assert media(raw={"color_transfer": "arib-std-b67"}).is_hdr
    assert media(raw={"side_data_list": [
        {"side_data_type": "Dolby Vision Metadata"}]}).is_hdr
    assert not media(raw={"color_transfer": "bt709"}).is_hdr


def test_the_check_can_be_switched_off(settings):
    settings.efficiency.enabled = False
    assert run(media(size_mb=30000), settings).findings == []


# -- status and policy ----------------------------------------------------

def test_a_real_fault_outranks_being_large(settings):
    """A corrupt file that is also oversized is corrupt: that is the finding
    that decides what happens to it."""
    result = run(media(size_mb=30000), settings)
    result.add(Finding("integrity", "decode_errors", "error", ""))
    assert result.status == "corrupt"


def test_untidy_metadata_outranks_being_large(settings):
    result = run(media(size_mb=30000), settings)
    result.add(Finding("hygiene", "audio_missing_language", "warning", ""))
    assert result.status == "hygiene"


def test_efficiency_findings_are_not_hygiene(settings):
    """`hygiene_action` must never be what decides to re-encode a large file —
    they are different problems with different consequences."""
    settings.policy.oversize_action = "none"
    settings.policy.hygiene_action = "transcode"
    assert decide(run(media(size_mb=30000), settings), settings).action == "none"


def test_the_default_policy_shrinks(settings):
    d = decide(run(media(size_mb=30000), settings), settings)
    assert d.action == "shrink"
    assert d.findings == ["bitrate_above_target"]


def test_oversize_action_can_never_delete():
    """The literal type is the enforcement, exactly as for hygiene_action."""
    from typing import get_args

    from unfuckarr.config import Policy
    assert set(get_args(Policy.model_fields["oversize_action"].annotation)) == \
        {"none", "flag", "shrink"}


def test_flag_only_reports_without_acting(settings):
    settings.policy.oversize_action = "flag"
    assert decide(run(media(size_mb=30000), settings), settings).action == "flag"


def test_shrinking_wins_over_a_hygiene_flag(settings):
    """A shrink rewrites every byte and carries the hygiene fixes with it, so
    letting a flag-only hygiene finding answer first would mask it."""
    result = run(media(size_mb=30000), settings)
    result.add(Finding("hygiene", "audio_missing_language", "warning", ""))
    assert decide(result, settings).action == "shrink"


def test_shrinking_is_blocked_when_the_target_profile_rejects_hevc(settings):
    """Shrinking into a codec the library's own profile rejects trades a size
    win for a file Emby has to transcode on every play."""
    settings.emby_compat.target_profile = "conservative"   # H.264 only
    assert "not in the target Emby profile" in (shrink_blocked(settings) or "")
    assert decide(run(media(size_mb=30000), settings), settings).action == "flag"


def test_shrinking_is_blocked_when_transcoding_is_off(settings):
    settings.transcode.enabled = False
    assert shrink_blocked(settings) is not None
    settings.transcode.enabled = True
    settings.shrink.enabled = False
    assert shrink_blocked(settings) is not None
