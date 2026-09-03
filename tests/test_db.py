"""The SQLite layer, and what a lost race with a writer leaves behind."""

from __future__ import annotations

import sqlite3
import time

import pytest

from unfuckarr import db


def _blocker() -> sqlite3.Connection:
    """A second connection holding the write lock, as a running scan does."""
    conn = sqlite3.connect(db.DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=100")
    conn.execute("BEGIN EXCLUSIVE")
    return conn


def test_a_failed_write_does_not_leave_the_transaction_open():
    """The regression, measured on the live instance 2026-09-03.

    Connections are per-thread and long-lived, and FastAPI reuses its worker
    threads — so a transient `database is locked` that leaves Python's
    implicit transaction open hands that thread a poisoned connection for
    every later request. What holds the lock long enough to matter is a scan
    starting: `sync_inventory` rewrites the whole library in one go.
    """
    conn = db.connect()
    conn.execute("PRAGMA busy_timeout=100")
    blocker = _blocker()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.log("blocked", "info", None, "x")
        assert not conn.in_transaction, (
            "a failed write must roll back — otherwise this thread's "
            "connection carries an open transaction into the next request")
    finally:
        blocker.rollback()
        blocker.close()
        conn.execute("PRAGMA busy_timeout=30000")


def test_writes_work_again_once_the_lock_clears():
    conn = db.connect()
    conn.execute("PRAGMA busy_timeout=100")
    blocker = _blocker()
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.log("blocked", "info", None, "x")
    finally:
        blocker.rollback()
        blocker.close()
        conn.execute("PRAGMA busy_timeout=30000")

    db.log("after", "info", None, "must succeed")
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE event='after'")["n"] == 1


def test_ex_still_returns_the_new_row_id():
    first = db.ex("INSERT INTO activity (ts, level, event) VALUES (?,?,?)",
                  (time.time(), "info", "a"))
    second = db.ex("INSERT INTO activity (ts, level, event) VALUES (?,?,?)",
                   (time.time(), "info", "b"))
    assert second == first + 1


def test_an_intake_pass_survives_a_locked_activity_log(settings, monkeypatch):
    """The pass had already fetched the queue, classified it and written its
    verdicts; losing all of that because the *summary line* could not be
    written turned a completed pass into an HTTP 500 on the live instance."""
    from unfuckarr.intake import IntakeWatcher

    settings.intake.enabled = True
    watcher = IntakeWatcher(lambda: settings)

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "log", boom)

    result = watcher.run_pass()
    assert result is not None, "the pass must come back, not raise"
    assert any("activity log" in e for e in result.errors)
