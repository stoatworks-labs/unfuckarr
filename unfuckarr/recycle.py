"""Recycle bin.

Remediation runs unattended, so a wrong call must be undoable. Deletes move the
file into a dated bin and record it; a retention sweep clears the bin later. Set
``recycle_bin_days`` to 0 to unlink immediately instead.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

DEFAULT_BIN = Path(os.environ.get("UNFUCKARR_CONFIG_DIR", "/config")) / "recycle"


def bin_path(configured: str) -> Path:
    return Path(configured) if configured else DEFAULT_BIN


def same_filesystem(configured: str, sample: str) -> bool | None:
    """Whether a delete of ``sample`` would be a rename or a full copy.

    This is the difference between a 40 GB delete taking no time at all and it
    taking as long as writing 40 GB, twice over the array. The default bin
    lives under /config, which on Unraid is appdata on the cache — a different
    filesystem from the media share, so every recycled file is copied across,
    and a handful of remuxes fills the cache. Putting the bin *inside* the
    media mount makes the move a rename.

    None when it cannot be determined (either path missing).
    """
    try:
        target = bin_path(configured)
        while not target.exists() and target != target.parent:
            target = target.parent
        return os.stat(target).st_dev == os.stat(sample).st_dev
    except OSError:
        return None


DATED_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class BinEntry:
    """One file actually sitting in the bin."""

    path: Path
    size: int
    when: float                 # recycled-at: the row, else the dated folder
    mtime: float = 0.0          # tiebreaker within a day
    row_id: int | None = None   # None for a file the database has no row for

    @property
    def order(self) -> tuple[float, float]:
        return (self.when, self.mtime)


def contents(configured_bin: str) -> list[BinEntry]:
    """Everything in the bin, read from **disk** rather than the database.

    Disk, because the two disagree and the disk is the one holding the space.
    Live on 2026-09-03 the database reported 0 files and 0 bytes while a
    14.5 GB Blu-ray remux sat in `/media/.recycle/2026-09-03/` with no row —
    so a "total size" taken from `SUM(size)` would have read zero next to
    14.5 GB of real disk.

    Rows still matter: they carry the time the file was recycled, which is
    what "oldest" means, and the id needed to keep the table consistent when
    something is pruned. A file with no row falls back to its dated folder —
    see below for why not its mtime.

    Only dated directories are read, so anything else that ends up under the
    bin path is neither counted nor ever deleted.
    """
    root = bin_path(configured_bin)
    if not root.is_dir():
        return []
    rows = {r["stored"]: r for r in
            db.q("SELECT id, stored, deleted FROM recycle WHERE stored != ''")}
    out: list[BinEntry] = []
    try:
        dated = sorted(root.iterdir())
    except OSError as exc:
        log.warning("could not read the recycle bin at %s: %s", root, exc)
        return []
    for day in dated:
        if not day.is_dir() or not DATED_DIR.fullmatch(day.name):
            continue
        # The folder name is when the file was *recycled*, and for a file with
        # no row it is the only honest answer. Not its mtime: `shutil.move`
        # preserves the original's, so an orphan's mtime is when the video was
        # made — which put a film recycled today behind an episode recycled a
        # fortnight ago when the limit came to choose. Observed doing exactly
        # that, 2026-09-03, before this was written.
        try:
            day_start = time.mktime(time.strptime(day.name, "%Y-%m-%d"))
        except ValueError:
            continue
        try:
            entries = list(day.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            row = rows.get(str(entry))
            out.append(BinEntry(
                path=entry,
                size=stat.st_size,
                when=(row["deleted"] if row else day_start),
                mtime=stat.st_mtime,
                row_id=(row["id"] if row else None),
            ))
    return out


def _forget(entry: BinEntry) -> None:
    if entry.row_id is not None:
        db.ex("DELETE FROM recycle WHERE id = ?", (entry.row_id,))


def enforce_limit(max_bytes: int, configured_bin: str,
                  incoming: int = 0) -> tuple[int, int]:
    """Prune the oldest entries until the bin fits. Returns (files, bytes).

    ``incoming`` is the size of a file about to be added, so the room is made
    *before* the bin goes over rather than after — which is what "auto-prune
    if you are going to exceed that size" has to mean if the limit is to be a
    limit rather than a high-water mark.

    Oldest first, by the time the file was recycled — the row's timestamp, or
    the dated folder for a file that has no row. Orphans are pruned on exactly
    the same footing as tracked files: they occupy the same disk, and a limit
    that could only see half the bin would quietly stop being a limit.

    **A single file larger than the whole limit is still stored**, after the
    bin has been emptied for it. Refusing would turn a recoverable delete into
    a permanent one, which is the opposite of what a recycle bin is for, so
    the limit is exceeded and said so loudly instead.
    """
    if max_bytes <= 0:
        return 0, 0
    entries = contents(configured_bin)
    total = sum(e.size for e in entries) + incoming
    if total <= max_bytes:
        return 0, 0

    files = freed = 0
    for entry in sorted(entries, key=lambda e: e.order):
        if total <= max_bytes:
            break
        try:
            entry.path.unlink()
        except OSError as exc:
            log.warning("could not prune %s: %s", entry.path, exc)
            continue
        _forget(entry)
        total -= entry.size
        freed += entry.size
        files += 1
        db.log("recycle_pruned", "warn", str(entry.path), {
            "bytes": entry.size, "reason": "the recycle bin is at its size limit",
            "tracked": entry.row_id is not None,
        })

    _tidy_empty_dirs(configured_bin)
    if files:
        db.log("recycle_limit_enforced", "warn", detail={
            "files": files, "bytes": freed, "limit": max_bytes,
        })
    if total > max_bytes:
        # Only reachable when one file is bigger than the whole limit.
        log.warning("recycle bin is %.1f GB over its %.1f GB limit after "
                    "pruning everything it could",
                    (total - max_bytes) / 1e9, max_bytes / 1e9)
        db.log("recycle_limit_exceeded", "warn", detail={
            "over_bytes": total - max_bytes, "limit": max_bytes,
        })
    return files, freed


def _tidy_empty_dirs(configured_bin: str) -> None:
    root = bin_path(configured_bin)
    if not root.is_dir():
        return
    for day in list(root.iterdir()):
        if not day.is_dir() or not DATED_DIR.fullmatch(day.name):
            continue
        try:
            if not any(day.iterdir()):
                day.rmdir()
        except OSError:
            pass


def store(path: str, reason: str, configured_bin: str, days: int,
          max_bytes: int = 0) -> str | None:
    """Move ``path`` into the bin. Returns the stored path, or None if the
    file was unlinked outright (retention disabled) or was already gone."""
    src = Path(path)
    if not src.exists():
        return None
    size = src.stat().st_size

    if days <= 0:
        src.unlink()
        db.ex("INSERT INTO recycle (original, stored, size, deleted, reason) "
              "VALUES (?,?,?,?,?)", (path, "", size, time.time(), reason))
        db.log("file_deleted", "warn", path, {"reason": reason, "recycled": False})
        return None

    # Make room before the move, not after: a limit enforced only on the next
    # scheduler tick is a high-water mark, and the tick is 30 seconds away.
    enforce_limit(max_bytes, configured_bin, incoming=size)

    root = bin_path(configured_bin) / time.strftime("%Y-%m-%d")
    root.mkdir(parents=True, exist_ok=True)
    # Preserve enough of the original path to tell two "video.mkv"s apart.
    stem = "_".join(p for p in src.parts[-3:-1] if p not in ("/", ""))
    dest = root / (f"{stem}__{src.name}" if stem else src.name)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.stem}.{n}{dest.suffix}")
        n += 1

    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        # Bin on another filesystem and no room, most likely. Better to keep a
        # broken file than to half-delete it.
        log.error("could not recycle %s: %s", path, exc)
        db.log("recycle_failed", "error", path, str(exc))
        raise

    db.ex("INSERT INTO recycle (original, stored, size, deleted, reason) "
          "VALUES (?,?,?,?,?)", (path, str(dest), size, time.time(), reason))
    db.log("file_recycled", "warn", path, {"reason": reason, "stored": str(dest)})
    return str(dest)


def restore(recycle_id: int) -> str:
    row = db.q1("SELECT * FROM recycle WHERE id = ?", (recycle_id,))
    if row is None:
        raise FileNotFoundError(f"no recycle entry {recycle_id}")
    if not row["stored"]:
        raise FileNotFoundError("that file was deleted outright, not recycled")
    stored = Path(row["stored"])
    if not stored.exists():
        raise FileNotFoundError(f"{stored} is gone — retention already swept it")
    original = Path(row["original"])
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stored), str(original))
    db.ex("DELETE FROM recycle WHERE id = ?", (recycle_id,))
    db.log("file_restored", "info", str(original))
    return str(original)


def sweep(days: int, configured_bin: str = "") -> int:
    """Delete bin entries older than the retention window. Returns the count.

    ``configured_bin`` is only needed to find files the database has no row
    for; without it the orphan pass looks in the default bin.
    """
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for row in db.q("SELECT * FROM recycle WHERE deleted < ?", (cutoff,)):
        stored = row["stored"]
        if stored:
            try:
                p = Path(stored)
                if p.exists():
                    p.unlink()
                # Tidy the empty dated directory behind us.
                parent = p.parent
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError as exc:
                log.warning("could not purge %s: %s", stored, exc)
                continue
        db.ex("DELETE FROM recycle WHERE id = ?", (row["id"],))
        removed += 1
    removed += _sweep_orphans(configured_bin, cutoff)
    if removed:
        db.log("recycle_swept", "info", detail={"removed": removed, "days": days})
    return removed


def _sweep_orphans(configured_bin: str, cutoff: float) -> int:
    """Purge bin files the database has no row for.

    The sweep above walks the *database*, so a file whose row is gone — a
    database restored from a backup taken after the file was recycled, a row
    removed by hand — is never looked at again and sits in the bin for ever.
    Live, that was **139 GB**, of which 126 GB was three identical copies of
    one 42 GB disc image, recycled repeatedly by a loop that has since been
    fixed and then left behind when the rows went.

    Only dated directories are considered, and only files older than the
    retention window, so nothing that the sweep above is about to handle can
    be caught here by accident.
    """
    root = bin_path(configured_bin)
    if not root.is_dir():
        return 0
    known = {r["stored"] for r in db.q("SELECT stored FROM recycle WHERE stored != ''")}
    removed = 0
    for dated in sorted(root.iterdir()):
        # "2026-08-19" — anything else in there is not ours to delete.
        if not dated.is_dir() or not DATED_DIR.fullmatch(dated.name):
            continue
        for entry in list(dated.iterdir()):
            if not entry.is_file() or str(entry) in known:
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
                size = entry.stat().st_size
                entry.unlink()
            except OSError as exc:
                log.warning("could not purge orphaned %s: %s", entry, exc)
                continue
            db.log("recycle_orphan_purged", "warn", str(entry), {"bytes": size})
            removed += 1
        try:
            if dated.is_dir() and not any(dated.iterdir()):
                dated.rmdir()
        except OSError:
            pass
    return removed


def usage(configured_bin: str) -> dict[str, object]:
    rows = db.q("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM recycle "
                "WHERE stored != ''")
    row = rows[0] if rows else None
    path = bin_path(configured_bin)
    # The bin is created on first use, so "does not exist yet" is normal and
    # not worth alarming about. Whether the *parent* is writable is not: a bin
    # pointed at a share that was never mounted fails at the first delete, and
    # the first delete is the worst possible time to find out.
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = 0
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        pass
    # What is actually there, which is not the same number as the database's.
    # `bytes` is the honest one and is what the UI shows as the bin's size;
    # `tracked_bytes` is what can still be restored from the UI. They differ
    # whenever a row is lost — measured live on 2026-09-03 as 0 tracked bytes
    # against 14.5 GB on disk — and the difference is worth showing rather
    # than quietly reporting the smaller one.
    entries = contents(configured_bin)
    on_disk = sum(e.size for e in entries)
    orphaned = sum(e.size for e in entries if e.row_id is None)
    return {
        "count": len(entries),
        "bytes": on_disk,
        "tracked_count": row["c"] if row else 0,
        "tracked_bytes": row["s"] if row else 0,
        "orphan_count": sum(1 for e in entries if e.row_id is None),
        "orphan_bytes": orphaned,
        "path": str(path),
        "configured": bool(configured_bin),
        "writable": os.access(probe, os.W_OK),
        "free": free,
    }
