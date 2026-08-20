"""Quality-targeted encoding: how small can this file get and still look the same.

Everything else in unfuckarr acts on a *fault* — a file that is broken, or that
Emby cannot direct play. This module exists for the files that are perfectly
fine and simply enormous: a 40 Mbps H.264 Blu-ray rip that would be
indistinguishable at a third of the size.

The dangerous way to do that is to pick a CRF and apply it to the whole
library. CRF is not a quality level; it is a rate-control knob whose meaning
depends entirely on the content. The same CRF 22 that is visually lossless on a
talking-heads documentary is mush on grain-heavy 35mm. So instead of guessing,
the encode is *measured*: short samples are encoded at candidate CRFs, each is
scored against the source with a full-reference metric, and the search returns
the largest CRF (smallest file) that still meets the quality target. That is
the same shape as shrinkray's "SmartShrink" and ab-av1's CRF search.

Two things about the metric are worth knowing before changing anything here:

* **VMAF is not available in any distro ffmpeg.** Debian bookworm, Debian
  trixie and jellyfin-ffmpeg are all built without ``--enable-libvmaf``. The
  container therefore ships a second, static ffmpeg (``ffmpeg-vmaf``) built by
  BtbN, used *only* for scoring — never for encoding, so the VAAPI/Mesa path
  that took real hardware to get right is left completely alone.
* **SSIM is the fallback, and it is a weaker guarantee.** When no libvmaf is
  found anywhere, scoring falls back to ffmpeg's built-in ``ssim`` filter. It
  is always available and it does detect gross quality loss, but it correlates
  less well with what a person sees, so the thresholds are approximate. The
  UI says so rather than pretending the two are interchangeable.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .checks.efficiency import audio_bytes
from .config import ShrinkConfig, TranscodeConfig
from .probe import MediaInfo
from .transcode import video_encode_args

log = logging.getLogger(__name__)

# Score at or above which each tier calls the encode good enough.
#
# The VMAF numbers are the conventional ones and match shrinkray's tiers.
# The SSIM numbers are a mapping, not an equivalence: they were chosen to sit
# at roughly the same place on typical live-action content and will not agree
# with VMAF on animation or heavy grain. Treat them as "about right", which is
# why VMAF is preferred whenever it can be found.
QUALITY_TIERS: dict[str, dict[str, float]] = {
    "vmaf": {"acceptable": 85.0, "good": 92.0, "excellent": 95.0},
    "ssim": {"acceptable": 0.960, "good": 0.975, "excellent": 0.985},
}

# How far below the target a single sample may sit while the mean still passes.
# One dark, grainy scene scoring badly is exactly the case a mean hides, and
# it is also the scene a viewer notices.
DEFAULT_WINDOW_TOLERANCE = {"vmaf": 3.0, "ssim": 0.010}

# Names tried, in order, when looking for an ffmpeg that can compute VMAF.
VMAF_BINARIES = ("ffmpeg-vmaf", "ffmpeg")

_filters_cache: dict[str, frozenset[str]] = {}


class QualityError(RuntimeError):
    """The search could not be carried out at all (not: it found nothing)."""


# -- metric discovery -----------------------------------------------------

def filters_for(binary: str) -> frozenset[str]:
    """Which filters an ffmpeg build has. Cached — it costs a process."""
    if binary in _filters_cache:
        return _filters_cache[binary]
    names: set[str] = set()
    try:
        proc = subprocess.run(
            [binary, "-hide_banner", "-nostdin", "-filters"],
            capture_output=True, text=True, timeout=30,
        )
        for line in proc.stdout.splitlines():
            parts = line.split()
            # " .. libvmaf           VV->V      Calculate the VMAF ..."
            if len(parts) >= 3 and parts[0].startswith((".", "T", "S")):
                names.add(parts[1])
    except (OSError, subprocess.SubprocessError):
        names = set()
    frozen = frozenset(names)
    _filters_cache[binary] = frozen
    return frozen


def forget_filters() -> None:
    """Drop the cache. Tests, and a settings change that renames a binary."""
    _filters_cache.clear()


@dataclass(frozen=True)
class Metric:
    name: str        # vmaf | ssim
    binary: str      # the ffmpeg that can compute it
    target: float
    tolerance: float

    @property
    def is_estimate(self) -> bool:
        """True when the thresholds are a mapping rather than the real thing."""
        return self.name == "ssim"


def resolve_metric(scfg: ShrinkConfig, ffmpeg: str = "ffmpeg") -> Metric | None:
    """Pick the metric and the binary that can compute it.

    Returns None when the configuration asks for VMAF specifically and no
    build has it — that is a real misconfiguration and the caller must not
    silently shrink anything on a weaker guarantee than the one asked for.
    """
    wanted = scfg.metric
    candidates: list[str] = []
    if scfg.vmaf_ffmpeg_path:
        candidates.append(scfg.vmaf_ffmpeg_path)
    candidates += [b for b in VMAF_BINARIES if b not in candidates]
    candidates.append(ffmpeg)

    vmaf_binary = None
    if wanted in ("auto", "vmaf"):
        for binary in candidates:
            if not (os.path.isabs(binary) or shutil.which(binary)):
                continue
            if "libvmaf" in filters_for(binary):
                vmaf_binary = binary
                break

    if vmaf_binary is not None:
        return Metric("vmaf", vmaf_binary,
                      _target(scfg, "vmaf"), _tolerance(scfg, "vmaf"))
    if wanted == "vmaf":
        return None
    if "ssim" not in filters_for(ffmpeg):
        return None
    return Metric("ssim", ffmpeg, _target(scfg, "ssim"), _tolerance(scfg, "ssim"))


def _target(scfg: ShrinkConfig, metric: str) -> float:
    if scfg.target_score > 0 and scfg.metric == metric:
        return scfg.target_score
    return QUALITY_TIERS[metric][scfg.quality]


def _tolerance(scfg: ShrinkConfig, metric: str) -> float:
    if scfg.window_tolerance > 0:
        return scfg.window_tolerance if metric == "vmaf" else scfg.window_tolerance / 300
    return DEFAULT_WINDOW_TOLERANCE[metric]


# -- sampling -------------------------------------------------------------

def sample_windows(duration: float, count: int, seconds: int,
                   skip_pct: float = 0.05) -> list[tuple[float, float]]:
    """Evenly spaced windows across the middle of the file.

    The first and last few percent are skipped deliberately: logos, fades to
    black and end credits compress unlike anything else in the file and would
    drag the estimate in whichever direction happened to dominate.
    """
    if duration <= 0 or count <= 0 or seconds <= 0:
        return []
    head = duration * skip_pct
    usable = duration * (1 - 2 * skip_pct)
    if usable <= seconds:
        # Too short to sample properly: score the middle of what there is.
        length = max(1.0, min(float(seconds), duration))
        return [(max(0.0, (duration - length) / 2), length)]
    count = max(1, min(count, int(usable // seconds)))
    stride = usable / count
    return [(head + i * stride + (stride - seconds) / 2, float(seconds))
            for i in range(count)]


def pix_fmt_for(info: MediaInfo) -> str:
    """The pixel format both the encode and the comparison must use.

    libvmaf and ssim both require their two inputs to be in the same format,
    and re-encoding a 10-bit source to 8-bit is a real quality change that the
    metric would (correctly) punish — so the source's depth is carried through
    rather than everything being flattened to yuv420p.
    """
    v = info.video
    if v is not None and v.bit_depth >= 10:
        return "yuv420p10le"
    return "yuv420p"


# -- one measurement ------------------------------------------------------

@dataclass
class Attempt:
    crf: int
    mean: float
    worst: float
    encoded_bytes: int
    window_seconds: float
    seconds: float = 0.0

    @property
    def bitrate(self) -> float:
        return (self.encoded_bytes * 8 / self.window_seconds
                if self.window_seconds else 0.0)


def _sample_encode(src: str, start: float, length: float, dst: str,
                   codec: str, crf: int, tcfg: TranscodeConfig,
                   pix_fmt: str, ffmpeg: str, timeout: int) -> None:
    hw = tcfg.hwaccel
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if hw == "vaapi":
        cmd += ["-vaapi_device", tcfg.vaapi_device]
    elif hw == "qsv":
        cmd += ["-hwaccel", "qsv"]
    # -ss before -i so ffmpeg seeks. Sampling the middle of a 40 GB file takes
    # seconds this way and minutes the other way.
    cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src,
            "-map", "0:v:0", "-an", "-sn", "-dn"]
    cmd += video_encode_args(codec, hw, crf, tcfg.preset, "mkv", pix_fmt=pix_fmt)
    cmd += ["-f", "matroska", dst]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not os.path.exists(dst):
        raise QualityError(
            (proc.stderr or "sample encode produced nothing").strip()[:300])


_VMAF_LINE = re.compile(r"VMAF score:\s*([\d.]+)")
_SSIM_ALL = re.compile(r"\bAll:\s*([\d.]+)")


def score_pair(distorted: str, reference: str, window: tuple[float, float],
               metric: Metric, pix_fmt: str, threads: int = 0,
               timeout: int = 1800,
               distorted_window: tuple[float, float] | None = None) -> float:
    """Score ``distorted`` against the matching window of ``reference``.

    The distorted file is input 0 and the reference is input 1 because that is
    the order ffmpeg's libvmaf filter wants (main first, reference second) —
    getting it backwards does not error, it just returns a different and wrong
    number.

    ``distorted_window`` seeks inside the distorted file too, which is how the
    finished output is scored: both inputs are *decoded* to the same
    timestamp, so the frames line up. Cutting the window out with ``-c copy``
    first would not — a stream copy cannot start mid-GOP, so it silently
    begins at the previous keyframe and every frame is then compared against
    the wrong one.
    """
    start, length = window
    both = (f"[0:v]settb=AVTB,setpts=PTS-STARTPTS,format={pix_fmt}[dist];"
            f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,format={pix_fmt}[ref];")
    with tempfile.TemporaryDirectory() as tmp:
        if metric.name == "vmaf":
            log_path = str(Path(tmp) / "vmaf.json")
            opts = f"log_fmt=json:log_path={log_path}"
            if threads > 0:
                opts += f":n_threads={threads}"
            lavfi = f"{both}[dist][ref]libvmaf={opts}"
            verbosity = "error"
        else:
            lavfi = f"{both}[dist][ref]ssim"
            log_path = ""
            verbosity = "info"

        cmd = [metric.binary, "-hide_banner", "-nostdin", "-v", verbosity]
        if distorted_window is not None:
            cmd += ["-ss", f"{distorted_window[0]:.3f}",
                    "-t", f"{distorted_window[1]:.3f}"]
        cmd += ["-i", distorted,
                "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", reference,
                "-lavfi", lavfi, "-f", "null", "-"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise QualityError(
                (proc.stderr or f"{metric.name} exited {proc.returncode}")
                .strip()[-300:])

        if metric.name == "vmaf":
            try:
                data = json.loads(Path(log_path).read_text())
                return float(data["pooled_metrics"]["vmaf"]["mean"])
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                # Older libvmaf builds print the score and write no log.
                m = _VMAF_LINE.search(proc.stderr or "")
                if m:
                    return float(m.group(1))
                raise QualityError("libvmaf produced no score") from None

    m = _SSIM_ALL.search(proc.stderr or "")
    if not m:
        raise QualityError("ssim produced no score")
    return float(m.group(1))


# -- the search -----------------------------------------------------------

@dataclass
class QualityPlan:
    """What the search found. ``ok`` False is a normal outcome, not an error."""

    ok: bool
    reason: str
    # True when the search could not be *carried out* — as opposed to having
    # been carried out and found nothing worth doing. The caller must not
    # write a file off permanently for the first kind: it is a statement about
    # the tooling, and the same file may shrink perfectly well once whatever
    # broke is fixed.
    error: bool = False
    metric: str = ""
    estimated: bool = False
    target: float = 0.0
    crf: int | None = None
    codec: str = ""
    score: float = 0.0
    worst: float = 0.0
    source_size: int = 0
    projected_size: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def saving_pct(self) -> float:
        if not self.source_size or not self.projected_size:
            return 0.0
        return (1 - self.projected_size / self.source_size) * 100

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "reason": self.reason, "error": self.error,
            "metric": self.metric,
            "estimated": self.estimated, "target": round(self.target, 4),
            "crf": self.crf, "codec": self.codec,
            "score": round(self.score, 4), "worst": round(self.worst, 4),
            "source_size": self.source_size,
            "projected_size": self.projected_size,
            "saving_pct": round(self.saving_pct, 1),
            "seconds": round(self.seconds, 1),
            "attempts": [
                {"crf": a.crf, "score": round(a.mean, 4),
                 "worst": round(a.worst, 4), "bitrate": round(a.bitrate)}
                for a in self.attempts
            ],
        }


def search(info: MediaInfo, scfg: ShrinkConfig, tcfg: TranscodeConfig,
           ffmpeg: str = "ffmpeg", metric: Metric | None = None,
           cancel=None) -> QualityPlan:
    """Find the largest CRF that still meets the quality target.

    Binary search over the CRF range, because the relationship is monotonic:
    a higher CRF is never higher quality. Each step encodes every sample
    window and scores it, so the cost is roughly
    ``search_steps × sample_count × sample_seconds`` of encoding plus the same
    again of scoring — minutes per file, which is why this is capped per scan.
    """
    started = time.time()
    if metric is None:
        metric = resolve_metric(scfg, ffmpeg)
    if metric is None:
        return QualityPlan(False, "no quality metric available — neither "
                                  "libvmaf nor ssim could be found", error=True)

    codec = scfg.codec
    pix_fmt = pix_fmt_for(info)
    windows = sample_windows(info.duration, scfg.sample_count, scfg.sample_seconds)
    if not windows:
        return QualityPlan(False, "file has no usable duration to sample",
                           metric=metric.name)
    window_seconds = sum(w[1] for w in windows)
    non_video = audio_bytes(info)
    # A sample encode of N seconds should not take longer than a few minutes;
    # anything past that is a stall, and the whole search must stay bounded.
    timeout = max(300, int(scfg.sample_seconds * 60))

    tried: dict[int, Attempt] = {}

    def evaluate(crf: int) -> Attempt:
        if crf in tried:
            return tried[crf]
        step_started = time.time()
        scores: list[float] = []
        total_bytes = 0
        with tempfile.TemporaryDirectory(prefix="unfuckarr-q-") as tmp:
            for i, (start, length) in enumerate(windows):
                if cancel is not None and cancel.is_set():
                    raise QualityError("cancelled")
                sample = str(Path(tmp) / f"s{i}-crf{crf}.mkv")
                # info.source, not info.path: a disc image is opened through
                # bluray:/subfile, and both the sample encode and the
                # comparison have to read the same way or they compare
                # nothing to nothing.
                _sample_encode(info.source, start, length, sample, codec, crf,
                               tcfg, pix_fmt, ffmpeg, timeout)
                total_bytes += os.path.getsize(sample)
                scores.append(score_pair(sample, info.source, (start, length),
                                         metric, pix_fmt,
                                         threads=scfg.metric_threads,
                                         timeout=timeout))
        attempt = Attempt(
            crf=crf, mean=statistics.fmean(scores), worst=min(scores),
            encoded_bytes=total_bytes, window_seconds=window_seconds,
            seconds=time.time() - step_started,
        )
        tried[crf] = attempt
        return attempt

    def passes(a: Attempt) -> bool:
        return a.mean >= metric.target and a.worst >= metric.target - metric.tolerance

    lo, hi = scfg.crf_min, scfg.crf_max
    best: Attempt | None = None
    steps = 0
    try:
        while lo <= hi and steps < scfg.search_steps:
            mid = (lo + hi) // 2
            attempt = evaluate(mid)
            steps += 1
            if passes(attempt):
                best = attempt
                lo = mid + 1        # a bigger CRF might still pass: smaller file
            else:
                hi = mid - 1
    except QualityError as exc:
        return QualityPlan(False, f"quality search failed: {exc}", error=True,
                           metric=metric.name, estimated=metric.is_estimate,
                           target=metric.target,
                           attempts=sorted(tried.values(), key=lambda a: a.crf),
                           seconds=time.time() - started)

    attempts = sorted(tried.values(), key=lambda a: a.crf)
    if best is None:
        lowest = attempts[0] if attempts else None
        detail = (f" (best was {lowest.mean:.1f} at CRF {lowest.crf})"
                  if lowest else "")
        return QualityPlan(
            False,
            f"cannot reach {metric.name.upper()} {metric.target:g} anywhere in "
            f"CRF {scfg.crf_min}–{scfg.crf_max}{detail} — this file is already "
            "about as small as it can be at this quality",
            metric=metric.name, estimated=metric.is_estimate,
            target=metric.target, attempts=attempts,
            source_size=info.size, seconds=time.time() - started)

    projected = int(best.bitrate * info.duration / 8) + non_video
    return QualityPlan(
        True,
        f"{metric.name.upper()} {best.mean:.1f} at CRF {best.crf}",
        metric=metric.name, estimated=metric.is_estimate, target=metric.target,
        crf=best.crf, codec=codec, score=best.mean, worst=best.worst,
        source_size=info.size, projected_size=projected, attempts=attempts,
        seconds=time.time() - started,
    )


def verify(source: str, output: str, info: MediaInfo, scfg: ShrinkConfig,
           metric: Metric, cancel=None) -> tuple[float, float]:
    """Score the finished file against the original. Returns (mean, worst).

    The search measured samples encoded on their own; a full encode is not
    guaranteed to land in the same place — rate control behaves differently
    over two hours than over fifteen seconds. This is the check that decides
    whether the output is kept, so it reads the real output, not a sample of
    the plan.
    """
    windows = sample_windows(info.duration, scfg.sample_count, scfg.sample_seconds)
    pix_fmt = pix_fmt_for(info)
    timeout = max(300, int(scfg.sample_seconds * 60))
    scores: list[float] = []
    for start, length in windows:
        if cancel is not None and cancel.is_set():
            raise QualityError("cancelled")
        scores.append(score_pair(output, source, (start, length), metric,
                                 pix_fmt, threads=scfg.metric_threads,
                                 timeout=timeout,
                                 distorted_window=(start, length)))
    if not scores:
        raise QualityError("nothing could be scored")
    return statistics.fmean(scores), min(scores)


def human_size(n: float) -> str:
    """Sizes appear in log lines and findings; keep one spelling of them."""
    if n <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = min(int(math.log(n, 1024)), len(units) - 1)
    return f"{n / 1024 ** i:.1f} {units[i]}".replace(".0 ", " ")
