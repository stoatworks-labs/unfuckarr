"""Recycle bin.

Remediation runs unattended, so a wrong call must be undoable. Deletes move the
file into a dated bin and record it; a retention sweep clears the bin later. Set
``recycle_bin_days`` to 0 to unlink immediately instead.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

DEFAULT_BIN = Path(os.environ.get("UNFUCKARR_CONFIG_DIR", "/config")) / "recycle"


def bin_path(configured: str) -> Path:
    return Path(configured) if configured else DEFAULT_BIN


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


def sweep(days: int) -> int:
    """Delete bin entries older than the retention window. Returns the count."""
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
    if removed:
        db.log("recycle_swept", "info", detail={"removed": removed, "days": days})
    return removed


def usage(configured_bin: str) -> dict[str, int]:
    rows = db.q("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM recycle "
                "WHERE stored != ''")
    row = rows[0] if rows else None
    return {"count": row["c"] if row else 0, "bytes": row["s"] if row else 0}
