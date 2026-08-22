"""Turning a disc image into Matroska, through MakeMKV.

`disc.py` reads an image well enough to *judge* it — is it intact, how long is
it, what is in it — by walking UDF/ISO9660 itself and handing ffmpeg a byte
range. That is deliberately the least machinery that answers the question, and
it is not enough to *convert* one:

- **The main title is not a file.** On a Blu-ray it is a playlist (`.mpls`)
  naming a sequence of `.m2ts` segments, and seamless branching means the
  segments are shared between several playlists in different orders.
  `disc.bluray_main_title` takes the largest `.m2ts`, which is a heuristic that
  reads short on exactly the discs people care about — measured live, a full
  encode came out 1620s of a 6604s film.
- **The extras are titles too**, and there is nothing in the filesystem that
  says which is the feature and which is a deleted scene.

MakeMKV resolves playlists properly, and that is the whole reason it is here.
It is *not* here for decryption: 99 of the 104 images in the live library open
through ffmpeg's `bluray:` protocol already, which they could not do if they
were still encrypted.

**It is never bundled.** The binary half of MakeMKV is not redistributable, so
baking it into a public image is not available to us, and its beta key expires
about monthly — an unattended service cannot depend on something that stops
working one morning with nothing changed. So this module runs whatever command
the operator configures: `makemkvcon` on the PATH, or a `docker run ...` shim
naming a container that has it. Two consequences worth stating plainly:

1. **Paths must mean the same thing on both sides.** A shim that mounts the
   array somewhere else hands MakeMKV a path that does not exist, and the error
   comes back as "cannot open disc" with nothing about mounts in it.
2. **An expired key is a configuration problem, not a property of the file.**
   It must never be recorded as "this disc cannot be converted" — see
   `KeyExpired`, which the caller treats the way a missing VMAF binary is
   treated.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

# MakeMKV's robot output (`-r`) is one record per line: PREFIX:csv. Values are
# either bare integers or double-quoted strings with `"` escaped as `""`.
_RECORD = re.compile(r"^([A-Z]+):(.*)$")

# ap_ItemAttributeId, the subset this module reads. The full list is in
# MakeMKV's apdefs.h; these are the ones that decide anything here.
ATTR_NAME = 2
ATTR_CHAPTERS = 8
ATTR_DURATION = 9
ATTR_SIZE_BYTES = 11
ATTR_SOURCE_FILE = 16
ATTR_SEGMENT_COUNT = 25
ATTR_SEGMENT_MAP = 26
ATTR_OUTPUT_FILE = 27

# Messages that mean "the installation is not usable", as opposed to "this disc
# is not usable". Matched on text rather than MSG code, because the codes have
# moved between MakeMKV releases and the wording has not.
#
# The first two patterns are transcribed from a real makemkvcon 1.18.4 with a
# lapsed beta key, run against a Blu-ray image from the live library:
#
#   MSG:5073,260,0,"Your temporary key has expired and was removed. ..."
#   MSG:5021,131332,1,"This application version is too old.  Please download
#                      the latest version at ... or enter a registration key
#                      to continue using the current version."
#
# That run also settles what happens without this check: MakeMKV reported *no
# titles at all*, which `titles` raises as "cannot read this image" — and the
# caller records that against the disc, permanently, for something a new key
# fixes. It would have fired on the first disc anyone converted.
_KEY_TROUBLE = re.compile(
    r"((temporary|registration|beta|activation) key .{0,40}"
    r"(expired|was removed|is not valid)"
    r"|evaluation (period|version) has expired"
    r"|application version is too old"
    r"|enter a (valid |)registration key)",
    re.I,
)


class MakeMKVError(RuntimeError):
    """MakeMKV could not do what it was asked."""


class Unavailable(MakeMKVError):
    """The configured command could not be run at all."""


class KeyExpired(Unavailable):
    """The installation needs a new key.

    Separate from every other failure because it is temporary and global: it
    says nothing about the disc, and recording it against one would write that
    disc off permanently for a reason that a new key fixes.
    """


@dataclass
class Title:
    """One title MakeMKV is offering from the image."""

    index: int
    seconds: float = 0.0
    size: int = 0
    chapters: int = 0
    name: str = ""
    output_name: str = ""
    segments: str = ""
    source_files: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """What makes two titles the same content.

        A disc offers the feature several times over — one playlist per angle,
        per commentary set, per "play all" — and they share a segment map. Two
        titles with the same map are the same frames, so only the first is
        worth taking.
        """
        return self.segments or f"{self.seconds:.0f}:{self.size}"


@dataclass
class Selection:
    """Which titles become what."""

    main: Title | None
    extras: list[Title] = field(default_factory=list)
    rejected: list[tuple[Title, str]] = field(default_factory=list)


def _split(csv: str) -> list[str]:
    """Split one robot-output record into its fields.

    `csv.reader` handles the quoting, but a title name legitimately contains a
    comma *and* MakeMKV emits bare integers alongside quoted strings, so this
    is done by hand to keep unquoted commas from splitting a name.
    """
    out: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    while i < len(csv):
        ch = csv[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < len(csv) and csv[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_quotes = False
            else:
                buf.append(ch)
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def parse_duration(text: str) -> float:
    """`1:52:31` or `52:31` into seconds. Anything else is 0."""
    parts = text.strip().split(":")
    if not parts or not all(p.strip().isdigit() for p in parts):
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def source_url(path: str) -> str:
    """How MakeMKV is told to open this.

    `iso:` for an image, `file:` for an already-extracted BDMV/VIDEO_TS tree —
    which is what a disc backup made by anything other than a mastering tool
    usually looks like.
    """
    return f"file:{path}" if os.path.isdir(path) else f"iso:{path}"


def base_command(command: str) -> list[str]:
    """The configured command, split the way a shell would.

    Deliberately a whole command line rather than a path, so that a `docker run
    --rm -v /mnt/user:/mnt/user ... makemkvcon` shim is expressible without
    this module knowing anything about containers.
    """
    parts = shlex.split(command or "")
    if not parts:
        raise Unavailable("no MakeMKV command is configured")
    return parts


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise Unavailable(f"{argv[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MakeMKVError(f"timed out after {timeout}s") from exc
    except OSError as exc:
        raise Unavailable(str(exc)) from exc


def _check_messages(lines: Iterable[str]) -> None:
    """Raise if the output says the installation, not the disc, is the problem."""
    for line in lines:
        if not line.startswith("MSG:"):
            continue
        if _KEY_TROUBLE.search(line):
            fields = _split(line[4:])
            text = fields[3] if len(fields) > 3 else line
            raise KeyExpired(text.strip())


def available(command: str, timeout: int = 60) -> str:
    """The version string, or raise.

    Cheap enough to call before every conversion, and worth doing: the failure
    this catches — an expired key — appears with nothing else changed.
    """
    argv = base_command(command) + ["-r", "info", "disc:9999"]
    proc = _run(argv, timeout)
    lines = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    _check_messages(lines)
    for line in lines:
        # MSG:1005 is the banner. Captured from a real makemkvcon 1.18.4:
        #
        #   MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started",
        #                "%1 started","MakeMKV v1.18.4 linux(x64-release)"
        #
        # The message field and the format parameter both start with
        # "MakeMKV"; the parameter is the clean one, so take the last match
        # rather than the first, which carries " started" on the end.
        if "MakeMKV" in line and "started" in line:
            fields = _split(line[4:]) if line.startswith("MSG:") else []
            named = [f.strip() for f in fields if f.startswith("MakeMKV")]
            if named:
                return named[-1]
            return line.strip()
    if proc.returncode != 0 and not lines:
        raise Unavailable(f"exited {proc.returncode} with no output")
    return "MakeMKV (version not reported)"


def titles(path: str, command: str, min_seconds: int = 120,
           timeout: int = 1800) -> list[Title]:
    """Ask MakeMKV what is on the image.

    `--minlength` is passed rather than filtered afterwards because it also
    decides what MakeMKV bothers to analyse, and the analysis is the slow part.
    """
    argv = base_command(command) + [
        "-r", "--cache=1", f"--minlength={int(min_seconds)}",
        "info", source_url(path),
    ]
    proc = _run(argv, timeout)
    lines = (proc.stdout or "").splitlines()
    _check_messages(lines + (proc.stderr or "").splitlines())

    found: dict[int, Title] = {}
    for line in lines:
        match = _RECORD.match(line)
        if not match or match.group(1) != "TINFO":
            continue
        fields = _split(match.group(2))
        if len(fields) < 4:
            continue
        try:
            index, attr = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        value = fields[3]
        title = found.setdefault(index, Title(index=index))
        if attr == ATTR_NAME:
            title.name = value
        elif attr == ATTR_DURATION:
            title.seconds = parse_duration(value)
        elif attr == ATTR_SIZE_BYTES:
            title.size = int(value) if value.isdigit() else 0
        elif attr == ATTR_CHAPTERS:
            title.chapters = int(value) if value.isdigit() else 0
        elif attr == ATTR_OUTPUT_FILE:
            title.output_name = value
        elif attr == ATTR_SEGMENT_MAP:
            title.segments = value
        elif attr == ATTR_SOURCE_FILE:
            title.source_files.append(value)

    if not found:
        detail = _last_message(lines) or f"exited {proc.returncode}"
        raise MakeMKVError(f"no titles reported: {detail}")
    return [found[i] for i in sorted(found)]


def _last_message(lines: Iterable[str]) -> str:
    text = ""
    for line in lines:
        if line.startswith("MSG:"):
            fields = _split(line[4:])
            if len(fields) > 3 and fields[3].strip():
                text = fields[3].strip()
    return text


def select(found: list[Title], expected_seconds: float = 0.0,
           tolerance_pct: float = 10.0, extras_min_seconds: int = 60,
           max_extras: int = 24) -> Selection:
    """Decide which title is the film and which are bonus features.

    The rules, in the order they matter:

    1. **The longest title is the feature.** Crude, and right on every disc
       measured — the failure mode it replaces (largest `.m2ts`) picked a
       trailer on one disc because the feature was split into segments.
    2. **Duplicates are dropped by segment map**, not by duration: a disc
       offers the same frames under several playlists, and ripping all of them
       fills the array with the same film.
    3. **A title matching the feature's length is a variant, not an extra** —
       the director's-cut playlist, the angle, the "play all". Only the first
       is taken, because there is no way to tell from the metadata which is
       which and taking both doubles the space for one film.
    4. **The *arr's runtime is a cross-check, never a chooser.** It is nominal
       (the broadcast slot for TV), so it can only reject a selection that is
       wildly wrong — a trailer where a film should be — and the tolerance is
       wide on purpose. See `duration_below_expected` for the same reasoning
       applied to the integrity check.
    """
    usable = [t for t in found if t.seconds > 0]
    if not usable:
        return Selection(None, [], [(t, "no duration reported") for t in found])

    seen: dict[str, Title] = {}
    rejected: list[tuple[Title, str]] = []
    for title in sorted(usable, key=lambda t: (-t.seconds, t.index)):
        if title.key in seen:
            rejected.append((title, f"same content as title {seen[title.key].index}"))
            continue
        seen[title.key] = title

    ordered = sorted(seen.values(), key=lambda t: (-t.seconds, t.index))
    main = ordered[0]

    if expected_seconds > 0 and tolerance_pct > 0:
        short_by = (expected_seconds - main.seconds) / expected_seconds * 100
        if short_by > tolerance_pct:
            return Selection(
                None, [],
                rejected + [(main, f"longest title is {main.seconds / 60:.0f} min "
                                   f"against an expected {expected_seconds / 60:.0f} "
                                   f"min — the wrong title, or a damaged image")])

    extras: list[Title] = []
    for title in ordered[1:]:
        if title.seconds < extras_min_seconds:
            rejected.append((title, "shorter than the extras floor"))
            continue
        # Within a few percent of the feature and it is another cut of it, not
        # a bonus feature. 90% is deliberately loose: a seamless-branching
        # alternative ending changes the runtime by minutes, not hours.
        if title.seconds >= main.seconds * 0.9:
            rejected.append((title, "another version of the feature"))
            continue
        if len(extras) >= max_extras:
            rejected.append((title, "past the extras limit"))
            continue
        extras.append(title)

    return Selection(main, extras, rejected)


def rip(path: str, title: Title, out_dir: str, command: str,
        min_seconds: int = 120, timeout: int = 8 * 3600,
        on_progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
        stall_timeout: int = 1800) -> str:
    """Copy one title out of the image into ``out_dir``, and return its path.

    Stream copy — MakeMKV does not re-encode. The result is the same video and
    the same lossless audio, in a container Emby can index, with the chapters
    and the subtitle tracks carried over.

    ``out_dir`` must be empty: the returned path is whatever `.mkv` appears in
    it, rather than the name MakeMKV said it would use, because the two differ
    whenever the volume name contains something the filesystem will not take.
    """
    argv = base_command(command) + [
        "-r", "--progress=-same", "--cache=1", f"--minlength={int(min_seconds)}",
        "mkv", source_url(path), str(title.index), out_dir,
    ]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    before = {p.name for p in Path(out_dir).iterdir()}

    log.info("makemkv: ripping title %d of %s", title.index, path)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    tail: list[str] = []
    killed = ""

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if line:
                tail.append(line)
                del tail[:-30]

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()

    started = time.time()
    last_output = started
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            # A ripping MakeMKV is silent for minutes at a time on a scratched
            # disc image; only a total silence past the stall timeout is a hang.
            if time.time() - last_output > stall_timeout:
                killed = f"no output for {stall_timeout}s"
                proc.kill()
                break
            if time.time() - started > timeout:
                killed = f"still running after {timeout}s"
                proc.kill()
                break
            time.sleep(0.5)
            continue

        last_output = time.time()
        line = line.strip()
        tail.append(line)
        del tail[:-30]
        if line.startswith("PRGV:") and on_progress is not None:
            fields = _split(line[5:])
            if len(fields) >= 3 and fields[1].isdigit() and fields[2].isdigit():
                total, maximum = int(fields[1]), int(fields[2])
                if maximum:
                    on_progress(min(1.0, total / maximum))
        if cancel is not None and cancel.is_set():
            killed = "cancelled"
            proc.kill()
            break

    proc.wait()
    err_thread.join(timeout=2)
    _check_messages(tail)

    produced = sorted(p for p in Path(out_dir).iterdir()
                      if p.name not in before and p.suffix.lower() == ".mkv")
    if killed:
        for stray in produced:
            stray.unlink(missing_ok=True)
        raise MakeMKVError(killed)
    if not produced:
        raise MakeMKVError(_last_message(tail) or
                           f"makemkvcon exited {proc.returncode} without writing anything")
    if len(produced) > 1:
        # Asking for one title and getting several means the index was not what
        # this module thought it was; taking the biggest would be a guess about
        # which film the user gets.
        for stray in produced:
            stray.unlink(missing_ok=True)
        raise MakeMKVError(f"expected one file for title {title.index}, got "
                           f"{len(produced)}")
    return str(produced[0])
