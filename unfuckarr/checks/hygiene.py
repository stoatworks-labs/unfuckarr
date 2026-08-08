"""Stream hygiene.

Nothing here means the file is broken; it means Emby will behave oddly — the
wrong audio track selected, subtitles that cannot be turned off, a language
picker full of "Unknown". Findings are warnings, and the default policy only
flags them.
"""

from __future__ import annotations

from ..config import HygieneConfig
from ..probe import MediaInfo
from . import CheckResult, Finding

# ffprobe leaves these in place of a real ISO 639 tag.
UNKNOWN_LANGS = {"", "und", "unknown", "none", "zxx"}


def check(info: MediaInfo, cfg: HygieneConfig, result: CheckResult) -> None:
    if not cfg.enabled or info is None:
        return

    audio = info.audio
    if audio and cfg.require_audio_language_tags:
        untagged = [a.index for a in audio if a.language in UNKNOWN_LANGS]
        if untagged:
            result.add(Finding(
                "hygiene", "audio_missing_language", "warning",
                f"{len(untagged)} of {len(audio)} audio track(s) have no language tag — "
                "Emby cannot honour a preferred-language setting",
                {"streams": untagged},
            ))

    if len(audio) > 1 and cfg.require_default_audio_track:
        defaults = [a for a in audio if a.is_default]
        if not defaults:
            result.add(Finding(
                "hygiene", "no_default_audio", "warning",
                f"{len(audio)} audio tracks and none flagged default — "
                "clients pick the first track, which is often the commentary",
            ))
        elif len(defaults) > 1:
            result.add(Finding(
                "hygiene", "multiple_default_audio", "warning",
                f"{len(defaults)} audio tracks flagged default",
            ))

    subs = info.subtitles
    if subs:
        if cfg.flag_image_subtitles_only and all(s.is_image_subtitle for s in subs):
            result.add(Finding(
                "hygiene", "image_subtitles_only", "warning",
                "only image-based subtitles (PGS/VOBSUB) — Emby must burn them in, "
                "which forces a video transcode whenever they are enabled",
                {"codecs": sorted({s.codec_name for s in subs})},
            ))
        if cfg.flag_missing_subtitle_language:
            untagged = [s.index for s in subs if s.language in UNKNOWN_LANGS]
            if untagged:
                result.add(Finding(
                    "hygiene", "subtitle_missing_language", "warning",
                    f"{len(untagged)} subtitle track(s) have no language tag",
                    {"streams": untagged},
                ))
        # Every track forced means no ordinary subtitle track exists, and the
        # client will burn in signage the viewer cannot switch off.
        forced = [s for s in subs if s.is_forced]
        if forced and len(forced) == len(subs) > 0:
            result.add(Finding(
                "hygiene", "all_subtitles_forced", "warning",
                "every subtitle track is flagged forced — they cannot be turned off",
            ))

    v = info.video
    if v is not None and v.fps:
        if v.fps < cfg.min_fps or v.fps > cfg.max_fps:
            result.add(Finding(
                "hygiene", "unusual_frame_rate", "warning",
                f"{v.fps:.3f} fps is outside {cfg.min_fps}–{cfg.max_fps} — "
                "client frame-rate matching will judder",
                {"fps": v.fps},
            ))

    if info.duration and info.size and info.duration > 60:
        # Well under a megabit for HD is a re-encode of a re-encode.
        mbps = info.size * 8 / info.duration / 1_000_000
        if v is not None and v.height >= 720 and mbps < 1.0:
            result.add(Finding(
                "hygiene", "very_low_bitrate", "warning",
                f"{mbps:.2f} Mbps at {v.height}p — heavily re-encoded",
                {"mbps": round(mbps, 2)},
            ))
