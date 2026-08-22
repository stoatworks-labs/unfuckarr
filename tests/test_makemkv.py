"""Reading MakeMKV's robot output, choosing a title, and the conversion.

MakeMKV is never installed here — it cannot be, it is not redistributable —
so these tests run a stand-in that speaks the same robot protocol and copies
real files that ffmpeg rendered. That covers everything unfuckarr is actually
responsible for: the parsing, the selection, and what happens to the media
either side of a conversion. What it cannot cover is whether MakeMKV picks the
right playlist off a real Blu-ray, which is the reason it is here at all.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest

from unfuckarr import db, makemkv, transcode
from unfuckarr.config import Settings
from unfuckarr.makemkv import Title
from unfuckarr.remediation import Decision, Remediator, convert_blocked, decide
from unfuckarr.scanner import check_file

from .conftest import needs_ffmpeg

# -- the robot protocol ---------------------------------------------------

SAMPLE = """\
MSG:1005,0,1,"MakeMKV v1.17.7 linux(x64-release) started","%1 started","MakeMKV v1.17.7 linux(x64-release)"
TCOUNT:3
TINFO:0,2,0,"Blu-ray disc"
TINFO:0,8,0,"24"
TINFO:0,9,0,"1:52:31"
TINFO:0,11,0,"32212254720"
TINFO:0,26,0,"1,2,3,4"
TINFO:0,27,0,"title_t00.mkv"
TINFO:1,2,0,"Deleted, extended scenes"
TINFO:1,8,0,"3"
TINFO:1,9,0,"0:12:04"
TINFO:1,11,0,"1610612736"
TINFO:1,26,0,"7"
TINFO:2,9,0,"0:00:31"
TINFO:2,11,0,"104857600"
TINFO:2,26,0,"9"
"""


REAL_BANNER = (
    'MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started",'
    '"%1 started","MakeMKV v1.18.4 linux(x64-release)"'
)


def test_the_version_is_read_off_a_real_banner():
    """Captured from makemkvcon 1.18.4 on the deployment target. Both the
    message and the format parameter begin with "MakeMKV"; the parameter is the
    clean one."""
    fields = makemkv._split(REAL_BANNER[4:])
    named = [f for f in fields if f.startswith("MakeMKV")]
    assert named[-1] == "MakeMKV v1.18.4 linux(x64-release)"
    # And the banner is not mistaken for the one message that must never be
    # mistaken for anything else.
    assert not makemkv._KEY_TROUBLE.search(REAL_BANNER)


# Transcribed from makemkvcon 1.18.4 on the deployment target, run against a
# real Blu-ray image with a lapsed beta key.
REAL_EXPIRED = [
    'MSG:5073,260,0,"Your temporary key has expired and was removed. Please '
    'restart the application.","Your temporary key has expired and was '
    'removed. Please restart the application."',
    'MSG:5021,131332,1,"This application version is too old.  Please download '
    'the latest version at http://www.makemkv.com/ or enter a registration '
    'key to continue using the current version.","...",'
    '"http://www.makemkv.com/"',
]


@pytest.mark.parametrize("line", REAL_EXPIRED)
def test_a_real_lapsed_key_is_recognised(line):
    """Both messages, because MakeMKV emits them together and either alone has
    to be enough. Without this the same run reports no titles at all, which
    reads as "cannot read this image" and is recorded against the disc for
    ever — for something a new key fixes."""
    with pytest.raises(makemkv.KeyExpired):
        makemkv._check_messages([line])


def test_records_split_on_real_quoting():
    """A title name legitimately contains a comma, and MakeMKV escapes a quote
    by doubling it. Splitting on commas loses both."""
    assert makemkv._split('1,2,0,"Deleted, extended scenes"') == \
        ["1", "2", "0", "Deleted, extended scenes"]
    assert makemkv._split('0,2,0,"He said ""no"""') == \
        ["0", "2", "0", 'He said "no"']


@pytest.mark.parametrize("text,seconds", [
    ("1:52:31", 6751.0), ("12:04", 724.0), ("0:00:31", 31.0), ("", 0.0),
    ("unknown", 0.0),
])
def test_durations_parse(text, seconds):
    assert makemkv.parse_duration(text) == seconds


def test_titles_are_read_from_robot_output(fake_makemkv, tmp_path):
    image = tmp_path / "film.iso"
    image.write_bytes(b"not really an iso")
    command = fake_makemkv({"titles": [
        {"index": 0, "duration": "1:52:31", "chapters": 24,
         "bytes": 32212254720, "segments": "1,2,3,4"},
        {"index": 1, "duration": "0:12:04", "chapters": 3,
         "bytes": 1610612736, "segments": "7"},
    ]})

    found = makemkv.titles(str(image), command)
    assert [t.index for t in found] == [0, 1]
    assert found[0].seconds == 6751.0
    assert found[0].chapters == 24
    assert found[0].size == 32212254720
    assert found[1].segments == "7"


