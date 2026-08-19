"""Disc images: reading a Blu-ray or DVD ISO without mounting it.

ffprobe cannot read a disc image. Handed one it says "Invalid data found when
processing input", which the integrity check quite reasonably reads as a file
that is broken — and the default policy for a broken file is to delete it and
ask the *arr for another copy.

That is not theoretical. On the live library it condemned every BR-DISK in the
collection, and the recycle bin is the only reason three 42 GB images are still
recoverable. A disc image is not corrupt; it is a container this application
did not know how to open.

So open it. Two routes, neither of which needs a loop mount, `SYS_ADMIN`, or
any privilege the container does not already have:

* **Blu-ray** — ffmpeg's `bluray:` protocol. libbluray bundles libudfread and
  reads the UDF filesystem straight out of the image file, then presents the
  longest playlist as an MPEG-TS stream. Debian's ffmpeg is built
  `--enable-libbluray`, so this works in the shipped container. Seeking works
  too, which is what makes the quality search usable on a disc.
* **DVD** — ffmpeg has no `dvd:` protocol (Debian bookworm builds without
  libdvdnav/libdvdread), so the ISO9660 directory is parsed here, in Python,
  to find the VOBs. A VOB is plain MPEG-PS and ISO9660 stores files
  contiguously, so each one can be handed to ffmpeg through the `subfile`
  protocol as a byte range of the image. No mount, no extra dependency.

Anything neither route can open is reported as *not inspectable*, at info
severity, and nothing acts on it. Being unable to read a file is not evidence
that the file is bad, and this is the one place in the codebase where that
distinction has already cost real media.
"""

from __future__ import annotations

import logging
import re
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Extensions that are a disc image rather than a media file.
DISC_EXTENSIONS = {".iso", ".img"}

# Both ISO9660 and UDF use 2048-byte sectors and put the Volume Recognition
# Sequence at sector 16.
SECTOR = 2048
VRS_SECTOR = 16
VRS_SECTORS = 16          # the sequence is short; 16 is generous

# Standard identifiers found in the VRS. NSR02/NSR03 mean UDF, which is what a
# Blu-ray uses — and, measured against a real library, what it uses *instead*
# of ISO9660: of 104 disc images, every single one was pure UDF (BEA01, NSR03,
# TEA01) with no ISO9660 descriptor anywhere. An identifier that insists on
# CD001 recognises none of them.
UDF_IDS = {b"NSR02", b"NSR03"}
ISO_ID = b"CD001"

_protocols_cache: dict[str, frozenset[str]] = {}


class DiscError(RuntimeError):
    """The image could not be opened."""


def is_disc_image(path: str) -> bool:
    return Path(path).suffix.lower() in DISC_EXTENSIONS


def protocols_for(binary: str) -> frozenset[str]:
    """Which input protocols an ffmpeg build has. Cached — it costs a process."""
    if binary in _protocols_cache:
        return _protocols_cache[binary]
    names: set[str] = set()
    try:
        proc = subprocess.run([binary, "-hide_banner", "-nostdin", "-protocols"],
                              capture_output=True, text=True, timeout=30)
        # Output is "Input:\n  file\n  http\n...Output:\n..." — take everything,
        # since we only ever ask about input protocols anyway.
        for line in proc.stdout.splitlines():
            name = line.strip()
            if name and " " not in name and not name.endswith(":"):
                names.add(name)
    except (OSError, subprocess.SubprocessError):
        names = set()
    frozen = frozenset(names)
    _protocols_cache[binary] = frozen
    return frozen


def forget_protocols() -> None:
    _protocols_cache.clear()


# -- ISO9660 -------------------------------------------------------------

@dataclass
class Entry:
    name: str
    start: int          # byte offset into the image
    length: int
    is_dir: bool


def _read(fh, offset: int, size: int) -> bytes:
    fh.seek(offset)
    data = fh.read(size)
    if len(data) < size:
        raise DiscError(f"image ends at {offset + len(data)}, wanted {offset + size}")
    return data


