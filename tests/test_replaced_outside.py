"""A file replaced by something other than unfuckarr starts with no history.

Rows in `files` are keyed by path, and an *arr upgrade imports the new file
under the same name, so the new file used to inherit everything recorded
against the old one: transcode attempts, shrink and conversion verdicts, the
research cooldown. Live, 2026-11-24: the genuine Cosmos (2014) remux replaced a
misfiled 1980 pack episode for episode. Six episodes were refused as "already
transcoded 3 time(s)", and two were taken for second goes at a fix that had
failed, so they were fully re-encoded to HEVC when all they needed was an AC-3
track added.
"""

from __future__ import annotations

import os
from pathlib import Path

from unfuckarr import db, recycle, scanner
from unfuckarr.checks import Finding
from unfuckarr.remediation import MAX_FIX_ATTEMPTS, Decision, Remediator
from unfuckarr.scanner import check_file, forget_replaced, persist_result

from .conftest import make_video, needs_ffmpeg

# Everything a pass records about the bytes rather than the path, set to
# values a long-lived file could really have accumulated.
HISTORY = {
    "fix_attempts": MAX_FIX_ATTEMPTS + 1,
    "shrunk": 1_700_000_000.0,
    "shrunk_from": 40_000_000_000,
    "shrink_score": 92.4,
    "shrink_metric": "vmaf",
    "shrink_skipped": "saving under the floor",
    "shrink_attempts": 1,
    "converted": 1_700_000_100.0,
    "converted_from": "/media/old.iso",
    "convert_skipped": "no title matches the runtime",
    "convert_attempts": 1,
    "last_research": 1_700_000_200.0,
    "research_attempts": 3,
}
CLEAN = {k: (0 if isinstance(v, int) and k.endswith(("attempts",)) else None)
         for k, v in HISTORY.items()}


def _row_with_history(path: Path, signature: str, arr_id: int = 101) -> dict:
    cols = ", ".join(HISTORY)
    marks = ", ".join("?" for _ in HISTORY)
    db.ex(f"INSERT INTO files (path, source, arr_id, title, status, last_checked, "
          f"checked_signature, {cols}) VALUES (?,?,?,?,?,?,?, {marks})",
          (str(path), "sonarr", arr_id, path.stem, "ok", 1.0, signature,
           *HISTORY.values()))
    return dict(db.q1("SELECT * FROM files WHERE path=?", (str(path),)))


def _signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime}"


def _history(path: Path) -> dict:
    row = db.q1("SELECT * FROM files WHERE path=?", (str(path),))
    return {k: row[k] for k in HISTORY}


def test_a_file_replaced_under_the_same_name_starts_with_no_history(tmp_path):
    path = tmp_path / "Cosmos (2014) - S01E04.mkv"
    path.write_bytes(b"1980 episode" * 1000)
    row = _row_with_history(path, _signature(path))

    # The *arr imports the genuine episode over it: same name, other bytes.
    path.write_bytes(b"2014 episode, a different length" * 1000)

    fresh = forget_replaced(row)

    assert {k: fresh[k] for k in HISTORY} == CLEAN
    assert _history(path) == CLEAN
    event = db.q1("SELECT * FROM activity WHERE event='file_replaced' AND path=?",
                  (str(path),))
    assert event is not None, "a forgotten history must say so in the log"
    assert "fix_attempts" in event["detail"]


def test_a_file_whose_size_has_not_changed_keeps_its_history(tmp_path):
    """A touched mtime is not a new file: Unraid's mover, a backup tool or a
    `touch` changes it and nothing else. Forgetting `shrunk` on that would
    shrink the file a second time, which invariant 11 forbids."""
    path = tmp_path / "Film (1999).mkv"
    path.write_bytes(b"x" * 4096)
    row = _row_with_history(path, _signature(path))
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 3600.5))

    kept = forget_replaced(row)

    assert {k: kept[k] for k in HISTORY} == HISTORY
    assert _history(path) == HISTORY


def test_a_file_never_checked_has_nothing_to_forget(tmp_path):
    path = tmp_path / "new.mkv"
    path.write_bytes(b"x" * 4096)
    db.ex("INSERT INTO files (path, fix_attempts) VALUES (?, ?)", (str(path), 1))
    row = dict(db.q1("SELECT * FROM files WHERE path=?", (str(path),)))

    assert forget_replaced(row)["fix_attempts"] == 1
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE event='file_replaced'")["n"] == 0


def test_a_missing_file_is_left_alone(tmp_path):
    path = tmp_path / "gone.mkv"
    path.write_bytes(b"x" * 4096)
    row = _row_with_history(path, _signature(path))
    path.unlink()

    assert {k: forget_replaced(row)[k] for k in HISTORY} == HISTORY


