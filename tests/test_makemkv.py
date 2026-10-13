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


def test_the_copy_that_keeps_the_chapters_wins_a_tie():
    """Measured on a real disc: No Time to Die offers the same 83.77 GB feature
    as titles 1 and 52 — identical duration, identical segment map — and only
    title 1 carries the 20 chapter marks. Ordering by index alone got that
    right by the luck of the numbering."""
    with_chapters = Title(index=52, seconds=163.6 * 60, size=83_770_000_000,
                          segments="294", chapters=20)
    without = Title(index=1, seconds=163.6 * 60, size=83_770_000_000,
                    segments="294", chapters=0)
    chosen = makemkv.select([without, with_chapters])
    assert chosen.main.index == 52
    assert chosen.main.chapters == 20
    assert len(chosen.rejected) == 1


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


# -- extras that repeat one another ---------------------------------------

# Transcribed from `makemkvcon -r --cache=1 --minlength=60 info` (1.18.4) on
# Jurassic World Rebirth (2025) BR-DISK.iso, 94,465,884,160 bytes, 77 titles:
# (index, duration, bytes, chapters, source file, segment map). Left out are
# 42 one-minute clips (titles 20-58 and 60-62) that use no segment any other
# title does and sit under every extras floor here.
JW_IMAGE = 94_465_884_160
JW_TITLES = [
    (0, "2:13:40", 79979642880, 20, "00040.mpls", "294"),
    (1, "0:03:11", 1081651200, 5, "00149.mpls", "174,175,175,175,473"),
    (2, "0:04:17", 548308992, 2, "01270.mpls", "477,478,479,480,481"),
    (3, "0:01:21", 174090240, 1, "01271.mpls", "478,481"),
    (4, "0:02:53", 368879616, 1, "01272.mpls", "480,481"),
    (5, "0:01:20", 172498944, 1, "01273.mpls", "477,478"),
    (6, "0:02:56", 375810048, 1, "01274.mpls", "479,480,481"),
    (7, "0:56:20", 7289389056, 6, "01275.mpls",
     "482,483,485,486,488,489,491,492,494,495,497,498,499"),
    (8, "0:06:57", 901650432, 1, "01276.mpls", "483,484"),
    (9, "0:09:56", 1286529024, 1, "01277.mpls", "486,487"),
    (10, "0:09:03", 1173030912, 1, "01278.mpls", "489,490"),
    (11, "0:08:14", 1065474048, 1, "01279.mpls", "492,493"),
    (12, "0:10:15", 1326839808, 1, "01280.mpls", "495,496"),
    (13, "0:11:53", 1536946176, 1, "01281.mpls", "498,499"),
    (14, "0:06:57", 899966976, 1, "01282.mpls", "482,483"),
    (15, "0:09:55", 1284943872, 1, "01283.mpls", "485,486"),
    (16, "0:09:02", 1171451904, 1, "01284.mpls", "488,489"),
    (17, "0:08:13", 1063888896, 1, "01285.mpls", "491,492"),
    (18, "0:10:14", 1325254656, 1, "01286.mpls", "494,495"),
    (19, "0:11:56", 1543882752, 1, "01287.mpls", "497,498,499"),
    (59, "2:13:40", 79979642880, 0, "00294.m2ts", "294"),
    (63, "0:01:39", 211968000, 0, "00476.m2ts", "476"),
    (64, "0:01:17", 165574656, 0, "00478.m2ts", "478"),
    (65, "0:02:49", 360364032, 0, "00480.m2ts", "480"),
    (66, "0:06:53", 893042688, 0, "00483.m2ts", "483"),
    (67, "0:09:52", 1278013440, 0, "00486.m2ts", "486"),
    (68, "0:08:59", 1164515328, 0, "00489.m2ts", "489"),
    (69, "0:08:10", 1056958464, 0, "00492.m2ts", "492"),
    (70, "0:10:11", 1318324224, 0, "00495.m2ts", "495"),
    (71, "0:11:49", 1528350720, 0, "00498.m2ts", "498"),
    (72, "0:01:52", 241410048, 0, "00500.m2ts", "500"),
    (73, "0:03:57", 572737536, 0, "00501.m2ts", "501"),
    (74, "0:05:34", 808372224, 0, "00502.m2ts", "502"),
    (75, "0:10:24", 1506816000, 0, "00503.m2ts", "503"),
    (76, "0:06:25", 930041856, 0, "00504.m2ts", "504"),
]


