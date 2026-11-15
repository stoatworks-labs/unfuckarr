"""Colour: files whose picture comes out the wrong colour.

One defect, measured on the live library, and nothing speculative beside it.
A survey of 18,000 files on 2026-11-15 found no RGB matrix on a Y'CbCr stream
and no HDR transfer on SDR primaries — the tag mismatches a colour check
usually looks for — and 68 of this one (70 with Slow Horses, then new):

**Dolby Vision with no fallback.** A profile 5 stream codes its base layer in
Dolby's IPTPQc2 colour space, not Y'CbCr, and is only right once the RPU's
reshaping has been applied. A client that does not decode Dolby Vision reads
the IPT planes as Y'CbCr and shows the picture purple and green and flat —
Slow Horses S01E01, Bridgerton S02, House of the Dragon S03. Streaming
services ship profile 5 by default; profile 8.1 and 7 carry HDR10 underneath
and degrade to it instead, so they are not flagged.

The same picture also turns up with the Dolby Vision *record* missing: RPU
NALs in every frame but no configuration record in the container, so a
player cannot even tell it is Dolby Vision (House of the Dragon S03E07). The
container tags say nothing — no transfer, no primaries, no matrix — so it is
found by reading the first frame (`probe.dovi_in_stream`).

**No rewrite fixes either.** An ordinary re-encode reads the IPT planes as
Y'CbCr and writes the purple and green into a normal file, with the Dolby
Vision metadata gone, so even a Dolby Vision client gets it wrong after that.
That happened to The Pitt S02E08 and Rings of Power S03E02 through the
shrink, before `is_hdr` knew the record's name. What fixes the file is a
release with an HDR10 or SDR base layer, so `decide` treats the finding as
unfixable (invariant 31) and the transcoder refuses to re-encode one.

**Dolby Vision baked in.** What that re-encode leaves behind: the purple and
green in the pixels of an ordinary file, with nothing of the Dolby Vision
left — no record, no RPU, no colour tags — so nothing in the file can say
what went wrong. A survey on 2026-11-15 found 22, by two signals that each
come from real files (invariant 32):

- *The release and the file disagree.* The release it was imported from
  (the *arr's `sceneName`) names Dolby Vision and no HDR — TRaSH's
  "DV (w/o HDR fallback)" — yet the file has no Dolby Vision and no colour
  tags. That catches the shrink's own outputs (Strange New Worlds S04E09/10:
  10-bit, limited range, untagged), which no stream property tells apart from
  a plain SDR encode.
- *The shape.* 8-bit, full-range HEVC at 2160p with no colour tags. Nineteen
  files in the library looked like that, re-encoded by some other tool, and
  every one was purple and green.

Either signal alone flags a file. A plain untagged SDR file trips neither: its
release says nothing of Dolby Vision, and an honest 8-bit 4K encode is limited
range or says what it is. Nor does a DV-named release whose picture is fine,
because that file still has its record, its RPU or its colour tags. The cure is
profile 5's, a different release, so `decide` routes it to `research` or
`replace` and never to the transcoder.
"""

from __future__ import annotations

import re

from ..config import EmbyCompatConfig
from ..probe import MediaInfo, _could_hide_dovi, _to_int
from . import CheckResult, Finding

CODE = "dolby_vision_no_fallback"
BAKED = "dolby_vision_baked"

# TRaSH's "DV (w/o HDR fallback)" custom format, as Sonarr and Radarr here run
# it since 2026-11-15 (`dv-wo-hdr-fallback.json`): the title names Dolby
# Vision and does not name HDR. The *arrs match custom formats without regard
# to case, so these do too. The format's other conditions (a WEB source, not
# the group Flights, not hulu) narrow which releases it refuses; they say
# nothing about whether a file with no Dolby Vision left came from one.
DV_TITLE = re.compile(r"\b(dv|dovi|dolby[ .]?v(ision)?)\b", re.IGNORECASE)
HDR_TITLE = re.compile(r"\bHDR(\b|\d)", re.IGNORECASE)

_UNTAGGED = ("", "unknown", "unspecified")


def names_dv_without_hdr(title: str | None) -> bool:
    """Whether a release title promises Dolby Vision with no HDR fallback."""
    return (bool(title) and bool(DV_TITLE.search(title))
            and not HDR_TITLE.search(title))


