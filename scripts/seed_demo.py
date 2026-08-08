#!/usr/bin/env python3
"""Populate a database with plausible library data, for screenshots and for
poking at the UI without a real Sonarr/Radarr.

Committed rather than thrown away so the README screenshots can be regenerated
from a known state instead of whatever happened to be on disk that day.

    UNFUCKARR_CONFIG_DIR=./demo python scripts/seed_demo.py
    UNFUCKARR_CONFIG_DIR=./demo python -m unfuckarr

Nothing here touches the filesystem — the paths are invented and no file is
created, moved or deleted.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unfuckarr import config, db  # noqa: E402

NOW = time.time()


def probe(height=1080, codec="h264", container="mkv", duration=6120, size=0,
          audio=None, subs=None, faststart=None, bit_depth=8, fps=23.976):
    return {
        "container": container, "duration": duration, "size": size,
        "bitrate": int(size * 8 / duration) if size and duration else 0,
        "video": {"codec": codec, "profile": "High", "width": int(height * 16 / 9),
                  "height": height, "pix_fmt": "yuv420p10le" if bit_depth > 8 else "yuv420p",
                  "bit_depth": bit_depth, "fps": fps},
        "audio": audio if audio is not None else [
            {"codec": "eac3", "channels": 6, "language": "eng", "default": True}],
        "subtitles": subs or [],
        "faststart": faststart,
    }


FILES = [
    # path, library, source, title, size, status, probe, findings
    ("/media/movies/Blade Runner 2049 (2017)/Blade Runner 2049 Bluray-2160p.mkv",
     "Movies", "radarr", "Blade Runner 2049", 34_100_000_000, "ok",
     probe(2160, "hevc", size=34_100_000_000, duration=9784, bit_depth=10), []),

    ("/media/movies/Dune Part Two (2024)/Dune Part Two Bluray-2160p.mkv",
     "Movies", "radarr", "Dune: Part Two", 61_400_000_000, "ok",
     probe(2160, "hevc", size=61_400_000_000, duration=9960, bit_depth=10), []),

    ("/media/movies/The Thing (1982)/The Thing DVD.avi",
     "Movies", "radarr", "The Thing", 1_400_000_000, "incompatible",
     probe(480, "mpeg2video", "avi", 6420, 1_400_000_000,
           audio=[{"codec": "ac3", "channels": 2, "language": "", "default": True}]),
     [("compat", "bad_container", "error",
       "AVI — Emby remuxes on every play and seeking is unreliable"),
      ("compat", "bad_video_codec", "error",
       "MPEG-2 — no client decodes it in hardware; Emby transcodes every play"),
      ("hygiene", "audio_missing_language", "warning",
       "1 of 1 audio track(s) have no language tag — Emby cannot honour a "
       "preferred-language setting")]),

    ("/media/movies/Arrival (2016)/Arrival Remux-1080p.mkv",
     "Movies", "radarr", "Arrival", 28_900_000_000, "incompatible",
     probe(1080, size=28_900_000_000, duration=6976,
           audio=[{"codec": "truehd", "channels": 8, "language": "eng", "default": True}]),
     [("emby", "no_direct_play", "error",
       "Emby would transcode this: audio codec not supported; too many reference frames")]),

    ("/media/movies/Heat (1995)/Heat Bluray-1080p.mp4",
     "Movies", "radarr", "Heat", 12_200_000_000, "incompatible",
     probe(1080, container="mp4", size=12_200_000_000, duration=10260, faststart=False),
     [("compat", "no_faststart", "error",
       "MP4 moov atom is after mdat — Emby must read the whole file to seek")]),

    ("/media/tv/Severance/Season 02/Severance - S02E04.mkv",
     "TV", "sonarr", "Severance — S02E04", 4_100_000_000, "corrupt",
     probe(1080, size=4_100_000_000, duration=1860),
     [("integrity", "duration_mismatch", "error",
       "31 min on disk vs 48 min expected (36% short)")]),

    ("/media/tv/Andor/Season 01/Andor - S01E09.mkv",
     "TV", "sonarr", "Andor — S01E09", 5_200_000_000, "corrupt",
     probe(2160, "hevc", size=5_200_000_000, duration=2640, bit_depth=10),
     [("integrity", "decode_errors", "error",
       "41 decode error(s): [end] Invalid NAL unit size; [demux] error splitting "
       "the input into NAL units; [end] concealing 118 dc, 118 ac")]),

    ("/media/tv/Chernobyl/Season 01/Chernobyl - S01E03.mkv",
     "TV", "sonarr", "Chernobyl — S01E03", 3_300_000_000, "hygiene",
     probe(1080, size=3_300_000_000, duration=3660,
           audio=[{"codec": "eac3", "channels": 6, "language": "", "default": False},
                  {"codec": "aac", "channels": 2, "language": "", "default": False}],
           subs=[{"codec": "hdmv_pgs_subtitle", "language": "eng",
                  "forced": False, "image": True}]),
     [("hygiene", "audio_missing_language", "warning",
       "2 of 2 audio track(s) have no language tag — Emby cannot honour a "
       "preferred-language setting"),
      ("hygiene", "no_default_audio", "warning",
       "2 audio tracks and none flagged default — clients pick the first track, "
       "which is often the commentary"),
      ("hygiene", "image_subtitles_only", "warning",
       "only image-based subtitles (PGS/VOBSUB) — Emby must burn them in, which "
       "forces a video transcode whenever they are enabled")]),

    ("/media/tv/Fallout/Season 01/Fallout - S01E02.mkv",
     "TV", "sonarr", "Fallout — S01E02", 0, "missing", None, []),

    ("/media/tv/The Bear/Season 03/The Bear - S03E07.mkv",
     "TV", "sonarr", "The Bear — S03E07", 2_800_000_000, "ok",
     probe(1080, size=2_800_000_000, duration=1980), []),
]

JOBS = [
    ("transcode", "/media/movies/The Thing (1982)/The Thing DVD.avi", "done", 1.0,
     "re-encode video, tag languages → The Thing DVD.mkv", 7200, 5400),
    ("redownload", "/media/tv/Fallout/Season 01/Fallout - S01E02.mkv", "done", 1.0,
     "moved to recycle bin; removed from sonarr; release blocklisted, search queued",
     1800, 1780),
    ("repair", "/media/movies/Heat (1995)/Heat Bluray-1080p.mp4", "done", 1.0,
     "remux, faststart → Heat Bluray-1080p.mp4", 900, 840),
    ("flag", "/media/tv/Chernobyl/Season 01/Chernobyl - S01E03.mkv", "done", 1.0,
     "stream metadata needs tidying", 600, 600),
]

ACTIVITY = [
    ("scan_finished", "info", None, '{"checked": 10, "failed": 6, "actions": 3}', 120),
    ("transcode_done", "info", "/media/movies/The Thing (1982)/The Thing DVD.mkv",
     "re-encode video, tag languages", 300),
    ("redownload", "warn", "/media/tv/Fallout/Season 01/Fallout - S01E02.mkv",
     "reason: file is corrupt", 900),
    ("arrival_checked", "info", "/media/downloads/complete/The.Bear.S03E07.mkv",
     '{"status": "ok", "action": "none"}', 1500),
    ("watch_settled", "info", "/media/downloads/complete/The.Bear.S03E07.mkv", None, 1560),
    ("scan_started", "info", None, '{"trigger": "scheduled"}', 1800),
    ("service_started", "info", None, None, 1900),
]


def main() -> None:
    db.init()
    s = config.load()
    s.extra_library_paths = ["/media/movies", "/media/tv"]

    # The watch folder must exist or the service logs "watch folder missing"
    # and the panel shows none — point it at a real directory beside the
    # config, or wherever UNFUCKARR_DEMO_WATCH says (the UI prints the full
    # path, so a tidy one matters for screenshots).
    watch = Path(os.environ.get("UNFUCKARR_DEMO_WATCH")
                 or Path(os.environ.get("UNFUCKARR_CONFIG_DIR", "/config")) / "watch")
    watch.mkdir(parents=True, exist_ok=True)
    s.watch_folders = [config.WatchFolder(path=str(watch))]

    if "--with-services" in sys.argv:
        # Points at scripts/demo_services.py. The connection panel then goes
        # green because the client code really did talk to something.
        port = 8989
        s.sonarr.enabled, s.sonarr.url, s.sonarr.api_key = True, f"http://127.0.0.1:{port}", "demo"
        s.radarr.enabled, s.radarr.url, s.radarr.api_key = True, f"http://127.0.0.1:{port}", "radarr-demo"
        s.emby.enabled, s.emby.url, s.emby.api_key = True, f"http://127.0.0.1:{port}", "demo"
    config.save(s)

    for table in ("files", "findings", "jobs", "activity", "recycle", "scans"):
        db.ex(f"DELETE FROM {table}")

    for path, lib, src, title, size, status, pr, findings in FILES:
        result = {
            "path": path, "status": status, "error": None,
            "findings": [{"category": c, "code": k, "severity": sv, "detail": d, "data": {}}
                         for c, k, sv, d in findings],
            "probe": pr,
        }
        db.ex("INSERT INTO files (path, library, source, title, size, mtime, status,"
              " last_checked, last_result, probe) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (path, lib, src, title, size, NOW, status, NOW - 3600,
               json.dumps(result), json.dumps(pr) if pr else None))
        for c, k, sv, d in findings:
            db.ex("INSERT INTO findings (path, category, code, severity, detail, created)"
                  " VALUES (?,?,?,?,?,?)", (path, c, k, sv, d, NOW - 3600))

    for kind, path, state, prog, msg, created, finished in JOBS:
        db.ex("INSERT INTO jobs (kind, path, state, progress, message, created,"
              " started, finished) VALUES (?,?,?,?,?,?,?,?)",
              (kind, path, state, prog, msg, NOW - created, NOW - created + 10,
               NOW - finished))

    for event, level, path, detail, age in ACTIVITY:
        db.ex("INSERT INTO activity (ts, level, event, path, detail) VALUES (?,?,?,?,?)",
              (NOW - age, level, event, path, detail))

    db.ex("INSERT INTO recycle (original, stored, size, deleted, reason) VALUES (?,?,?,?,?)",
          ("/media/tv/Fallout/Season 01/Fallout - S01E02.mkv",
           "/config/recycle/2026-08-08/Season 01__Fallout - S01E02.mkv",
           3_900_000_000, NOW - 1780, "redownload: file is corrupt"))
    db.ex("INSERT INTO recycle (original, stored, size, deleted, reason) VALUES (?,?,?,?,?)",
          ("/media/movies/The Thing (1982)/The Thing DVD.avi",
           "/config/recycle/2026-08-08/The Thing (1982)__The Thing DVD.avi",
           1_400_000_000, NOW - 5400,
           "replaced by transcode (re-encode video, tag languages)"))
    db.ex("INSERT INTO scans (started, finished, trigger, total, checked, ok, failed,"
          " actions) VALUES (?,?,?,?,?,?,?,?)",
          (NOW - 1800, NOW - 120, "scheduled", 10, 10, 4, 6, 3))

    print(f"seeded {len(FILES)} files into "
          f"{os.environ.get('UNFUCKARR_CONFIG_DIR', '/config')}")


if __name__ == "__main__":
    main()