def jurassic_world_rebirth() -> list[Title]:
    return [Title(index=i, seconds=makemkv.parse_duration(d), size=size,
                  chapters=chapters, source_files=[source], segments=segments)
            for i, d, size, chapters, source, segments in JW_TITLES]


def select_jw(**overrides) -> makemkv.Selection:
    # The live settings: 90 s extras floor, 24 extras, the image as budget.
    args = {"extras_min_seconds": 90, "max_extras": 24, "budget": JW_IMAGE}
    return makemkv.select(jurassic_world_rebirth(), 8020, 25, **(args | overrides))


def why(chosen: makemkv.Selection, index: int) -> str:
    return next(reason for t, reason in chosen.rejected if t.index == index)


def test_a_featurette_offered_four_ways_is_ripped_once():
    """Live, 2026-10-13: segment 498 is the raw clip (title 71), the core of
    two playlists that differ by a 3-second title card (13 and 19), and the
    last part of a 56-minute "play all" (7). Matching only identical segment
    maps kept all four, and 24 extras came to 31.6 GB beside a 76.3 GB feature
    from a 94.5 GB image — a conversion that grew the library."""
    chosen = select_jw()
    assert chosen.main.index == 0

    kept = [chosen.main] + chosen.extras
    holding = [t.index for t in kept if "498" in t.segment_ids]
    assert len(holding) == 1, holding
    # And the same for every segment of real length: nothing is copied twice.
    timed = {s for t in jurassic_world_rebirth() if t.is_clip
             for s in t.segment_ids}
    for segment in timed:
        assert sum(segment in t.segment_ids for t in kept) <= 1, segment

    assert len(chosen.extras) == 14
    assert sum(t.size for t in kept) < JW_IMAGE


def test_the_play_all_gives_way_to_its_pieces():
    """Six featurettes a viewer can pick from beat one 56-minute file they
    have to scroll through — and the pieces kept are the playlists the disc's
    own menu plays, not the raw clips MakeMKV adds on its own account."""
    chosen = select_jw()
    assert why(chosen, 7) == "all but 22s of it is in titles 8, 9, 10, 11, 12, 13"

    featurettes = {8, 9, 10, 11, 12, 13}
    assert featurettes <= {t.index for t in chosen.extras}
    for index in featurettes:
        assert not next(t for t in chosen.extras if t.index == index).is_clip
    # The raw clip inside each one is contained outright; its twin playlist
    # differs from it by nothing but a title card.
    assert why(chosen, 71) == "contained in title 13"
    assert why(chosen, 19) == "all but 3s of it is in title 13"


def test_what_a_rejected_play_all_alone_carried_is_held_to_the_floor():
    """Title 2 strings together a 77-second piece (478) and a 169-second one
    (480). The longer is kept as its own playlist; the shorter was already
    under the 90-second floor as a title of its own, and it does not get past
    it by riding along inside a play-all."""
    chosen = select_jw()
    assert why(chosen, 2) == "all but 84s of it is in title 4"

    # Lower the floor and the short piece is worth keeping — as its own
    # playlist, so the play-all still gives way and nothing is copied twice.
    lower = select_jw(extras_min_seconds=60)
    assert {3, 4} <= {t.index for t in lower.extras}
    assert why(lower, 2) == "all but 7s of it is in titles 3, 4"


