"""Stopping a scan, restarting one, and the two phases it reports.

A scan is a checking pass (minutes to hours) followed by a repairing pass that
takes as long as the actions do — on a library with a backlog, weeks. Live,
one sat at "7,533 of 7,533 checked" for six days while it worked through its
repairs, nothing said so, and nothing could stop it. These tests pin what a
stop costs in each phase and that the UI is told which phase it is looking at.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from unfuckarr import db, transcode
from unfuckarr.checks import CheckResult, Finding
from unfuckarr.remediation import Decision, Remediator
from unfuckarr.scanner import Scanner, check_file
from unfuckarr.state import ScanProgress, set_task, state

from .conftest import needs_ffmpeg


def _corrupt() -> CheckResult:
    r = CheckResult(path="/x")
    r.add(Finding("integrity", "zero_length", "error", ""))
    return r


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- the checking pass ----------------------------------------------------

def test_a_stop_during_checking_discards_the_probe_queue(settings, monkeypatch):
    """Every probe is submitted up front. A stop must drop the ones not yet
    started, not drain them — with two workers and a library of thousands,
    draining is the rest of the day."""
    paths = [f"/media/{i:03}.mkv" for i in range(40)]
    for p in paths:
        db.ex("INSERT INTO files (path, library, source, title) VALUES (?,?,?,?)",
              (p, "Films", "folder", p))

    probed: list[str] = []
    started = threading.Event()

    def slow_probe(self, row, s, emby):
        started.set()
        time.sleep(0.25)
        probed.append(row["path"])
        record = {k: row[k] for k in row.keys()}
        return record, CheckResult(path=row["path"]), None

    monkeypatch.setattr(Scanner, "_check_one", slow_probe)
    scanner = Scanner(lambda: settings, Remediator(lambda: settings))

    outcome: dict = {}
    t = threading.Thread(target=lambda: outcome.update(scanner.run(paths=paths)))
    t.start()
    assert started.wait(5)
    assert state.scan.phase == "checking"
    scanner.request_stop()
    assert state.scan.stopping, "the UI is told a stop is under way"
    t.join(timeout=5)
    assert not t.is_alive(), "the scan must let go within a probe or two"

    assert outcome["aborted"] == "stopped by user"
    assert len(probed) < 8, f"{len(probed)} probes ran after the stop"
    assert state.scan.running is False and state.scan.stopping is False
    row = db.q1("SELECT aborted FROM scans ORDER BY id DESC LIMIT 1")
    assert row["aborted"] == "stopped by user"
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE event='scan_stopped'")["n"] == 1


def test_the_checking_pass_clears_its_task_before_repairing(settings, monkeypatch):
    """The per-probe task read "7530/7533" for the whole of a six-day repair
    phase, because nothing cleared it until the scan ended."""
    db.ex("INSERT INTO files (path, library, source, title) VALUES (?,?,?,?)",
          ("/media/a.mkv", "Films", "folder", "a"))

    def probe_stub(self, row, s, emby):
        # What the real one does: report itself as the current task.
        set_task("scan", kind="probing", path=row["path"], detail="1/1")
        return {k: row[k] for k in row.keys()}, CheckResult(path=row["path"]), None
    monkeypatch.setattr(Scanner, "_check_one", probe_stub)
    seen: list[bool] = []
    scanner = Scanner(lambda: settings, Remediator(lambda: settings))
    monkeypatch.setattr(scanner, "_remediate",
                        lambda *a, **k: seen.append("scan" in state.tasks) or {})
    scanner.run(paths=["/media/a.mkv"])
    assert seen == [False]


# -- the repairing pass ---------------------------------------------------

def test_repairing_reports_its_own_progress(settings, monkeypatch):
    """`checked/total` is the first pass only. The second has its own pair,
    so a bar cannot sit at 100% while the scan has weeks of work left."""
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply", lambda *a, **k: {"ok": True})
    scanner = Scanner(lambda: settings, rem)
    settings.policy.abort_if_failure_ratio_over = 1.0

    phases: list[tuple[str, int, int]] = []
    orig_apply = rem.apply

    def watching_apply(*a, **k):
        phases.append((state.scan.phase, state.scan.position, state.scan.pending))
        return orig_apply(*a, **k)
    monkeypatch.setattr(rem, "apply", watching_apply)

    state.scan = ScanProgress(running=True, checked=10, total=10)
    pending = [({"path": f"/media/{i}.mkv"}, _corrupt(), None,
                Decision("transcode", "bad")) for i in range(4)]
    scanner._remediate(settings, pending, population=100)

    assert phases == [("repairing", 1, 4), ("repairing", 2, 4),
                      ("repairing", 3, 4), ("repairing", 4, 4)]
    assert state.scan.phase == "finishing"
    assert state.scan.actions == 4


def test_a_stop_during_repairing_ends_the_job_in_flight(settings, monkeypatch):
    """The loop always checked the flag between jobs; it never reached the
    job under way. A transcode is minutes and a shrink an hour or more, so a
    stop that waits for it is a stop that appears not to work — that is the
    Aug 27 note, and it was right about the symptom and wrong about why."""
    rem = Remediator(lambda: settings)
    running = threading.Event()
    cancelled: list[bool] = []

    def blocking_apply(row, result, info, decision, cancel=None):
        running.set()
        cancelled.append(cancel.wait(10))
        return {"action": "flag", "ok": False, "message": "cancelled"}
    monkeypatch.setattr(rem, "apply", blocking_apply)

    scanner = Scanner(lambda: settings, rem)
    settings.policy.abort_if_failure_ratio_over = 1.0
    state.scan = ScanProgress(running=True, checked=10, total=10)
    pending = [({"path": f"/media/{i}.mkv"}, _corrupt(), None,
                Decision("transcode", "bad")) for i in range(5)]

    out: dict = {}
    t = threading.Thread(
        target=lambda: out.update(scanner._remediate(settings, pending, 100)))
    t.start()
    assert running.wait(5)
    scanner.request_stop()
    t.join(timeout=5)
    assert not t.is_alive(), "the stop must reach the job in flight"

    assert cancelled == [True], "exactly one job ran, and it was cancelled"
    assert out["aborted"] == "stopped by user"
    assert state.scan.position == 1 and state.scan.pending == 5
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE event='scan_finished'")["n"] == 0


def test_a_stop_that_lands_before_the_job_starts_still_stops(settings, monkeypatch):
    """The window between "not stopped" and the job's cancel event existing is
    closed by re-checking after the event is in place."""
    rem = Remediator(lambda: settings)
    scanner = Scanner(lambda: settings, rem)
    applied: list[str] = []

    def apply(row, result, info, decision, cancel=None):
        applied.append(row["path"])
        # The stop arrives while this job runs — the next must not start.
        scanner.request_stop()
        return {"ok": True}
    monkeypatch.setattr(rem, "apply", apply)
    settings.policy.abort_if_failure_ratio_over = 1.0
    state.scan = ScanProgress(running=True, checked=10, total=10)
    pending = [({"path": f"/media/{i}.mkv"}, _corrupt(), None,
                Decision("transcode", "bad")) for i in range(5)]
    out = scanner._remediate(settings, pending, population=100)
    assert applied == ["/media/0.mkv"]
    assert out["aborted"] == "stopped by user"


# -- the transcode slot ---------------------------------------------------

def test_a_job_waiting_for_a_slot_is_cancelled_without_waiting_for_the_holder(
        settings):
    """With one slot shared with the continuous shrink worker, a scan's job
    routinely queues behind an encode with an hour to run. Its cancel must
    not wait for that encode."""
    rem = Remediator(lambda: settings)
    sema = threading.Semaphore(1)
    assert sema.acquire(blocking=False), "the test holds the only slot"
    cancel = threading.Event()
    got: list[bool] = []

    def waiter() -> None:
        with rem._slot(sema, cancel) as ok:
            got.append(ok)
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.3)
    assert not got, "it is still waiting for the slot"
    cancel.set()
    t.join(timeout=3)
    assert got == [False]
    assert not sema.acquire(blocking=False), "the holder still holds it"
    sema.release()


def test_a_slot_is_released_after_the_body(settings):
    rem = Remediator(lambda: settings)
    sema = threading.Semaphore(1)
    with rem._slot(sema, threading.Event()) as ok:
        assert ok is True
        assert not sema.acquire(blocking=False)
    assert sema.acquire(blocking=False)


@needs_ffmpeg
def test_a_cancelled_repair_is_not_a_failed_repair(video_factory, settings,
                                                   monkeypatch):
    """A failed repair falls through to a redownload, and a cancel used to
    take that path too: stopping a scan mid-remux would have deleted and
    re-searched the very file being repaired."""
    path = video_factory("stopme.mkv", seconds=8)
    row = {"path": str(path), "source": "folder", "title": "Stop me",
           "arr_id": None, "arr_parent_id": None}
    db.ex("INSERT INTO files (path) VALUES (?)", (str(path),))
    result, info = check_file(str(path), settings)
    result.add(Finding("integrity", "decode_errors", "error", ""))
    settings.policy.corrupt_action = "redownload"

    def killed_run(cmd, duration, *a, cancel=None, **k):
        cancel.set()                     # what the scanner's stop does
        return False, "cancelled"
    monkeypatch.setattr(transcode, "run", killed_run)

    rem = Remediator(lambda: settings)
    out = rem.apply(row, result, info, Decision("repair", "container damage"))

    assert out["message"] == "cancelled" and out["action"] != "redownload"
    assert path.exists(), "a cancel must leave the file exactly as it was"
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 0
    assert db.q1("SELECT state FROM jobs ORDER BY id DESC LIMIT 1")["state"] == "cancelled"
    assert not (db.q1("SELECT fix_attempts FROM files WHERE path=?",
                      (str(path),))["fix_attempts"] or 0)


# -- the service ----------------------------------------------------------

@pytest.fixture
def client(monkeypatch, settings):
    # As in test_api.py: the routes without the lifespan's threads.
    from unfuckarr import api as api_mod
    from unfuckarr.service import service
    monkeypatch.setattr(service, "start", lambda: None)
    monkeypatch.setattr(service, "stop", lambda: None)
    with TestClient(api_mod.app) as c:
        yield c


def test_interrupted_scans_are_closed_at_startup(settings):
    """A scan the restart killed never writes its finish. Left open it reads
    as a scan that never happened, and the schedule counts from the last one
    that completed — nineteen days and eight scans earlier, live."""
    from unfuckarr.service import Service

    db.ex("INSERT INTO scans (started, trigger, finished) VALUES (?,?,?)",
          (1000.0, "manual", 2000.0))
    for started in (3000.0, 4000.0):
        db.ex("INSERT INTO scans (started, trigger) VALUES (?,?)",
              (started, "scheduled"))

    svc = Service()
    before = time.time()
    svc._reconcile_interrupted_work()
    svc._restore_last_scan()

    rows = db.q("SELECT started, finished, aborted FROM scans ORDER BY id")
    assert rows[0]["aborted"] is None and rows[0]["finished"] == 2000.0
    for r in rows[1:]:
        assert r["aborted"] == "interrupted by a restart"
        assert r["finished"] >= before
    assert state.last_scan_finished >= before


def test_stop_and_restart_over_the_api(client, monkeypatch):
    """Stop with nothing running says so; restart with nothing running is a
    start; restart while running queues a start behind the stop."""
    from unfuckarr.service import service

    state.scan = ScanProgress()
    assert client.post("/api/scan/stop").json() == {"stopping": False}

    # Restart with nothing running: an ordinary start.
    launched: list[str] = []
    monkeypatch.setattr(service, "_launch", lambda trigger, paths: (
        launched.append(trigger), service._scan_lock.release()))
    r = client.post("/api/scan/restart").json()
    assert r == {"restarting": False, "started": True}
    assert launched == ["manual"]

    # Restart while a scan runs: the running one is asked to stop and the
    # new one starts the moment the lock is free.
    stops: list[bool] = []
    monkeypatch.setattr(service.scanner, "request_stop", lambda: stops.append(True))
    assert service._scan_lock.acquire(blocking=False)
    state.scan = ScanProgress(running=True)
    r = client.post("/api/scan/restart").json()
    assert r == {"restarting": True, "started": False}
    assert stops == [True]
    assert launched == ["manual"], "not yet — the running scan has not let go"
    service._scan_lock.release()              # the running scan ends
    assert _wait_for(lambda: launched == ["manual", "manual"])

    # A plain stop while the restart is waiting withdraws it.
    assert service._scan_lock.acquire(blocking=False)
    state.scan = ScanProgress(running=True)
    client.post("/api/scan/restart")
    assert client.post("/api/scan/stop").json() == {"stopping": True}
    service._scan_lock.release()
    time.sleep(0.3)
    assert launched == ["manual", "manual"], "the withdrawn restart did not fire"
    assert service._scan_lock.acquire(blocking=False), "and it let the lock go"
    service._scan_lock.release()