def baked(info: MediaInfo | None, release: str | None = None) -> list[str]:
    """Which signals say this file's colour was baked in: ``release``, ``shape``.

    Empty when the file still carries Dolby Vision (that is `CODE`, and the
    RPU can still be applied by a client that decodes it), when it has any
    colour tag at all (whoever wrote it knew what colour it was), and when its
    first frame could have held an RPU and could not be read: "could not look"
    is not "nothing left".

    ``release`` is the title of the release the file was imported from — the
    *arr's ``sceneName`` — or, for a replacement on its way, the title that was
    grabbed. It survives a re-encode in place, which is the point: Sonarr kept
    `…S04E09.DV.2160p.WEB.H265-CAKES` against the shrink's output.
    """
    v = info.video if info is not None else None
    if v is None or info.is_disc:
        return []
    if any((v.raw.get(k) or "").lower() not in _UNTAGGED
           for k in ("color_transfer", "color_space", "color_primaries")):
        return []
    if info.dovi is not None or info.dovi_in_stream:
        return []
    if info.dovi_in_stream is None and _could_hide_dovi(info):
        return []
    signals = []
    if names_dv_without_hdr(release):
        signals.append("release")
    full_range = (v.pix_fmt == "yuvj420p"
                  or (v.raw.get("color_range") or "").lower() in ("pc", "jpeg"))
    # Width counts as well as height: A Knight of the Seven Kingdoms is
    # 3840x1920, and still a 2160p release.
    uhd = max(v.height, round(v.width * 9 / 16)) >= 2160 * 0.9
    if v.codec_name == "hevc" and v.bit_depth == 8 and full_range and uhd:
        signals.append("shape")
    return signals


def check(info: MediaInfo | None, cfg: EmbyCompatConfig, result: CheckResult,
          release: str | None = None) -> None:
    """Append colour findings to ``result``.

    Runs whether or not Emby answered the direct-play question, unlike the
    codec table: Emby says yes to these files — the client asked for HEVC
    Main 10 and gets it — and the yes is exactly how the purple picture
    reaches the screen. So this is not the local table contradicting Emby
    (invariant 7); it is a fact Emby's answer does not cover.

    ``release`` is the *arr's ``sceneName`` for the file, when it has one.
    """
    if not cfg.enabled or info is None or info.video is None:
        return
    signals = baked(info, release)
    if signals:
        result.add(_baked_finding(info, release, signals))
        return
    if not info.dovi_without_fallback:
        return

    d = info.dovi or {}
    profile = _to_int(d.get("dv_profile")) or None
    if info.dovi is None:
        what = ("Dolby Vision with its configuration record missing — every "
                "frame carries Dolby Vision metadata but the container does "
                "not say so, so no player can tell")
    elif profile == 5:
        what = "Dolby Vision profile 5, with no HDR10 or SDR base layer"
    else:
        what = (f"Dolby Vision profile {profile} whose base layer is not "
                "HDR10, SDR or HLG")
    if cfg.dolby_vision_clients:
        result.add(Finding(
            "compat", CODE, "info",
            f"{what}. Every client here is set as decoding Dolby Vision, so "
            "this is noted rather than flagged — but it can never be "
            "re-encoded, and a client without Dolby Vision shows it purple "
            "and green.",
            {"dv_profile": profile, "record": info.dovi is not None},
        ))
        return
    result.add(Finding(
        "compat", CODE, "error",
        f"{what}. Any client that does not decode Dolby Vision itself — most "
        "Emby apps, every browser — shows it purple and green. No rewrite "
        "can fix that, and a re-encode would make it permanent; it needs a "
        "release with an HDR10 or SDR base layer (Dolby Vision profile 8, or "
        "plain HDR10).",
        {"dv_profile": profile, "record": info.dovi is not None},
    ))


def _baked_finding(info: MediaInfo, release: str | None,
                   signals: list[str]) -> Finding:
    """The one finding for both signals, saying which of them it rests on.

    An error whatever `dolby_vision_clients` says: once the cast is in the
    pixels a Dolby Vision client has nothing to apply either.
    """
    v = info.video
    evidence = []
    if "release" in signals:
        evidence.append(f"its release, {release}, is Dolby Vision with no HDR "
                        "fallback, and this file has no Dolby Vision record, "
                        "no RPU and no colour tags left")
    if "shape" in signals:
        evidence.append(f"it is 8-bit full-range HEVC at {v.width}x{v.height} "
                        "with no colour tags, which is what an old 8-bit "
                        "re-encode of a profile 5 stream leaves behind")
    because = "; and ".join(evidence)
    return Finding(
        "compat", BAKED, "error",
        "The colour is baked in: this was re-encoded from Dolby Vision "
        "profile 5 without the Dolby Vision processing, so the purple and "
        "green cast is now in the pixels — wrong on every client, Dolby Vision "
        f"ones included. Evidence: {because}. No rewrite can undo it; it "
        "needs a different release, with an HDR10 or SDR base layer.",
        {"signals": signals, "release": release or None},
    )