def test_a_film_authored_as_clips_is_kept_whole():
    """MakeMKV lists every clip over `--minlength` beside the playlists, so a
    20-minute documentary authored as four clips arrives with all four listed
    too. Waiting for those, the way a play-all waits for its playlists, would
    swap one film for four fragments cut wherever the files happened to end."""
    doc = Title(index=1, seconds=20 * 60, segments="11,12,13,14",
                source_files=["00800.mpls"])
    clips = [Title(index=2 + n, seconds=5 * 60, segments=str(11 + n),
                   source_files=[f"000{11 + n}.m2ts"]) for n in range(4)]
    chosen = makemkv.select([title(0, 112, segments="1"), doc, *clips],
                            extras_min_seconds=90)
    assert [t.index for t in chosen.extras] == [1]
    assert all(why(chosen, c.index) == "contained in title 1" for c in clips)


def test_an_extra_inside_the_feature_is_not_an_extra():
    """A scene-selection playlist is frames the feature already holds."""
    chosen = makemkv.select([title(0, 112, segments="1,2,3,4"),
                             title(1, 9, segments="3")])
    assert chosen.extras == []
    assert why(chosen, 1) == "contained in title 0"


def test_a_shared_bumper_does_not_merge_two_featurettes():
    """Overlap nothing timed on its own counts as new. Two featurettes that
    open on the same studio card share a segment, and that is all they
    share; the cost of being wrong the other way is a featurette gone."""
    chosen = makemkv.select([title(0, 112, segments="1"),
                             title(1, 9, segments="50,51"),
                             title(2, 8, segments="50,52")],
                            extras_min_seconds=90)
    assert [t.index for t in chosen.extras] == [1, 2]


def test_the_extras_never_take_the_output_past_the_image():
    """An image holds each segment once, so output larger than it is something
    copied twice. A later, smaller extra still gets its turn."""
    found = [title(0, 112, segments="1", size=80),
             title(1, 12, segments="2", size=15),
             title(2, 9, segments="3", size=4)]
    chosen = makemkv.select(found, budget=90)
    assert [t.index for t in chosen.extras] == [2]
    assert why(chosen, 1) == "would make the output larger than the image"

    # No budget, no limit: a size nobody measured is not a reason to refuse.
    assert len(makemkv.select(found).extras) == 2


@pytest.mark.parametrize("spec,ids", [
    ("482,483,485", ["482", "483", "485"]),
    ("174,175,175,175,473", ["174", "175", "175", "175", "473"]),
    ("23-25,30", ["23", "24", "25", "30"]),
    ("0482", ["482"]),
    ("", []),
])
def test_segment_maps_are_read_as_lists(spec, ids):
    """Repeats are kept — a title can play one clip three times — and a range
    is expanded, because left as the string `23-25` it overlaps nothing."""
    assert Title(index=0, segments=spec).segment_ids == ids


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


def _convert_film_with_an_extra(fake_makemkv, video_factory, settings,
                                tmp_path, padding: int, **row):
    """Convert an image of ``padding`` bytes more than its feature, with one
    extra, and return (image size, feature + extra on disk afterwards)."""
    feature = video_factory("src-feature.mkv", seconds=6)
    extra = video_factory("src-extra.mkv", seconds=3)
    image = tmp_path / "Film (2011).iso"
    image.write_bytes(feature.read_bytes() + b"\0" * padding)
    image_size = image.stat().st_size

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:06", "file": str(feature),
         "segments": "1,2"},
        {"index": 1, "duration": "0:00:03", "file": str(extra),
         "segments": "9"},
    ]})
    settings.makemkv.extras_min_seconds = 1
    settings.makemkv.min_title_seconds = 1

    stored = insert(image, **row)
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        stored, result, info, Decision("convert", "disc image"))
    assert out["ok"], out
    return image_size, feature.stat().st_size + extra.stat().st_size


