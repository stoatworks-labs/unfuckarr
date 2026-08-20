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


def store(path: str, reason: str, configured_bin: str, days: int) -> str | None:
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
        if not dated.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dated.name):
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
    return {
        "count": row["c"] if row else 0,
        "bytes": row["s"] if row else 0,
        "path": str(path),
        "configured": bool(configured_bin),
        "writable": os.access(probe, os.W_OK),
        "free": free,
    }
