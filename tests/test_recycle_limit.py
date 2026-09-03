"""The recycle bin's size ceiling, and what its total actually measures."""

from __future__ import annotations

import time
from pathlib import Path

from unfuckarr import db, recycle


def _bin(tmp_path) -> str:
    d = tmp_path / "bin"
    d.mkdir()
    return str(d)


def _put(configured: str, name: str, size: int, day: str = "2026-09-03",
         tracked: bool = True, deleted: float | None = None) -> Path:
    """Put a file in the bin, with or without a database row."""
    folder = Path(configured) / day
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    # Sparse: these fixtures go up to gigabytes and only `st_size` is ever
    # read, so allocating the bytes would cost real memory for nothing.
    with open(p, "wb") as fh:
        fh.truncate(size)
    if tracked:
        db.ex("INSERT INTO recycle (original, stored, size, deleted, reason) "
              "VALUES (?,?,?,?,?)",
              (f"/media/{name}", str(p), size,
               deleted if deleted is not None else time.time(), "test"))
    return p


# -- what the total measures ----------------------------------------------

def test_the_total_is_what_is_on_disk_not_what_the_database_remembers(tmp_path):
    """Live on 2026-09-03 the database reported 0 files and 0 bytes while a
    14.5 GB remux sat in the bin with no row. A total taken from `SUM(size)`
    would have read zero next to 14.5 GB of real disk."""
    b = _bin(tmp_path)
    _put(b, "orphan.mkv", 5000, tracked=False)
    _put(b, "tracked.mkv", 3000, tracked=True)

    u = recycle.usage(b)
    assert u["bytes"] == 8000, "the total counts everything in the bin"
    assert u["count"] == 2
    assert u["tracked_bytes"] == 3000, "and still says how much is restorable"
    assert u["orphan_bytes"] == 5000
    assert u["orphan_count"] == 1


def test_files_outside_a_dated_directory_are_neither_counted_nor_deleted(tmp_path):
    b = _bin(tmp_path)
    stray = Path(b) / "notes.txt"
    with open(stray, "wb") as fh:
        fh.truncate(100)
    _put(b, "real.mkv", 1000)

    assert recycle.usage(b)["bytes"] == 1000
    recycle.enforce_limit(1, b)
    assert stray.exists(), "only dated directories are ours to touch"


# -- the limit ------------------------------------------------------------

def test_no_limit_prunes_nothing(tmp_path):
    b = _bin(tmp_path)
    _put(b, "a.mkv", 5000)
    assert recycle.enforce_limit(0, b) == (0, 0)
    assert recycle.usage(b)["bytes"] == 5000


def test_a_bin_under_the_limit_is_left_alone(tmp_path):
    b = _bin(tmp_path)
    _put(b, "a.mkv", 5000)
    assert recycle.enforce_limit(10_000, b) == (0, 0)
    assert recycle.usage(b)["bytes"] == 5000


def test_the_oldest_go_first(tmp_path):
    b = _bin(tmp_path)
    now = time.time()
    oldest = _put(b, "old.mkv", 4000, deleted=now - 3000)
    middle = _put(b, "mid.mkv", 4000, deleted=now - 2000)
    newest = _put(b, "new.mkv", 4000, deleted=now - 1000)

    files, freed = recycle.enforce_limit(9000, b)
    assert (files, freed) == (1, 4000)
    assert not oldest.exists()
    assert middle.exists() and newest.exists()


def test_pruning_removes_the_database_row_too(tmp_path):
    b = _bin(tmp_path)
    _put(b, "a.mkv", 8000, deleted=time.time() - 100)
    recycle.enforce_limit(1000, b)
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 0, (
        "a pruned file must not be left restorable in the UI")


def test_orphans_are_pruned_on_the_same_footing(tmp_path):
    """A limit that could only see the tracked half of the bin would quietly
    stop being a limit — which is exactly the state the live bin was in."""
    b = _bin(tmp_path)
    now = time.time()
    orphan = _put(b, "orphan.mkv", 6000, day="2026-01-01", tracked=False)
    tracked = _put(b, "tracked.mkv", 4000, day="2026-09-03", deleted=now)

    recycle.enforce_limit(5000, b)
    assert not orphan.exists(), "the older orphan should have gone first"
    assert tracked.exists()


