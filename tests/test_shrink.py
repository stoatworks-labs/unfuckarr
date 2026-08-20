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
