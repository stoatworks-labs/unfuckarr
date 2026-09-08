"""The ffmpeg runner: where progress comes from, and when a job is dead.

None of this needs ffmpeg. The child is a Python script that behaves like
ffmpeg 8 on a Blu-ray remux with a forced subtitle track — it prints a
progress counter that never moves — and, standing in for the kernel, writes
its own ``/proc/<pid>/fd`` and ``fdinfo`` entries into a directory the runner
is pointed at.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from unfuckarr import transcode

# Argv: root ticks step_bytes start_pos -i source. Prints an out_time that is
# stuck at 121,997,000 µs (the real number from the Pirates job) on every
# tick, while the read position it reports advances by step_bytes a tick.
FAKE_FFMPEG = """
import os, sys, time
root, ticks, step, pos = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
source = sys.argv[-1]
me = os.path.join(root, str(os.getpid()))
os.makedirs(os.path.join(me, "fd"))
os.makedirs(os.path.join(me, "fdinfo"))
os.symlink(os.devnull, os.path.join(me, "fd", "0"))
with open(os.path.join(me, "fdinfo", "0"), "w") as fh:
    fh.write("pos:\\t999999\\nflags:\\t02\\n")
os.symlink(os.path.realpath(source), os.path.join(me, "fd", "3"))
for _ in range(ticks):
    with open(os.path.join(me, "fdinfo", "3"), "w") as fh:
        fh.write("pos:\\t%d\\nflags:\\t0100000\\nmnt_id:\\t29\\nino:\\t7\\n" % pos)
    sys.stdout.write("frame=1\\nout_time_ms=121997000\\nprogress=continue\\n")
    sys.stdout.flush()
    time.sleep(0.1)
    pos += step
sys.stdout.write("progress=end\\n")
sys.stdout.flush()
"""

# Argv: ticks out_time_step. No /proc entries at all — a Mac, or a kernel
# that will not say — printing a counter that moves by out_time_step a tick.
COUNTER_ONLY = """
import sys, time
ticks, step = int(sys.argv[1]), int(sys.argv[2])
t = 5_000_000
for _ in range(ticks):
    sys.stdout.write("out_time_ms=%d\\nprogress=continue\\n" % t)
    sys.stdout.flush()
    time.sleep(0.1)
    t += step
"""

SIZE = 100_000


def fixture(tmp_path: Path, script: str):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"\0" * SIZE)
    path = tmp_path / "fake.py"
    path.write_text(script)
    root = tmp_path / "proc"
    root.mkdir()
    return src, path, root


def fake_proc(tmp_path: Path, pid: int, source: Path, pos: int) -> Path:
    root = tmp_path / "proc"
    me = root / str(pid)
    (me / "fd").mkdir(parents=True)
    (me / "fdinfo").mkdir()
    (me / "fd" / "0").symlink_to(os.devnull)
    (me / "fdinfo" / "0").write_text("pos:\t999999\nflags:\t02\n")
    (me / "fd" / "3").symlink_to(os.path.realpath(source))
    (me / "fdinfo" / "3").write_text(
        f"pos:\t{pos}\nflags:\t0100000\nmnt_id:\t29\nino:\t7\n")
    return root


# -- reading the position -------------------------------------------------

def test_source_of_is_the_input_after_the_last_dash_i():
    cmd = ["ffmpeg", "-hide_banner", "-i", "/media/a.mkv", "-map", "0", "out.mkv"]
    assert transcode.source_of(cmd) == "/media/a.mkv"
    assert transcode.source_of(["ffmpeg", "-i", "a", "-i", "b", "out"]) == "b"
    assert transcode.source_of(["ffmpeg", "out.mkv"]) is None
    assert transcode.source_of(["ffmpeg", "-i"]) is None


def test_position_is_read_from_the_fd_that_points_at_the_source(tmp_path):
    """Pick the source's fd by what it links to, not by number; then read
    ``pos`` from the matching fdinfo entry and nothing else's."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"\0" * SIZE)
    root = fake_proc(tmp_path, 4242, src, 1234)

    p = transcode.SourcePosition(4242, str(src), proc_root=str(root))
    assert p.usable
    assert p.size == SIZE
    assert p.read() == 1234

    # The fd is cached; a new pos on it is still seen.
    (root / "4242" / "fdinfo" / "3").write_text("pos:\t86000\nflags:\t0100000\n")
    assert p.read() == 86000

    # ...and if the cached fd stops pointing at the source, rescan.
    (root / "4242" / "fd" / "3").unlink()
    (root / "4242" / "fd" / "3").symlink_to(os.devnull)
    (root / "4242" / "fd" / "5").symlink_to(os.path.realpath(src))
    (root / "4242" / "fdinfo" / "5").write_text("pos:\t99000\n")
    assert p.read() == 99000


