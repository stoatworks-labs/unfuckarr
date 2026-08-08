"""ffprobe / ffmpeg inspection.

Everything here shells out; nothing links libav. That keeps the container to a
distro ffmpeg and makes the failure modes legible in the activity log.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ffmpeg writes decode complaints to stderr with these markers. Matching on the
# text is unpleasant but it is the only signal a decode-only pass produces.
DECODE_ERROR_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\berror\b.*\b(decod|while|reading|submitting)",
        r"invalid data found when processing input",
        r"could not find codec parameters",
        r"non-existing (pps|sps)",
        r"\bcorrupt\b",
        r"missing picture in access unit",
        r"co located pocs unavailable",
        r"concealing \d+ dc, \d+ ac",
        r"error splitting the input into nal units",
        r"moov atom not found",
        r"invalid nal unit size",
        r"truncat",
    )
]


class ProbeError(RuntimeError):
    """ffprobe could not read the file at all."""


@dataclass
class Stream:
    index: int
    codec_type: str
    codec_name: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    bit_depth: int = 8
    fps: float = 0.0
    channels: int = 0
    channel_layout: str = ""
    language: str = ""
    title: str = ""
    is_default: bool = False
    is_forced: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_image_subtitle(self) -> bool:
        return self.codec_name in ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub")


@dataclass
class MediaInfo:
    path: str
    container: str = ""
    format_name: str = ""
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    streams: list[Stream] = field(default_factory=list)
    faststart: bool | None = None  # None when the question does not apply
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def video(self) -> Stream | None:
        # The first video stream that is not an embedded cover image.
        for s in self.streams:
            if s.codec_type == "video" and s.codec_name not in ("mjpeg", "png", "bmp", "gif"):
                return s
        return None

    @property
    def audio(self) -> list[Stream]:
        return [s for s in self.streams if s.codec_type == "audio"]

    @property
    def subtitles(self) -> list[Stream]:
        return [s for s in self.streams if s.codec_type == "subtitle"]

    def summary(self) -> dict[str, Any]:
        """Compact form stored in the DB and rendered in the UI."""
        v = self.video
        return {
            "container": self.container,
            "duration": round(self.duration, 2),
            "size": self.size,
            "bitrate": self.bitrate,
            "video": {
                "codec": v.codec_name, "profile": v.profile,
                "width": v.width, "height": v.height,
                "pix_fmt": v.pix_fmt, "bit_depth": v.bit_depth,
                "fps": round(v.fps, 3),
            } if v else None,
            "audio": [
                {"codec": a.codec_name, "channels": a.channels,
                 "language": a.language, "default": a.is_default}
                for a in self.audio
            ],
            "subtitles": [
                {"codec": s.codec_name, "language": s.language,
                 "forced": s.is_forced, "image": s.is_image_subtitle}
                for s in self.subtitles
            ],
            "faststart": self.faststart,
        }


def _to_float(value: Any) -> float:
    """Parse ffprobe rationals ("24000/1001") and plain numbers."""
    if value in (None, "", "N/A", "0/0"):
        return 0.0
    try:
        if isinstance(value, str) and "/" in value:
            num, _, den = value.partition("/")
            d = float(den)
            return float(num) / d if d else 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bit_depth(s: dict[str, Any]) -> int:
    depth = _to_int(s.get("bits_per_raw_sample"))
    if depth:
        return depth
    pix = s.get("pix_fmt", "")
    for n in (16, 14, 12, 10):
        if f"{n}le" in pix or f"{n}be" in pix or f"p{n}" in pix:
            return n
    return 8


def _parse_stream(s: dict[str, Any]) -> Stream:
    tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
    disp = s.get("disposition") or {}
    fps = _to_float(s.get("avg_frame_rate")) or _to_float(s.get("r_frame_rate"))
    return Stream(
        index=_to_int(s.get("index")),
        codec_type=s.get("codec_type", ""),
        codec_name=(s.get("codec_name") or "").lower(),
        profile=s.get("profile") or "",
        width=_to_int(s.get("width")),
        height=_to_int(s.get("height")),
        pix_fmt=s.get("pix_fmt") or "",
        bit_depth=_bit_depth(s),
        fps=fps,
        channels=_to_int(s.get("channels")),
        channel_layout=s.get("channel_layout") or "",
        language=(tags.get("language") or "").lower(),
        title=tags.get("title") or "",
        is_default=bool(disp.get("default")),
        is_forced=bool(disp.get("forced")),
        raw=s,
    )


def probe(path: str | Path, ffprobe: str = "ffprobe", timeout: int = 120) -> MediaInfo:
    """Read container and stream metadata. Raises ProbeError if unreadable."""
    path = str(path)
    cmd = [
        ffprobe, "-v", "error", "-hide_banner",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ProbeError(f"ffprobe not found at {ffprobe!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout}s") from exc

    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProbeError((proc.stderr or "ffprobe produced no output").strip()[:500])

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned unparseable JSON: {exc}") from exc

    fmt = data.get("format") or {}
    streams = [_parse_stream(s) for s in data.get("streams") or []]
    format_name = fmt.get("format_name", "")
    info = MediaInfo(
        path=path,
        format_name=format_name,
        container=_container_from(path, format_name),
        duration=_to_float(fmt.get("duration")),
        size=_to_int(fmt.get("size")),
        bitrate=_to_int(fmt.get("bit_rate")),
        streams=streams,
        raw=data,
    )
    if info.container in ("mp4", "m4v", "mov"):
        info.faststart = check_faststart(path)
    # A duration missing from the container header is normal for MPEG-TS;
    # fall back to the video stream's own duration before calling it unknown.
    if info.duration <= 0:
        for s in streams:
            d = _to_float((s.raw.get("tags") or {}).get("DURATION-eng")) or _to_float(s.raw.get("duration"))
            if d > 0:
                info.duration = d
                break
    return info


def _container_from(path: str, format_name: str) -> str:
    """ffprobe reports format families ("matroska,webm"); pick something usable."""
    ext = Path(path).suffix.lower().lstrip(".")
    parts = [p.strip() for p in format_name.split(",") if p.strip()]
    if ext and ext in parts:
        return ext
    if "matroska" in parts:
        return "mkv"
    if "mov" in parts or "mp4" in parts:
        return ext if ext in ("mp4", "m4v", "mov") else "mp4"
    if "mpegts" in parts:
        return "ts"
    return parts[0] if parts else ext


def check_faststart(path: str, ffprobe: str = "ffprobe") -> bool | None:
    """True when the MP4 moov atom precedes mdat.

    Emby has to buffer the whole file before it can seek otherwise, which the
    user experiences as "it won't play" on a large file.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(2 * 1024 * 1024)
    except OSError:
        return None
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    if moov == -1 and mdat == -1:
        return None          # neither atom in the first 2 MB — cannot say
    if moov == -1:
        return False         # mdat first, moov somewhere far away
    if mdat == -1:
        return True
    return moov < mdat


