"""SQLite state: file inventory, findings, jobs, activity log.

One connection per thread (SQLite objects are not shareable across threads and
the scan/transcode workers are threads). WAL keeps the web UI readable while a
scan is writing.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

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
    shrink_attempts INTEGER DEFAULT 0,
    -- How far above the reference bitrate for its resolution this file sits.
    -- Only an ordering key: the continuous worker takes the fattest first,
    -- because the biggest savings should land first, not eventually.
    shrink_priority REAL,
    -- Disc-image conversion state, permanent for the same reason the shrink
    -- state is: a disc that has been converted no longer exists to convert,
    -- and a disc the selection has already refused (the title MakeMKV offers
    -- does not match the runtime, the image will not open) refuses for a
    -- reason that will still be true in the morning. A failure that is about
    -- the *installation* rather than the disc — no MakeMKV, an expired key —
    -- is never written here.
    converted       REAL,             -- when it became an mkv
    converted_from  TEXT,             -- the image path it replaced
    convert_skipped TEXT,             -- why we will not try this disc again
    convert_attempts INTEGER DEFAULT 0
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

-- Lifetime counters. Deliberately not derived from `jobs` or `activity`,
-- which `prune()` caps at 2,000 and 5,000 rows, nor from `recycle`, whose rows
-- go when retention sweeps them. A total that quietly resets after a busy week
-- is worse than no total: it reads as "nothing has happened here" precisely
-- when the most has.
CREATE TABLE IF NOT EXISTS totals (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

-- The download queue, as this application last saw it. One row per download,
-- keyed by the *client's* id rather than the queue row's: a queue id changes
-- when the *arr restarts, and `blocked_since` has to survive that or the
-- min-blocked timer resets every time Sonarr is updated.
--
-- Rows are kept after the item leaves the queue so that "we already acted on
-- this" is answerable, and so a verdict the user disagreed with is still
-- there to look at. `sweep_intake` ages them out.
CREATE TABLE IF NOT EXISTS intake (
    source        TEXT NOT NULL,      -- sonarr | radarr
    download_id   TEXT NOT NULL,      -- the download client's id/hash
    queue_id      INTEGER,
    title         TEXT,
    protocol      TEXT,
    indexer       TEXT,
    download_client TEXT,
    arr_parent_id INTEGER,
    arr_episode_ids TEXT,             -- JSON list
    output_path   TEXT,               -- as the *arr reports it
    local_path    TEXT,               -- after path mapping
    size          INTEGER,
    state         TEXT,               -- trackedDownloadState
    messages      TEXT,               -- JSON list of statusMessages
    verdict       TEXT,               -- working|manual|bad_release|unrecognised
    reason        TEXT,
    evidence      TEXT,               -- JSON: what opening the files found
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    blocked_since REAL,               -- when it first stopped making progress
    gone          REAL,               -- when it left the queue
    acted         REAL,               -- when we removed it; NULL if never
    outcome       TEXT,
    PRIMARY KEY (source, download_id)
);
CREATE INDEX IF NOT EXISTS idx_intake_verdict ON intake(verdict);
CREATE INDEX IF NOT EXISTS idx_intake_seen ON intake(last_seen DESC);

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
    ("files", "shrink_priority", "REAL"),
    ("files", "converted", "REAL"),
    ("files", "converted_from", "TEXT"),
    ("files", "convert_skipped", "TEXT"),
    ("files", "convert_attempts", "INTEGER DEFAULT 0"),
]


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    for table, column, spec in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    conn.commit()
    _backfill_totals(conn)


def _backfill_totals(conn: sqlite3.Connection) -> None:
    """Seed the shrink counters from work done before they existed.

    The counters are written as things happen, so on an install that has been
    running for months they start at zero — and a header reading "0 saved" on a
    system that has genuinely reclaimed terabytes is exactly the lie they were
    added to prevent. Measured on the live instance the day they shipped: 386
    files shrunk and 2.27 TB reclaimed, all of it reported as nothing.

    Only these two can be recovered, and only because `files.shrunk` and
    `files.shrunk_from` are per-file and permanent. Repairs, deletions and
    redownloads have no such record — `jobs` and `activity` are both pruned —
    so they start from zero and say so by being absent rather than wrong.

    Runs once: the guard is the counter's own existence, so a later run adds
    nothing and a counter reset is not silently undone.
    """
    seeded = conn.execute(
        "SELECT 1 FROM totals WHERE name = 'files_shrunk'").fetchone()
    if seeded:
        return
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(shrunk_from - size), 0) saved "
        "FROM files WHERE shrunk IS NOT NULL AND shrunk_from IS NOT NULL"
    ).fetchone()
    if not row or not row["n"]:
        return
    conn.execute("INSERT OR IGNORE INTO totals (name, value) VALUES ('files_shrunk', ?)",
                 (int(row["n"]),))
    conn.execute("INSERT INTO totals (name, value) VALUES ('bytes_saved', ?) "
                 "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
                 (int(row["saved"]),))
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
    """One write, committed.

    The rollback is the part that matters. A statement that fails — and the
    one that realistically fails here is a write that waited out
    `busy_timeout` against a lock somebody else is holding — leaves Python's
    implicit transaction **open** on this thread's connection
    (`conn.in_transaction` stays True; measured on the live instance
    2026-09-03). Connections are per-thread and long-lived, and FastAPI reuses
    its worker threads, so without this a single transient `database is
    locked` leaves that thread holding an open transaction against every
    later request that lands on it.

    What makes the lock get held that long in the first place is a scan
    starting: `scanner.sync_inventory` writes the whole library — 18,440 rows
    on the live instance — and an API write that arrives during it can wait
    out all 30 seconds and fail. That is worth fixing separately; this makes
    sure the failure costs one statement rather than a connection.
    """
    conn = connect()
    try:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid or 0
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - the original error is the useful one
            log.debug("rollback failed after a failed write", exc_info=True)
        raise


def bump(name: str, by: int = 1) -> None:
    """Add to a lifetime counter.

    Upsert rather than read-modify-write: two workers finishing at once is
    normal here (the scan thread and the shrink worker), and the read-modify
    version loses one of them.
    """
    if not by:
        return
    ex("INSERT INTO totals (name, value) VALUES (?, ?) "
       "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
       (name, int(by)))


def totals() -> dict[str, int]:
    return {r["name"]: r["value"] for r in q("SELECT name, value FROM totals")}


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