def test_position_is_none_when_the_kernel_cannot_say(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"\0" * SIZE)
    # No /proc at all (a Mac).
    assert transcode.SourcePosition(1, str(src), str(tmp_path / "nope")).read() is None
    # A process that has not opened the source (yet, or ever).
    root = fake_proc(tmp_path, 7, tmp_path / "other.mkv", 5)
    assert transcode.SourcePosition(7, str(src), str(root)).read() is None
    # No source to measure against.
    assert not transcode.SourcePosition(7, None, str(root)).usable
    assert transcode.SourcePosition(7, None, str(root)).read() is None
    # A source that is not there.
    assert not transcode.SourcePosition(7, str(tmp_path / "gone"), str(root)).usable


# -- progress -------------------------------------------------------------

def test_progress_is_the_read_position_not_the_frozen_counter(tmp_path):
    """The Pirates job: out_time stuck at 122 s of an 8,000 s film (1.5%)
    while ffmpeg was 86% of the way through the file. The bar must say 86."""
    src, script, root = fixture(tmp_path, FAKE_FFMPEG)
    seen: list[tuple[float, float | None]] = []
    cmd = [sys.executable, str(script), str(root), "3", "0",
           str(int(SIZE * 0.86)), "-i", str(src)]
    ok, message = transcode.run(
        cmd, total_duration=8000.0, on_progress=lambda f, e: seen.append((f, e)),
        stall_timeout=5, poll_interval=0.05, proc_root=str(root))
    assert ok, message
    assert seen, "no progress reported"
    fracs = [f for f, _ in seen]
    assert all(abs(f - 0.86) < 0.001 for f in fracs), fracs
    assert all(e is not None and e >= 0 for _, e in seen)


def test_progress_falls_back_to_the_counter_without_proc(tmp_path):
    """Where there is no fdinfo, out_time is still better than nothing."""
    src, script, root = fixture(tmp_path, COUNTER_ONLY)
    seen: list[float] = []
    ok, message = transcode.run(
        [sys.executable, str(script), "4", "1000000"], total_duration=100.0,
        on_progress=lambda f, e: seen.append(f),
        stall_timeout=5, poll_interval=0.05, proc_root=str(root))
    assert ok, message
    assert seen[0] == 0.05
    assert seen == sorted(seen) and seen[-1] > seen[0]


# -- the stall detector ---------------------------------------------------

def test_a_silent_hung_child_is_killed(tmp_path):
    """The old loop checked the timeout only after ``readline()`` returned,
    and a blocking readline returns only at EOF: a hung ffmpeg held the
    worker for ever. Now the check runs on a timer."""
    started = time.time()
    ok, message = transcode.run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        total_duration=100.0, stall_timeout=1, poll_interval=0.05,
        proc_root=str(tmp_path / "noproc"))
    assert (ok, message) == (False, "no progress for 1s")
    assert time.time() - started < 10


def test_a_child_printing_a_frozen_counter_is_killed(tmp_path):
    """Lines arriving is not progress. ffmpeg 8 prints a stuck out_time every
    stats period for the whole job, and a job whose counter has not moved
    for the timeout — with nothing better to go on — is dead."""
    src, script, root = fixture(tmp_path, COUNTER_ONLY)
    started = time.time()
    ok, message = transcode.run(
        [sys.executable, str(script), "600", "0"], total_duration=100.0,
        stall_timeout=1, poll_interval=0.05, proc_root=str(root))
    assert (ok, message) == (False, "no progress for 1s")
    assert time.time() - started < 10


def test_a_frozen_counter_with_an_advancing_read_position_is_alive(tmp_path):
    """The same stuck counter, but the kernel says the file is being read:
    that is a healthy job and must run to the end."""
    src, script, root = fixture(tmp_path, FAKE_FFMPEG)
    seen: list[float] = []
    cmd = [sys.executable, str(script), str(root), "20", str(SIZE // 25), "0",
           "-i", str(src)]
    ok, message = transcode.run(
        cmd, total_duration=8000.0, on_progress=lambda f, e: seen.append(f),
        stall_timeout=1, poll_interval=0.05, proc_root=str(root))
    assert ok, message
    assert seen == sorted(seen) and seen[-1] > 0.5, seen


def test_cancel_reaches_a_silent_child(tmp_path):
    """Cancel had the same blind spot: it was only looked at between lines."""
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.time()
    ok, message = transcode.run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        total_duration=100.0, stall_timeout=30, poll_interval=0.05,
        cancel=cancel, proc_root=str(tmp_path / "noproc"))
    assert (ok, message) == (False, "cancelled")
    assert time.time() - started < 10
