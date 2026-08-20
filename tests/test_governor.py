"""Pacing: holding our share of the GPU encode engine, and the worker.

The numbers here are calibrated against the target hardware (Radeon 880M,
gfx1150), measured 2026-08-19: a 4K HEVC encode running flat out reports
**958 ms of encode-engine time per wall-second**; SIGSTOP takes that to a true
**0** with the process in state T; SIGCONT returns it to **966 ms/s** and the
finished file is valid. Those three facts are what the whole design rests on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from unfuckarr import db, governor


# -- reading the counter --------------------------------------------------

def write_fdinfo(tmp_path, pid: int, entries: list[tuple[str, int, int]]) -> None:
    """Fake a /proc/<pid>/fdinfo directory. (fd, client_id, enc_ns) each."""
    d = tmp_path / str(pid) / "fdinfo"
    d.mkdir(parents=True, exist_ok=True)
    for fd, client, ns in entries:
        (d / str(fd)).write_text(
            "pos:\t0\n"
            "drm-driver:\tamdgpu\n"
            f"drm-client-id:\t{client}\n"
            "drm-memory-vram:\t68116 KiB\n"
            f"drm-engine-enc:\t{ns} ns\n")


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    real_listdir, real_open = os.listdir, open

    def listdir(path):
        if isinstance(path, str) and path.startswith("/proc/"):
            return real_listdir(str(tmp_path / path[len("/proc/"):]))
        return real_listdir(path)

    def opener(path, *a, **kw):
        if isinstance(path, str) and path.startswith("/proc/"):
            return real_open(str(tmp_path / path[len("/proc/"):]), *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(governor.os, "listdir", listdir)
    monkeypatch.setattr("builtins.open", opener)
    return tmp_path


def test_the_counter_is_read_from_fdinfo(fake_proc):
    write_fdinfo(fake_proc, 42, [(3, "40", 17_775_914_898)])
    assert governor.engine_ns(42) == 17_775_914_898


def test_duplicate_fds_of_one_client_are_not_counted_twice(fake_proc):
    """Several fds can refer to the same DRM client and each reports the same
    cumulative figure. Summing per fd reads as several hundred percent."""
    write_fdinfo(fake_proc, 43, [(3, "40", 1_000_000_000),
                                 (4, "40", 1_000_000_000)])
    assert governor.engine_ns(43) == 1_000_000_000


def test_separate_clients_are_added_up(fake_proc):
    write_fdinfo(fake_proc, 44, [(3, "40", 600_000_000),
                                 (5, "41", 400_000_000)])
    assert governor.engine_ns(44) == 1_000_000_000


def test_a_process_with_no_drm_fd_reports_nothing(fake_proc):
    (fake_proc / "45" / "fdinfo").mkdir(parents=True)
    (fake_proc / "45" / "fdinfo" / "3").write_text("pos:\t0\n")
    assert governor.engine_ns(45) is None
    assert governor.engine_ns(999999) is None


# -- the control loop -----------------------------------------------------

def test_a_disabled_target_governs_nothing():
    assert not governor.Governor(target=0).active
    assert not governor.Governor(target=1.0).active
    assert governor.Governor(target=0.5).active


def test_the_correction_converges_on_the_target():
    """Flat out measures ~0.96 on the real hardware, so holding 0.5 means
    running roughly half the time — but the controller must find that from
    what it measures rather than assuming the relationship is exact."""
    g = governor.Governor(target=0.5)
    g._on_fraction = 1.0
    # Model the hardware: engine share tracks the fraction of wall clock the
    # process is allowed to run, at the measured 96% saturation.
    for _ in range(25):
        g._measured = g._on_fraction * 0.96
        g._correct()
    assert 0.45 <= g._on_fraction * 0.96 <= 0.55, g._on_fraction


def test_a_measured_zero_opens_up_rather_than_clamping_shut():
    """If the counter says nothing is happening, the answer is to let the job
    run, not to throttle it further on the strength of a zero."""
    g = governor.Governor(target=0.5)
    g._on_fraction = 0.2
    g._measured = 0.0
    g._correct()
    assert g._on_fraction > 0.2


@pytest.mark.skipif(not hasattr(os, "kill"), reason="needs POSIX signals")
def test_a_governed_process_is_always_left_running(fake_proc):
    """A job cancelled while stopped would sit in state T for ever, holding a
    semaphore and looking exactly like a hang."""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(30)"])
    try:
        write_fdinfo(fake_proc, proc.pid, [(3, "1", 0)])
        stop = threading.Event()
        g = governor.Governor(target=0.5, period=0.2)
        t = threading.Thread(target=g.run, args=(proc.pid, stop), daemon=True)
        t.start()
        time.sleep(0.5)
        stop.set()
        t.join(timeout=5)

        # State T means stopped; anything else means it was left runnable.
        with open(f"/proc/{proc.pid}/stat") if os.path.exists(f"/proc/{proc.pid}/stat") \
                else open(os.devnull) as fh:
            content = fh.read()
        if content:
            assert content.split()[2] != "T", "left the process stopped"
    finally:
        proc.kill()
        proc.wait()


def test_a_software_encode_is_left_completely_alone(fake_proc, monkeypatch):
    """There is no encode engine to share, so throttling would be pure loss —
    and `nice` already handles being polite about the CPU."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (fake_proc / str(proc.pid) / "fdinfo").mkdir(parents=True)
        (fake_proc / str(proc.pid) / "fdinfo" / "3").write_text("pos:\t0\n")
        stop = threading.Event()
        g = governor.Governor(target=0.5, period=0.05)
        g.run(proc.pid, stop)          # returns on its own once it gives up
        assert g._inert, "should have stopped interfering"
        assert not g.active
    finally:
        proc.kill()
        proc.wait()


