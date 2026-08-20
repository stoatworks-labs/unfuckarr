"""The quality search, and every brake on the shrink action.

Shrinking is the only thing unfuckarr does to a file that nothing is wrong
with, so the tests that matter are the ones that prove it declines: when the
saving is not there, when the result does not measure up, when the file has
been shrunk once already. A shrink that goes ahead is the easy case.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import subprocess
from pathlib import Path

import pytest

from unfuckarr import db, quality, transcode
from unfuckarr.probe import probe
from unfuckarr.quality import Attempt, Metric, QualityPlan
from unfuckarr.remediation import Decision, Remediator
from unfuckarr.scanner import check_file

from .conftest import FFMPEG, needs_ffmpeg

def _vmaf_binary() -> str | None:
    """Find a libvmaf-capable ffmpeg the same way the application does.

    Not `filters_for(FFMPEG)`: no distro ffmpeg has libvmaf, so checking only
    the one on PATH skips these tests everywhere — including CI, which then
    cannot exercise the part of the application that decides whether a
    re-encode may replace someone's file. CI installs the same static
    `ffmpeg-vmaf` the image ships, and this finds it.
    """
    for binary in quality.VMAF_BINARIES + ((FFMPEG,) if FFMPEG else ()):
        if not (binary and shutil.which(binary)):
            continue
        if "libvmaf" in quality.filters_for(binary):
            return binary
    return None


VMAF_BINARY = _vmaf_binary()
HAS_VMAF = VMAF_BINARY is not None
needs_vmaf = pytest.mark.skipif(
    not HAS_VMAF, reason="no ffmpeg on this machine was built with libvmaf")

VMAF = Metric("vmaf", VMAF_BINARY or "ffmpeg", target=92.0, tolerance=3.0)


# -- sampling -------------------------------------------------------------

def test_samples_avoid_the_head_and_the_tail():
    """Logos, fades to black and end credits compress unlike anything else in
    the file and would drag the estimate whichever way they dominated."""
    windows = quality.sample_windows(3600, 3, 15)
    assert len(windows) == 3
    assert windows[0][0] > 3600 * 0.05
    assert windows[-1][0] + windows[-1][1] < 3600 * 0.95
    starts = [w[0] for w in windows]
    assert starts == sorted(starts)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert max(gaps) - min(gaps) < 1.0, "windows should be evenly spaced"


def test_a_file_too_short_to_sample_properly_uses_its_middle():
    assert quality.sample_windows(20, 3, 15) == [(2.5, 15.0)]


def test_no_duration_means_no_samples():
    assert quality.sample_windows(0, 3, 15) == []


def test_ten_bit_sources_are_not_flattened_to_eight():
    """Both the encode and the comparison have to use the same format, and
    dropping to 8-bit is a real quality change the metric would punish."""
    from .test_efficiency import media
    assert quality.pix_fmt_for(media(bit_depth=10)) == "yuv420p10le"
    assert quality.pix_fmt_for(media(bit_depth=8)) == "yuv420p"


# -- metric discovery -----------------------------------------------------

def test_ssim_is_the_fallback_and_says_so(settings, monkeypatch):
    monkeypatch.setattr(quality, "filters_for",
                        lambda binary: frozenset({"ssim", "scale"}))
    metric = quality.resolve_metric(settings.shrink, "ffmpeg")
    assert metric is not None and metric.name == "ssim"
    assert metric.is_estimate, "SSIM thresholds are a mapping, not the real thing"
    assert metric.target == quality.QUALITY_TIERS["ssim"]["good"]


def test_asking_for_vmaf_specifically_fails_rather_than_downgrading(
        settings, monkeypatch):
    """Silently scoring with a weaker metric than the one asked for would mean
    shrinking a library on a guarantee the user did not agree to."""
    monkeypatch.setattr(quality, "filters_for",
                        lambda binary: frozenset({"ssim"}))
    settings.shrink.metric = "vmaf"
    assert quality.resolve_metric(settings.shrink, "ffmpeg") is None


def test_no_metric_at_all_is_reported(settings, monkeypatch):
    monkeypatch.setattr(quality, "filters_for", lambda binary: frozenset())
    assert quality.resolve_metric(settings.shrink, "ffmpeg") is None


def test_the_tier_sets_the_target(settings, monkeypatch):
    monkeypatch.setattr(quality, "filters_for",
                        lambda binary: frozenset({"libvmaf"}))
    for tier, expected in (("acceptable", 85.0), ("good", 92.0),
                           ("excellent", 95.0)):
        settings.shrink.quality = tier
        assert quality.resolve_metric(settings.shrink, "ffmpeg").target == expected


# -- the search -----------------------------------------------------------

def fake_encoder(monkeypatch, score_at, bytes_at):
    """Stand in for ffmpeg with a monotonic model of it."""
    def _encode(src, start, length, dst, codec, crf, tcfg, pix_fmt, ffmpeg, timeout):
        Path(dst).write_bytes(b"\0" * bytes_at(crf))

    monkeypatch.setattr(quality, "extract_window",
                        lambda src, st, ln, dst, *a, **k: Path(dst).write_bytes(b"ref"))
    monkeypatch.setattr(quality, "_sample_encode", _encode)
    monkeypatch.setattr(quality, "score_pair",
                        lambda distorted, *a, **k: score_at(
                            int(Path(distorted).stem.split("crf")[1])))


def test_the_search_returns_the_largest_crf_that_still_passes(
        settings, monkeypatch):
    """Largest, not safest: a lower CRF always passes and always saves less,
    so a search that stops at the first success saves almost nothing."""
    from .test_efficiency import media
    fake_encoder(monkeypatch,
                 score_at=lambda crf: 100 - (crf - 15) * 1.5,
                 bytes_at=lambda crf: 10_000_000 // crf)

    plan = quality.search(media(), settings.shrink, settings.transcode,
                          metric=VMAF)
    assert plan.ok, plan.reason
    assert plan.crf == 20, [a.crf for a in plan.attempts]
    assert plan.score >= VMAF.target
    assert len(plan.attempts) <= settings.shrink.search_steps


def test_the_search_gives_up_when_the_target_is_out_of_reach(
        settings, monkeypatch):
    """A file already at the edge of its quality is not a failure; it is an
    answer, and it has to be recorded as one or it is recomputed for ever."""
    from .test_efficiency import media
    fake_encoder(monkeypatch,
                 score_at=lambda crf: 100 - (crf - 15) * 10,
                 bytes_at=lambda crf: 10_000_000 // crf)

    plan = quality.search(media(), settings.shrink, settings.transcode,
                          metric=VMAF)
    assert not plan.ok
    assert "cannot reach" in plan.reason
    assert plan.attempts, "the attempts should still be reportable"


def test_one_bad_scene_fails_a_passing_mean(settings, monkeypatch):
    """A mean hides exactly the dark, grainy scene a viewer notices."""
    from .test_efficiency import media
    settings.shrink.sample_count = 2

    calls = {"n": 0}

    def _encode(src, start, length, dst, codec, crf, tcfg, pix_fmt, ffmpeg, timeout):
        Path(dst).write_bytes(b"\0" * 1000)

    def _score(distorted, *a, **k):
        calls["n"] += 1
        # One window scores far below the target, the other far above it, so
        # the mean passes and the worst sample does not.
        return 99.0 if calls["n"] % 2 else 86.0

    monkeypatch.setattr(quality, "extract_window",
                        lambda src, st, ln, dst, *a, **k: Path(dst).write_bytes(b"ref"))
    monkeypatch.setattr(quality, "_sample_encode", _encode)
    monkeypatch.setattr(quality, "score_pair", _score)
    plan = quality.search(media(), settings.shrink, settings.transcode,
                          metric=VMAF)
    assert not plan.ok, "a 92.5 mean built from an 86 is not a pass"


def test_the_projection_includes_the_audio_it_will_copy(settings, monkeypatch):
    from .test_efficiency import media
    fake_encoder(monkeypatch,
                 score_at=lambda crf: 99.0,
                 bytes_at=lambda crf: 1000)
    info = media(size_mb=30000, duration=7200)
    plan = quality.search(info, settings.shrink, settings.transcode, metric=VMAF)
    assert plan.ok
    from unfuckarr.checks.efficiency import audio_bytes
    assert plan.projected_size >= audio_bytes(info)


# -- the metric against real media ---------------------------------------

@needs_vmaf
def test_vmaf_scores_a_real_encode(video_factory, settings, tmp_path):
    """The whole design rests on this number meaning something, so measure a
    real encode rather than trusting the plumbing."""
    src = video_factory("ref.mkv", seconds=6, size="320x180")
    good = tmp_path / "good.mkv"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src),
                    "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset",
                    "ultrafast", "-crf", "10", "-pix_fmt", "yuv420p",
                    str(good)], check=True, capture_output=True)
    bad = tmp_path / "bad.mkv"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src),
                    "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset",
                    "ultrafast", "-crf", "51", "-pix_fmt", "yuv420p",
                    str(bad)], check=True, capture_output=True)

    # Both inputs are seeked to the same window. Scoring a whole file against
    # a window of the reference compares frame 0 with frame 25 and produces a
    # number that looks like quality loss and is not.
    high = quality.score_pair(str(good), str(src), (1.0, 3.0), VMAF, "yuv420p",
                              distorted_window=(1.0, 3.0))
    low = quality.score_pair(str(bad), str(src), (1.0, 3.0), VMAF, "yuv420p",
                             distorted_window=(1.0, 3.0))
    assert high > low, f"CRF 10 scored {high}, CRF 51 scored {low}"
    assert high > 90, "a near-lossless re-encode should score close to the source"
    assert low < 80, "CRF 51 is visibly destroyed and must score like it"


@needs_vmaf
def test_a_misaligned_comparison_is_not_mistaken_for_quality_loss(
        video_factory, settings, tmp_path):
    """The trap this API exists to avoid: seeking one input and not the other
    scores a perfect copy as badly damaged."""
    src = video_factory("align.mkv", seconds=6, size="320x180")
    copy = tmp_path / "copy.mkv"
    shutil.copy(src, copy)

    aligned = quality.score_pair(str(copy), str(src), (1.0, 3.0), VMAF,
                                 "yuv420p", distorted_window=(1.0, 3.0))
    assert aligned > 95, f"a byte-identical copy scored {aligned}"


@needs_vmaf
def test_a_whole_shrink_runs_against_real_media(video_factory, settings,
                                                monkeypatch):
    """One end-to-end pass: search, encode, verify, recycle, replace, and the
    permanent marker that stops it happening twice."""
    settings.shrink.sample_count = 1
    settings.shrink.sample_seconds = 2
    settings.shrink.search_steps = 2
    settings.shrink.min_saving_pct = 1.0
    settings.emby_compat.target_profile = "modern"
    # Keep the search well inside the target rather than right on it. A CRF
    # chosen from a two-second sample can land a point over the line and then
    # miss it over the whole file — which is a real thing that happens, and
    # which the final verification is there to catch (see
    # test_a_result_that_measures_worse_than_the_target_is_discarded). This
    # test is about the path where everything works.
    settings.shrink.crf_max = 22

    path = video_factory("big.mkv", seconds=6, size="320x180",
                         extra=["-crf", "5"])
    row = insert(path)
    result, info = check_file(str(path), settings)
    before = path.stat().st_size

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["ok"], out

    final = Path(out["path"])
    assert final.exists()
    assert final.stat().st_size < before
    stored = db.q1("SELECT shrunk, shrunk_from, shrink_score, shrink_metric "
                   "FROM files WHERE path=?", (str(final),))
    assert stored["shrunk"] and stored["shrink_metric"] == "vmaf"
    assert stored["shrunk_from"] == before
    assert stored["shrink_score"] >= 92.0
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 1


# -- the brakes -----------------------------------------------------------

def insert(path: Path, **extra) -> dict:
    db.ex("INSERT INTO files (path, library, source, title, size) "
          "VALUES (?,?,?,?,?)",
          (str(path), "Movies", "folder", path.stem,
           path.stat().st_size if path.exists() else 0))
    for column, value in extra.items():
        db.ex(f"UPDATE files SET {column}=? WHERE path=?", (value, str(path)))
    row = db.q1("SELECT * FROM files WHERE path=?", (str(path),))
    return {k: row[k] for k in row.keys()}


def good_plan(**kw) -> QualityPlan:
    defaults = dict(ok=True, reason="VMAF 93.0 at CRF 24", metric="vmaf",
                    target=92.0, crf=24, codec="hevc", score=93.0, worst=92.5,
                    source_size=1_000_000, projected_size=100_000,
                    attempts=[Attempt(24, 93.0, 92.5, 1000, 15.0)])
    defaults.update(kw)
    return QualityPlan(**defaults)


def arm(monkeypatch, settings, plan=None, measured=(95.0, 94.0), shrink_to=0.1):
    """Point the shrink at a fixed search result, encode and verification.

    These tests are about the brakes, not about ffmpeg: a four-second test
    clip does not reliably get smaller when re-encoded, which would make every
    one of them fail for a reason that has nothing to do with what it asserts.
    The real encoder is exercised by
    ``test_a_whole_shrink_runs_against_real_media``.
    """
    monkeypatch.setattr(quality, "resolve_metric", lambda *a, **k: VMAF)
    monkeypatch.setattr(quality, "search", lambda *a, **k: plan or good_plan())
    monkeypatch.setattr(quality, "verify", lambda *a, **k: measured)
    monkeypatch.setattr(Remediator, "_verify_output", lambda *a, **k: None)
    ran = []

    def fake_run(cmd, *a, **k):
        ran.append(cmd)
        source = Path(cmd[cmd.index("-i") + 1])
        Path(cmd[-1]).write_bytes(
            b"\0" * max(1, int(source.stat().st_size * shrink_to)))
        return True, "faked"
    monkeypatch.setattr(transcode, "run", fake_run)
    return ran


@needs_ffmpeg
def test_a_file_is_never_shrunk_twice(video_factory, settings, monkeypatch):
    """Re-encoding an encode is a second generation of loss for a fraction of
    the saving, and nothing about the file says it has happened."""
    path = video_factory("once.mkv", seconds=4)
    row = insert(path, shrunk=1234.0, shrunk_from=999_999)
    ran = arm(monkeypatch, settings)
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag"
    assert "already shrunk" in out["message"]
    assert not ran, "nothing should have been encoded"


@needs_ffmpeg
def test_a_file_already_assessed_is_not_reassessed(video_factory, settings,
                                                   monkeypatch):
    """The reasons are properties of the content and will be just as true next
    scan, after another few hours of CPU spent finding that out again."""
    path = video_factory("done.mkv", seconds=4)
    row = insert(path, shrink_skipped="only 4% smaller at VMAF 92")
    ran = arm(monkeypatch, settings)
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag" and not ran


@needs_ffmpeg
def test_an_explicit_request_reopens_a_skipped_file(video_factory, settings,
                                                    monkeypatch):
    """The settings may have changed since — but a forced request still does
    not override "already shrunk once"."""
    path = video_factory("retry.mkv", seconds=4)
    row = insert(path, shrink_skipped="only 4% smaller at VMAF 92")
    ran = arm(monkeypatch, settings)
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "asked for", force=True))
    assert out["ok"] and ran


@needs_ffmpeg
def test_a_projection_below_the_floor_never_starts_an_encode(
        video_factory, settings, monkeypatch):
    """Hours of CPU are committed at this point; the cheap check comes first."""
    path = video_factory("meh.mkv", seconds=4)
    row = insert(path)
    ran = arm(monkeypatch, settings,
              plan=good_plan(source_size=1_000_000, projected_size=900_000))
    result, info = check_file(str(path), settings)
    before = path.read_bytes()

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert not ran, "10% is under the 25% floor"
    assert path.read_bytes() == before
    skipped = db.q1("SELECT shrink_skipped FROM files WHERE path=?",
                    (str(path),))["shrink_skipped"]
    assert skipped and "10%" in skipped


@needs_ffmpeg
def test_a_result_that_is_not_actually_smaller_is_discarded(
        video_factory, settings, monkeypatch):
    """The projection came from short samples; rate control over two hours does
    not have to agree with it. When it does not, the original wins."""
    path = video_factory("nosave.mkv", seconds=4)
    row = insert(path)
    arm(monkeypatch, settings, shrink_to=2.0)      # the output came out bigger
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag"
    assert path.exists(), "the original must survive"
    assert not list(path.parent.glob("*.unfuckarr.*"))
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 0


@needs_ffmpeg
def test_a_result_that_measures_worse_than_the_target_is_discarded(
        video_factory, settings, monkeypatch):
    """This is the check the whole feature rests on: smaller is not enough."""
    path = video_factory("ugly.mkv", seconds=4)
    row = insert(path)
    arm(monkeypatch, settings, measured=(74.0, 70.0))
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag"
    assert path.exists()
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 0
    skipped = db.q1("SELECT shrink_skipped FROM files WHERE path=?",
                    (str(path),))["shrink_skipped"]
    assert "74.0" in skipped


@needs_ffmpeg
def test_one_bad_sample_discards_a_passing_mean(video_factory, settings,
                                                monkeypatch):
    path = video_factory("spotty.mkv", seconds=4)
    row = insert(path)
    arm(monkeypatch, settings, measured=(93.0, 85.0))
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag" and path.exists()


@needs_ffmpeg
def test_an_unverifiable_result_is_discarded(video_factory, settings,
                                             monkeypatch):
    """Being unable to prove the output is good is not knowing it is bad, but
    it is not a licence to replace the original either."""
    path = video_factory("unver.mkv", seconds=4)
    row = insert(path)
    arm(monkeypatch, settings)
    monkeypatch.setattr(quality, "verify", lambda *a, **k: (_ for _ in ()).throw(
        quality.QualityError("ffmpeg died")))
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert not out["ok"] and path.exists()
    assert db.q1("SELECT shrink_attempts FROM files WHERE path=?",
                 (str(path),))["shrink_attempts"] == 1


@needs_ffmpeg
def test_hdr_is_left_alone(video_factory, settings, monkeypatch):
    path = video_factory("hdr.mkv", seconds=4)
    row = insert(path)
    ran = arm(monkeypatch, settings)
    result, info = check_file(str(path), settings)
    monkeypatch.setattr(type(info), "is_hdr", property(lambda self: True))

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag" and not ran
    assert "HDR" in db.q1("SELECT shrink_skipped FROM files WHERE path=?",
                          (str(path),))["shrink_skipped"]


@needs_ffmpeg
def test_no_metric_means_nothing_is_shrunk_and_nothing_is_written_off(
        video_factory, settings, monkeypatch):
    """A missing libvmaf is a configuration problem, not a property of the
    file: installing one should be enough to make it work next scan."""
    path = video_factory("nometric.mkv", seconds=4)
    row = insert(path)
    monkeypatch.setattr(quality, "resolve_metric", lambda *a, **k: None)
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag"
    assert db.q1("SELECT shrink_skipped FROM files WHERE path=?",
                 (str(path),))["shrink_skipped"] is None


@needs_ffmpeg
def test_shrinking_waits_for_its_window(video_factory, settings, monkeypatch):
    path = video_factory("night.mkv", seconds=4)
    row = insert(path)
    ran = arm(monkeypatch, settings)
    import unfuckarr.remediation as remediation
    monkeypatch.setattr(remediation, "_window_wait",
                        lambda w: "outside the 22-06 shrink window (it is 14:00)")
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert out["action"] == "flag" and not ran
    assert db.q1("SELECT shrink_skipped FROM files WHERE path=?",
                 (str(path),))["shrink_skipped"] is None, \
        "the time of day says nothing about the file"


def test_the_shrink_window_wraps_past_midnight(monkeypatch):
    import time as time_module

    import unfuckarr.remediation as remediation

    def at(hour):
        monkeypatch.setattr(
            remediation.time, "localtime",
            lambda *a: time_module.struct_time((2026, 8, 19, hour, 0, 0, 2, 231, 0)))

    at(23)
    assert remediation._window_wait("22-06") is None
    at(3)
    assert remediation._window_wait("22-06") is None
    at(14)
    assert remediation._window_wait("22-06") is not None
    at(14)
    assert remediation._window_wait("") is None


# -- the scan-level brakes ------------------------------------------------

def test_shrinks_do_not_count_towards_the_abort_ratio(settings, monkeypatch):
    """The brake exists to notice a library that has just broken. "Most of this
    library is H.264" is not that, and counting it here would disable the
    scanner permanently on almost every real library."""
    settings.shrink.continuous = False   # this is the batch path
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    from .test_efficiency import media, run as run_check

    applied = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[0]["path"]) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)
    settings.policy.max_shrinks_per_scan = 100

    state.scan = ScanProgress(running=True, checked=90)
    result = run_check(media(size_mb=30000), settings)
    pending = [({"path": f"/media/{i}.mkv"}, result, None,
                Decision("shrink", "unmeasured")) for i in range(90)]
    out = scanner._remediate(settings, pending, population=100)

    assert "aborted" not in out, "90% of a library being large is not a fault"
    assert len(applied) == 90


def test_shrinks_have_their_own_much_smaller_cap(settings, monkeypatch):
    settings.shrink.continuous = False   # this is the batch path
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    applied = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[0]["path"]) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)
    settings.policy.max_shrinks_per_scan = 3
    settings.policy.max_actions_per_scan = 50

    state.scan = ScanProgress(running=True, checked=20)
    from .test_efficiency import media, run as run_check
    result = run_check(media(size_mb=30000), settings)
    pending = [({"path": f"/media/{i}.mkv"}, result, None,
                Decision("shrink", "unmeasured")) for i in range(20)]
    out = scanner._remediate(settings, pending, population=100)

    assert len(applied) == 3
    assert out["shrinks"] == 3 and out["actions"] == 0


def test_repairs_are_applied_before_shrinks(settings, monkeypatch):
    """One multi-hour shrink must never consume the pass a corrupt file is
    waiting on."""
    settings.shrink.continuous = False   # this is the batch path
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    from .test_policy import corrupt

    applied = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[3].action) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)
    state.scan = ScanProgress(running=True, checked=4)

    pending = [
        ({"path": "/media/big.mkv"}, corrupt(), None, Decision("shrink", "big")),
        ({"path": "/media/broken.mkv"}, corrupt(), None,
         Decision("redownload", "corrupt")),
    ]
    scanner._remediate(settings, pending, population=100)
    assert applied == ["redownload", "shrink"]


# -- the estimate ---------------------------------------------------------

@needs_ffmpeg
def test_an_estimate_measures_and_changes_nothing(video_factory, settings,
                                                  monkeypatch):
    """The entire point of "estimate": it answers "what would this save" with
    the same search the real action uses, and leaves the file alone."""
    from unfuckarr import config
    from unfuckarr.service import Service

    path = video_factory("ask.mkv", seconds=4)
    before = path.read_bytes()
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(quality, "resolve_metric", lambda *a, **k: VMAF)
    monkeypatch.setattr(quality, "search", lambda *a, **k: good_plan())

    service = Service()
    assert service.estimate_shrink(str(path))
    for _ in range(100):
        row = db.q1("SELECT detail FROM activity WHERE event='shrink_estimate'")
        if row:
            break
        time.sleep(0.05)
    assert row is not None, "the estimate never reported"

    reported = json.loads(row["detail"])
    assert reported["ok"] and reported["crf"] == 24
    assert path.read_bytes() == before, "an estimate must not touch the file"
    assert db.q1("SELECT COUNT(*) n FROM jobs")["n"] == 0
    assert db.q1("SELECT shrunk, shrink_skipped FROM files WHERE path=?",
                 (str(path),)) is None


@needs_ffmpeg
def test_only_one_estimate_runs_at_a_time(video_factory, settings, monkeypatch):
    """Each one is minutes of encoding; two at once on a media server is not a
    service anyone wants."""
    from unfuckarr import config
    from unfuckarr.service import Service

    path = video_factory("busy.mkv", seconds=4)
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(quality, "resolve_metric", lambda *a, **k: VMAF)
    monkeypatch.setattr(quality, "search",
                        lambda *a, **k: time.sleep(0.5) or good_plan())

    service = Service()
    assert service.estimate_shrink(str(path))
    assert not service.estimate_shrink(str(path))


# -- schema ---------------------------------------------------------------

def test_shrink_columns_are_added_to_an_existing_database(tmp_path):
    """The live instance has a 1.0.0-shaped database with 17,000 rows in it."""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    # Comments stripped before the schema is created, because SQLite's DROP
    # COLUMN rewrites the stored CREATE TABLE text and a trailing `-- ...`
    # then swallows the line after the one it removed: "error in table files
    # after drop column: incomplete input". Fixed in newer SQLite, so this
    # passes on a modern local build and fails on the CI runner's older one.
    # Nothing in production drops a column — the migration only ever ADDs —
    # so this is the test's problem to avoid, not the schema's.
    conn.executescript(re.sub(r"--[^\n]*", "", db.SCHEMA))
    for column in ("shrunk", "shrunk_from", "shrink_score", "shrink_metric",
                   "shrink_skipped", "shrink_attempts"):
        conn.execute(f"ALTER TABLE files DROP COLUMN {column}")
    conn.execute("INSERT INTO files (path, status) VALUES ('/media/a.mkv', 'ok')")
    conn.commit()
    conn.close()

    db.reset_for_tests(old)
    columns = {r["name"] for r in db.q("PRAGMA table_info(files)")}
    assert {"shrunk", "shrunk_from", "shrink_score", "shrink_metric",
            "shrink_skipped", "shrink_attempts"} <= columns
    row = db.q1("SELECT status, shrunk FROM files WHERE path='/media/a.mkv'")
    assert row["status"] == "ok", "the existing rows must survive"
    assert row["shrunk"] is None


# -- what the hardware encoder taught us ---------------------------------

@needs_ffmpeg
def test_an_output_that_changed_size_is_rejected(video_factory, settings,
                                                 monkeypatch):
    """Nothing here ever asks for a resolution change, so one is a defect.

    Found on real hardware: `hevc_vaapi` on Mesa/AMD pads 1080 to the 1088 CTB
    boundary without signalling a conformance window, so the output decodes
    eight rows taller than the source — and the container metadata still says
    1080, so ffprobe does not give it away. Verified by decoding a frame and
    counting bytes: 3,133,440 against the source's 3,110,400. Nothing in the
    verification looked at dimensions, so a shrink would have replaced a
    1080p file with a padded 1088p one.
    """
    from unfuckarr.probe import probe

    path = video_factory("src.mkv", seconds=4, size="320x180")
    taller = Path(str(path).replace("src.mkv", "taller.mkv"))
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", str(path), "-map", "0:v:0",
         "-map", "0:a?", "-vf", "pad=320:184:0:0", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(taller)],
        check=True, capture_output=True)

    source = probe(str(path), settings.ffprobe_path)
    bad = Remediator(lambda: settings)._verify_output(
        str(taller), source, settings)
    assert bad is not None and "320x184" in bad and "320x180" in bad


@needs_ffmpeg
def test_a_search_that_could_not_run_does_not_write_the_file_off(
        video_factory, settings, monkeypatch):
    """A broken encoder setting is a statement about the tooling, not the
    file. Writing it off permanently would quietly retire the whole library
    over a configuration problem one setting away from being fixed — which is
    exactly what a VAAPI encoder producing uncomparable output would have
    done."""
    path = video_factory("tooling.mkv", seconds=4)
    row = insert(path)
    monkeypatch.setattr(quality, "resolve_metric", lambda *a, **k: VMAF)
    monkeypatch.setattr(quality, "search", lambda *a, **k: QualityPlan(
        False, "quality search failed: Width and height of input videos must "
               "be same.", error=True, metric="vmaf"))
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert not out["ok"]
    assert db.q1("SELECT shrink_skipped FROM files WHERE path=?",
                 (str(path),))["shrink_skipped"] is None, \
        "a tooling failure must not be recorded as a verdict on the file"


def test_a_search_that_found_nothing_worth_doing_is_final():
    """The other half: a verdict about the content is permanent, because it
    will be just as true next time and costs hours of CPU to reach again."""
    plan = QualityPlan(False, "cannot reach VMAF 92 anywhere in CRF 18-34",
                       metric="vmaf")
    assert not plan.error


@needs_vmaf
def test_a_lossless_encode_scores_as_lossless(video_factory, settings, tmp_path):
    """The control that should have existed from the start.

    The comparison used to score a sample against a *re-seeked* source, which
    does not reliably land on the same frame. When it did not, the metric
    reported quality loss that was not there — and it did so *plausibly*: lossy
    encodes still produced believable numbers, so a whole CRF calibration was
    run and reported before anyone checked the one case with a known answer.
    Measured on real media, a lossless encode scored VMAF 62 that way, and
    99.9 against a window extracted once.

    If this ever drops below ~98 again, every number the search produces is
    wrong by an unknown amount, in the direction of encoding more than needed.
    """
    src = video_factory("ref.mkv", seconds=6, size="320x180")
    window = tmp_path / "window.mkv"
    quality.extract_window(str(src), 1.0, 3.0, str(window), "yuv420p",
                           settings.ffmpeg_path, 300)

    lossless = tmp_path / "lossless.mkv"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", str(window), "-map", "0:v:0",
         "-an", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
         "-pix_fmt", "yuv420p", str(lossless)],
        check=True, capture_output=True)

    score = quality.score_pair(str(lossless), str(window), (0.0, 0.0),
                               VMAF, "yuv420p")
    # 95, not 99: VMAF's model is trained on 1080p and reads a little low on a
    # 320x180 fixture (~97.9 here against 99.9 measured on a real 1080p
    # remux). The bug this guards against scored 62, so the gap is enormous
    # either way.
    assert score > 95, f"a lossless encode scored {score} — the comparison is misaligned"


@needs_ffmpeg
def test_the_extracted_window_is_really_lossless(video_factory, settings, tmp_path):
    """Everything downstream is compared against this, so if the extraction
    itself lost anything, every score would be measuring that instead."""
    from unfuckarr.probe import probe

    src = video_factory("src.mkv", seconds=6, size="320x180")
    window = tmp_path / "w.mkv"
    quality.extract_window(str(src), 1.0, 2.0, str(window), "yuv420p",
                           settings.ffmpeg_path, 300)

    info = probe(str(window), settings.ffprobe_path)
    assert info.video is not None
    assert abs(info.duration - 2.0) < 0.5, info.duration

    # Byte-identical decode against the same window of the source.
    def frames(path, seek):
        cmd = [FFMPEG, "-v", "error"]
        if seek:
            cmd += ["-ss", "1.0", "-t", "2.0"]
        cmd += ["-i", str(path), "-map", "0:v:0", "-frames:v", "1",
                "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]
        return subprocess.run(cmd, capture_output=True).stdout

    assert frames(window, False) == frames(src, True), \
        "the extracted window is not the frames it claims to be"


@needs_ffmpeg
def test_the_size_on_record_follows_the_file(video_factory, settings):
    """A file rewritten in place is a different size, and nothing else updates
    the column until the next full enumeration. Left stale, the first live
    shrink reported a saving of zero — 10.71 GiB became 3.45 GiB on disk while
    `files.size` still said 10.71, and the reclaimed total is
    `shrunk_from - size`."""
    from unfuckarr.scanner import check_file, persist_result

    path = video_factory("resized.mkv", seconds=4)
    db.ex("INSERT INTO files (path, status, size) VALUES (?,?,?)",
          (str(path), "unknown", 999_999_999))

    result, info = check_file(str(path), settings)
    stat = path.stat()
    persist_result(str(path), result, info, stat.st_size, stat.st_mtime)

    row = db.q1("SELECT size, mtime FROM files WHERE path=?", (str(path),))
    assert row["size"] == stat.st_size
    assert row["mtime"] == stat.st_mtime


@needs_ffmpeg
def test_the_watcher_does_not_undo_a_finished_shrink(video_factory, settings,
                                                     monkeypatch):
    """A watch folder covering the library sees the *output* of a shrink as an
    arrival about a minute later. Checking it without knowing the file is
    already shrunk re-raises `not_measured` and overwrites the post-shrink
    state, so a finished file sits in the backlog for ever — which is exactly
    what happened within two minutes of the first real shrink."""
    from unfuckarr.service import Service

    path = video_factory("done.mkv", seconds=4, size="640x360")
    settings.efficiency.min_size_mb = 0
    settings.efficiency.min_duration_seconds = 0
    db.ex("INSERT INTO files (path, status, size, shrunk, shrunk_from) "
          "VALUES (?,?,?,?,?)",
          (str(path), "ok", path.stat().st_size, 123.0, 999_999_999))

    Service()._check_arrival(str(path))

    row = db.q1("SELECT status FROM files WHERE path=?", (str(path),))
    assert row["status"] != "unmeasured", \
        "the watcher put a finished file back in the shrink backlog"
    open_codes = {r["code"] for r in db.q(
        "SELECT code FROM findings WHERE path=? AND resolved IS NULL", (str(path),))}
    assert "not_measured" not in open_codes


@needs_ffmpeg
def test_an_extraction_that_wrote_no_frames_is_caught(video_factory, settings,
                                                      tmp_path, monkeypatch):
    """ffmpeg exits 0 having written a header and nothing else when the region
    it was asked for is damaged. Checking the exit code and that the file
    exists — the obvious pair — catches neither, and live that turned a
    Matroska with EBML damage into a 576-byte "window" whose every downstream
    failure pointed at the sample encode instead of at the source."""
    src = video_factory("src.mkv", seconds=4)
    dst = tmp_path / "window.mkv"

    real_run = quality.subprocess.run

    def header_only(cmd, *a, **kw):
        # Behave exactly as ffmpeg did: complain, write a stub, succeed.
        Path(cmd[-1]).write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 500)
        class R:
            returncode = 0
            stderr = ("[matroska,webm @ 0x1] 0x00 at pos 1991519 invalid as "
                      "first byte of an EBML number\n")
            stdout = ""
        return R()

    monkeypatch.setattr(quality.subprocess, "run", header_only)
    with pytest.raises(quality.QualityError) as caught:
        quality.extract_window(str(src), 1.0, 3.0, str(dst), "yuv420p",
                               settings.ffmpeg_path, 60)
    message = str(caught.value)
    assert "wrote no frames" in message
    assert "EBML" in message, "the real cause has to survive into the message"
    monkeypatch.setattr(quality.subprocess, "run", real_run)


def test_the_useful_end_of_an_ffmpeg_error_is_kept():
    """ffmpeg narrates as it goes, so the cause is the last thing it says.
    Taking the first 300 characters returned the warnings and threw away the
    error — and with no writable home, Mesa's shader-cache complaint was the
    first thing on every single invocation."""
    stderr = (
        "Failed to create /home/unfuckarr for shader cache (Permission denied)"
        "---disabling.\n"
        "    Last message repeated 1 times\n"
        "[matroska,webm @ 0x1] Duplicate element\n"
        "Conversion failed!\n")
    out = quality.ffmpeg_error(stderr, "fallback")
    assert "shader cache" not in out
    assert "Last message repeated" not in out
    assert "Conversion failed!" in out
    assert quality.ffmpeg_error("", "fallback") == "fallback"


@needs_ffmpeg
def test_a_failed_search_cannot_loop_on_the_same_file(video_factory, settings,
                                                      monkeypatch):
    """The continuous worker picks the fattest candidate every time it looks,
    so a failure that repeats deterministically is an infinite loop on one
    file. Live that was 330 identical failures on a damaged Matroska, one
    every 55 seconds, while the rest of the backlog waited."""
    path = video_factory("damaged.mkv", seconds=4)
    row = insert(path)
    monkeypatch.setattr(quality, "resolve_metric", lambda *a, **k: VMAF)
    monkeypatch.setattr(quality, "search", lambda *a, **k: QualityPlan(
        False, "quality search failed: wrote no frames", error=True,
        metric="vmaf"))
    result, info = check_file(str(path), settings)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("shrink", "unmeasured"))
    assert not out["ok"]

    stored = db.q1("SELECT shrink_attempts, shrink_skipped FROM files WHERE path=?",
                   (str(path),))
    assert stored["shrink_attempts"] == 1, "the attempt has to be counted"
    assert stored["shrink_skipped"] is None, \
        "a tooling failure is not a verdict on the file"

    from unfuckarr.service import Service
    db.ex("UPDATE files SET status='unmeasured' WHERE path=?", (str(path),))
    db.ex("INSERT INTO findings (path, category, code, severity, created) "
          "VALUES (?,?,?,?,?)", (str(path), "efficiency", "not_measured", "info", 1.0))
    assert Service._next_shrink_candidate() is None, \
        "the worker would pick it straight back up and fail identically"
