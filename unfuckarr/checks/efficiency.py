"""Which files are worth measuring for a saving.

Every other check in this package asks whether something is wrong. This one
asks whether a question has been *answered* yet: has this file been measured
to see how small it can be at full perceptual quality?

That distinction is the whole design. An earlier version of this module tried
to decide up front which files were "too big", from their bitrate against a
target for their resolution. That is a guess about how an encoder will behave
on content it has not seen, and it is wrong in both directions — it condemns a
grain-heavy 30 Mbps remux that will not compress at all, and it waves through
a lazily-encoded 6 Mbps 1080p whose picture fits in 2. The quality search
answers the question properly, per file, by measuring. So nothing here
pre-judges the answer; it only decides whether spending the search is
worthwhile, which is a question about *size and running time*, not quality.

What comes out is `not_measured`: an info-severity finding meaning exactly
what it says. Every file ends in one of two terminal states recorded on the
row — shrunk, or measured and left alone — and the finding stops being raised
for it. The backlog therefore drains, and the count of unmeasured files is a
real progress figure rather than a claim about anyone's library.

Nothing here is ever an error, and nothing here can reach a policy that
deletes.
"""

from __future__ import annotations

from ..config import EfficiencyConfig
from ..probe import MediaInfo, _to_int
from . import CheckResult, Finding

MB = 1024 * 1024


def audio_bytes(info: MediaInfo) -> int:
    """What a shrink copies through untouched.

    A shrink re-encodes video and stream-copies everything else, so the audio
    is the floor under the output size. Left out of the projection, a 30 GB
    remux carrying 4 GB of TrueHD looks far more compressible than it is.
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
    """The reference bitrate for a resolution.

    Used for ordering the backlog, not for deciding what goes into it. The
    largest bucket at or below the file's height wins, so an unusual height
    (1600p, a cropped 2.39:1 4K) lands on the bucket below rather than falling
    through to nothing.
    """
    buckets = sorted(((int(k), v) for k, v in targets.items()), reverse=True)
    if not buckets:
        return 0.0
    for h, mbps in buckets:
        if height >= h:
            return mbps
    return buckets[-1][1]


def priority(info: MediaInfo, cfg: EfficiencyConfig) -> float:
    """How far above its reference bitrate this file sits.

    The backlog is worked fattest-first, because the per-scan cap means the
    order decides which savings land this month and which land next year. A
    file below its reference still gets measured — just later.
    """
    v = info.video if info is not None else None
    if v is None:
        return 0.0
    target = target_for_height(v.height, cfg.target_mbps)
    return video_mbps(info) / target if target > 0 else 0.0


def check(info: MediaInfo, cfg: EfficiencyConfig, result: CheckResult,
          already_shrunk: bool = False) -> None:
    if not cfg.enabled or info is None:
        return
    v = info.video
    if v is None or info.duration <= 0 or info.size <= 0:
        return

    # Already shrunk, or already measured and found not worth it. Both are
    # permanent: re-encoding an encode is a second generation of loss for a
    # fraction of the saving, and re-measuring a file the search has already
    # priced costs hours of CPU to reach the same answer.
    if already_shrunk:
        return

    if info.size < cfg.min_size_mb * MB:
        return
    if info.duration < cfg.min_duration_seconds:
        return
    if v.codec_name in cfg.skip_codecs:
        return
    if info.is_disc and not cfg.shrink_disc_images:
        result.add(Finding(
            "efficiency", "disc_not_shrunk", "info",
            f"{v.height}p disc image — readable and measurable, but encoding "
            "one out of a raw stream is not proven yet, so it is left alone. "
            "Enable shrink_disc_images to include it.",
            {"disc": info.disc_kind, "size": info.size},
        ))
        return

    mbps = video_mbps(info)
    target = target_for_height(v.height, cfg.target_mbps)
    data = {
        "mbps": round(mbps, 2), "reference_mbps": target,
        "ratio": round(priority(info, cfg), 2),
        "codec": v.codec_name, "height": v.height,
        "size": info.size, "duration": round(info.duration),
    }

    if info.is_hdr and not cfg.allow_hdr:
        # Worth saying out loud rather than staying silent: "why is this 60 GB
        # file never measured" is otherwise unanswerable from the UI.
        result.add(Finding(
            "efficiency", "hdr_not_shrunk", "info",
            f"{mbps:.1f} Mbps at {v.height}p, but this is HDR — re-encoding it "
            "risks losing the mastering metadata, which produces a washed-out "
            "file that still plays. Enable allow_hdr to include it.",
            {**data, "hdr": True},
        ))
        return

    headline = (f"{v.codec_name} at {mbps:.1f} Mbps, {v.height}p"
                if mbps else f"{v.codec_name}, {v.height}p")
    result.add(Finding(
        "efficiency", "not_measured", "info",
        f"{headline} — not yet measured to see how small it can be at full "
        "perceptual quality. The encode only replaces the file if the result "
        "is both meaningfully smaller and still scores at the quality target.",
        data,
    ))