@needs_ffmpeg
def test_the_saving_is_measured_off_the_image_not_its_row(
        fake_makemkv, video_factory, settings, tmp_path):
    """Live, 2026-10-08: a Blu-ray whose row had no size was counted as saving
    minus the whole film, and the header's total fell by 58 GB. A watch-folder
    arrival inserts exactly that row."""
    pad = 4 * 1024 * 1024
    image_size, replaced = _convert_film_with_an_extra(
        fake_makemkv, video_factory, settings, tmp_path, pad, size=None)

    assert db.totals()["bytes_saved"] == image_size - replaced > 0
    assert db.totals()["discs_converted"] == 1


@needs_ffmpeg
def test_a_conversion_never_takes_the_saving_down(
        fake_makemkv, video_factory, settings, tmp_path):
    """Output larger than the image is a bad measurement or extras that repeat
    one another — four live discs did the second. Logged, never subtracted."""
    _convert_film_with_an_extra(
        fake_makemkv, video_factory, settings, tmp_path, 0)

    assert db.totals().get("bytes_saved", 0) == 0
    assert db.totals()["discs_converted"] == 1
    assert db.q1("SELECT COUNT(*) n FROM activity "
                 "WHERE event='convert_output_larger'")["n"] == 1


@needs_ffmpeg
def test_a_conversion_holds_its_extras_inside_the_image(
        fake_makemkv, video_factory, settings, tmp_path):
    """The budget is the image measured off the disk, and an extra MakeMKV
    says would take the output past it is never ripped."""
    feature = video_factory("src-feature.mkv", seconds=6)
    extra = video_factory("src-extra.mkv", seconds=3)
    image = tmp_path / "Film (2011).iso"
    image.write_bytes(feature.read_bytes() + b"\0" * 4096)

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    settings.makemkv.command = fake_makemkv({"titles": [
        {"index": 0, "duration": "0:00:06", "file": str(feature),
         "segments": "1,2", "bytes": feature.stat().st_size},
        {"index": 1, "duration": "0:00:03", "file": str(extra),
         "segments": "9", "bytes": 8192},
    ]})
    settings.makemkv.extras_min_seconds = 1
    settings.makemkv.min_title_seconds = 1

    row = insert(image)
    result, info = check_file(str(image), settings)
    out = Remediator(lambda: settings).apply(
        row, result, info, Decision("convert", "disc image"))
    assert out["ok"], out

    assert not (tmp_path / "extras").exists()
    selected = json.loads(db.q1("SELECT detail FROM activity "
                                "WHERE event='convert_selected'")["detail"])
    assert selected["rejected"] == [
        {"index": 1, "why": "would make the output larger than the image"}]


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


def test_a_written_off_disc_does_not_spend_a_conversion_slot(settings, monkeypatch):
    """Live, 2026-10-08 to 10-12: `decide` cannot see the row, so it went on
    deciding `convert` for two discs that had each failed once; they were the
    biggest, so they headed every pass, took both of its two slots to be
    refused inside `apply`, and the disc behind them was never reached."""
    scanner, applied = _scan_harness(settings, monkeypatch)
    settings.policy.max_conversions_per_scan = 2

    from unfuckarr.checks import CheckResult
    refused = [
        {"path": "/media/F9.iso", "convert_attempts": 1},
        {"path": "/media/Blues Brothers.iso", "convert_attempts": 1},
        {"path": "/media/Odd.iso", "convert_skipped": "no title near the runtime"},
        {"path": "/media/Done.iso", "converted": 1760000000.0},
    ]
    fresh = [{"path": "/media/Rebirth.iso"}, {"path": "/media/Next.iso"}]
    pending = [(row, CheckResult(path=row["path"]), None,
                Decision("convert", "disc image")) for row in refused + fresh]
    out = scanner._remediate(settings, pending, population=50)

    assert applied == ["/media/Rebirth.iso", "/media/Next.iso"]
    assert out["conversions"] == 2