def _records(block: bytes) -> list[Entry]:
    """Parse ISO9660 directory records out of one directory extent."""
    out: list[Entry] = []
    i = 0
    while i < len(block):
        length = block[i]
        if length == 0:
            # Records do not straddle a sector boundary; a zero length means
            # "skip to the next sector".
            i = (i // SECTOR + 1) * SECTOR
            if i >= len(block):
                break
            continue
        rec = block[i:i + length]
        if len(rec) < 33:
            break
        extent = struct.unpack("<I", rec[2:6])[0]
        size = struct.unpack("<I", rec[10:14])[0]
        flags = rec[25]
        name_len = rec[32]
        name = rec[33:33 + name_len].decode("latin-1", "replace")
        # "FILE.EXT;1" — the version suffix is noise.
        name = name.split(";")[0].upper()
        if name not in ("\x00", "\x01"):
            out.append(Entry(name, extent * SECTOR, size, bool(flags & 0x02)))
        i += length
    return out


def read_directory(fh, entry: Entry) -> list[Entry]:
    return _records(_read(fh, entry.start, entry.length))


def volume_ids(fh) -> list[bytes]:
    """The standard identifiers in the Volume Recognition Sequence."""
    ids: list[bytes] = []
    for sector in range(VRS_SECTOR, VRS_SECTOR + VRS_SECTORS):
        try:
            block = _read(fh, sector * SECTOR, 8)
        except DiscError:
            break
        ident = block[1:6]
        if not ident.isalnum():
            break
        ids.append(ident)
        if ident == b"TEA01":
            break
    return ids


def iso_root(fh) -> Entry:
    """The root directory record from the ISO9660 Primary Volume Descriptor.

    Only reachable when the image actually has one; a Blu-ray usually does
    not.
    """
    for sector in range(VRS_SECTOR, VRS_SECTOR + VRS_SECTORS):
        block = _read(fh, sector * SECTOR, SECTOR)
        if block[1:6] != ISO_ID:
            continue
        if block[0] != 1:            # 1 = Primary Volume Descriptor
            continue
        rec = block[156:190]
        extent = struct.unpack("<I", rec[2:6])[0]
        size = struct.unpack("<I", rec[10:14])[0]
        return Entry("/", extent * SECTOR, size, True)
    raise DiscError("no ISO9660 primary volume descriptor")


# -- identification ------------------------------------------------------

@dataclass
class Disc:
    kind: str                      # bluray | dvd | unknown
    path: str
    filesystem: str = ""           # udf | iso9660 | udf+iso9660 | ""
    titles: list[Entry] = field(default_factory=list)
    detail: str = ""

    @property
    def largest(self) -> Entry | None:
        return max(self.titles, key=lambda e: e.length) if self.titles else None


def identify(path: str) -> Disc:
    """Work out what kind of disc the image holds, and who can read it.

    Reads a few kilobytes, not the image. A BD-DISK is 40 GB and identifying
    one must not become a reason to touch all of it.

    UDF is not parsed here. It could be — anchor pointer, partition
    descriptor, file set descriptor, ICBs — but libbluray already does it,
    correctly, inside the container, and the only question this needs to
    answer is which reader to hand the image to. ISO9660 *is* parsed, because
    that is the DVD case and nothing in the shipped ffmpeg can read a DVD at
    all (Debian builds without libdvdread).
    """
    try:
        with open(path, "rb") as fh:
            ids = volume_ids(fh)
            has_udf = any(i in UDF_IDS for i in ids)
            has_iso = ISO_ID in ids
            fs = "+".join(
                [n for n, present in (("udf", has_udf), ("iso9660", has_iso))
                 if present])

            if has_iso:
                # An ISO9660 directory says outright which kind of disc it is,
                # so prefer it when it is there — it is the only way to tell a
                # DVD from a Blu-ray without parsing UDF.
                try:
                    top = {e.name: e
                           for e in read_directory(fh, iso_root(fh))}
                except DiscError:
                    top = {}
                if "VIDEO_TS" in top:
                    vts = read_directory(fh, top["VIDEO_TS"])
                    # VTS_nn_m.VOB hold the titles; VTS_nn_0.VOB is the menu
                    # and VIDEO_TS.VOB is the disc intro.
                    vobs = [e for e in vts
                            if re.fullmatch(r"VTS_\d\d_[1-9]\.VOB", e.name)]
                    return Disc("dvd", path, fs, vobs,
                                f"VIDEO_TS, {len(vobs)} title VOB(s)")
                if "BDMV" in top:
                    return Disc("bluray", path, fs, detail="BDMV")

            if has_udf:
                # Pure UDF. In practice that is a Blu-ray — and it is what
                # every image in the live library turned out to be. libbluray
                # reads UDF straight out of the file; if the image is really
                # something else it fails there, and the caller reports "not
                # inspectable" rather than "corrupt".
                return Disc("bluray", path, fs, detail="UDF")

            if not ids:
                return Disc("unknown", path, fs,
                            detail="no volume descriptors at sector 16 — "
                                   "not a disc image, or truncated before the "
                                   "filesystem starts")
            return Disc("unknown", path, fs,
                        detail="unrecognised volume descriptors: "
                               + ", ".join(i.decode("latin-1") for i in ids[:4]))
    except DiscError as exc:
        return Disc("unknown", path, detail=str(exc))
    except OSError as exc:
        return Disc("unknown", path, detail=f"cannot read: {exc}")


# -- turning a disc into something ffmpeg can open -----------------------

def subfile_url(path: str, start: int, length: int) -> str:
    """A byte range of a file, as ffmpeg's `subfile` protocol wants it.

    ISO9660 stores every file in one contiguous run, so a VOB's extent is all
    ffmpeg needs to demux it as the plain MPEG-PS it is.

    The doubled commas are not a typo and cannot be tidied away: ffmpeg's
    option-carrying URL syntax is ``proto,<sep>opts<sep>,:url``, and dropping
    either one fails with "Error parsing options string" rather than anything
    that points at the cause.
    """
    return f"subfile,,start,{start},end,{start + length},,:{path}"


def input_url(path: str, ffmpeg: str = "ffmpeg",
              disc: Disc | None = None) -> tuple[str, Disc]:
    """What to hand ffmpeg for this disc image.

    Raises DiscError when there is no route, so that the caller can say "not
    inspectable" rather than "corrupt". The difference between those two
    sentences is a 40 GB file.
    """
    disc = disc or identify(path)
    protocols = protocols_for(ffmpeg)

    if disc.kind == "bluray":
        if "bluray" not in protocols:
            raise DiscError(
                "this is a Blu-ray image, but this ffmpeg has no bluray "
                "protocol (it needs to be built --enable-libbluray)")
        # libbluray or nothing, deliberately. Some images carry an ISO9660
        # bridge alongside the UDF, and it is tempting to fall back to walking
        # that and picking the largest .m2ts when libudfread cannot read the
        # UDF. Do not: ISO9660 cannot describe a file over 4 GB in one extent,
        # so the bridge lists the main feature in fragments. Measured on a
        # real 96 GB disc, the largest entry the bridge offered was 1.1 GiB —
        # a trailer. A fallback like that does not fail, it silently picks the
        # wrong title, which is far worse than saying so.
        return f"bluray:{path}", disc

    if disc.kind == "dvd":
        title = disc.largest
        if title is None:
            raise DiscError("DVD image with no title VOBs in VIDEO_TS")
        if "subfile" not in protocols:
            raise DiscError("this ffmpeg has no subfile protocol")
        # Safe here where it is not for Blu-ray: the DVD-Video spec caps a VOB
        # at 1 GB precisely so it fits ISO9660's single-extent limit, so the
        # directory entry is the whole file.
        return subfile_url(path, title.start, title.length), disc

    raise DiscError(disc.detail or "unrecognised disc image")


# libbluray writes these to stderr on every open. They are not problems and
# must not be counted as decode errors — "BD-J check" is the disc's Java menus,
# which nothing here cares about, and the SPN warning is normal for a playlist
# whose first clip has no timestamp.
NOISE = re.compile(
    r"BD-J check|failed to load jvm|no timestamp for SPN|"
    r"bdj\.c:|bluray\.c:|First slice in a frame missing|"
    r"Unsupported codec|aacs|libaacs|bdplus",
    re.IGNORECASE,
)


def is_noise(line: str) -> bool:
    return bool(NOISE.search(line))
