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
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Callable

from . import governor as governor_mod
from .checks import CheckResult
from .checks.compat import default_audio, resolve as resolve_profile
from .config import EmbyCompatConfig, TranscodeConfig
from .probe import MediaInfo, Stream

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
    """Whether this path is one of ours, in flight.

    Every component is checked, not just the name: a disc conversion rips into
    a *directory* named with the marker, and the files MakeMKV writes inside it
    are named by MakeMKV, so the only thing marking them as ours is the folder
    they are in. Passing a bare filename still works — it is its own only part.
    """
    return any(TEMP_MARKER in part for part in Path(path).parts)


# Directories holding bonus features rather than library items. Emby indexes
# these as extras of the item in the parent folder, and unfuckarr must not
# treat their contents as media of their own: a deleted scene is short, often
# interlaced, frequently has no audio track worth the name, and would be found
# corrupt or incompatible and then *repaired* — or, worse, redownloaded, which
# on a file inheriting its parent's *arr identity would delete the film.
#
# The list is deliberately short, and what is *missing* from it is the point.
# Emby also reads `scenes`, `shorts`, `samples` and `other` as extras folders,
# but those are equally plausible as somebody's category folder —
# `/media/movies/Shorts/A Film (2011)/` is a library, not a bonus feature — and
# skipping one would hide part of a library with nothing said about it. The
# same goes for `specials`, which is Sonarr's season-zero folder. A name only
# earns a place here if it cannot reasonably mean anything else.
EXTRAS_DIRS = {
    "extras", "featurettes", "behind the scenes", "behind-the-scenes",
    "deleted scenes", "deleted-scenes", "interviews", "trailers",
}


def is_extras_path(path: str) -> bool:
    """Whether this file sits under an extras directory at any depth."""
    return any(part.lower() in EXTRAS_DIRS for part in Path(path).parts[:-1])


def abandoned_outputs(media_paths) -> list[str]:
    """Every leftover of ours sitting beside a known media file.

    A transcode killed part-way — a container restart, an Unraid reboot, an
    OOM — leaves its half-written output behind for ever. `is_temp_output`
    makes the scanner and the watcher ignore it, which stops the file being
    acted on and also stops anyone noticing it: live, four of these had
    accumulated to **199 GB** inside the library, and Emby had indexed each one
    as a separate film and downloaded artwork for it (507 files of that too).

    The artwork is matched as well as the video, because the marker string
    only ever appears in names we caused. Sidecars are named
    ``X.unfuckarr-banner.jpg`` — no trailing dot — so this matches the marker
    without it.
    """
    seen: set[str] = set()
    found: list[str] = []
    for path in media_paths:
        parent = os.path.dirname(path)
        if not parent or parent in seen:
            continue
        seen.add(parent)
        try:
            entries = os.listdir(parent)
        except OSError:
            continue
        for name in entries:
            if TEMP_MARKER.rstrip(".") in name:
                found.append(os.path.join(parent, name))
    return found


# Subtitle codecs each container will accept. Copying a PGS track into MP4
# fails the whole job, so those get dropped instead.
CONTAINER_SUBTITLES = {
    "mkv": {"subrip", "srt", "ass", "ssa", "mov_text", "hdmv_pgs_subtitle",
            "dvd_subtitle", "dvb_subtitle"},
    "mp4": {"mov_text"},
}


@dataclass
class AddedAudio:
    """A playable copy of one source audio track, written as the new default."""
    source: int        # index into `MediaInfo.audio`
    codec: str
    channels: int
    bitrate: str

    @property
    def layout(self) -> str:
        return {1: "mono", 2: "stereo", 6: "5.1"}.get(self.channels,
                                                      f"{self.channels}ch")

    @property
    def describe(self) -> str:
        return f"add {self.codec} {self.layout} as the default audio"