def test_an_expired_key_is_told_apart_from_a_bad_disc(fake_makemkv, tmp_path):
    """It is the one failure here that is about the installation rather than
    the media, and the caller must not record it against the disc."""
    image = tmp_path / "film.iso"
    image.write_bytes(b"x")
    command = fake_makemkv({"message": "The evaluation period has expired."})

    with pytest.raises(makemkv.KeyExpired):
        makemkv.titles(str(image), command)
    # It is an Unavailable, so every caller that already distinguishes "cannot
    # run the tool" from "cannot read this file" gets it right for free.
    assert issubclass(makemkv.KeyExpired, makemkv.Unavailable)


def test_no_titles_at_all_is_an_error_not_an_empty_list(fake_makemkv, tmp_path):
    image = tmp_path / "film.iso"
    image.write_bytes(b"x")
    command = fake_makemkv({"titles": [], "message": "Failed to open disc"})
    with pytest.raises(makemkv.MakeMKVError, match="Failed to open disc"):
        makemkv.titles(str(image), command)


# -- choosing what to rip -------------------------------------------------

def title(index, minutes, segments="", size=0):
    return Title(index=index, seconds=minutes * 60, segments=segments,
                 size=size or int(minutes * 60 * 5_000_000))


def test_the_longest_title_is_the_feature():
    chosen = makemkv.select([title(0, 4), title(1, 112), title(2, 12)])
    assert chosen.main.index == 1
    # Longest first, because that is the order they are ripped in and the
    # order they are numbered in `extras/`.
    assert [t.index for t in chosen.extras] == [2, 0]


def test_the_same_content_under_two_playlists_is_ripped_once():
    """A disc offers the feature several times over — one playlist per angle,
    per commentary set. They share a segment map, and ripping all of them fills
    the array with the same film."""
    chosen = makemkv.select([
        title(0, 112, segments="1,2,3"),
        title(1, 112, segments="1,2,3"),
        title(2, 9, segments="8"),
    ])
    assert chosen.main.index == 0
    assert [t.index for t in chosen.extras] == [2]
    assert any("same content as title 0" in why for _, why in chosen.rejected)


def test_another_cut_of_the_feature_is_not_a_bonus_feature():
    """A director's cut is not an extra: taking it doubles the space for one
    film, and nothing in the metadata says which one the user wanted."""
    chosen = makemkv.select([title(0, 112, segments="1,2"),
                             title(1, 108, segments="1,3")])
    assert chosen.main.index == 0
    assert chosen.extras == []
    assert any("another version" in why for _, why in chosen.rejected)


def test_titles_below_the_extras_floor_are_left_behind():
    chosen = makemkv.select([title(0, 112, segments="1"),
                             title(1, 0.5, segments="2")],
                            extras_min_seconds=90)
    assert chosen.extras == []


def test_a_trailer_is_not_accepted_as_the_feature():
    """The failure this replaces is real: reading a disc by taking its largest
    `.m2ts` picked a 1.1 GiB trailer on one image in the live library."""
    chosen = makemkv.select([title(0, 3, segments="1")],
                            expected_seconds=112 * 60, tolerance_pct=25)
    assert chosen.main is None


def test_the_arr_runtime_is_only_ever_a_cross_check():
    """It is nominal — the broadcast slot, ad breaks included — so a title
    comfortably inside the tolerance is accepted even though it is short."""
    chosen = makemkv.select([title(0, 44, segments="1")],
                            expected_seconds=50 * 60, tolerance_pct=25)
    assert chosen.main is not None


def test_the_extras_limit_is_honoured():
    found = [title(0, 112, segments="main")]
    found += [title(i, 5, segments=f"s{i}") for i in range(1, 30)]
    chosen = makemkv.select(found, max_extras=4)
    assert len(chosen.extras) == 4


# -- ripping --------------------------------------------------------------

@needs_ffmpeg
def test_a_rip_returns_the_file_that_appeared(fake_makemkv, video_factory,
                                              tmp_path):
    source = video_factory("feature.mkv", seconds=3)
    image = tmp_path / "film.iso"
    image.write_bytes(b"x")
    command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:03", "file": str(source)}]})

    seen: list[float] = []
    out = makemkv.rip(str(image), Title(index=0), str(tmp_path / "work"),
                      command, on_progress=seen.append)
    assert Path(out).exists() and Path(out).suffix == ".mkv"
    assert seen and seen[-1] == 1.0


