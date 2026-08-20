"""Integrity checks — is this file actually intact?

Ordered cheapest-first. A file that fails ffprobe never reaches the decode
pass, because there is nothing left to learn from spending ten minutes on it.
"""

from __future__ import annotations

import os

from ..config import IntegrityConfig
from ..disc import is_disc_image
from ..probe import (DiscUnreadable, MediaInfo, ProbeError, decode_check,
                     decode_check_full, probe)
from . import CheckResult, Finding

# Below this nothing is a real video file — it is a stub, a placeholder, or a
# download that never started. Deliberately low: "a 20 MB file that should be
# 8 GB" is a *duration* problem, and `duration_mismatch` and `too_short` catch
# it properly. A size floor high enough to catch that would also condemn every
# legitimately short extra in the library.
TINY_FILE_BYTES = 128 * 1024


def check(
    path: str,
    cfg: IntegrityConfig,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    expected_runtime: int | None = None,
    info: MediaInfo | None = None,
) -> tuple[CheckResult, MediaInfo | None]:
    """Run integrity checks, returning the result and the probe for reuse."""
    result = CheckResult(path=path)
    if not cfg.enabled:
        return result, info

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        result.error = f"cannot stat file: {exc}"
        result.add(Finding("integrity", "unreadable", "error", str(exc)))
        return result, None

    if size == 0:
        result.add(Finding("integrity", "zero_length", "error", "file is 0 bytes"))
        return result, None
    if size < TINY_FILE_BYTES:
        result.add(Finding(
            "integrity", "suspiciously_small", "error",
            f"only {size} bytes — sample or failed download", {"size": size},
        ))

    if info is None:
        if is_disc_image(path) and not cfg.inspect_disc_images:
            result.add(Finding(
                "integrity", "disc_not_inspected", "info",
                "disc image, and disc inspection is switched off",
            ))
            return result, None
        try:
            info = probe(path, ffprobe, ffmpeg=ffmpeg)
        except DiscUnreadable as exc:
            # A disc image this build has no way to open. That is a statement
            # about the tooling, not about the file, and it must never reach a
            # policy that deletes: doing so put three 42 GB Blu-ray images in
            # the recycle bin on the live library before this existed.
            result.add(Finding(
                "integrity", "disc_not_inspectable", "info",
                f"disc image that could not be opened for inspection: {exc}. "
                "Nothing has been done to it — being unable to read a file is "
                "not evidence that the file is bad.",
            ))
            return result, None
        except ProbeError as exc:
            result.add(Finding(
                "integrity", "probe_failed", "error",
                f"ffprobe could not read the file: {exc}",
            ))
            return result, None

    result.probe = info.summary()

    if info.video is None:
        result.add(Finding("integrity", "no_video_stream", "error",
                           "no decodable video stream"))
    if cfg.fail_on_missing_audio and not info.audio:
        result.add(Finding("integrity", "no_audio_stream", "error",
                           "no audio stream"))

    if info.duration <= 0:
        result.add(Finding("integrity", "no_duration", "error",
                           "container reports no duration — usually a truncated write"))
    elif info.duration < cfg.min_duration_seconds:
        result.add(Finding(
            "integrity", "too_short", "error",
            f"only {info.duration:.0f}s long", {"duration": info.duration},
        ))
    elif expected_runtime and expected_runtime > 0 and info.duration < expected_runtime:
        # The *arr's runtime is nominal, not measured: for TV it is the
        # broadcast slot, which counts the ad breaks the file does not have.
        # A 22 min sitcom against a 25 min slot, or a 44 min drama against a
        # 60 min one, is a perfectly healthy file — calling either truncated
        # sends a pristine remux to be deleted and re-searched. Only a file
        # short of half its runtime is unambiguously cut off; between the two
        # thresholds the gap is worth showing and not worth acting on.
        short = (expected_runtime - info.duration) / expected_runtime * 100
        detail = (f"{info.duration / 60:.0f} min on disk vs "
                  f"{expected_runtime / 60:.0f} min expected ({short:.0f}% short)")
        data = {"duration": info.duration, "expected": expected_runtime,
                "short_pct": short}
        if info.is_disc:
            # A disc image that reads short is far more likely to be a disc
            # whose feature is split across several streams than a truncated
            # download — the main title is picked as the largest .m2ts, and on
            # a disc using seamless branching that is one segment of the film.
            # Measured across ten real discs, two read short for exactly that
            # reason, one of them by 85%. Condemning those would delete a
            # perfectly good 10 GB image over a heuristic.
            result.add(Finding(
                "integrity", "duration_below_expected", "info",
                f"{detail} — on a disc image this usually means the feature is "
                "split across several streams rather than that anything is "
                "wrong with it",
                data,
            ))
        elif short >= cfg.duration_truncated_pct:
            result.add(Finding(
                "integrity", "duration_mismatch", "error", detail, data))
        elif short > cfg.duration_tolerance_pct:
            result.add(Finding(
                "integrity", "duration_below_expected", "info",
                f"{detail} — the expected runtime is nominal, so this is "
                "usually ad breaks rather than a truncated file",
                data,
            ))

    # Already failing on structure — a decode pass adds cost, not information.
    if result.errors or cfg.depth == "quick":
        return result, info

    _decode_pass(info.source, cfg, ffmpeg, info, result)
    return result, info