# Bitrates for an added default track, as (surround, stereo). Not
# `TranscodeConfig.audio_bitrate`: that is sized for re-encoding a whole file's
# audio to AAC stereo, and 192k across six channels is what a soundtrack
# sounds like over a bad phone line. 640k is AC-3's ceiling and what a
# Blu-ray's own AC-3 core uses.
ADDED_AUDIO_BITRATE = {
    "ac3": ("640k", "224k"),
    "eac3": ("640k", "224k"),
    "aac": ("384k", "192k"),
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
    # Mapped ahead of every source track, so it is output audio 0, and it
    # takes the default flag from all of them. `set_default_audio` is ignored
    # when this is set.
    add_audio: AddedAudio | None = None
    clear_forced_subtitles: bool = False
    faststart: bool = False
    is_remux: bool = False
    # Set by the shrink path: the codec and CRF the quality search settled on,
    # which are deliberately not the global transcode defaults.
    codec: str | None = None
    crf: int | None = None
    pix_fmt: str = "yuv420p"
    is_shrink: bool = False

    @property
    def describe(self) -> str:
        bits = []
        if self.is_shrink:
            bits.append(f"shrink to {self.codec or 'hevc'} "
                        f"at CRF {self.crf}")
        elif self.video_action == "encode":
            bits.append("re-encode video")
        if self.audio_action == "encode":
            bits.append("re-encode audio")
        if self.add_audio is not None:
            bits.append(self.add_audio.describe)
        if self.is_remux and not bits:
            bits.append("remux")
        if self.drop_subtitles:
            bits.append(f"drop {len(self.drop_subtitles)} subtitle track(s)")
        if self.fix_language_tags:
            bits.append("tag languages")
        if self.set_default_audio is not None and self.add_audio is None:
            bits.append("set default audio")
        if self.clear_forced_subtitles:
            bits.append("clear forced subtitles")
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

    if codes & {"bad_container", "container_not_in_profile", "no_faststart"}:
        p.is_remux = True
    p.faststart = container == "mp4"
    p.drop_subtitles = _subtitles_to_drop(info, container)

    apply_hygiene_fixes(p, info, codes)

    # After the hygiene fixes, because `no_default_audio` and
    # `multiple_default_audio` choose by channel count — and the track with
    # the most channels is very often the DTS this is here to step around.
    # `bad_audio_codec` counts only when nothing is playable: Emby's reasons
    # can add it to a file that has a playable track, and there the default
    # track is the question, which the compat check asks on its own.
    if info.audio and (
            "default_audio_unplayable" in codes
            or ("bad_audio_codec" in codes
                and not any(a.codec_name in profile.audio for a in info.audio))):
        _fix_default_audio(p, info, profile, tcfg)

    if p.video_action == "copy" and p.audio_action == "copy":
        p.is_remux = True
    return p


def _fix_default_audio(p: TranscodePlan, info: MediaInfo, profile: Any,
                       tcfg: TranscodeConfig) -> None:
    """Give the file a default audio track the target profile can play.

    A playable track that can stand in for the current default is promoted,
    which is a remux measured in seconds and changes no audio. "Stand in"
    means the same language, not a commentary or a description, and at
    least as many channels as the default — capped at 5.1, because that is
    all a converted track could offer either, so a Blu-ray's AC-3 5.1 core
    is as good a default as anything made from its TrueHD 7.1.

    Without one, a converted copy of the default goes in front and becomes
    the default, and the original stays as it was for anything that can
    pass it through. Promoting the stereo track instead would quietly turn
    a 5.1 film into a stereo one for every viewer. That includes a file with
    no playable track at all: it used to have every track re-encoded to the
    configured codec — AAC at 192k across six channels, with the DTS thrown
    away — and Allan chose to keep the originals there too (2026-10-12).

    Only Matroska carries the originals reliably; DTS and TrueHD into MP4
    fail the mux. For any other container every track is re-encoded to the
    configured codec, which was the only behaviour before this existed.
    """
    lead = default_audio(info.audio)
    want = min(lead.channels or 2, 6)
    stand_ins = [a for a in info.audio
                 if a.codec_name in profile.audio and a.channels >= want
                 and _language(a) == _language(lead) and not _is_secondary(a)]
    if stand_ins:
        best = max(stand_ins, key=lambda a: (a.channels, -a.index))
        p.set_default_audio = info.audio.index(best)
        return
    if tcfg.audio_codec == "copy":
        return
    codec = next((c for c in ADDED_AUDIO_BITRATE if c in profile.audio), None)
    if codec is None or p.container != "mkv":
        p.audio_action = "encode"
        return
    surround, stereo = ADDED_AUDIO_BITRATE[codec]
    p.add_audio = AddedAudio(source=info.audio.index(lead), codec=codec,
                             channels=want,
                             bitrate=surround if want > 2 else stereo)
    p.set_default_audio = None


def _language(stream: Stream) -> str:
    lang = (stream.language or "").lower()
    return "" if lang in ("und", "unknown") else lang


def _is_secondary(stream: Stream) -> bool:
    """A commentary or an audio description — never anybody's default."""
    title = (stream.title or "").lower()
    disposition = stream.raw.get("disposition") or {}
    return ("comment" in title or "descri" in title
            or bool(disposition.get("comment"))
            or bool(disposition.get("visual_impaired")))


def changes_nothing(p: TranscodePlan, info: MediaInfo) -> bool:
    """Whether running this plan would write the source back out unchanged.

    Every stream copied, into the container it is already in, with no track
    dropped, added or re-flagged. `decide` is meant never to send such a file
    here (invariant 22), and `_transcode` refuses one anyway, because the two
    have drifted apart before: `plan_has_work` called `mixed_audio_support`
    work that `plan` never did, and live that rewrote ~15 TB in five days —
    Blade II 162 times — each copy reported as a fix, because the re-check
    after a transcode does not ask Emby, and each one picked up again by the
    watch folder a minute later because the swap looks like an arrival.
    """
    return (p.video_action == "copy" and p.audio_action == "copy"
            and p.add_audio is None and not p.drop_subtitles
            and not p.fix_language_tags and p.set_default_audio is None
            and not p.clear_forced_subtitles and not p.faststart
            and not p.is_shrink
            and (info.container or "").lower() == p.container)


# The hygiene findings a transcode can actually clear. Everything else the
# hygiene check raises describes the *source* — image-only subtitles, a bitrate
# that says the file is a re-encode of a re-encode, a frame rate no rewrite can
# change — and asking for a remux to fix one produces a byte-for-byte copy of
# the file, a recycled original, and the identical finding on the far side.
# Live proof (2026-08-26): 269 files sat at `fix_attempts` 2 having each been
# remuxed twice for nothing, 240 of them for `image_subtitles_only` alone, and
# 2,733 of the 3,749 hygiene files were queued to join them. `decide` consults
# this so a warning with no fix behind it is flagged instead of rewritten.
HYGIENE_FIXABLE = frozenset({
    "audio_missing_language",
    "subtitle_missing_language",
    "no_default_audio",
    "multiple_default_audio",
    "all_subtitles_forced",
})

# The compat codes `plan` consumes, in the order it reads them: the video set,
# the audio set, and the container set. Kept beside the hygiene set because
# `decide` has to answer the same question about both — is there any work here
# — and it cannot answer it by building a plan, being a pure function of the
# result and the policy with no `MediaInfo` to hand.
#
# `mixed_audio_support` is deliberately absent. It was listed here, and `plan`
# only ever re-encoded audio when *no* track was playable — which that finding
# rules out by definition — so every file carrying it was sent for a stream
# copy that changed nothing. `default_audio_unplayable` is the half of it that
# is a real fault, and `_fix_default_audio` is its fix.
COMPAT_ACTIONABLE = frozenset({
    "bad_video_codec", "video_codec_not_in_profile", "high_bit_depth",
    "resolution_too_high", "unusual_pixel_format",
    "bad_audio_codec", "default_audio_unplayable",
    "bad_container", "container_not_in_profile", "no_faststart",
})


def plan_has_work(findings: Iterable[Any]) -> bool:
    """Whether a plan built from these findings would do anything.

    `plan` reads `{f.code for f in result.findings}` — *every* finding, not
    just the errors — so this has to as well. Getting that wrong flags a file
    the transcoder could genuinely have fixed, such as one whose only
    actionable code is the *warning* `unusual_pixel_format`.
    """
    for f in findings:
        code = getattr(f, "code", None)
        if code in COMPAT_ACTIONABLE or code in HYGIENE_FIXABLE:
            return True
        # Emby naming the offending stream is work in itself: `plan` turns
        # those reasons into a video encode or an audio re-encode.
        if code == "no_direct_play" and (getattr(f, "data", None) or {}).get("reasons"):
            return True
    return False


def apply_hygiene_fixes(p: TranscodePlan, info: MediaInfo,
                        codes: set[str]) -> TranscodePlan:
    """Set the stream-metadata half of a plan from the hygiene findings.

    Shared with the shrink path, which rides these fixes along on an encode it
    is doing anyway rather than leaving them for a second full pass — so the
    two must not be able to drift apart. Every code touched here is in
    `HYGIENE_FIXABLE`, and that is the contract `decide` relies on.
    """
    if codes & {"audio_missing_language", "subtitle_missing_language"}:
        p.fix_language_tags = True

    if info.audio:
        if "no_default_audio" in codes:
            # Pick the track most likely to be the main one: highest channel
            # count, tie-broken by stream order.
            best = max(info.audio, key=lambda a: (a.channels, -a.index))
            p.set_default_audio = info.audio.index(best)
        elif "multiple_default_audio" in codes:
            # Several tracks claim to be default, so the client picks one at
            # random — often the commentary. Choose between the tracks that
            # already carry the flag rather than promoting one that does not:
            # the file is telling us which tracks were meant to be playable,
            # and only the ambiguity needs settling.
            defaults = [a for a in info.audio if a.is_default]
            if defaults:
                best = max(defaults, key=lambda a: (a.channels, -a.index))
                p.set_default_audio = info.audio.index(best)

    if "all_subtitles_forced" in codes and info.subtitles:
        # When every track is forced the flag has stopped meaning anything —
        # it cannot be distinguishing signage from dialogue if there is nothing
        # to distinguish it from — and the viewer cannot switch any of them
        # off. Clearing it costs nothing and gives the choice back.
        p.clear_forced_subtitles = True
    return p


def _subtitles_to_drop(info: MediaInfo, container: str) -> list[int]:
    allowed = CONTAINER_SUBTITLES.get(container, set())
    return [i for i, s in enumerate(info.subtitles) if s.codec_name not in allowed]


def video_encode_args(codec: str, hw: str, crf: int, preset: str,
                     container: str, pix_fmt: str = "yuv420p") -> list[str]:
    """The encoder half of an ffmpeg command, on its own.

    Split out because the quality search in ``quality.py`` has to encode short
    samples with *exactly* the settings the real job will use. A sample encoded
    by a different encoder, or at a quality scale that means something else,
    produces a CRF that does not transfer — and the whole point of the search
    is that the number it returns is the number the full encode uses.
    """
    args = ["-c:v", ENCODERS.get((codec, hw), "libx264")]
    if hw == "vaapi":
        # format before hwupload: the encoder wants an nv12/p010 surface, and
        # doing the conversion on the CPU side keeps this working for 10-bit
        # and odd-pixel-format sources too. -qp, not -crf: VAAPI has no CRF.
        surface = "p010" if "10" in pix_fmt else "nv12"
        args += ["-vf", f"format={surface},hwupload", "-qp", str(crf)]
        if surface == "p010" and codec == "hevc":
            args += ["-profile:v", "main10"]
        # Never re-emit A/53 closed captions. ffmpeg lifts the caption SEI off
        # every source frame as side data and the VAAPI encoders pass it back
        # to the driver as a packed SEI header per frame; Mesa's radeonsi
        # parser (vlVaHandleVAEncPackedHeaderDataBufferTypeHEVC, 25.3.3) then
        # spins ~0.7 s of CPU on each one. Measured on the Radeon 880M: a
        # captioned 1080p WEB-DL encodes at 0.7 fps with the default
        # `-sei hdr+a53_cc` and at 19x realtime with the captions dropped;
        # the same command on a caption-free source runs at 15x either way.
        # The captions are the only loss, and every source this has bitten
        # carried a text subtitle track beside them.
        args += ["-sei", "hdr" if codec == "hevc" else "-a53_cc"]
    elif hw == "nvenc":
        args += ["-preset", "p5", "-cq", str(crf), "-pix_fmt", pix_fmt]
    elif hw == "qsv":
        args += ["-global_quality", str(crf), "-preset", preset]
    elif hw == "videotoolbox":
        args += ["-q:v", str(max(1, min(100, 100 - crf * 2)))]
    else:
        args += ["-preset", preset, "-crf", str(crf), "-pix_fmt", pix_fmt]
    if codec == "hevc" and container == "mp4":
        # Without this tag Apple clients refuse HEVC in MP4.
        args += ["-tag:v", "hvc1"]
    return args


def build_command(src: str, dst: str, info: MediaInfo, p: TranscodePlan,
                  tcfg: TranscodeConfig, ffmpeg: str = "ffmpeg",
                  default_language: str = "eng") -> list[str]:
    """Assemble the ffmpeg invocation for a plan."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error",
           "-progress", "pipe:1", "-stats_period", "2"]

    hw = tcfg.hwaccel
    if p.video_action == "encode" and hw == "vaapi":
        # Decode in software and upload, rather than -hwaccel vaapi
        # -hwaccel_output_format vaapi. Hardware *decode* is per-codec and the
        # sources this tool exists to fix are exactly the ones GPUs no longer
        # decode — no recent AMD part has an MPEG-2 decoder at all. When the
        # decoder falls back to software the frames are not VA surfaces, and
        # scale_vaapi dies mid-stream with "Failed to inject frame into filter
        # network: Function not implemented" rather than at startup. The encode
        # is what needs the GPU; decoding an SD MPEG-2 in software is cheap.
        cmd += ["-vaapi_device", tcfg.vaapi_device]
    elif p.video_action == "encode" and hw == "qsv":
        cmd += ["-hwaccel", "qsv"]

    # Rebuild timestamps: a truncated or badly muxed source often has broken
    # ones, and they are the usual cause of a remux that plays but will not seek.
    # A disc image has to be opened through its protocol; ffmpeg cannot read
    # one by filename. Everything downstream (mapping, encoding) is identical.
    cmd += ["-fflags", "+genpts+igndts", "-i",
            info.source if info.is_disc else src]

    cmd += ["-map", "0:v:0?"]
    # An added track is mapped first, so it is output audio 0 and every
    # source track sits one place later than it did.
    added = p.add_audio
    shift = 1 if added is not None else 0
    if added is not None:
        cmd += ["-map", f"0:a:{added.source}"]
    cmd += ["-map", "0:a?"]
    if tcfg.keep_subtitles:
        for i, _ in enumerate(info.subtitles):
            if i not in p.drop_subtitles:
                cmd += ["-map", f"0:s:{i}?"]
    # Keep chapters and attachments (fonts for ASS subtitles).
    if p.container == "mkv":
        cmd += ["-map", "0:t?", "-map_chapters", "0"]

    if p.video_action == "encode":
        cmd += video_encode_args(
            p.codec or tcfg.video_codec, hw,
            tcfg.crf if p.crf is None else p.crf,
            tcfg.preset, p.container, pix_fmt=p.pix_fmt)
        cmd += colour_args(info)
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
        if added is not None:
            # Stream-specific, and after the blanket copy, so it wins for
            # output audio 0 and leaves the originals copied.
            cmd += ["-c:a:0", added.codec, "-b:a:0", added.bitrate,
                    "-ac:a:0", str(added.channels)]
            lang = _language(info.audio[added.source]) or default_language
            cmd += ["-metadata:s:a:0", f"language={lang}",
                    "-metadata:s:a:0",
                    f"title={added.codec.upper()} {added.layout}"]

    if tcfg.keep_subtitles and info.subtitles:
        cmd += ["-c:s", "copy" if p.container == "mkv" else "mov_text"]

    if p.fix_language_tags:
        for i, a in enumerate(info.audio):
            if not a.language or a.language in ("und", "unknown"):
                cmd += [f"-metadata:s:a:{i + shift}", f"language={default_language}"]
        out_i = 0
        for i, s in enumerate(info.subtitles):
            if i in p.drop_subtitles:
                continue
            if not s.language or s.language in ("und", "unknown"):
                cmd += [f"-metadata:s:s:{out_i}", f"language={default_language}"]
            out_i += 1

    if added is not None:
        cmd += ["-disposition:a:0", "default"]
        for i in range(len(info.audio)):
            cmd += [f"-disposition:a:{i + 1}", "0"]
    elif p.set_default_audio is not None:
        for i in range(len(info.audio)):
            flag = "default" if i == p.set_default_audio else "0"
            cmd += [f"-disposition:a:{i}", flag]

    if p.clear_forced_subtitles:
        # Output-stream indices, so the dropped tracks are skipped the same way
        # the language tagging above skips them.
        out_i = 0
        for i in range(len(info.subtitles)):
            if i in p.drop_subtitles:
                continue
            cmd += [f"-disposition:s:{out_i}", "0"]
            out_i += 1

    if p.faststart and p.container == "mp4":
        cmd += ["-movflags", "+faststart"]

    cmd += ["-max_muxing_queue_size", "4096", dst]
    return cmd


def colour_args(info: MediaInfo) -> list[str]:
    """Carry the source's colour description onto the encode.

    ffmpeg does not copy these tags across a re-encode. Losing them on an SDR
    file is invisible; losing them on HDR gives a file that still plays and
    looks grey and washed out, which is the worst kind of failure because
    nothing reports it — hence `EfficiencyConfig.allow_hdr` defaults off.

    This carries the transfer function, primaries and matrix, which is what
    decides whether a player treats the file as HDR at all. It does **not**
    carry mastering-display or MaxCLL side data; that needs per-encoder
    plumbing (`-x265-params master-display=...`) and there is no HDR source
    here to verify it against. So enabling `allow_hdr` is better than it was
    and still not proven — see AGENTS.md.
    """
    v = info.video
    if v is None:
        return []
    args: list[str] = []
    for flag, key in (("-color_primaries", "color_primaries"),
                      ("-color_trc", "color_transfer"),
                      ("-colorspace", "color_space"),
                      ("-color_range", "color_range")):
        value = v.raw.get(key)
        if value and value not in ("unknown", "und"):
            args += [flag, str(value)]
    return args


_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_POS_RE = re.compile(r"^pos:\s*(\d+)", re.MULTILINE)

# Where the kernel describes a process's open files. A parameter so a test
# can stand up a fake tree; the real one only exists on Linux.
PROC_ROOT = "/proc"


def source_of(cmd: list[str]) -> str | None:
    """The input path of an ffmpeg command: the argument after its last
    ``-i``. A disc image's input is a ``bluray:`` URL rather than a path, so
    callers that know the real file should pass it instead."""
    src = None
    for i, arg in enumerate(cmd[:-1]):
        if arg == "-i":
            src = cmd[i + 1]
    return src


class SourcePosition:
    """How far ffmpeg has read into its input, from ``/proc/<pid>/fdinfo``.

    ffmpeg 8's ``out_time`` is the *minimum* last-muxed timestamp across all
    output streams (7.x reported the maximum). Any source with a sparse
    subtitle track — a forced PGS track on a Blu-ray remux has a handful of
    packets in the first five minutes — therefore reports an out_time that
    sits still for most of the job, and progress derived from it froze at
    1.5% with a 47,000 s ETA on a job that was 86% done. The kernel's read
    position on the source file has no such opinion about streams: it works
    for a remux and an encode alike, costs one small file read per poll, and
    needs nothing from ffmpeg.
    """

    def __init__(self, pid: int, source: str | None,
                 proc_root: str = PROC_ROOT) -> None:
        self.dir = os.path.join(proc_root, str(pid))
        self.targets: set[str] = set()
        self.size = 0
        if source:
            self.targets = {source, os.path.realpath(source)}
            try:
                self.size = os.stat(source).st_size
            except OSError:
                self.size = 0
        self._fd: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.targets) and self.size > 0

    def _find_fd(self) -> str | None:
        # The fd stays put for the life of the process, so remember it and
        # only rescan when the cached one stops pointing at the source.
        fd_dir = os.path.join(self.dir, "fd")
        if self._fd is not None and self._points_at_source(fd_dir, self._fd):
            return self._fd
        self._fd = None
        try:
            names = os.listdir(fd_dir)
        except OSError:
            return None
        for name in names:
            if self._points_at_source(fd_dir, name):
                self._fd = name
                break
        return self._fd

    def _points_at_source(self, fd_dir: str, name: str) -> bool:
        try:
            return os.readlink(os.path.join(fd_dir, name)) in self.targets
        except OSError:
            return False

    def read(self) -> int | None:
        """Bytes of the source read so far, or None when the kernel cannot
        tell us (no /proc, process gone, source not open yet)."""
        if not self.usable:
            return None
        fd = self._find_fd()
        if fd is None:
            return None
        try:
            with open(os.path.join(self.dir, "fdinfo", fd)) as fh:
                m = _POS_RE.search(fh.read())
        except OSError:
            return None
        return int(m.group(1)) if m else None


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
        cancel: threading.Event | None = None,
        governor: "governor_mod.Governor | None" = None,
        source: str | None = None,
        poll_interval: float = 1.0,
        proc_root: str = PROC_ROOT) -> tuple[bool, str]:
    """Run ffmpeg, reporting progress and killing a stalled job.

    Progress is how far ffmpeg has read into ``source`` (the input after
    ``-i`` when not given), read from ``/proc``; see :class:`SourcePosition`
    for why ``out_time`` from ``-progress pipe:1`` cannot be trusted. Where
    there is no ``/proc`` — a developer's Mac — ``out_time`` is what we have.

    A job whose read position has not advanced for ``stall_timeout`` seconds
    is killed — a hung read on a dead NFS mount otherwise sits there for ever
    holding the worker. The check runs on a timer, not on ffmpeg's output: a
    blocking ``readline()`` returns only at EOF, so a hung child that emits
    nothing could never be caught that way, and one that keeps printing a
    frozen counter must not count as alive either.
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
    stderr_tail: list[str] = []
    position = SourcePosition(proc.pid, source or source_of(cmd), proc_root)

    # Hold this encode to its share of the GPU's encode engine, on its own
    # thread, so its stop/resume cadence is independent of the poll below.
    gov_stop = threading.Event()
    gov_thread = None
    if governor is not None and governor.active:
        gov_thread = threading.Thread(
            target=governor.run, args=(proc.pid, gov_stop), daemon=True,
            name="unfuckarr-governor")
        gov_thread.start()

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if line:
                stderr_tail.append(line)
                del stderr_tail[:-30]

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()

    # ffmpeg's progress lines arrive through a queue so the loop below wakes
    # every poll_interval whether or not ffmpeg said anything. None marks EOF.
    lines: queue.Queue[str | None] = queue.Queue()

    def drain_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    out_thread = threading.Thread(target=drain_stdout, daemon=True)
    out_thread.start()

    killed_for = ""
    last_advance = started
    last_pos: int | None = None
    last_out_time: int | None = None
    while True:
        try:
            line = lines.get(timeout=poll_interval)
        except queue.Empty:
            line = ""
        if line is None:
            break
        if cancel is not None and cancel.is_set():
            killed_for = "cancelled"
            proc.kill()
            break
        m = _TIME_RE.search(line) if line else None
        out_time = int(m.group(1)) if m else None
        # Once per progress block or per quiet poll, not once per line.
        if line and out_time is None:
            continue

        now = time.time()
        pos = position.read()
        # Forward progress is whatever drives the progress bar: the read
        # position where the kernel reports one, the muxed timestamp where
        # it does not. A change in either direction counts — the Matroska
        # demuxer seeks to the tail for its cues at open and comes back.
        if pos is not None:
            if pos != last_pos:
                last_advance = now
            last_pos = pos
        elif out_time is not None:
            if out_time != last_out_time:
                last_advance = now
            last_out_time = out_time
        if now - last_advance > stall_timeout:
            killed_for = f"no progress for {stall_timeout}s"
            proc.kill()
            break

        if out_time is not None and on_progress:
            frac = None
            if pos is not None:
                frac = pos / position.size
            elif total_duration > 0:
                frac = out_time / 1_000_000 / total_duration
            if frac is not None:
                frac = max(0.0, min(1.0, frac))
                elapsed = now - started
                eta = (elapsed / frac - elapsed) if frac > 0.01 else None
                on_progress(frac, eta)

    gov_stop.set()
    if gov_thread is not None:
        gov_thread.join(timeout=5)
    proc.wait()
    out_thread.join(timeout=2)
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


# What ffmpeg, MakeMKV and Python's strerror print for ENOSPC and EDQUOT.
NO_SPACE_MARKERS = ("No space left on device", "Disk quota exceeded")


def out_of_space(message: str) -> bool:
    """Whether a failure was the disk filling up, not anything about the file.

    `free_space_ok` cannot see this coming on Unraid. `shutil.disk_usage` on a
    user share reports the whole array, and a share that writes to the cache
    pool first fails as soon as the *pool* fills. On 2026-09-21 the pool
    filled with terabytes free on the array; that scan started 4,059 repairs
    and finished in 20 minutes, the next evening's did the same in 8, and
    each failure was counted against its file — 3,815 files written off in
    a day, every one of them byte-for-byte untouched.
    """
    return any(m in (message or "") for m in NO_SPACE_MARKERS)


def free_space_ok(path: str, needed: int, margin: float = 1.2) -> bool:
    """A transcode that fills the array is worse than a broken file."""
    try:
        usage = shutil.disk_usage(os.path.dirname(path) or ".")
    except OSError:
        return True
    return usage.free > needed * margin