def test_room_is_made_before_a_new_file_is_stored(tmp_path):
    """The point of `incoming`: a limit enforced only afterwards is a
    high-water mark that the next tick tidies up 30 seconds later."""
    b = _bin(tmp_path)
    _put(b, "old.mkv", 6000, deleted=time.time() - 500)

    src = tmp_path / "new.mkv"
    with open(src, "wb") as fh:
        fh.truncate(5000)
    stored = recycle.store(str(src), "test", b, days=14, max_bytes=8000)

    assert stored and Path(stored).exists()
    assert recycle.usage(b)["bytes"] == 5000, "the old one made room first"


def test_a_file_bigger_than_the_whole_limit_is_still_kept(tmp_path):
    """Refusing would turn a recoverable delete into a permanent one, which is
    the opposite of what a recycle bin is for."""
    b = _bin(tmp_path)
    src = tmp_path / "huge.mkv"
    with open(src, "wb") as fh:
        fh.truncate(9000)

    stored = recycle.store(str(src), "test", b, days=14, max_bytes=1000)
    assert stored is not None and Path(stored).exists()
    assert recycle.usage(b)["bytes"] == 9000
    assert db.q1("SELECT COUNT(*) n FROM activity "
                 "WHERE event='recycle_limit_exceeded'")["n"] == 1


def test_a_pruned_dated_directory_is_tidied_away(tmp_path):
    b = _bin(tmp_path)
    _put(b, "a.mkv", 8000, day="2026-01-01", deleted=time.time() - 500)
    recycle.enforce_limit(1, b)
    assert not (Path(b) / "2026-01-01").exists()


def test_the_limit_survives_a_bin_that_does_not_exist_yet(tmp_path):
    assert recycle.enforce_limit(1000, str(tmp_path / "nope")) == (0, 0)
    assert recycle.usage(str(tmp_path / "nope"))["bytes"] == 0


# -- the sweep's orphan pass now looks in the configured bin ---------------

def test_the_scheduler_housekeeping_reaches_the_configured_bin(tmp_path):
    """`service._scheduler` called `sweep(days)` with no bin path, so the
    orphan pass looked in the *default* bin — meaning on any install that
    moved its bin, which is every install following the README, an orphan was
    never purged by anything. Only "Empty now" in the UI passed the path.

    Driven through `Service.housekeeping`, which is what the scheduler tick
    actually calls, against a real bin on disk.
    """
    import os

    from unfuckarr import config
    from unfuckarr.service import Service

    b = _bin(tmp_path)
    old = time.time() - 40 * 86400
    orphan = _put(b, "ancient.mkv", 1000, tracked=False)
    os.utime(orphan, (old, old))
    recent = _put(b, "recent.mkv", 1000, tracked=False)

    s = config.get()
    s.policy.recycle_bin_path = b
    s.policy.recycle_bin_days = 14
    s.policy.recycle_bin_max_gb = 0

    Service.housekeeping(s)

    assert not orphan.exists(), "an orphan past retention must be purged"
    assert recent.exists(), "and one inside the window must not be"


def test_the_scheduler_housekeeping_enforces_the_size_limit(tmp_path):
    from unfuckarr import config
    from unfuckarr.service import Service

    b = _bin(tmp_path)
    now = time.time()
    older = _put(b, "older.mkv", 4_000_000_000, deleted=now - 500)
    newer = _put(b, "newer.mkv", 4_000_000_000, deleted=now)

    s = config.get()
    s.policy.recycle_bin_path = b
    s.policy.recycle_bin_days = 14
    s.policy.recycle_bin_max_gb = 5          # 5 GiB, holds one of the two

    Service.housekeeping(s)

    assert not older.exists()
    assert newer.exists()


def test_sweep_purges_an_old_orphan_from_the_configured_bin(tmp_path):
    b = _bin(tmp_path)
    orphan = _put(b, "ancient.mkv", 1000, tracked=False)
    import os
    old = time.time() - 40 * 86400
    os.utime(orphan, (old, old))

    removed = recycle.sweep(14, b)
    assert removed == 1
    assert not orphan.exists()