@needs_ffmpeg
def test_more_than_one_file_for_one_title_is_refused(fake_makemkv,
                                                     video_factory, tmp_path):
    """Asking for one title and getting two means the index meant something
    else; taking the bigger one would be a guess about which film the user
    ends up with."""
    source = video_factory("feature.mkv", seconds=2)
    image = tmp_path / "film.iso"
    image.write_bytes(b"x")
    command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:02", "file": str(source), "copies": 2}]})

    work = tmp_path / "work"
    with pytest.raises(makemkv.MakeMKVError, match="expected one file"):
        makemkv.rip(str(image), Title(index=0), str(work), command)
    assert list(work.iterdir()) == [], "the strays should have been cleaned up"


# -- what the rest of the application does with it ------------------------

def test_a_conversion_can_never_delete():
    """Same enforcement as hygiene and oversize: the literal type is what
    stops a policy that only ever rewrites a file from growing a delete."""
    from typing import get_args
    field = Settings().policy.model_fields["disc_action"]
    assert set(get_args(field.annotation)) == {"none", "flag", "convert"}


def test_conversion_is_off_until_configured():
    s = Settings()
    assert convert_blocked(s) == "disc images are set to flag only"
    s.policy.disc_action = "convert"
    assert convert_blocked(s) == "MakeMKV is switched off"
    s.makemkv.enabled = True
    assert convert_blocked(s) is None
    s.makemkv.command = "  "
    assert convert_blocked(s) == "no MakeMKV command is configured"


def test_extras_are_not_library_items():
    """A deleted scene checked as if it were a film is found broken and then
    repaired — or redownloaded, which on a file carrying its parent's *arr
    identity deletes the film."""
    assert transcode.is_extras_path("/media/movies/Film (2011)/extras/Extra 01.mkv")
    assert transcode.is_extras_path("/media/movies/Film/Behind The Scenes/a.mkv")
    assert not transcode.is_extras_path("/media/movies/Film (2011)/Film.mkv")
    # Sonarr's season-zero folder is called Specials and is a real part of the
    # library — and so is a category folder that happens to share a name with
    # one of Emby's looser extras folders.
    assert not transcode.is_extras_path("/media/tv/Show/Specials/S00E01.mkv")
    assert not transcode.is_extras_path("/media/movies/Shorts/A Film/A Film.mkv")


def test_a_rip_in_flight_is_invisible_to_everything_else():
    """MakeMKV names the files it writes, so the only thing marking them as
    ours is the folder they are in."""
    assert transcode.is_temp_output(
        "/media/movies/Film/Film.unfuckarr.convert/main/title_t00.mkv")
    assert transcode.is_temp_output("Film.unfuckarr.mkv")
    assert not transcode.is_temp_output("/media/movies/Film/Film.mkv")


@needs_ffmpeg
def test_a_disc_image_is_decided_for_conversion(video_factory, settings,
                                                tmp_path):
    """And only when there is something to convert it with — otherwise the
    decision falls through to what it was before this existed."""
    # Comfortably past integrity.TINY_FILE_BYTES: the size floor is an error,
    # and an error decides before anything here gets a say.
    image = tmp_path / "disc.iso"
    shutil.copy(video_factory("src-disc.mkv", seconds=12), image)
    result, _ = check_file(str(image), settings)

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    assert decide(result, settings).action == "convert"

    settings.makemkv.enabled = False
    assert decide(result, settings).action != "convert"


@needs_ffmpeg
def test_an_image_that_would_not_open_is_still_offered_to_makemkv(settings):
    """Five images in the live library are refused by libudfread and report
    `disc_not_inspectable`. Those are exactly the ones a real MakeMKV has the
    best chance of reading, and "I cannot open it" is not "it is broken"."""
    from unfuckarr.checks import CheckResult, Finding

    result = CheckResult(path="/media/movies/Odd/Odd.iso")
    result.findings.append(Finding(
        category="integrity", code="disc_not_inspectable", severity="info",
        detail="ECMA 167 Volume Recognition failed"))

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    assert decide(result, settings).action == "convert"


def test_an_interrupted_rip_is_found_by_the_startup_sweep(tmp_path):
    """The 199 GB trap, in directory form: a conversion killed by a restart
    leaves the work dir behind, and everything that enumerates media is now
    built to look straight past it."""
    film = tmp_path / "Film.mkv"
    film.write_bytes(b"x" * 10)
    work = tmp_path / f"Film{transcode.TEMP_MARKER}convert" / "main"
    work.mkdir(parents=True)
    (work / "title_t00.mkv").write_bytes(b"y" * 4096)

    found = transcode.abandoned_outputs([str(film)])
    assert found == [str(tmp_path / f"Film{transcode.TEMP_MARKER}convert")]