def test_a_forced_conversion_reopens_a_failed_disc_but_never_a_converted_one(
        settings, monkeypatch):
    """The UI's Convert button exists for a disc whose failure has since been
    fixed; the scan must not stand between it and the remediator."""
    scanner, applied = _scan_harness(settings, monkeypatch)
    # Room for all three, so it is the refusal and not the cap that stops one.
    settings.policy.max_conversions_per_scan = 5

    from unfuckarr.checks import CheckResult
    rows = [{"path": "/media/F9.iso", "convert_attempts": 1},
            {"path": "/media/Odd.iso", "convert_skipped": "no title"},
            {"path": "/media/Done.iso", "converted": 1760000000.0}]
    pending = [(row, CheckResult(path=row["path"]), None,
                Decision("convert", "requested", force=True)) for row in rows]
    scanner._remediate(settings, pending, population=50)

    assert applied == ["/media/F9.iso", "/media/Odd.iso"]


def test_the_remediator_still_refuses_a_written_off_disc(settings, tmp_path):
    """The scan's check is a filter, not the enforcement: the watch folder and
    the UI reach `apply` without passing through it."""
    from unfuckarr.checks import CheckResult

    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    # Never run: a refusal returns before MakeMKV is asked anything, and a
    # disc that got past it would come back "not usable" instead.
    settings.makemkv.command = str(tmp_path / "no-such-makemkvcon")
    rem = Remediator(lambda: settings)

    for name, extra, expected in [
            ("Failed", {"convert_attempts": 1}, "not trying again"),
            ("Skipped", {"convert_skipped": "no title"}, "already assessed"),
            ("Done", {"converted": 1760000000.0}, "already converted")]:
        row = insert(tmp_path / f"{name}.iso", **extra)
        out = rem.apply(row, CheckResult(path=row["path"]), None,
                        Decision("convert", "disc image"))
        assert out["action"] == "flag" and out["ok"], (name, out)
        assert expected in out["message"], (name, out)


# -- what the first real conversion found ---------------------------------

def test_seek_artefacts_only_count_against_an_ordinary_file():
    """Measured on a real Blu-ray: reading it produced 207 decode errors, all
    of these two shapes, which made a perfectly good 17 GB disc read as
    `corrupt` — and corrupt decides before everything else. MakeMKV's stream
    copy of the same disc probes with zero. On an ordinary file the same lines
    are the primary evidence of damage and must still count."""
    from unfuckarr import disc

    real = [
        "[aist#0:1/dts @ 0x55bb06899440] [dec:dca @ 0x55bb06929000] "
        "Error submitting packet to decoder: Invalid argument",
        "[h264 @ 0x55bb06847200] error while decoding MB 0 0, bytestream 9",
    ]
    for line in real:
        assert disc.is_noise(line, on_disc=True), line
        assert not disc.is_noise(line), f"must still count on a file: {line}"

    # Whereas libbluray's own chatter is noise wherever it appears, because
    # the strings only occur on a disc read in the first place.
    assert disc.is_noise("BD-J check: failed to load jvm")


@needs_ffmpeg
def test_a_disc_with_integrity_findings_converts_rather_than_being_deleted(
        video_factory, settings, tmp_path):
    """`repair` on a disc remuxes through the byte-range path that came out
    short, and when that fails the policy deletes a 40 GB image and asks for
    another copy."""
    from unfuckarr.checks import CheckResult, Finding

    result = CheckResult(path=str(tmp_path / "Film.iso"))
    result.add(Finding("integrity", "decode_errors", "error", "207 decode errors"))

    settings.policy.corrupt_action = "redownload"
    settings.policy.disc_action = "convert"
    settings.makemkv.enabled = True
    assert decide(result, settings).action == "convert"

    # Without a MakeMKV there is nothing better to do, so the old behaviour
    # stands rather than the file being left broken.
    settings.makemkv.enabled = False
    assert decide(result, settings).action in ("repair", "redownload")
