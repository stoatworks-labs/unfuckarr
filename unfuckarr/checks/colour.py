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
"""

from __future__ import annotations

from ..config import EmbyCompatConfig
from ..probe import MediaInfo, _to_int
from . import CheckResult, Finding

CODE = "dolby_vision_no_fallback"


def check(info: MediaInfo | None, cfg: EmbyCompatConfig, result: CheckResult) -> None:
    """Append colour findings to ``result``.

    Runs whether or not Emby answered the direct-play question, unlike the
    codec table: Emby says yes to these files — the client asked for HEVC
    Main 10 and gets it — and the yes is exactly how the purple picture
    reaches the screen. So this is not the local table contradicting Emby
    (invariant 7); it is a fact Emby's answer does not cover.
    """
    if not cfg.enabled or info is None or info.video is None:
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
