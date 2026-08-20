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


# -- UDF ------------------------------------------------------------------
#
# A Blu-ray is UDF, and ffmpeg can only open one through libbluray — which the
# ffmpeg this image is built from does not have, and which has now been the
# reason disc support broke twice. So the filesystem is read here instead, and
# the resulting byte range is handed to ffmpeg's `subfile` protocol, which is a
# builtin and cannot be configured away.
#
# Only the parts needed to find one file are implemented: anchor → partition →
# logical volume → file set → root directory → walk. No writing, no permissions,
# no metadata beyond names and extents.

# Descriptor tag identifiers (ECMA-167 3/7.2).
TAG_PRIMARY_VOLUME = 1
TAG_ANCHOR = 2
TAG_PARTITION = 5
TAG_LOGICAL_VOLUME = 6
TAG_TERMINATING = 8
TAG_FILE_SET = 256
TAG_FILE_IDENTIFIER = 257
TAG_FILE_ENTRY = 261
TAG_EXTENDED_FILE_ENTRY = 266

# The anchor is required to be at sector 256; the other two locations are
# mirrors at the end of the volume, used here only if the first is unreadable.
ANCHOR_SECTORS = (256, 512)

# An extent's length field carries its type in the top two bits, so a length is
# only 30 bits — which is why a 30 GB stream is recorded as ~30 extents rather
# than one, and why they have to be stitched back together below.
EXTENT_LENGTH_MASK = 0x3FFFFFFF
EXTENT_TYPE_SHIFT = 30
EXTENT_RECORDED = 0

# ICB file types (ECMA-167 4/14.6.6).
ICB_DIRECTORY = 4
ICB_FILE = 5

# A title in more pieces than this is not something to stitch together with a
# concat URL; something about the disc is unusual and saying so beats guessing.
# Real discs measured here were one run, or two.
MAX_RUNS = 8

# How much of the stream to read when measuring its duration. The head only
# has to reach the first video packet; the tail has to contain the last one,
# and a 24 MB window comfortably does at Blu-ray bitrates.
DURATION_HEAD = 8 * 1024 * 1024
DURATION_TAIL = 24 * 1024 * 1024

# MPEG-2 timestamps are 33 bits at 90 kHz, so they wrap after about 26.5
# hours. Nothing here is that long, but a wrap between the two probes would
# otherwise read as a negative duration.
PTS_WRAP_SECONDS = (1 << 33) / 90000


@dataclass
class Extent:
    """One contiguous run of bytes belonging to a file."""

    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass
class PartitionMap:
    """Where a partition's logical blocks actually live.

    ``metadata_extents`` is set for a UDF 2.50 metadata partition, whose block
    space is the contents of a file rather than a run of the disc.
    """

    start: int = 0
    metadata_extents: list[tuple[int, int]] | None = None


@dataclass
class UdfFile:
    name: str
    is_dir: bool
    extents: list[Extent] = field(default_factory=list)
    length: int = 0
    partition: int = 0

    @property
    def contiguous(self) -> Extent | None:
        """The whole file as one range, when its extents happen to be adjacent.

        They usually are: UDF splits a large file only because the length
        field is 30 bits, not because the data is scattered. When they are not
        adjacent this returns None and the caller has to say so rather than
        silently reading the wrong bytes.
        """
        if not self.extents:
            return None
        merged = self.extents[0]
        for nxt in self.extents[1:]:
            if nxt.offset != merged.end:
                return None
            merged = Extent(merged.offset, merged.length + nxt.length)
        return merged


def _tag_id(block: bytes) -> int:
    return struct.unpack_from("<H", block, 0)[0] if len(block) >= 2 else 0


def _long_ad(block: bytes, at: int) -> tuple[int, int, int]:
    """(length, logical block, partition) from a long_ad."""
    length, lbn, part = struct.unpack_from("<IIH", block, at)
    return length, lbn, part


