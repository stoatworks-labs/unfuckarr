"""Emby direct-play compatibility.

An intact file that Emby has to transcode is a different problem from a broken
one: the server burns CPU on every play, and a weak client just fails. These
checks are a local model of what Emby would decide. When an Emby server is
configured, ``clients/emby.py`` asks the server directly and its answer wins —
this module is the fallback and the pre-filter.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import EmbyCompatConfig
from ..probe import MediaInfo
from . import CheckResult, Finding


@dataclass(frozen=True)
class Profile:
    video: frozenset[str]
    audio: frozenset[str]
    containers: frozenset[str]
    allow_10bit: bool
    max_height: int


PRESETS: dict[str, Profile] = {
    # Anything with a current Emby app: HEVC, 10-bit, AV1 on newer hardware.
    "modern": Profile(
        video=frozenset({"h264", "hevc", "vp9", "av1"}),
        audio=frozenset({"aac", "ac3", "eac3", "mp3", "flac", "opus", "vorbis"}),
        containers=frozenset({"mkv", "mp4", "m4v", "webm"}),
        allow_10bit=True,
        max_height=2160,
    ),
    # Chromecast-class and older smart TVs: H.264 8-bit only, no lossless audio.
    "conservative": Profile(
        video=frozenset({"h264"}),
        audio=frozenset({"aac", "ac3", "mp3"}),
        containers=frozenset({"mp4", "mkv"}),
        allow_10bit=False,
        max_height=1080,
    ),
    # Only reject things nothing plays.
    "permissive": Profile(
        video=frozenset({"h264", "hevc", "vp9", "av1", "mpeg4", "vc1", "mpeg2video", "vp8"}),
        audio=frozenset({"aac", "ac3", "eac3", "mp3", "flac", "opus", "vorbis",
                         "dts", "truehd", "pcm_s16le", "pcm_s24le"}),
        containers=frozenset({"mkv", "mp4", "m4v", "mov", "webm", "ts", "mpegts", "avi"}),
        allow_10bit=True,
        max_height=4320,
    ),
}

# Codecs that are not merely "will transcode" but routinely break playback or
# pin a CPU on every stream. Called out separately so the UI can explain why.
PROBLEM_VIDEO = {
    "mpeg2video": "MPEG-2 — no client decodes it in hardware; Emby transcodes every play",
    "vc1": "VC-1 — very little client support, software transcode only",
    "mpeg4": "MPEG-4 ASP (DivX/Xvid) — transcoded by almost every client",
    "msmpeg4v3": "MS-MPEG4v3 — legacy DivX, always transcoded",
    "wmv3": "WMV3 — effectively unplayable outside Windows clients",
    "rv40": "RealVideo — unsupported",
}
PROBLEM_AUDIO = {
    "truehd": "TrueHD — no direct play without passthrough; forces an audio transcode",
    "dts": "DTS — not supported by browser or most TV clients",
    "dca": "DTS — not supported by browser or most TV clients",
    "mlp": "MLP — unsupported outside AV receivers",
    "pcm_bluray": "Bluray PCM — huge and unsupported by most clients",
}
# Containers Emby can read but cannot stream without a remux.
PROBLEM_CONTAINERS = {
    "avi": "AVI — Emby remuxes on every play and seeking is unreliable",
    "wmv": "WMV/ASF — no direct play",
    "asf": "WMV/ASF — no direct play",
    "flv": "FLV — no direct play",
    "rm": "RealMedia — unsupported",
    "vob": "VOB — MPEG-2 program stream, always transcoded",
    "ogm": "OGM — unsupported",
    "iso": "Disc image — Emby cannot stream this without ripping it first",
    "img": "Disc image — Emby cannot stream this without ripping it first",
}


def resolve(cfg: EmbyCompatConfig) -> Profile:
    if cfg.target_profile == "custom":
        return Profile(
            video=frozenset(c.lower() for c in cfg.video_codecs),
            audio=frozenset(c.lower() for c in cfg.audio_codecs),
            containers=frozenset(c.lower() for c in cfg.containers),
            allow_10bit=cfg.allow_10bit,
            max_height=cfg.max_height,
        )
    return PRESETS[cfg.target_profile]


def check(info: MediaInfo, cfg: EmbyCompatConfig, result: CheckResult) -> None:
    """Append compatibility findings to ``result``."""
    if not cfg.enabled or info is None:
        return
    profile = resolve(cfg)

    container = (info.container or "").lower()
    if container in PROBLEM_CONTAINERS:
        result.add(Finding("compat", "bad_container", "error",
                           PROBLEM_CONTAINERS[container], {"container": container}))
    elif container and container not in profile.containers:
        result.add(Finding(
            "compat", "container_not_in_profile", "error",
            f"container {container} is outside the target profile",
            {"container": container},
        ))

    v = info.video
    if v is not None:
        codec = v.codec_name
        if codec in PROBLEM_VIDEO:
            result.add(Finding("compat", "bad_video_codec", "error",
                               PROBLEM_VIDEO[codec], {"codec": codec}))
        elif codec and codec not in profile.video:
            result.add(Finding(
                "compat", "video_codec_not_in_profile", "error",
                f"video codec {codec} is outside the target profile",
                {"codec": codec},
            ))
        if v.bit_depth > 8 and not profile.allow_10bit:
            result.add(Finding(
                "compat", "high_bit_depth", "error",
                f"{v.bit_depth}-bit video — the target profile is 8-bit only",
                {"bit_depth": v.bit_depth},
            ))
        if v.height and v.height > profile.max_height:
            result.add(Finding(
                "compat", "resolution_too_high", "error",
                f"{v.width}x{v.height} exceeds the profile's {profile.max_height}p ceiling",
                {"height": v.height},
            ))
        # 4:2:2 / 4:4:4 chroma is a hardware-decode miss on every consumer client.
        if v.pix_fmt and not v.pix_fmt.startswith("yuv420") and v.pix_fmt != "yuvj420p":
            result.add(Finding(
                "compat", "unusual_pixel_format", "warning",
                f"pixel format {v.pix_fmt} — no consumer client decodes this in hardware",
                {"pix_fmt": v.pix_fmt},
            ))

    audio = info.audio
    if audio:
        # One playable track is enough: Emby picks it. Only complain when every
        # track needs transcoding.
        playable = [a for a in audio if a.codec_name in profile.audio]
        if not playable:
            worst = audio[0].codec_name
            detail = PROBLEM_AUDIO.get(
                worst, f"no audio track in the target profile (found {worst})")
            result.add(Finding("compat", "bad_audio_codec", "error", detail,
                               {"codecs": [a.codec_name for a in audio]}))
        else:
            problem = [a.codec_name for a in audio if a.codec_name in PROBLEM_AUDIO]
            if problem and len(playable) < len(audio):
                result.add(Finding(
                    "compat", "mixed_audio_support", "info",
                    f"some tracks need transcoding ({', '.join(sorted(set(problem)))}) "
                    f"but {len(playable)} playable track(s) exist",
                    {"problem": problem},
                ))

    if cfg.require_faststart_mp4 and container in ("mp4", "m4v", "mov"):
        if info.faststart is False:
            result.add(Finding(
                "compat", "no_faststart", "error",
                "MP4 moov atom is after mdat — Emby must read the whole file to seek",
            ))