# -- picking the next candidate ------------------------------------------

def test_the_fattest_candidate_is_taken_first(settings):
    """The order decides which savings land this month and which land next
    year, so it is not incidental."""
    from unfuckarr.service import Service

    rows = [("/media/lean.mkv", 1.1, 8_000_000_000),
            ("/media/fat.mkv", 4.8, 40_000_000_000),
            ("/media/middling.mkv", 2.4, 20_000_000_000)]
    for path, priority, size in rows:
        db.ex("INSERT INTO files (path, status, size, shrink_priority) "
              "VALUES (?,?,?,?)", (path, "unmeasured", size, priority))

    assert Service._next_shrink_candidate()["path"] == "/media/fat.mkv"


def test_finished_and_written_off_files_are_not_picked_up_again(settings):
    from unfuckarr.service import Service

    db.ex("INSERT INTO files (path, status, size, shrink_priority, shrunk) "
          "VALUES (?,?,?,?,?)", ("/media/done.mkv", "unmeasured", 9, 9.0, 1.0))
    db.ex("INSERT INTO files (path, status, size, shrink_priority, shrink_skipped) "
          "VALUES (?,?,?,?,?)", ("/media/no.mkv", "unmeasured", 9, 8.0, "no saving"))
    db.ex("INSERT INTO files (path, status, size, shrink_priority, shrink_attempts) "
          "VALUES (?,?,?,?,?)", ("/media/failed.mkv", "unmeasured", 9, 7.0, 1))
    assert Service._next_shrink_candidate() is None

    db.ex("INSERT INTO files (path, status, size, shrink_priority) "
          "VALUES (?,?,?,?)", ("/media/next.mkv", "unmeasured", 9, 1.0))
    assert Service._next_shrink_candidate()["path"] == "/media/next.mkv"


def test_an_unpriced_row_sorts_behind_a_priced_one(settings):
    """NULLs sort last under DESC in SQLite, which is what we want: a row
    nothing has assessed should not jump the queue."""
    from unfuckarr.service import Service

    db.ex("INSERT INTO files (path, status, size) VALUES (?,?,?)",
          ("/media/unknown.mkv", "unmeasured", 50_000_000_000))
    db.ex("INSERT INTO files (path, status, size, shrink_priority) VALUES (?,?,?,?)",
          ("/media/priced.mkv", "unmeasured", 1_000_000_000, 0.1))
    assert Service._next_shrink_candidate()["path"] == "/media/priced.mkv"


def test_the_worker_stands_down_when_there_is_nothing_to_do(settings):
    from unfuckarr.remediation import shrink_blocked
    from unfuckarr.service import Service
    from unfuckarr.state import state

    assert Service._shrink_idle_reason(settings, shrink_blocked) is None

    settings.shrink.continuous = False
    assert Service._shrink_idle_reason(settings, shrink_blocked) is not None
    settings.shrink.continuous = True

    settings.policy.oversize_action = "flag"
    assert Service._shrink_idle_reason(settings, shrink_blocked) is not None
    settings.policy.oversize_action = "shrink"

    state.paused = True
    try:
        assert Service._shrink_idle_reason(settings, shrink_blocked) == "paused"
    finally:
        state.paused = False
