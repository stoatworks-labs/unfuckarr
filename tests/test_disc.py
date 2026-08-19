"""Disc images — the check that has already cost this library real files.

`ffprobe` cannot open a disc image. Handed one it reports "Invalid data found
when processing input", which the integrity check reads as a broken file and
the default policy answers with delete-and-re-search. On the live library that
condemned every BR-DISK in the collection; three 42 GB images survive only
because the recycle bin caught them.

So the tests that matter here are the ones that prove a disc image is
recognised, and — for the ones nothing can open — that it is *reported* rather
than condemned.
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

import pytest

from unfuckarr import disc
from unfuckarr.checks import CheckResult
from unfuckarr.checks import integrity as integrity_checks
from unfuckarr.probe import DiscUnreadable, probe

from .conftest import FFMPEG, needs_ffmpeg

SECTOR = disc.SECTOR
# integrity.TINY_FILE_BYTES is 128 KB; anything smaller is a stub before the
# question of what filesystem it holds ever comes up.
MIN_SECTORS = 128


# -- building real images, byte by byte -----------------------------------
#
# Rather than shipping a fixture or shelling out to xorriso (which is not on
# every runner), the images are assembled here. They are small, they are real
# enough for the parser, and building them documents the layout the parser
# depends on.

def _dir_record(name: str, extent: int, length: int, is_dir: bool) -> bytes:
    raw = name.encode("ascii")
    rec = bytearray(33 + len(raw))
    rec[2:6] = struct.pack("<I", extent)
    rec[6:10] = struct.pack(">I", extent)
    rec[10:14] = struct.pack("<I", length)
    rec[14:18] = struct.pack(">I", length)
    rec[25] = 0x02 if is_dir else 0x00
    rec[32] = len(raw)
    rec[33:] = raw
    if len(rec) % 2:
        rec.append(0)
    rec[0] = len(rec)
    return bytes(rec)


def build_iso9660(path: Path, files: dict[str, bytes],
                  directory: str = "VIDEO_TS") -> Path:
    """A minimal but genuine ISO9660 image holding one directory of files."""
    root_lba, sub_lba, first_file_lba = 20, 21, 22

    payloads, lba = [], first_file_lba
    sub_records = [_dir_record("\x00", sub_lba, SECTOR, True),
                   _dir_record("\x01", root_lba, SECTOR, True)]
    for name, data in files.items():
        sub_records.append(_dir_record(f"{name};1", lba, len(data), False))
        payloads.append((lba, data))
        lba += (len(data) + SECTOR - 1) // SECTOR

    root_records = [_dir_record("\x00", root_lba, SECTOR, True),
                    _dir_record("\x01", root_lba, SECTOR, True),
                    _dir_record(directory, sub_lba, SECTOR, True)]

    # Comfortably past TINY_FILE_BYTES: the size floor is checked before
    # anything disc-aware runs, and a 50 KB "disc image" is a stub whatever
    # its filesystem says.
    image = bytearray(max(lba, MIN_SECTORS) * SECTOR)

    pvd = bytearray(SECTOR)
    pvd[0] = 1                                  # Primary Volume Descriptor
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[156:190] = _dir_record("\x00", root_lba, SECTOR, True).ljust(34, b"\0")[:34]
    image[16 * SECTOR:17 * SECTOR] = pvd

    term = bytearray(SECTOR)
    term[0] = 255
    term[1:6] = b"CD001"
    image[17 * SECTOR:18 * SECTOR] = term

    image[root_lba * SECTOR:root_lba * SECTOR + sum(len(r) for r in root_records)] = \
        b"".join(root_records)
    image[sub_lba * SECTOR:sub_lba * SECTOR + sum(len(r) for r in sub_records)] = \
        b"".join(sub_records)
    for at, data in payloads:
        image[at * SECTOR:at * SECTOR + len(data)] = data

    path.write_bytes(bytes(image))
    return path


def build_udf(path: Path) -> Path:
    """A pure-UDF volume recognition sequence — what a Blu-ray actually has.

    Of 104 disc images on the live library, every one was pure UDF with no
    ISO9660 descriptor anywhere. A reader that insists on CD001 recognises
    none of them, which is exactly the bug this fixture guards.
    """
    image = bytearray(MIN_SECTORS * SECTOR)
    for i, ident in enumerate((b"BEA01", b"NSR03", b"TEA01")):
        off = (16 + i) * SECTOR
        image[off] = 0
        image[off + 1:off + 6] = ident
        image[off + 6] = 1
    path.write_bytes(bytes(image))
    return path


# -- identification -------------------------------------------------------

def test_a_pure_udf_image_is_recognised_as_a_blu_ray(tmp_path):
    d = disc.identify(str(build_udf(tmp_path / "film.iso")))
    assert d.kind == "bluray"
    assert d.filesystem == "udf"


def test_a_dvd_is_recognised_from_its_video_ts(tmp_path):
    iso = build_iso9660(tmp_path / "dvd.iso", {
        "VTS_01_0.VOB": b"menu" * 100,
        "VTS_01_1.VOB": b"title" * 400,
        "VTS_01_2.VOB": b"title" * 200,
    })
    d = disc.identify(str(iso))
    assert d.kind == "dvd"
    assert d.filesystem == "iso9660"
    # VTS_nn_0.VOB is the menu, not a title, and must not be picked.
    assert {t.name for t in d.titles} == {"VTS_01_1.VOB", "VTS_01_2.VOB"}
    assert d.largest.name == "VTS_01_1.VOB"


def test_a_blu_ray_with_an_iso9660_bridge_is_still_a_blu_ray(tmp_path):
    iso = build_iso9660(tmp_path / "bd.iso", {"INDEX.BDMV": b"x" * 64},
                        directory="BDMV")
    assert disc.identify(str(iso)).kind == "bluray"


def test_a_file_that_is_not_a_disc_image_is_not_guessed_at(tmp_path):
    plain = tmp_path / "notadisc.iso"
    plain.write_bytes(b"\0" * (MIN_SECTORS * SECTOR))
    d = disc.identify(str(plain))
    assert d.kind == "unknown"
    assert "no volume descriptors" in d.detail


def test_a_truncated_image_says_so(tmp_path):
    stub = tmp_path / "cut.iso"
    stub.write_bytes(b"\0" * 4096)
    assert disc.identify(str(stub)).kind == "unknown"


def test_only_disc_extensions_are_treated_as_discs():
    assert disc.is_disc_image("/media/Film/Film.iso")
    assert disc.is_disc_image("/media/Film/Film.IMG")
    assert not disc.is_disc_image("/media/Film/Film.mkv")


# -- turning a disc into an ffmpeg input ----------------------------------

def test_a_blu_ray_is_opened_through_libbluray(tmp_path, monkeypatch):
    iso = build_udf(tmp_path / "film.iso")
    monkeypatch.setattr(disc, "protocols_for",
                        lambda b: frozenset({"file", "bluray", "subfile"}))
    url, found = disc.input_url(str(iso))
    assert url == f"bluray:{iso}"
    assert found.kind == "bluray"


def test_a_blu_ray_without_libbluray_is_refused_not_guessed(tmp_path, monkeypatch):
    """An ISO9660 bridge cannot stand in: it cannot describe a file over 4 GB
    in one extent, so it lists the main feature in fragments. Measured on a
    real 96 GB disc the largest entry it offered was 1.1 GiB — a trailer.
    Falling back to it would silently pick the wrong title."""
    iso = build_iso9660(tmp_path / "bd.iso", {"INDEX.BDMV": b"x" * 64},
                        directory="BDMV")
    monkeypatch.setattr(disc, "protocols_for", lambda b: frozenset({"file"}))
    with pytest.raises(disc.DiscError, match="no bluray protocol"):
        disc.input_url(str(iso))


def test_a_dvd_title_is_addressed_as_a_byte_range(tmp_path, monkeypatch):
    payload = bytes(range(256)) * 40
    iso = build_iso9660(tmp_path / "dvd.iso", {"VTS_01_1.VOB": payload})
    monkeypatch.setattr(disc, "protocols_for",
                        lambda b: frozenset({"file", "subfile"}))
    url, found = disc.input_url(str(iso))

    assert url.startswith("subfile,,start,")
    start = int(url.split("start,")[1].split(",")[0])
    end = int(url.split("end,")[1].split(",")[0])
    # The range has to actually be the VOB, not merely well-formed.
    assert iso.read_bytes()[start:end] == payload


def test_libbluray_chatter_is_not_mistaken_for_damage():
    """libbluray narrates every open. Counting any of it as a decode error
    would make every disc image look broken — the exact failure being fixed."""
    for line in ("bdj.c:795: BD-J check: Failed to load JVM library",
                 "bluray.c:299: 00294.m2ts: no timestamp for SPN 0 (got 0).",
                 "[hevc @ 0x55] First slice in a frame missing."):
        assert disc.is_noise(line)
    assert not disc.is_noise("Invalid data found when processing input")
    assert not disc.is_noise("error while decoding MB 12 4, bytestream -7")


# -- the integrity check --------------------------------------------------

def test_an_unopenable_disc_is_reported_not_condemned(tmp_path, settings):
    """The whole point. A disc image nothing can open must never reach a policy
    that deletes — being unable to read a file is not evidence that the file is
    bad, and treating it as such put three 42 GB images in the recycle bin."""
    iso = tmp_path / "mystery.iso"
    iso.write_bytes(b"\0" * (MIN_SECTORS * SECTOR))

    result, info = integrity_checks.check(
        str(iso), settings.integrity, ffprobe=settings.ffprobe_path,
        ffmpeg=settings.ffmpeg_path)

    assert info is None
    codes = {f.code for f in result.findings}
    assert codes == {"disc_not_inspectable"}
    assert result.errors == [], "an unreadable disc must raise no error"
    assert result.status != "corrupt"

    from unfuckarr.remediation import decide
    assert decide(result, settings).action == "none"


def test_disc_inspection_can_be_switched_off(tmp_path, settings):
    iso = build_udf(tmp_path / "film.iso")
    settings.integrity.inspect_disc_images = False
    result, info = integrity_checks.check(
        str(iso), settings.integrity, ffprobe=settings.ffprobe_path,
        ffmpeg=settings.ffmpeg_path)
    assert {f.code for f in result.findings} == {"disc_not_inspected"}
    assert result.errors == []


@needs_ffmpeg
def test_a_disc_image_never_reaches_probe_failed(tmp_path, settings):
    """`probe_failed` is in REPAIRABLE_CODES and leads to a remux and then a
    redownload. A disc image must not be able to get there."""
    iso = build_udf(tmp_path / "film.iso")
    with pytest.raises(DiscUnreadable):
        probe(str(iso), settings.ffprobe_path, ffmpeg=settings.ffmpeg_path)


@needs_ffmpeg
def test_the_subfile_route_really_reads_video(tmp_path, settings):
    """Proves the DVD path end to end without needing a DVD: a real MPEG-PS
    stream embedded in a real ISO9660 image, read back by ffprobe through the
    byte range the parser computed."""
    vob = tmp_path / "title.vob"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=160x120:rate=25:duration=2",
         "-c:v", "mpeg2video", "-b:v", "600k", "-f", "vob", str(vob)],
        check=True, capture_output=True)

    iso = build_iso9660(tmp_path / "dvd.iso", {"VTS_01_1.VOB": vob.read_bytes()})
    url, found = disc.input_url(str(iso), ffmpeg=settings.ffmpeg_path)
    assert found.kind == "dvd"

    out = subprocess.run(
        [settings.ffprobe_path, "-v", "error", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", url],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "mpeg2video" in out.stdout


def test_colour_description_is_carried_onto_the_encode():
    """ffmpeg does not copy these across a re-encode. Losing them on an SDR
    file is invisible; losing them on HDR gives a file that still plays and
    looks grey and washed out, which is why `allow_hdr` defaults off."""
    from unfuckarr import transcode

    from .test_efficiency import media
    info = media(raw={"color_primaries": "bt2020",
                      "color_transfer": "smpte2084",
                      "color_space": "bt2020nc",
                      "color_range": "tv"})
    args = transcode.colour_args(info)
    assert args == ["-color_primaries", "bt2020",
                    "-color_trc", "smpte2084",
                    "-colorspace", "bt2020nc",
                    "-color_range", "tv"]


def test_unknown_colour_tags_are_not_invented():
    """"unknown" is what ffprobe reports for an untagged stream, and passing it
    back to the encoder as a value is worse than saying nothing."""
    from unfuckarr import transcode

    from .test_efficiency import media
    assert transcode.colour_args(media(raw={"color_primaries": "unknown"})) == []
    assert transcode.colour_args(media(raw={})) == []


@needs_ffmpeg
def test_the_encode_command_carries_them(video_factory, settings, tmp_path):
    """Same thing, through the real command builder and a real encode."""
    from unfuckarr import transcode
    from unfuckarr.probe import probe

    src = video_factory("colour.mkv", seconds=3, size="160x120",
                        extra=["-colorspace", "bt2020nc"])
    info = probe(str(src), settings.ffprobe_path)
    assert info.video.raw.get("color_space") == "bt2020nc", "fixture not tagged"

    plan = transcode.TranscodePlan(reason="shrink", video_action="encode",
                                   codec="hevc", crf=30, is_shrink=True)
    cmd = transcode.build_command(str(src), str(tmp_path / "out.mkv"), info,
                                  plan, settings.transcode,
                                  ffmpeg=settings.ffmpeg_path)
    assert "-colorspace" in cmd and "bt2020nc" in cmd

    subprocess.run(cmd, check=True, capture_output=True)
    out = probe(str(tmp_path / "out.mkv"), settings.ffprobe_path)
    assert out.video.raw.get("color_space") == "bt2020nc", \
        "the re-encode dropped the colour description"