def _decode_pass(source: str, cfg: IntegrityConfig, ffmpeg: str,
                 info: MediaInfo, result: CheckResult) -> None:
    """``source`` is what ffmpeg opens — a path, or ``bluray:`` for a disc."""
    # One audio track, not all of them, when the source is a disc: a Blu-ray
    # playlist carries a dozen, several TrueHD, and decoding every one turns a
    # five-second sample into a multi-minute one without answering anything
    # the first track does not.
    streams = ("0:v:0?", "0:a:0?") if info.is_disc else ("0:v:0?", "0:a?")
    if cfg.depth == "full":
        res = decode_check_full(source, ffmpeg, streams=streams)
        windows = [("whole file", res)]
    else:
        # Head, middle and tail. Damage from an interrupted download lands at
        # the tail; damage from a bad remux lands at the head; the middle
        # catches disk-level rot that neither end would show.
        span = float(cfg.sample_seconds)
        dur = info.duration
        offsets: list[tuple[str, float]] = [("start", 0.0)]
        if dur > span * 3:
            offsets.append(("middle", max(0.0, dur / 2 - span / 2)))
            offsets.append(("end", max(0.0, dur - span - 1)))
        windows = [
            (label, decode_check_full(source, ffmpeg, start=off, duration=span,
                              streams=streams))
            for label, off in offsets
        ]
        # A container whose index is broken demuxes badly even where the video
        # decodes; one cheap copy pass over the whole file catches that.
        #
        # Not for a disc image. "Cheap" here means I/O-bound rather than
        # CPU-bound, which is true of a 40 GB mkv and emphatically not true of
        # a 94 GB Blu-ray read through libbluray — it reads the whole disc,
        # for hours, to answer a question that does not arise: libbluray built
        # its own index from the playlist to open the thing at all, so a
        # broken index would have failed already.
        if not info.is_disc:
            windows.append(("demux", decode_check(source, ffmpeg)))

    total = 0
    messages: list[str] = []
    for label, res in windows:
        if res.timed_out:
            result.add(Finding("integrity", "decode_timeout", "error",
                               f"decode of {label} timed out"))
            continue
        if res.errors:
            total += res.errors
            messages.extend(f"[{label}] {m}" for m in res.messages[:3])

    if not total:
        return
    severity = "error" if total >= cfg.max_decode_errors else "warning"
    result.add(Finding(
        "integrity", "decode_errors", severity,
        f"{total} decode error(s): " + "; ".join(messages[:4]),
        {"count": total, "messages": messages},
    ))


# Damage confined to the container — index, atom order, a mangled header — is
# usually fixed by a remux, which is far cheaper than a 40 GB re-download.
REPAIRABLE_CODES = {"decode_errors", "no_duration", "probe_failed"}


def looks_repairable(result: CheckResult) -> bool:
    """Whether a remux is worth trying before throwing the file away."""
    codes = {f.code for f in result.errors}
    if not codes:
        return False
    hopeless = {"zero_length", "suspiciously_small", "no_video_stream",
                "duration_mismatch", "too_short", "unreadable"}
    return codes.issubset(REPAIRABLE_CODES) and not (codes & hopeless)