def test_the_limit_is_gibibytes_so_the_ui_and_the_pruner_agree(tmp_path):
    """The UI renders every size with a 1024-based helper. A limit measured in
    decimal GB therefore displayed as "18.6 GB" when you had typed 20, next to
    a tile reading "20 GB" — two different numbers for one setting."""
    from unfuckarr import config
    from unfuckarr.remediation import _max_bin_bytes

    s = config.get()
    s.policy.recycle_bin_max_gb = 20
    assert _max_bin_bytes(s) == 20 * 1024 ** 3


def test_the_bin_is_swept_even_when_scheduled_scans_are_off(tmp_path,
                                                            monkeypatch):
    """Retention used to sit behind the scheduler's `if not scan_enabled:
    continue`, so turning scheduled scans off — an ordinary thing to do —
    silently stopped the bin ever being swept, and it grew without limit. The
    comment in the loop always claimed it "needs no scan to have happened".
    """
    from unfuckarr import config, service as service_mod
    from unfuckarr.state import state as app_state

    b = _bin(tmp_path)
    _put(b, "a.mkv", 8_000_000_000, deleted=time.time() - 500)
    _put(b, "b.mkv", 8_000_000_000, deleted=time.time())

    s = config.get()
    s.policy.recycle_bin_path = b
    s.policy.recycle_bin_days = 14
    s.policy.recycle_bin_max_gb = 10
    s.schedule.scan_enabled = False          # the case that used to skip it

    calls: list[int] = []
    real = service_mod.Service.housekeeping
    monkeypatch.setattr(service_mod.Service, "housekeeping",
                        staticmethod(lambda cfg: (calls.append(1), real(cfg))[1]))

    svc = service_mod.Service()
    # One tick, then stop: wait() returns False the first time (proceed) and
    # True afterwards (loop exits).
    ticks = iter([False, True, True])
    monkeypatch.setattr(svc._sched_stop, "wait", lambda t: next(ticks, True))
    monkeypatch.setattr(svc, "_recompute_next_scan", lambda: None)
    app_state.paused = False

    svc._scheduler()

    assert calls, "housekeeping must run with scheduled scans off"
    assert recycle.usage(b)["bytes"] <= 10 * 1024 ** 3


def test_housekeeping_is_skipped_while_paused(tmp_path, monkeypatch):
    """Paused means the user has asked for nothing to happen."""
    from unfuckarr import config, service as service_mod
    from unfuckarr.state import state as app_state

    b = _bin(tmp_path)
    kept = _put(b, "a.mkv", 8_000_000_000, deleted=time.time() - 500)

    s = config.get()
    s.policy.recycle_bin_path = b
    s.policy.recycle_bin_max_gb = 1

    svc = service_mod.Service()
    ticks = iter([False, True, True])
    monkeypatch.setattr(svc._sched_stop, "wait", lambda t: next(ticks, True))
    monkeypatch.setattr(svc, "_recompute_next_scan", lambda: None)
    app_state.paused = True
    try:
        svc._scheduler()
    finally:
        app_state.paused = False

    assert kept.exists(), "nothing should be pruned while paused"


def test_an_orphans_age_comes_from_its_folder_not_its_mtime(tmp_path):
    """`shutil.move` preserves the original file's mtime, so an orphan's mtime
    is when the *video* was made, not when it was recycled. Judging age by it
    put a film recycled today behind an episode recycled a fortnight earlier —
    observed doing exactly that on 2026-09-03. The dated folder is the only
    honest record of when a row-less file entered the bin.
    """
    import os

    b = _bin(tmp_path)
    # Recycled long ago, but the media file itself is brand new.
    old_bin_new_file = _put(b, "old.mkv", 6000, day="2026-01-01", tracked=False)
    os.utime(old_bin_new_file, (time.time(), time.time()))
    # Recycled today, but the media file is ancient.
    new_bin_old_file = _put(b, "new.mkv", 6000, day="2026-09-03", tracked=False)
    ancient = time.time() - 400 * 86400
    os.utime(new_bin_old_file, (ancient, ancient))

    recycle.enforce_limit(7000, b)

    assert not old_bin_new_file.exists(), "recycled first, so pruned first"
    assert new_bin_old_file.exists()