class Udf:
    """Just enough UDF to find one file and say where its bytes are.

    The awkward part is the **metadata partition**. UDF 2.50 — which is what
    BD-ROM uses — does not necessarily put file metadata directly in the
    physical partition. It can put it inside a *metadata file*, and then
    address it through a second, virtual partition whose block numbers are
    offsets into that file's extents. Every disc measured here does exactly
    that, so a reader that resolves logical blocks directly finds the file set
    descriptor missing and gives up. (It is also what libudfread fails on for
    a handful of discs: "read metadata file 0: unexpected tag 261".)

    So partition maps are parsed, and every logical block is resolved through
    the map its long_ad names.
    """

    def __init__(self, fh):
        self.fh = fh
        self.block_size = SECTOR
        self.maps: list[PartitionMap] = []
        self.root: UdfFile | None = None
        self._root_partition = 0

    # -- raw access -------------------------------------------------------

    def _sector(self, n: int, count: int = 1) -> bytes:
        self.fh.seek(n * self.block_size)
        data = self.fh.read(self.block_size * count)
        if len(data) < self.block_size:
            raise DiscError(f"image ends before block {n}")
        return data

    def physical(self, lbn: int, partition: int) -> int:
        """Where a *metadata* block lives — a file entry, or directory data.

        See ``physical_data`` for the other half of this: in a UDF 2.50
        volume the two are not the same place.
        """
        if partition >= len(self.maps):
            raise DiscError(f"long_ad names partition {partition}, "
                            f"but the volume declares {len(self.maps)}")
        pmap = self.maps[partition]
        if pmap.metadata_extents is None:
            return pmap.start + lbn
        # Walk the metadata file's extents: its blocks, in order, *are* the
        # virtual partition's block space.
        remaining = lbn
        for first, count in pmap.metadata_extents:
            if remaining < count:
                return first + remaining
            remaining -= count
        raise DiscError(f"block {lbn} is past the end of the metadata file")

    def physical_data(self, lbn: int, partition: int) -> int:
        """Where *file data* lives, which is not where its metadata lives.

        A metadata partition holds file entries and directory contents; the
        file data those entries describe stays in the physical partition
        underneath. Resolving data blocks through the metadata file — the
        obvious thing, since that is the partition the entry was read from —
        lands past its end, which is exactly the symptom that gave this away:
        every directory read fine and every .m2ts came back "past the end of
        the metadata file".
        """
        if partition >= len(self.maps):
            raise DiscError(f"long_ad names partition {partition}, "
                            f"but the volume declares {len(self.maps)}")
        return self.maps[partition].start + lbn

    def _block(self, lbn: int, partition: int, count: int = 1) -> bytes:
        return self._sector(self.physical(lbn, partition), count)

    # -- the descriptor chain ---------------------------------------------

    def open(self) -> None:
        location, length = self._anchor()

        partitions: dict[int, int] = {}
        raw_maps: list[bytes] = []
        fsd_lbn = fsd_part = None

        for i in range(max(1, length // self.block_size)):
            try:
                block = self._sector(location + i)
            except DiscError:
                break
            tag = _tag_id(block)
            if tag in (0, TAG_TERMINATING):
                break
            if tag == TAG_PARTITION:
                number = struct.unpack_from("<H", block, 22)[0]
                partitions[number] = struct.unpack_from("<I", block, 188)[0]
            elif tag == TAG_LOGICAL_VOLUME:
                self.block_size = struct.unpack_from("<I", block, 212)[0] or SECTOR
                _, fsd_lbn, fsd_part = _long_ad(block, 248)
                map_bytes = struct.unpack_from("<I", block, 264)[0]
                map_count = struct.unpack_from("<I", block, 268)[0]
                raw_maps = _split_partition_maps(block[440:440 + map_bytes], map_count)

        if fsd_lbn is None:
            raise DiscError("no UDF logical volume descriptor")
        if not partitions:
            raise DiscError("no UDF partition descriptor")

        self.maps = self._build_maps(raw_maps, partitions)
        self._root_partition = fsd_part or 0

        fsd = self._block(fsd_lbn, self._root_partition)
        if _tag_id(fsd) != TAG_FILE_SET:
            raise DiscError("UDF file set descriptor not where the volume said")
        _, root_lbn, root_part = _long_ad(fsd, 400)
        self._root_partition = root_part
        self.root = self._entry("/", True, root_lbn, root_part)

    def _anchor(self) -> tuple[int, int]:
        for sector in ANCHOR_SECTORS:
            try:
                block = self._sector(sector)
            except DiscError:
                continue
            if _tag_id(block) == TAG_ANCHOR:
                length, location = struct.unpack_from("<II", block, 16)
                return location, length
        raise DiscError("no UDF anchor descriptor at sector 256")

    def _build_maps(self, raw_maps: list[bytes],
                    partitions: dict[int, int]) -> list[PartitionMap]:
        """One entry per declared map, in the order long_ads refer to them."""
        out: list[PartitionMap] = []
        for raw in raw_maps:
            if not raw:
                continue
            kind = raw[0]
            if kind == 1 and len(raw) >= 6:
                number = struct.unpack_from("<H", raw, 4)[0]
                out.append(PartitionMap(start=partitions.get(number, 0)))
                continue
            if kind == 2 and len(raw) >= 64:
                identifier = raw[5:36].split(b"\x00")[0]
                number = struct.unpack_from("<H", raw, 38)[0]
                base = partitions.get(number, 0)
                if b"Metadata Partition" in identifier:
                    file_lbn = struct.unpack_from("<I", raw, 40)[0]
                    out.append(PartitionMap(
                        start=base,
                        metadata_extents=self._metadata_extents(base, file_lbn)))
                    continue
                # A virtual or sparable partition. Neither appears on a
                # BD-ROM, and guessing at one would read the wrong blocks.
                raise DiscError(
                    f"unsupported UDF partition map: {identifier.decode('latin-1', 'replace')}")
            raise DiscError(f"unrecognised UDF partition map type {kind}")
        if not out:
            # No maps declared: a plain single-partition volume.
            out.append(PartitionMap(start=next(iter(partitions.values()))))
        return out

    def _metadata_extents(self, base: int, file_lbn: int) -> list[tuple[int, int]]:
        """The physical blocks the metadata file occupies, in order."""
        block = self._sector(base + file_lbn, 2)
        _, _, blocks = _parse_file_entry(block)
        out = [(base + lbn, (size + self.block_size - 1) // self.block_size)
               for lbn, size in blocks]
        if not out:
            raise DiscError("the UDF metadata file has no extents")
        return out

    # -- files and directories --------------------------------------------

    def _entry(self, name: str, is_dir: bool, lbn: int, partition: int) -> UdfFile:
        block = self._block(lbn, partition, 2)
        file_type, length, blocks = _parse_file_entry(block)
        directory = file_type == ICB_DIRECTORY or is_dir
        # Directory contents are metadata and live with the entry; file data
        # does not. Using one resolver for both is the bug this split exists
        # to prevent.
        resolve = self.physical if directory else self.physical_data
        extents = [Extent(resolve(lbn_, partition) * self.block_size, size)
                   for lbn_, size in blocks]
        return UdfFile(name, directory, extents, length, partition)

    def listdir(self, directory: UdfFile) -> list[UdfFile]:
        """The File Identifier Descriptors inside a directory."""
        if not directory.is_dir:
            return []
        data = b"".join(self._read_extent(e) for e in directory.extents)
        out: list[UdfFile] = []
        at = 0
        while at + 38 <= len(data):
            if _tag_id(data[at:at + 2]) != TAG_FILE_IDENTIFIER:
                break
            characteristics = data[at + 18]
            name_len = data[at + 19]
            _, lbn, part = _long_ad(data, at + 20)
            iu_len = struct.unpack_from("<H", data, at + 36)[0]
            name_at = at + 38 + iu_len
            raw = data[name_at:name_at + name_len]
            at = (at + 38 + iu_len + name_len + 3) & ~3
            if characteristics & 0x08:          # the parent entry, unnamed
                continue
            name = _dstring(raw)
            if not name:
                continue
            try:
                out.append(self._entry(name, bool(characteristics & 0x02),
                                       lbn, part))
            except DiscError:
                continue
        return out

    def _read_extent(self, extent: Extent) -> bytes:
        self.fh.seek(extent.offset)
        return self.fh.read(extent.length)

    def find(self, *parts: str) -> UdfFile | None:
        """Walk a path from the root, case-insensitively."""
        if self.root is None:
            return None
        node = self.root
        for part in parts:
            match = next((c for c in self.listdir(node)
                          if c.name.upper() == part.upper()), None)
            if match is None:
                return None
            node = match
        return node


def _split_partition_maps(table: bytes, count: int) -> list[bytes]:
    """The map table is a run of variable-length records."""
    out, at = [], 0
    for _ in range(count):
        if at + 2 > len(table):
            break
        length = table[at + 1]
        if length == 0:
            break
        out.append(table[at:at + length])
        at += length
    return out


def _parse_file_entry(block: bytes) -> tuple[int, int, list[tuple[int, int]]]:
    """(file type, information length, [(logical block, byte length), ...]).

    Deliberately returns *logical* blocks: whether they resolve through the
    metadata partition or the physical one underneath depends on what kind of
    file this is, and only the caller knows that.
    """
    tag = _tag_id(block)
    if tag == TAG_FILE_ENTRY:
        head, ea_at = 176, 168
    elif tag == TAG_EXTENDED_FILE_ENTRY:
        head, ea_at = 216, 208
    else:
        raise DiscError(f"expected a file entry, found descriptor tag {tag}")

    file_type = block[16 + 11]
    icb_flags = struct.unpack_from("<H", block, 16 + 18)[0]
    ad_type = icb_flags & 0x07
    info_length = struct.unpack_from("<Q", block, 56)[0]
    ea_length = struct.unpack_from("<I", block, ea_at)[0]
    ad_length = struct.unpack_from("<I", block, ea_at + 4)[0]
    at = head + ea_length

    blocks: list[tuple[int, int]] = []
    if ad_type == 0:                        # short_ad
        stride, unpack = 8, lambda off: struct.unpack_from("<II", block, off)
    elif ad_type == 1:                      # long_ad
        stride, unpack = 16, lambda off: struct.unpack_from("<II", block, off)
    elif ad_type == 3:
        raise DiscError("UDF file stored inline, which no media file is")
    else:
        raise DiscError(f"unsupported UDF allocation descriptor type {ad_type}")

    for i in range(ad_length // stride):
        raw_len, pos = unpack(at + i * stride)
        if raw_len >> EXTENT_TYPE_SHIFT != EXTENT_RECORDED:
            continue
        length = raw_len & EXTENT_LENGTH_MASK
        if length:
            blocks.append((pos, length))

    return file_type, info_length, blocks


def _dstring(raw: bytes) -> str:
    """A UDF d-string: the first byte says how the rest is encoded."""
    if not raw:
        return ""
    kind, body = raw[0], raw[1:]
    if kind == 8:
        return body.decode("latin-1", "replace").rstrip("\x00")
    if kind == 16:
        return body.decode("utf-16-be", "replace").rstrip("\x00")
    return body.decode("latin-1", "replace").rstrip("\x00")


def merge_extents(extents: list[Extent]) -> list[Extent]:
    """Join runs that are adjacent on disc.

    UDF splits a large file because its extent length field is only 30 bits,
    not because the data is scattered: a 77 GiB stream arrives as 78 extents
    that are almost always one contiguous run. Measured across a real library,
    most titles merged to exactly one run and the rest to two.
    """
    if not extents:
        return []
    runs = [Extent(extents[0].offset, extents[0].length)]
    for nxt in extents[1:]:
        if nxt.offset == runs[-1].end:
            runs[-1] = Extent(runs[-1].offset, runs[-1].length + nxt.length)
        else:
            runs.append(Extent(nxt.offset, nxt.length))
    return runs


def stream_duration(path: str, runs: list[Extent],
                    ffprobe: str = "ffprobe", timeout: int = 120) -> float:
    """How long the stream is, from its first and last timestamps.

    A raw MPEG-TS read through `subfile` does not report a usable duration —
    ffprobe guessed 117 seconds for a 137-minute film — and that number is
    load-bearing: the integrity check compares it against the runtime the *arr
    expects, and a film that reads as two minutes long is "truncated" and gets
    deleted. So it is measured instead, from the presentation timestamp of the
    first video packet and of the last one, each found by probing a small
    window rather than reading 77 GiB.

    Cross-checked against libbluray on a disc both can read: 8201.7 seconds
    here against libbluray's 8201.4.
    """
    if not runs:
        return 0.0
    first = _first_pts(subfile_url(path, runs[0].offset,
                                   min(DURATION_HEAD, runs[0].length)),
                       ffprobe, timeout, last=False)
    tail = runs[-1]
    window = min(DURATION_TAIL, tail.length)
    last = _first_pts(subfile_url(path, tail.end - window, window),
                      ffprobe, timeout, last=True)
    if first is None or last is None:
        return 0.0
    duration = last - first
    if duration < 0:
        duration += PTS_WRAP_SECONDS
    return duration if duration > 0 else 0.0


def _first_pts(url: str, ffprobe: str, timeout: int, last: bool) -> float | None:
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time", "-of", "csv=p=0"]
    if not last:
        cmd += ["-read_intervals", "%+#1"]
    cmd += [url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    values = [ln.split(",")[0] for ln in (proc.stdout or "").splitlines()
              if ln.strip() and ln.split(",")[0] not in ("N/A", "")]
    if not values:
        return None
    try:
        return float(values[-1] if last else values[0])
    except ValueError:
        return None


def bluray_main_title(path: str) -> UdfFile:
    """The largest stream on a Blu-ray, which is the feature.

    Reading the playlists in BDMV/PLAYLIST would be more correct — they are
    what a player follows — but the largest .m2ts is the feature on
    essentially every disc, and it is the same heuristic the DVD path uses for
    VOBs. Getting it wrong picks a trailer, which the duration check then
    reports as far shorter than the *arr expects rather than quietly
    shrinking the wrong thing.
    """
    with open(path, "rb") as fh:
        udf = Udf(fh)
        udf.open()
        stream = udf.find("BDMV", "STREAM")
        if stream is None:
            raise DiscError("UDF volume with no BDMV/STREAM directory")
        titles = [f for f in udf.listdir(stream)
                  if not f.is_dir and f.name.upper().endswith(".M2TS")]
        if not titles:
            raise DiscError("no .m2ts streams in BDMV/STREAM")
        return max(titles, key=lambda f: f.length)


# -- identification ------------------------------------------------------

@dataclass
class Disc:
    kind: str                      # bluray | dvd | unknown
    path: str
    filesystem: str = ""           # udf | iso9660 | udf+iso9660 | ""
    titles: list[Entry] = field(default_factory=list)
    detail: str = ""
    # The byte runs of the chosen title, when it was found by reading the
    # filesystem here. Kept because the duration has to be measured from them
    # afterwards — see `stream_duration`.
    runs: list["Extent"] = field(default_factory=list)

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
        # libbluray first when it is there: it follows the disc's own
        # playlists, which is more faithful than picking the largest stream.
        if "bluray" in protocols:
            return f"bluray:{path}", disc

        # Otherwise read the UDF here and address the stream directly. This is
        # the durable path — `subfile` is a builtin, so unlike libbluray it
        # cannot be configured out of an ffmpeg build, which has now been the
        # reason disc support broke twice.
        if "subfile" not in protocols:
            raise DiscError("this ffmpeg has no subfile protocol")
        title = bluray_main_title(path)
        runs = merge_extents(title.extents)
        if len(runs) > MAX_RUNS:
            raise DiscError(
                f"{title.name} is stored in {len(runs)} separate pieces, which "
                "is more than can sensibly be stitched back together")
        disc.runs = runs
        disc.titles = [Entry(title.name, runs[0].offset, title.length, False)]
        disc.detail = (f"UDF, {title.name} ({title.length / 2**30:.1f} GiB"
                       + (f", {len(runs)} pieces)" if len(runs) > 1 else ")"))
        if len(runs) == 1:
            return subfile_url(path, runs[0].offset, runs[0].length), disc
        # More than one run. MPEG-TS is a stream of fixed-size packets with no
        # header to reconcile, so the pieces can simply be read one after the
        # other — which is what `concat:` does. Verified against a disc
        # libudfread cannot open at all.
        return ("concat:" + "|".join(
            subfile_url(path, r.offset, r.length) for r in runs), disc)

    if disc.kind == "dvd":
        title = disc.largest
        if title is None:
            raise DiscError("DVD image with no title VOBs in VIDEO_TS")
        if "subfile" not in protocols:
            raise DiscError("this ffmpeg has no subfile protocol")
        # ISO9660 is safe for a DVD where it would not be for a Blu-ray: the
        # DVD-Video spec caps a VOB at 1 GB precisely so it fits ISO9660's
        # single-extent limit, so the directory entry is the whole file. A
        # Blu-ray's ISO9660 bridge, by contrast, lists a 60 GB feature in
        # fragments — measured on a real 96 GB disc, the largest entry it
        # offered was 1.1 GiB, a trailer. That is why Blu-rays are read
        # through UDF above and never through a bridge.
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
