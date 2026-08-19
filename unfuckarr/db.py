"""SQLite state: file inventory, findings, jobs, activity log.

One connection per thread (SQLite objects are not shareable across threads and
the scan/transcode workers are threads). WAL keeps the web UI readable while a
scan is writing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DB_DIR = Path(os.environ.get("UNFUCKARR_CONFIG_DIR", "/config"))
DB_PATH = DB_DIR / "unfuckarr.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    library         TEXT,
    source          TEXT,            -- sonarr | radarr | folder | watch
    arr_id          INTEGER,         -- movieFile / episodeFile id
    arr_parent_id   INTEGER,         -- movie / series id
    arr_episode_ids TEXT,            -- JSON list, sonarr only
    title           TEXT,
    size            INTEGER,
    mtime           REAL,
    expected_runtime INTEGER,        -- seconds, from the *arr
    status          TEXT DEFAULT 'unknown',   -- unknown|ok|corrupt|incompatible|hygiene|missing|error
    last_checked    REAL,
    last_result     TEXT,            -- JSON CheckResult
    probe           TEXT,            -- JSON ffprobe summary
    checked_signature TEXT,          -- size:mtime at last clean pass
    -- How many times we have transcoded this file and it still failed
    -- afterwards. Guards against transcoding the same file for ever.
    fix_attempts    INTEGER DEFAULT 0,
    -- Space-saving shrink state. `shrunk` and `shrink_skipped` are both
    -- permanent: a file that has been shrunk must never be shrunk again
    -- (that is a second generation of loss for a fraction of the saving),
    -- and a file the search has already decided is not worth shrinking must
    -- not have hours of CPU spent re-deciding it on every scan.
    shrunk          REAL,             -- when it was shrunk
    shrunk_from     INTEGER,          -- size before, so the saving is reportable
    shrink_score    REAL,             -- measured quality of the result
    shrink_metric   TEXT,             -- vmaf | ssim — which number that is
    shrink_skipped  TEXT,             -- why we will not try this file again
    shrink_attempts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_status  ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_library ON files(library);

CREATE TABLE IF NOT EXISTS findings (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL,
    category TEXT NOT NULL,          -- integrity | compat | hygiene | emby
    code     TEXT NOT NULL,
    severity TEXT NOT NULL,          -- error | warning | info
    detail   TEXT,
    created  REAL NOT NULL,
    resolved REAL
);
CREATE INDEX IF NOT EXISTS idx_findings_path ON findings(path);
CREATE INDEX IF NOT EXISTS idx_findings_open ON findings(resolved);

CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,         -- scan | probe | transcode | redownload | delete
    path      TEXT,
    state     TEXT NOT NULL,         -- queued | running | done | failed | cancelled
    progress  REAL DEFAULT 0,
    message   TEXT,
    payload   TEXT,
    created   REAL NOT NULL,
    started   REAL,
    finished  REAL,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created DESC);

CREATE TABLE IF NOT EXISTS activity (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL,
    event    TEXT NOT NULL,
    path     TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts DESC);

CREATE TABLE IF NOT EXISTS scans (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   REAL NOT NULL,
    finished  REAL,
    trigger   TEXT,
    total     INTEGER DEFAULT 0,
    checked   INTEGER DEFAULT 0,
    ok        INTEGER DEFAULT 0,
    failed    INTEGER DEFAULT 0,
    actions   INTEGER DEFAULT 0,
    aborted   TEXT
);

CREATE TABLE IF NOT EXISTS recycle (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    original  TEXT NOT NULL,
    stored    TEXT NOT NULL,
    size      INTEGER,
    deleted   REAL NOT NULL,
    reason    TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


# Columns added after 1.0.0. `CREATE TABLE IF NOT EXISTS` will not add them to
# a database that already exists, so they are applied by hand.
MIGRATIONS = [
    ("files", "fix_attempts", "INTEGER DEFAULT 0"),
    ("files", "shrunk", "REAL"),
    ("files", "shrunk_from", "INTEGER"),
    ("files", "shrink_score", "REAL"),
    ("files", "shrink_metric", "TEXT"),
    ("files", "shrink_skipped", "TEXT"),
    ("files", "shrink_attempts", "INTEGER DEFAULT 0"),
]


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    for table, column, spec in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    conn.commit()


def reset_for_tests(path: Path) -> None:
    """Point the module at a scratch database. Tests only."""
    global DB_PATH, DB_DIR
    DB_PATH = path
    DB_DIR = path.parent
    if getattr(_local, "conn", None) is not None:
        _local.conn.close()
        _local.conn = None
    init()


def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def ex(sql: str, params: Iterable[Any] = ()) -> int:
    conn = connect()
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur.lastrowid or 0


def log(event: str, level: str = "info", path: str | None = None,
        detail: Any = None) -> None:
    ex(
        "INSERT INTO activity (ts, level, event, path, detail) VALUES (?,?,?,?,?)",
        (time.time(), level, event, path,
         detail if isinstance(detail, str) or detail is None else json.dumps(detail)),
    )


def prune(activity_keep: int = 5000, jobs_keep: int = 2000) -> None:
    """Keep the tables bounded — a nightly sweep of a big library writes a lot."""
    ex("DELETE FROM activity WHERE id NOT IN "
       "(SELECT id FROM activity ORDER BY ts DESC LIMIT ?)", (activity_keep,))
    ex("DELETE FROM jobs WHERE state IN ('done','failed','cancelled') AND id NOT IN "
       "(SELECT id FROM jobs WHERE state IN ('done','failed','cancelled') "
       "ORDER BY created DESC LIMIT ?)", (jobs_keep,))