@needs_ffmpeg
def test_unfuckarrs_own_swap_is_not_mistaken_for_a_replacement(
        tmp_path, settings, monkeypatch):
    """The swap writes the new file's signature itself, so even when the
    re-check that follows it fails — swallowed, by design — the next look at
    the file still knows the bytes are unfuckarr's. Without that, a re-encode's
    `shrink_skipped` ("another encode would be a second generation of loss")
    and its capped attempts were one failed re-check away from being wiped."""
    src = make_video(tmp_path / "Old Show - S01E01.avi", seconds=4,
                     vcodec="mpeg2video", acodec="mp2")
    db.ex("INSERT INTO files (path, source, title) VALUES (?,?,?)",
          (str(src), "folder", src.stem))
    result, info = check_file(str(src), settings)
    persist_result(str(src), result, info, src.stat().st_size, src.stat().st_mtime)
    db.ex("UPDATE files SET shrink_skipped='kept by the swap' WHERE path=?",
          (str(src),))
    row = dict(db.q1("SELECT * FROM files WHERE path=?", (str(src),)))
    if not any(f.code == "bad_container" for f in result.findings):
        result.add(Finding("compat", "bad_container", "error", "avi"))

    def recheck_fails(*a, **kw):
        raise RuntimeError("ffprobe timed out on a busy array")
    monkeypatch.setattr(scanner, "check_file", recheck_fails)

    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("transcode", "incompatible"))
    assert out["ok"], out
    final = Path(out["path"])
    assert final.exists() and final != src

    after = dict(db.q1("SELECT * FROM files WHERE path=?", (str(final),)))
    kept = forget_replaced(after)
    assert kept["shrink_skipped"] == "kept by the swap"
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE event='file_replaced'")["n"] == 0


def test_a_restored_original_keeps_its_history(tmp_path, settings):
    """Restoring from the recycle bin is an undo. Read as a new file it would
    lose `shrunk`, and the worker would shrink again the very file the user
    had just put back."""
    path = tmp_path / "Film (2001).mkv"
    path.write_bytes(b"the original remux" * 4000)
    recycle_id_before = db.q1("SELECT COALESCE(MAX(id), 0) m FROM recycle")["m"]
    recycle.store(str(path), "replaced by shrink", str(tmp_path / "bin"), 14)
    rid = db.q1("SELECT id FROM recycle WHERE id > ?", (recycle_id_before,))["id"]
    path.write_bytes(b"the shrunk copy" * 1000)
    _row_with_history(path, _signature(path))

    # Restore moves the original back over the path, as the UI's button does.
    path.unlink()
    recycle.restore(rid)

    row = dict(db.q1("SELECT * FROM files WHERE path=?", (str(path),)))
    assert {k: forget_replaced(row)[k] for k in HISTORY} == HISTORY


@needs_ffmpeg
def test_an_arrival_under_the_same_name_is_judged_as_new(tmp_path, settings,
                                                         monkeypatch):
    """The live shape: the watch folder sees the import minutes before any
    scan refreshes the inventory, and its check and its action both read the
    row as it stands."""
    from unfuckarr import service as service_mod

    path = tmp_path / "Cosmos (2014) - S01E08.mkv"
    path.write_bytes(b"1980 episode" * 1000)
    _row_with_history(path, _signature(path))
    path.unlink()
    make_video(path, seconds=4)

    seen: dict = {}
    real_check = service_mod.check_file

    def spy_check(*a, **kw):
        seen["already_shrunk"] = kw.get("already_shrunk")
        return real_check(*a, **kw)
    monkeypatch.setattr(service_mod, "check_file", spy_check)

    svc = service_mod.Service()

    def spy_apply(record, result, info, decision):
        seen["record"] = record
        return {"action": decision.action, "ok": True}
    monkeypatch.setattr(svc.remediator, "apply", spy_apply)

    svc._check_arrival(str(path))

    assert seen["already_shrunk"] is False, \
        "the arrival was checked as if the old file's shrink still applied"
    assert _history(path) == CLEAN
    if "record" in seen:
        assert seen["record"]["fix_attempts"] == 0


@needs_ffmpeg
def test_a_scan_judges_a_replaced_file_as_new(tmp_path, settings, monkeypatch):
    path = tmp_path / "Show - S02E03.mkv"
    path.write_bytes(b"old" * 5000)
    row = _row_with_history(path, _signature(path))
    path.unlink()
    make_video(path, seconds=4)

    seen: dict = {}
    real_check = scanner.check_file

    def spy(*a, **kw):
        seen["already_shrunk"] = kw.get("already_shrunk")
        return real_check(*a, **kw)
    monkeypatch.setattr(scanner, "check_file", spy)

    scan = scanner.Scanner(lambda: settings, Remediator(lambda: settings))
    record, _result, _info = scan._check_one(
        db.q1("SELECT * FROM files WHERE path=?", (str(path),)), settings, None)

    assert seen["already_shrunk"] is False
    assert {k: record[k] for k in HISTORY} == CLEAN
    assert row["fix_attempts"] == HISTORY["fix_attempts"]   # the old row really had it
