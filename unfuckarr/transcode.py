"""Transcode and remux planning, and the ffmpeg runner.

The plan is built per stream. Re-encoding a stream that already satisfies the
target profile costs hours and loses quality for nothing, so the default is to
copy everything that passes and touch only what fails. A file whose *only*
problem is its container gets a remux, which is I/O-bound rather than
CPU-bound and finishes in seconds.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .checks import CheckResult
from .checks.compat import resolve as resolve_profile
from .config import EmbyCompatConfig, TranscodeConfig
from .probe import MediaInfo

log = logging.getLogger(__name__)

# Encoder names by codec and acceleration backend.
ENCODERS = {
    ("h264", "none"): "libx264",
    ("h264", "qsv"): "h264_qsv",
    ("h264", "nvenc"): "h264_nvenc",
    ("h264", "vaapi"): "h264_vaapi",
    ("h264", "videotoolbox"): "h264_videotoolbox",
    ("hevc", "none"): "libx265",
    ("hevc", "qsv"): "hevc_qsv",
    ("hevc", "nvenc"): "hevc_nvenc",
    ("hevc", "vaapi"): "hevc_vaapi",
    ("hevc", "videotoolbox"): "hevc_videotoolbox",
}

# Names an in-flight output. Everything that enumerates media must skip these:
# a half-written output looks like a media file, carries the same findings as
# its source, and checking or transcoding it races the job that owns it.
TEMP_MARKER = ".unfuckarr."


def is_temp_output(path: str) -> bool:
    return TEMP_MARKER in Path(path).name


# Subtitle codecs each container will accept. Copying a PGS track into MP4
# fails the whole job, so those get dropped instead.
CONTAINER_SUBTITLES = {
    "mkv": {"subrip", "srt", "ass", "ssa", "mov_text", "hdmv_pgs_subtitle",
            "dvd_subtitle", "dvb_subtitle"},
    "mp4": {"mov_text"},
}


@dataclass
class TranscodePlan:
    reason: str
    video_action: str = "copy"      # copy | encode
    audio_action: str = "copy"      # copy | encode
    container: str = "mkv"
    drop_subtitles: list[int] = field(default_factory=list)
    fix_language_tags: bool = False
    set_default_audio: int | None = None
    faststart: bool = False
    is_remux: bool = False

    @property
    def describe(self) -> str:
        bits = []
        if self.video_action == "encode":
            bits.append("re-encode video")
        if self.audio_action == "encode":
            bits.append("re-encode audio")
        if self.is_remux and not bits:
            bits.append("remux")
        if self.drop_subtitles:
            bits.append(f"drop {len(self.drop_subtitles)} subtitle track(s)")
        if self.fix_language_tags:
            bits.append("tag languages")
        if self.set_default_audio is not None:
            bits.append("set default audio")
        if self.faststart:
            bits.append("faststart")
        return ", ".join(bits) or "no change"


def plan(info: MediaInfo, result: CheckResult, tcfg: TranscodeConfig,
         ccfg: EmbyCompatConfig, repair: bool = False) -> TranscodePlan:
    """Decide the cheapest ffmpeg job that clears the findings."""
    profile = resolve_profile(ccfg)
    codes = {f.code for f in result.findings}
    container = tcfg.container

    p = TranscodePlan(reason="repair" if repair else "compatibility",
                      container=container)

    if repair:
        # Container damage: rebuild the container, keep the streams. If that
        # is not enough the verify step catches it and we fall through to a
        # redownload.
        p.is_remux = True
        p.faststart = container == "mp4"
        p.drop_subtitles = _subtitles_to_drop(info, container)
        return p

    v = info.video
    needs_video = bool(codes & {
        "bad_video_codec", "video_codec_not_in_profile", "high_bit_depth",
        "resolution_too_high", "unusual_pixel_format",
    })
    # Emby's own verdict names the stream; honour it even when our local model
    # was happy.
    for f in result.findings:
        if f.code == "no_direct_play":
            reasons = " ".join(f.data.get("reasons", []))
            if re.search(r"Video(Codec|Profile|Level|BitDepth|Resolution|Bitrate|"
                         r"Framerate)|RefFrames|Anamorphic|Interlaced", reasons):
                needs_video = True
            if re.search(r"Audio(Codec|Profile|Channels|SampleRate|BitDepth|Bitrate)",
                         reasons):
                codes.add("bad_audio_codec")
            if "ContainerNotSupported" in reasons:
                p.is_remux = True

    if needs_video and tcfg.video_codec != "copy" and v is not None:
        p.video_action = "encode"
    elif v is not None and tcfg.copy_compatible_streams:
        p.video_action = "copy"

    needs_audio = bool(codes & {"bad_audio_codec", "mixed_audio_support"})
    if needs_audio and tcfg.audio_codec != "copy":
        # Only re-encode when nothing already playable is present; otherwise
        # the existing good track is enough for Emby to pick.
        if not any(a.codec_name in profile.audio for a in info.audio):
            p.audio_action = "encode"

    if codes & {"bad_container", "container_not_in_profile", "no_faststart"}:
        p.is_remux = True
    p.faststart = container == "mp4"
    p.drop_subtitles = _subtitles_to_drop(info, container)

    if "audio_missing_language" in codes or "subtitle_missing_language" in codes:
        p.fix_language_tags = True
    if "no_default_audio" in codes and info.audio:
        # Pick the track most likely to be the main one: highest channel count,
        # tie-broken by stream order.
        best = max(info.audio, key=lambda a: (a.channels, -a.index))
        p.set_default_audio = info.audio.index(best)

    if p.video_action == "copy" and p.audio_action == "copy":
        p.is_remux = True
    return p


def _subtitles_to_drop(info: MediaInfo, container: str) -> list[int]:
    allowed = CONTAINER_SUBTITLES.get(container, set())
    return [i for i, s in enumerate(info.subtitles) if s.codec_name not in allowed]


def build_command(src: str, dst: str, info: MediaInfo, p: TranscodePlan,
                  tcfg: TranscodeConfig, ffmpeg: str = "ffmpeg",
                  default_language: str = "eng") -> list[str]:
    """Assemble the ffmpeg invocation for a plan."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error",
           "-progress", "pipe:1", "-stats_period", "2"]

    hw = tcfg.hwaccel
    if p.video_action == "encode" and hw == "vaapi":
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", tcfg.vaapi_device,
                "-hwaccel_output_format", "vaapi"]
    elif p.video_action == "encode" and hw == "qsv":
        cmd += ["-hwaccel", "qsv"]

    # Rebuild timestamps: a truncated or badly muxed source often has broken
    # ones, and they are the usual cause of a remux that plays but will not seek.
    cmd += ["-fflags", "+genpts+igndts", "-i", src]

    cmd += ["-map", "0:v:0?", "-map", "0:a?"]
    if tcfg.keep_subtitles:
        for i, _ in enumerate(info.subtitles):
            if i not in p.drop_subtitles:
                cmd += ["-map", f"0:s:{i}?"]
    # Keep chapters and attachments (fonts for ASS subtitles).
    if p.container == "mkv":
        cmd += ["-map", "0:t?", "-map_chapters", "0"]

    if p.video_action == "encode":
        encoder = ENCODERS.get((tcfg.video_codec, hw), "libx264")
        cmd += ["-c:v", encoder]
        if hw == "vaapi":
            cmd += ["-vf", "scale_vaapi=format=nv12", "-qp", str(tcfg.crf)]
        elif hw == "nvenc":
            cmd += ["-preset", "p5", "-cq", str(tcfg.crf), "-pix_fmt", "yuv420p"]
        elif hw == "qsv":
            cmd += ["-global_quality", str(tcfg.crf), "-preset", tcfg.preset]
        elif hw == "videotoolbox":
            cmd += ["-q:v", str(max(1, min(100, 100 - tcfg.crf * 2)))]
        else:
            cmd += ["-preset", tcfg.preset, "-crf", str(tcfg.crf),
                    "-pix_fmt", "yuv420p"]
        if tcfg.video_codec == "hevc" and p.container == "mp4":
            # Without this tag Apple clients refuse HEVC in MP4.
            cmd += ["-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "copy"]

    if p.audio_action == "encode":
        cmd += ["-c:a", tcfg.audio_codec, "-b:a", tcfg.audio_bitrate]
        # 7.1 downmixed by ffmpeg's default matrix is quiet and muddy; cap at
        # 5.1 and let the client do the rest.
        if any(a.channels > 6 for a in info.audio):
            cmd += ["-ac", "6"]
    else:
        cmd += ["-c:a", "copy"]

    if tcfg.keep_subtitles and info.subtitles:
        cmd += ["-c:s", "copy" if p.container == "mkv" else "mov_text"]

    if p.fix_language_tags:
        kept_audio = list(enumerate(info.audio))
        for out_i, a in kept_audio:
            if not a.language or a.language in ("und", "unknown"):
                cmd += [f"-metadata:s:a:{out_i}", f"language={default_language}"]
        out_i = 0
        for i, s in enumerate(info.subtitles):
            if i in p.drop_subtitles:
                continue
            if not s.language or s.language in ("und", "unknown"):
                cmd += [f"-metadata:s:s:{out_i}", f"language={default_language}"]
            out_i += 1

    if p.set_default_audio is not None:
        for i in range(len(info.audio)):
            flag = "default" if i == p.set_default_audio else "0"
            cmd += [f"-disposition:a:{i}", flag]

    if p.faststart and p.container == "mp4":
        cmd += ["-movflags", "+faststart"]

    cmd += ["-max_muxing_queue_size", "4096", dst]
    return cmd


_TIME_RE = re.compile(r"out_time_ms=(\d+)")


@dataclass
class TranscodeOutcome:
    ok: bool
    output: str | None
    message: str
    duration: float
    command: list[str] = field(default_factory=list)


def run(cmd: list[str], total_duration: float,
        on_progress: Callable[[float, float | None], None] | None = None,
        stall_timeout: int = 900,
        nice_level: int = 10,
        cancel: threading.Event | None = None) -> tuple[bool, str]:
    """Run ffmpeg, reporting progress and killing a stalled job.

    ffmpeg's ``-progress pipe:1`` emits key=value blocks; ``out_time_ms`` is
    the only field we need. A job that emits nothing for ``stall_timeout``
    seconds is killed — a hung read on a dead NFS mount otherwise sits there
    for ever holding the worker.
    """
    started = time.time()
    preexec = None
    if nice_level and hasattr(os, "nice"):
        def preexec() -> None:  # pragma: no cover - child process only
            os.nice(nice_level)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, preexec_fn=preexec,
    )
    last_output = time.time()
    stderr_tail: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if line:
                stderr_tail.append(line)
                del stderr_tail[:-30]

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()

    killed_for = ""
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            if cancel is not None and cancel.is_set():
                killed_for = "cancelled"
                proc.kill()
                break
            if time.time() - last_output > stall_timeout:
                killed_for = f"no progress for {stall_timeout}s"
                proc.kill()
                break
            time.sleep(0.2)
            continue
        last_output = time.time()
        m = _TIME_RE.search(line)
        if m and on_progress and total_duration > 0:
            done = int(m.group(1)) / 1_000_000
            frac = max(0.0, min(1.0, done / total_duration))
            elapsed = time.time() - started
            eta = (elapsed / frac - elapsed) if frac > 0.01 else None
            on_progress(frac, eta)
        if cancel is not None and cancel.is_set():
            killed_for = "cancelled"
            proc.kill()
            break

    proc.wait()
    err_thread.join(timeout=2)
    if killed_for:
        return False, killed_for
    if proc.returncode != 0:
        return False, "; ".join(stderr_tail[-3:]) or f"ffmpeg exited {proc.returncode}"
    return True, f"completed in {time.time() - started:.0f}s"


def output_path(src: str, container: str, work_dir: str | None = None) -> str:
    """Where the new file is written.

    Alongside the source by default so the rename at the end is atomic; a
    separate work dir is available for people who want the churn off the array.
    """
    p = Path(src)
    if work_dir:
        d = Path(work_dir)
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"{p.stem}{TEMP_MARKER}{container}")
    return str(p.with_name(f"{p.stem}{TEMP_MARKER}{container}"))


def replace(src: str, new: str) -> str:
    """Swap the transcode in for the original.

    Returns the final path, which differs from ``src`` when the container
    changed. The original is expected to have been recycled by the caller
    already; this only moves the new file into place.
    """
    final = str(Path(src).with_suffix(Path(new).suffix))
    if os.path.abspath(new) == os.path.abspath(final):
        return final
    # os.replace is atomic within a filesystem and fails across one, which is
    # the case when a work dir is on a different mount.
    try:
        os.replace(new, final)
    except OSError:
        shutil.move(new, final)
    return final


def free_space_ok(path: str, needed: int, margin: float = 1.2) -> bool:
    """A transcode that fills the array is worse than a broken file."""
    try:
        usage = shutil.disk_usage(os.path.dirname(path) or ".")
    except OSError:
        return True
    return usage.free > needed * margin