# -- end to end -----------------------------------------------------------

@needs_ffmpeg
def test_a_disc_becomes_a_film_with_its_extras_beside_it(
        fake_makemkv, video_factory, settings, tmp_path):
    feature = video_factory("src-feature.mkv", seconds=6)
    extra = video_factory("src-extra.mkv", seconds=3)
    folder = tmp_path / "Film (2011)"
    folder.mkdir()
    image = folder / "Film (2011).iso"
    shutil.copy(feature, image)          # a real file, so sizes are real

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:06", "file": str(feature),
         "chapters": 12, "segments": "1,2"},
        {"index": 1, "duration": "0:00:03", "file": str(extra),
         "segments": "9"},
    ]})
    settings.makemkv.extras_min_seconds = 1
    settings.makemkv.min_title_seconds = 1

    row = insert(image)
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("convert", "disc image"))
    assert out["ok"], out

    final = Path(out["path"])
    assert final == folder / "Film (2011).mkv"
    assert final.exists() and not image.exists()

    extras = sorted((folder / "extras").iterdir())
    assert len(extras) == 1 and extras[0].suffix == ".mkv"

    stored = db.q1("SELECT converted, converted_from FROM files WHERE path=?",
                   (str(final),))
    assert stored["converted"] and stored["converted_from"] == str(image)
    # The image is recoverable, which is the whole safety net for this action:
    # nothing about a conversion is verifiable after the fact except by
    # watching it.
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 1


@needs_ffmpeg
def test_a_copy_that_stops_early_leaves_the_image_alone(
        fake_makemkv, video_factory, settings, tmp_path):
    """The failure that actually happened on the live library: an encode out of
    a disc came back 1620s of a 6604s film. It was caught by the duration
    check, and this is that check."""
    feature = video_factory("src-long.mkv", seconds=8)
    short = video_factory("src-short.mkv", seconds=2)
    image = tmp_path / "Film.iso"
    shutil.copy(feature, image)

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.min_title_seconds = 1
    # MakeMKV reports the full length and then hands back a truncated file.
    settings.makemkv.command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:08", "file": str(short),
         "segments": "1"}]})

    row = insert(image)
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("convert", "disc image"))

    assert not out["ok"]
    assert "against an expected" in out["message"]
    assert image.exists(), "the image must still be there"
    assert not (tmp_path / "Film.mkv").exists()
    assert db.q1("SELECT COUNT(*) n FROM recycle")["n"] == 0
    assert db.q1("SELECT convert_attempts a FROM files WHERE path=?",
                 (str(image),))["a"] == 1


@needs_ffmpeg
def test_a_disc_with_no_usable_title_is_never_reassessed(
        fake_makemkv, video_factory, settings, tmp_path):
    image = tmp_path / "Odd.iso"
    shutil.copy(video_factory("src.mkv", seconds=4), image)

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.command = fake_makemkv({
        "titles": [], "message": "Failed to open disc"})

    row = insert(image)
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("convert", "disc image"))

    assert out["action"] == "flag"
    skipped = db.q1("SELECT convert_skipped s FROM files WHERE path=?",
                    (str(image),))["s"]
    assert "cannot read this image" in skipped
    assert image.exists()


@needs_ffmpeg
def test_an_expired_key_does_not_write_the_disc_off(
        fake_makemkv, video_factory, settings, tmp_path):
    """A new key fixes it, and the disc was never the problem — so nothing
    permanent may be recorded against it."""
    image = tmp_path / "Film.iso"
    shutil.copy(video_factory("src.mkv", seconds=4), image)

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.command = fake_makemkv(
        {"message": "This beta key has expired"})

    row = insert(image)
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("convert", "disc image"))

    assert not out["ok"]
    stored = db.q1("SELECT convert_skipped s, convert_attempts a "
                   "FROM files WHERE path=?", (str(image),))
    assert stored["s"] is None
    assert (stored["a"] or 0) == 0
    assert image.exists()


# -- helpers --------------------------------------------------------------

def insert(path: Path, **extra) -> dict:
    db.ex("INSERT INTO files (path, library, source, title, size) "
          "VALUES (?,?,?,?,?)",
          (str(path), "Movies", "folder", path.stem,
           path.stat().st_size if path.exists() else 0))
    for column, value in extra.items():
        db.ex(f"UPDATE files SET {column}=? WHERE path=?", (value, str(path)))
    return dict(db.q1("SELECT * FROM files WHERE path=?", (str(path),)))


FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for makemkvcon: same robot protocol, real files out."""
    import json, os, shutil, sys
    from pathlib import Path

    plan = json.loads(Path(os.environ["FAKE_MAKEMKV_PLAN"]).read_text())
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    print('MSG:1005,0,1,"MakeMKV v1.17.7 linux(x64-release) started",'
          '"%1 started","MakeMKV v1.17.7 linux(x64-release)"')

    if plan.get("message"):
        print('MSG:5021,0,1,"%s","%%1",""' % plan["message"])
        if not plan.get("titles"):
            sys.exit(1)

    titles = plan.get("titles", [])
    if args and args[0] == "info":
        print("TCOUNT:%d" % len(titles))
        for t in titles:
            i = t["index"]
            print('TINFO:%d,2,0,"Blu-ray disc"' % i)
            print('TINFO:%d,8,0,"%d"' % (i, t.get("chapters", 0)))
            print('TINFO:%d,9,0,"%s"' % (i, t["duration"]))
            print('TINFO:%d,11,0,"%d"' % (i, t.get("bytes", 0)))
            print('TINFO:%d,26,0,"%s"' % (i, t.get("segments", str(i))))
            print('TINFO:%d,27,0,"title_t%02d.mkv"' % (i, i))
        sys.exit(0)

    # mkv <source> <index> <outdir>
    index, out_dir = int(args[2]), args[3]
    wanted = next((t for t in titles if t["index"] == index), None)
    if wanted is None or not wanted.get("file"):
        print('MSG:5010,0,1,"no such title","%1",""')
        sys.exit(1)
    for step in (0, 32768, 65536):
        print("PRGV:%d,%d,65536" % (step, step))
    for n in range(wanted.get("copies", 1)):
        shutil.copy(wanted["file"],
                    os.path.join(out_dir, "title_t%02d_%d.mkv" % (index, n)))
    print('MSG:5036,0,1,"Copy complete.","%1",""')
''')


@pytest.fixture
def fake_makemkv(tmp_path):
    """Build a stand-in makemkvcon around a plan, and return the command line.

    A whole command line rather than a path, because that is what the setting
    is — the shape that lets someone point this at a container.
    """
    made = {"n": 0}

    def build(plan: dict) -> str:
        made["n"] += 1
        n = made["n"]
        script = tmp_path / f"fake-makemkvcon-{n}.py"
        script.write_text(FAKE)
        script.chmod(0o755)
        plan_file = tmp_path / f"plan-{n}.json"
        plan_file.write_text(json.dumps(plan))
        os_env = shutil.which("env") or "/usr/bin/env"
        return (f"{os_env} FAKE_MAKEMKV_PLAN={plan_file} "
                f"{shutil.which('python3') or 'python3'} {script}")

    return build


# -- the scan-level brakes ------------------------------------------------

def _scan_harness(settings, monkeypatch):
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    applied: list[str] = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[0]["path"]) or {"ok": True})
    state.scan = ScanProgress(running=True, checked=40)
    return Scanner(lambda: settings, rem), applied


def test_conversions_do_not_count_towards_the_abort_ratio(settings, monkeypatch):
    """"Most of this library is disc images" describes somebody's collection,
    not a mount that has just gone away — and a disc-heavy library would
    otherwise abort every scan it ever ran."""
    scanner, applied = _scan_harness(settings, monkeypatch)
    settings.policy.max_conversions_per_scan = 100

    from unfuckarr.checks import CheckResult
    pending = [({"path": f"/media/{i}.iso"}, CheckResult(path=f"/media/{i}.iso"),
                None, Decision("convert", "disc image")) for i in range(40)]
    out = scanner._remediate(settings, pending, population=50)

    assert "aborted" not in out
    assert len(applied) == 40


def test_conversions_have_their_own_much_smaller_cap(settings, monkeypatch):
    """One is tens of minutes of solid I/O copying a Blu-ray, and there is no
    continuous worker to hand them to."""
    scanner, applied = _scan_harness(settings, monkeypatch)
    settings.policy.max_conversions_per_scan = 2
    settings.policy.max_actions_per_scan = 50

    from unfuckarr.checks import CheckResult
    pending = [({"path": f"/media/{i}.iso"}, CheckResult(path=f"/media/{i}.iso"),
                None, Decision("convert", "disc image")) for i in range(9)]
    out = scanner._remediate(settings, pending, population=50)

    assert len(applied) == 2
    assert out["conversions"] == 2 and out["actions"] == 0
