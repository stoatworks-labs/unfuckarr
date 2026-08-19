"""Files that are intact, playable, and far bigger than they need to be.

Every other check in this package is looking for something wrong. This one is
not: a 38 Mbps H.264 Blu-ray remux is a perfectly good file, and the only
thing to be said against it is that it is four times the size of an HEVC
encode nobody could tell apart from it.

That difference in kind is why nothing here is ever an ``error`` and why the
category is carried separately all the way through:

* it can never make ``CheckResult.status`` read ``corrupt`` or
  ``incompatible``, so it can never reach a policy that deletes;
* it is excluded from the hygiene warning set, so ``hygiene_action`` cannot
  act on it either;
* and it is excluded from the scan's abort ratio, because "most of this
  library is large" is not what an unmounted array looks like — an unmounted
  array produces integrity failures, and a file that ffprobe cannot read never
  gets this far.

The thresholds are deliberately generous. The target is the remux and the
MPEG-2 rip, not an argument about whether a well-encoded 10 Mbps 1080p could
have been 8.
"""

from __future__ import annotations

from ..config import EfficiencyConfig
from ..probe import MediaInfo, _to_int
from . import CheckResult, Finding

MB = 1024 * 1024


def audio_bytes(info: MediaInfo) -> int:
    """What a shrink copies through untouched.

    A shrink re-encodes video and stream-copies everything else, so the audio
    is the floor under the output size. Left out of the bitrate sum, a 30 GB
    remux with 4 GB of TrueHD looks far more compressible than it is.
    """
    if info.duration <= 0:
        return 0
    total = 0.0
    for a in info.audio:
        bitrate = _to_int(a.raw.get("bit_rate"))
        if not bitrate:
            # ffprobe reports no per-stream bit_rate for most Matroska audio.
            # Assume the usual for the channel count rather than dropping the
            # track out of the estimate altogether.
            bitrate = 640_000 if a.channels > 2 else 192_000
        total += bitrate * info.duration / 8
    return int(total)


def video_mbps(info: MediaInfo) -> float:
    """The video stream's share of the bitrate, in Mbps."""
    if info.duration <= 0 or info.size <= 0:
        return 0.0
    video = max(0, info.size - audio_bytes(info))
    return video * 8 / info.duration / 1_000_000


def target_for_height(height: int, targets: dict[str, float]) -> float:
    """The bitrate ceiling for a resolution.

    The largest bucket at or below the file's height wins, so an unusual
    height (1600p, a cropped 2.39:1 4K) lands on the bucket below it rather
    than falling through to nothing.
    """
    buckets = sorted(((int(k), v) for k, v in targets.items()), reverse=True)
    if not buckets:
        return 0.0
    for h, mbps in buckets:
        if height >= h:
            return mbps
    return buckets[-1][1]


def check(info: MediaInfo, cfg: EfficiencyConfig, result: CheckResult,
          already_shrunk: bool = False) -> None:
    if not cfg.enabled or info is None:
        return
    v = info.video
    if v is None or info.duration <= 0 or info.size <= 0:
        return

    # A file this application has already shrunk is never a candidate again.
    # Re-encoding an encode is a generation of loss for a fraction of the
    # saving, and a check that kept saying "oversized" about a file nothing
    # will ever act on is just noise in the UI.
    if already_shrunk:
        return

    if info.size < cfg.min_size_mb * MB:
        return
    if info.duration < cfg.min_duration_seconds:
        return
    if v.codec_name in cfg.efficient_codecs:
        return

    mbps = video_mbps(info)
    target = target_for_height(v.height, cfg.target_mbps)
    if target <= 0:
        return

    over_bitrate = mbps > target
    old_codec = (v.codec_name in cfg.inefficient_codecs
                 and mbps > target * cfg.codec_bitrate_ratio)
    if not (over_bitrate or old_codec):
        return

    if info.is_hdr and not cfg.allow_hdr:
        # Worth saying out loud rather than staying silent: "why is this 60 GB
        # file not being shrunk" is otherwise unanswerable from the UI.
        result.add(Finding(
            "efficiency", "hdr_not_shrunk", "info",
            f"{mbps:.1f} Mbps at {v.height}p, but this is HDR — re-encoding it "
            "risks losing the metadata, which produces a washed-out file that "
            "still plays. Enable allow_hdr to include it.",
            {"mbps": round(mbps, 2), "target_mbps": target, "hdr": True},
        ))
        return

    data = {
        "mbps": round(mbps, 2), "target_mbps": target,
        "codec": v.codec_name, "height": v.height,
        "size": info.size, "duration": round(info.duration),
    }
    if over_bitrate:
        result.add(Finding(
            "efficiency", "bitrate_above_target", "warning",
            f"{mbps:.1f} Mbps of video at {v.height}p, against a "
            f"{target:g} Mbps target — a measured re-encode should be much "
            "smaller with no visible difference",
            data,
        ))
    else:
        result.add(Finding(
            "efficiency", "inefficient_codec", "warning",
            f"{v.codec_name} at {mbps:.1f} Mbps — an HEVC encode of the same "
            "picture is typically far smaller",
            data,
        ))