@dataclass
class DecodeResult:
    errors: int
    messages: list[str]
    timed_out: bool = False
    returncode: int = 0


def decode_check(
    path: str | Path,
    ffmpeg: str = "ffmpeg",
    start: float | None = None,
    duration: float | None = None,
    timeout: int = 3600,
) -> DecodeResult:
    """Decode to null and count the complaints ffmpeg makes.

    ``-xerror`` is deliberately not used: we want the whole error count, not an
    abort on the first one, so a handful of damaged macroblocks can be told
    apart from a file that falls over.
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    if start is not None:
        # Before -i so ffmpeg seeks rather than decoding up to the point.
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0", "-c", "copy", "-f", "null", "-"]
    # -c copy validates demuxing (container/index damage) cheaply; the caller
    # asks for a real decode by passing decode=True below.
    return _run_decode(cmd, timeout)


def decode_check_full(
    path: str | Path,
    ffmpeg: str = "ffmpeg",
    start: float | None = None,
    duration: float | None = None,
    timeout: int = 3600,
) -> DecodeResult:
    """Actually decode the video stream — catches damage a remux would miss."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:v:0?", "-map", "0:a?", "-f", "null", "-"]
    return _run_decode(cmd, timeout)


def _run_decode(cmd: list[str], timeout: int) -> DecodeResult:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ProbeError(f"ffmpeg not found at {cmd[0]!r}") from exc
    except subprocess.TimeoutExpired:
        return DecodeResult(errors=0, messages=["decode timed out"], timed_out=True)

    messages: list[str] = []
    for line in (proc.stderr or "").splitlines():
        line = line.strip()
        if line and any(p.search(line) for p in DECODE_ERROR_PATTERNS):
            messages.append(line)
    # A non-zero exit with no matched line is still a failure; keep the tail so
    # the UI shows something more useful than "it failed".
    if proc.returncode != 0 and not messages:
        tail = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
        messages = tail[-3:] or [f"ffmpeg exited {proc.returncode}"]
    return DecodeResult(
        errors=len(messages),
        messages=messages[:20],
        returncode=proc.returncode,
    )
