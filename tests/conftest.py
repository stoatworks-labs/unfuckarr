from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


@pytest.fixture(autouse=True)
def scratch_config(tmp_path, monkeypatch):
    """Every test gets its own config dir and database."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("UNFUCKARR_CONFIG_DIR", str(cfg))

    from unfuckarr import config, db, recycle
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg / "config.json")
    monkeypatch.setattr(recycle, "DEFAULT_BIN", cfg / "recycle")
    db.reset_for_tests(cfg / "test.db")
    config.load()
    yield cfg


@pytest.fixture
def settings():
    from unfuckarr import config
    s = config.load()
    s.ffmpeg_path = FFMPEG or "ffmpeg"
    s.ffprobe_path = FFPROBE or "ffprobe"
    s.transcode.preset = "ultrafast"
    # Test clips are a few seconds long so the suite stays fast. The real
    # default (30s) would condemn every one of them as "too short", which is
    # correct for a library and useless for a fixture.
    s.integrity.min_duration_seconds = 3
    config._settings = s
    return s


def make_video(path: Path, seconds: int = 10, vcodec: str = "libx264",
               acodec: str = "aac", size: str = "320x180",
               extra: list[str] | None = None) -> Path:
    """Render a small synthetic clip. Fast enough to use freely in tests."""
    cmd = [
        FFMPEG, "-v", "error", "-y",
        # 25 fps, because the hygiene check rightly flags anything below 20.
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", vcodec, "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", acodec, *(extra or []), str(path),
    ]
    if vcodec == "mpeg2video":
        cmd.remove("-preset")
        cmd.remove("ultrafast")
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture
def video_factory(tmp_path):
    def factory(name: str = "clip.mkv", **kw):
        return make_video(tmp_path / name, **kw)
    return factory
