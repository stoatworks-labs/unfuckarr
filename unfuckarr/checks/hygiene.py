"""Stream hygiene.

Nothing here means the file is broken; it means Emby will behave oddly — the
wrong audio track selected, subtitles that cannot be turned off, a language
picker full of "Unknown". Findings are warnings, and the default policy only
flags them.
"""

from __future__ import annotations

import os
import threading

from ..config import HygieneConfig
from ..probe import MediaInfo
from . import CheckResult, Finding

# ffprobe leaves these in place of a real ISO 639 tag.
UNKNOWN_LANGS = {"", "und", "unknown", "none", "zxx"}

# Text subtitle files that live beside the media rather than inside it. Emby
# serves these as a separate stream, so they cost nothing — which is the whole
# point: `image_subtitles_only` fires because PGS/VOBSUB must be *burned in*,
# and burning in forces a video transcode. A text sidecar removes that need
# entirely, so the finding is not true any more.
SIDECAR_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi"}

# One `os.listdir` per directory rather than a glob per file: a season folder
# holds ~20 episodes and a scan walks them consecutively, so caching a single
# directory turns 20 listings into 1.
#
# Thread-local, for throughput rather than correctness: a module-global would
# still answer correctly (the `cached[0] != parent` guard rebuilds on a miss,
# and the tuple swap is atomic), but the scanner probes on a pool, so two
# threads in different directories would evict each other on every call and the
# cache would earn nothing. One entry per thread keeps the locality.
#
# Deliberately not an `lru_cache`: a scan runs for hours and Bazarr writes
# sidecars while it does, so an entry that outlived its directory would hide a
# subtitle that had just arrived.
_local = threading.local()


def has_text_sidecar(path: str) -> bool:
    """Whether a text subtitle file sits beside this media file.

    Matches `Film.srt`, `Film.en.srt`, `Film.en.forced.srt` — anything sharing
    the media file's stem and carrying a text subtitle extension.
    """
    parent = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    if not parent or not stem:
        return False
    cached = getattr(_local, "dir_cache", None)
    if cached is None or cached[0] != parent:
        try:
            cached = (parent, set(os.listdir(parent)))
        except OSError:
            cached = (parent, set())
        _local.dir_cache = cached
    for name in cached[1]:
        if not name.startswith(stem + "."):
            continue
        if os.path.splitext(name)[1].lower() in SIDECAR_SUBTITLE_EXTS:
            return True
    return False


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
        if (cfg.flag_image_subtitles_only
                and all(s.is_image_subtitle for s in subs)
                and not has_text_sidecar(info.path)):
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
