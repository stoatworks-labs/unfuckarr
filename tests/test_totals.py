"""The lifetime counters behind the header strip.

These exist because the obvious implementation — count the `jobs` table — is
wrong in a way nobody would notice for months: `prune()` caps jobs at 2,000 and
activity at 5,000 rows, and the recycle table is swept on retention, so a
derived total quietly falls after a busy week. The counters are therefore
written at the moment the thing happens, and what these tests pin down is
*which* moment.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from unfuckarr import db
from unfuckarr.api import totals_summary
from unfuckarr.checks import Finding
from unfuckarr.remediation import Decision, Remediator
from unfuckarr.scanner import check_file

from .conftest import needs_ffmpeg
# The conversion counters need a stand-in makemkvcon, and pytest picks up a
# fixture imported into a test module.
from .test_makemkv import fake_makemkv, insert  # noqa: F401


def test_a_counter_accumulates_rather_than_being_overwritten():
    db.bump("files_fixed")
    db.bump("files_fixed", 2)
    db.bump("bytes_saved", 1024)
    assert db.totals() == {"files_fixed": 3, "bytes_saved": 1024}


def test_a_zero_bump_writes_nothing():
    """A conversion that reclaims nothing should not create a row that reads
    as an event."""
    db.bump("bytes_saved", 0)
    assert db.totals() == {}


def test_every_key_is_present_from_the_first_load():
    """Or the header changes shape the first time something is repaired, which
    looks like a bug in the page."""
    assert totals_summary() == {
        "bytes_saved": 0, "files_fixed": 0, "files_shrunk": 0,
        "discs_converted": 0, "files_deleted": 0, "redownloads": 0,
    }


@needs_ffmpeg
def test_a_repair_counts_only_once_it_is_confirmed(video_factory, settings):
    path = video_factory("legacy.avi", seconds=8, vcodec="mpeg2video",
                         acodec="ac3", extra=["-b:v", "1M"])
    db.ex("INSERT INTO files (path, library, source, title) VALUES (?,?,?,?)",
          (str(path), "Movies", "folder", "Legacy"))
    row = {"path": str(path), "source": "folder", "title": "Legacy",
           "arr_id": None, "arr_parent_id": None}

    result, info = check_file(str(path), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("transcode", "incompatible"))

    assert out["ok"], out
    assert db.totals().get("files_fixed") == 1


@needs_ffmpeg
def test_a_transcode_that_fixes_nothing_is_not_counted_as_a_fix(
        video_factory, settings, monkeypatch):
    """ffmpeg exiting 0 is not the same as the finding being cleared — the
    live 86-remux loop was exactly this, and a header claiming 86 fixes would
    have made it look like the tool was working."""
    path = video_factory("loop.mkv", seconds=8)
    db.ex("INSERT INTO files (path, title) VALUES (?,?)", (str(path), "Loop"))
    row = {"path": str(path), "source": "folder", "title": "Loop",
           "arr_id": None, "arr_parent_id": None, "fix_attempts": 0}

    from unfuckarr import scanner as scanner_mod
    real_check = scanner_mod.check_file

    def always_bad(p, s, **kw):
        result, info = real_check(p, s, **kw)
        result.add(Finding("compat", "bad_container", "error", "still wrong"))
        return result, info
    monkeypatch.setattr(scanner_mod, "check_file", always_bad)

    result, info = check_file(str(path), settings)
    result.add(Finding("compat", "bad_container", "error", ""))
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("transcode", "incompatible",
                                    ["bad_container"]))

    assert out["ok"], out
    assert db.totals().get("files_fixed", 0) == 0


# -- deletions and re-searches --------------------------------------------

class FakeArr:
    flavour = "radarr"

    def __init__(self):
        self.searched = False

    def delete_file(self, arr_id):
        pass

    def blocklist_last_grab(self, parent, episodes):
        self.searched = True
        return True

    def search(self, parent, episodes):
        self.searched = True

    def rescan(self, parent):
        pass


def test_a_deletion_with_an_arr_counts_as_both(tmp_path, settings, monkeypatch):
    path = tmp_path / "broken.mkv"
    path.write_bytes(b"x" * 2048)
    db.ex("INSERT INTO files (path, title) VALUES (?,?)", (str(path), "Broken"))
    row = {"path": str(path), "source": "radarr", "title": "Broken",
           "arr_id": 7, "arr_parent_id": 3}

    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "_arr_for", lambda _row: FakeArr())
    out = rem._redownload_row(row, "corrupt")

    assert out["ok"], out
    assert db.totals().get("files_deleted") == 1
    assert db.totals().get("redownloads") == 1


def test_a_deletion_nothing_will_replace_is_not_a_re_search(
        tmp_path, settings, monkeypatch):
    """A file no *arr owns is removed with nothing coming to replace it.
    Counting that as a re-search would have the header promise a replacement
    that was never asked for."""
    path = tmp_path / "orphan.mkv"
    path.write_bytes(b"x" * 2048)
    db.ex("INSERT INTO files (path, title) VALUES (?,?)", (str(path), "Orphan"))
    row = {"path": str(path), "source": "folder", "title": "Orphan",
           "arr_id": None, "arr_parent_id": None}

    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "_arr_for", lambda _row: None)
    out = rem._redownload_row(row, "corrupt")

    assert out["ok"], out
    assert db.totals().get("files_deleted") == 1
    assert db.totals().get("redownloads", 0) == 0


# -- conversions ----------------------------------------------------------

@needs_ffmpeg
def test_a_conversion_counts_the_saving_against_everything_that_replaced_it(
        fake_makemkv, video_factory, settings, tmp_path):  # noqa: F811
    """Counting the feature alone while its extras sit beside it would report
    space that was never reclaimed."""
    feature = video_factory("src-feature.mkv", seconds=6)
    extra = video_factory("src-extra.mkv", seconds=3)
    folder = tmp_path / "Film (2011)"
    folder.mkdir()
    image = folder / "Film (2011).iso"
    # An image bigger than what comes out of it, as a real one is.
    image.write_bytes(feature.read_bytes() + b"\0" * 4_000_000)

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.min_title_seconds = 1
    settings.makemkv.extras_min_seconds = 1
    settings.makemkv.command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:06", "file": str(feature),
         "segments": "1,2"},
        {"index": 1, "duration": "0:00:03", "file": str(extra),
         "segments": "9"},
    ]})

    row = insert(image)
    before = image.stat().st_size
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("convert", "disc image"))
    assert out["ok"], out

    replaced = (folder / "Film (2011).mkv").stat().st_size
    replaced += sum(f.stat().st_size for f in (folder / "extras").iterdir())
    assert db.totals().get("discs_converted") == 1
    assert db.totals().get("bytes_saved") == before - replaced


@needs_ffmpeg
def test_keeping_the_image_reclaims_nothing_and_says_so(
        fake_makemkv, video_factory, settings, tmp_path):  # noqa: F811
    feature = video_factory("src.mkv", seconds=6)
    image = tmp_path / "Film.iso"
    shutil.copy(feature, image)

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.min_title_seconds = 1
    settings.makemkv.keep_disc_image = True
    settings.makemkv.command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:06", "file": str(feature),
         "segments": "1"}]})

    row = insert(image)
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("convert", "disc image"))
    assert out["ok"], out

    assert image.exists()
    assert db.totals().get("discs_converted") == 1
    assert db.totals().get("bytes_saved", 0) == 0


def test_the_counters_survive_a_prune():
    """The whole reason they are counters. `prune()` is what makes a total
    derived from `jobs` fall over time."""
    db.bump("files_fixed", 5)
    for _ in range(30):
        db.ex("INSERT INTO jobs (kind, path, state, created) VALUES (?,?,?,?)",
              ("transcode", "/media/x.mkv", "done", 1.0))
    db.prune(activity_keep=1, jobs_keep=1)

    assert db.q1("SELECT COUNT(*) n FROM jobs")["n"] == 1
    assert db.totals()["files_fixed"] == 5


def test_shrinks_from_before_the_counters_existed_are_recovered(tmp_path):
    """A header reading "0 saved" on a system that has reclaimed terabytes is
    the exact lie these counters exist to prevent. Measured on the live
    instance the day they shipped: 386 files and 2.27 TB, reported as nothing.
    """
    db.ex("INSERT INTO files (path, size, shrunk, shrunk_from) VALUES (?,?,?,?)",
          ("/media/a.mkv", 1_000, 111.0, 4_000))
    db.ex("INSERT INTO files (path, size, shrunk, shrunk_from) VALUES (?,?,?,?)",
          ("/media/b.mkv", 2_000, 222.0, 5_000))
    # Never shrunk, and a shrink with no recorded "before" — neither can be counted.
    db.ex("INSERT INTO files (path, size) VALUES (?,?)", ("/media/c.mkv", 9_000))
    db.ex("INSERT INTO files (path, size, shrunk) VALUES (?,?,?)",
          ("/media/d.mkv", 8_000, 333.0))

    db._backfill_totals(db.connect())
    assert db.totals() == {"files_shrunk": 2, "bytes_saved": 6_000}

    # Runs once: a second pass must not double the saving.
    db._backfill_totals(db.connect())
    assert db.totals() == {"files_shrunk": 2, "bytes_saved": 6_000}


def test_backfill_leaves_what_it_cannot_know_alone():
    """Repairs, deletions and redownloads have no per-file record — jobs and
    activity are both pruned — so they start at zero rather than at a guess."""
    db.ex("INSERT INTO files (path, size, shrunk, shrunk_from) VALUES (?,?,?,?)",
          ("/media/a.mkv", 1_000, 111.0, 4_000))
    db._backfill_totals(db.connect())
    assert "files_fixed" not in db.totals()
    assert "redownloads" not in db.totals()
