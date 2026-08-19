"""Settings model and persistence.

Config lives in one JSON file under ``UNFUCKARR_CONFIG_DIR`` (``/config`` in the
container). Environment variables override the file on load so an Unraid
template can seed a working install without anyone opening the settings page;
anything saved from the web UI is written back to the file.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path(os.environ.get("UNFUCKARR_CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"

# Containers Emby will happily direct play given a supported codec inside.
SANE_CONTAINERS = {"mkv", "mp4", "m4v", "mov", "webm", "ts", "mpegts"}

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts",
    ".mts", ".mpg", ".mpeg", ".vob", ".divx", ".webm", ".ogm", ".rm", ".rmvb",
    ".asf", ".3gp", ".iso", ".img",
}


class ArrConfig(BaseModel):
    """A Sonarr or Radarr instance."""

    enabled: bool = False
    url: str = ""
    api_key: str = ""
    # Maps an *arr-side path onto the path unfuckarr sees. Sonarr may report
    # /tv while the container mounts /media/tv; without this every lookup misses.
    path_mappings: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class EmbyConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    # Ask Emby itself whether it would direct play each file, rather than
    # inferring from ffprobe. Costs one request per file but is authoritative.
    use_playback_info: bool = True
    # Read Emby's activity log for real playback failures.
    read_activity_log: bool = True
    path_mappings: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class IntegrityConfig(BaseModel):
    enabled: bool = True
    # "quick"   – ffprobe only: header, stream list, duration sanity.
    # "sample"  – quick, plus decode of the first/middle/last N seconds.
    # "full"    – decode every frame. Correct, and very slow.
    depth: Literal["quick", "sample", "full"] = "sample"
    sample_seconds: int = 20
    # The *arr's runtime is nominal, not measured — for TV it is the broadcast
    # slot from TVDB, which includes the ad breaks the file does not have. So
    # two thresholds: past `duration_tolerance_pct` short the gap is worth
    # showing, but only past `duration_truncated_pct` is it worth acting on.
    # A 22 min sitcom in a 25 min slot and a 44 min drama in a 60 min one are
    # both healthy; a file short of *half* its runtime is not.
    duration_tolerance_pct: float = 10.0
    duration_truncated_pct: float = 50.0
    min_duration_seconds: int = 30
    # Decode errors below this count are noise (a few damaged macroblocks in an
    # otherwise fine file); at or above it the file is called broken.
    max_decode_errors: int = 5
    fail_on_missing_audio: bool = True
    # Read .iso/.img through libbluray (Blu-ray) or the ISO9660 directory
    # (DVD) rather than handing the image to ffprobe, which cannot open one
    # and says "Invalid data found" — indistinguishable, to these checks,
    # from a genuinely broken file. Switch this off and disc images are
    # skipped entirely rather than guessed at. Never left to guess: an
    # image that cannot be opened is reported, not condemned.
    inspect_disc_images: bool = True


class EmbyCompatConfig(BaseModel):
    enabled: bool = True
    # Profile the library must satisfy. "modern" assumes an Emby client that
    # handles HEVC/10-bit; "conservative" targets Chromecast-class hardware.
    # The explicit lists below apply only when this is "custom".
    target_profile: Literal["modern", "conservative", "permissive", "custom"] = "modern"
    video_codecs: list[str] = Field(default_factory=lambda: ["h264", "hevc", "vp9", "av1"])
    audio_codecs: list[str] = Field(default_factory=lambda: ["aac", "ac3", "eac3", "mp3", "flac", "opus"])
    containers: list[str] = Field(default_factory=lambda: ["mkv", "mp4"])
    allow_10bit: bool = True
    max_height: int = 2160
    # A non-faststart MP4 makes Emby read the whole file before it can seek.
    require_faststart_mp4: bool = True


class HygieneConfig(BaseModel):
    """Stream-hygiene checks. Cosmetic by nature, so they never justify a
    delete on their own — see ``Policy.hygiene_action``."""

    enabled: bool = True
    require_audio_language_tags: bool = True
    require_default_audio_track: bool = True
    flag_image_subtitles_only: bool = True
    flag_missing_subtitle_language: bool = True
    # Anything outside this range confuses client frame-rate matching.
    min_fps: float = 20.0
    max_fps: float = 61.0


class EfficiencyConfig(BaseModel):
    """Which files are worth *measuring* for a saving.

    Deliberately not "which files are too big". A bitrate threshold is a guess
    about what an encoder will manage on content it has not seen, and it is
    wrong in both directions: it condemns a well-encoded 30 Mbps remux of
    grain-heavy 35mm that will not compress, and it lets a lazily-encoded
    6 Mbps 1080p through when the same picture fits in 2. The quality search
    already answers the question properly, per file, by measuring — so these
    settings decide only whether spending that search is *worthwhile*, and the
    measurement decides everything else.

    Nothing raised here is ever an error, and nothing raised here can lead to
    a delete: the worst outcome is a re-encode, guarded by
    ``Policy.oversize_action`` and every gate in ``ShrinkConfig``.
    """

    enabled: bool = True
    # Floors, in the sense of "is there anything here worth hours of CPU".
    min_size_mb: int = 500
    min_duration_seconds: int = 300
    # A cost optimisation and nothing more. A search on a file already in one
    # of these will almost always fail `min_saving_pct` — and take minutes to
    # say so, per file, across a whole library. Empty the list to measure them
    # anyway; a genuinely bloated AV1 does exist, it is just rare enough not
    # to be worth the sweep.
    skip_codecs: list[str] = Field(default_factory=lambda: ["av1"])
    # HDR survives a re-encode only if its metadata does, and getting that
    # wrong produces a grey, washed-out file that still plays — the worst kind
    # of failure, because nothing reports it. Off until asked for.
    allow_hdr: bool = False
    # NOT a gate. The backlog is worked through fattest-first so the biggest
    # wins land first, and this is what "fattest" is measured against: the
    # ratio of a file's video bitrate to the target for its height. A file
    # under its target is still assessed, just later.
    target_mbps: dict[str, float] = Field(default_factory=lambda: {
        "2160": 25.0, "1440": 14.0, "1080": 8.0, "720": 4.0, "480": 2.5,
    })


class ShrinkConfig(BaseModel):
    """How a shrink is carried out, and every brake on it.

    The defaults are conservative on purpose. This is the one action in
    unfuckarr that changes a file nothing is wrong with, so it has to be worth
    it (``min_saving_pct``), it has to be measured rather than assumed (the
    quality target), and it must never happen twice to the same file.
    """

    enabled: bool = True
    # Only HEVC. Emby direct play is the premise of this whole application and
    # HEVC is in the default target profile; AV1 is not, and no AMD part before
    # RDNA3 can encode it, so shrinking to AV1 would risk trading a size win
    # for a file Emby has to transcode on every play.
    codec: Literal["hevc"] = "hevc"
    # Quality tier. See quality.QUALITY_TIERS — 85 / 92 / 95 VMAF.
    quality: Literal["acceptable", "good", "excellent"] = "good"
    # Overrides the tier when non-zero, in whatever units `metric` is in.
    target_score: float = 0.0
    # How far below the target one sample may sit while the mean still passes,
    # in VMAF points. One bad scene is exactly what a mean hides.
    window_tolerance: float = 3.0
    metric: Literal["auto", "vmaf", "ssim"] = "auto"
    # An ffmpeg built with libvmaf, if it is not on PATH as ffmpeg-vmaf.
    vmaf_ffmpeg_path: str = ""
    metric_threads: int = 0
    # The search itself.
    sample_count: int = 3
    sample_seconds: int = 15
    crf_min: int = 18
    crf_max: int = 34
    search_steps: int = 5
    # Do not touch the file unless this much is actually saved — checked twice,
    # once against the search's projection before the encode starts and once
    # against the finished file before it is allowed to replace anything.
    min_saving_pct: float = 25.0
    # Restrict shrinking to a window of the day, e.g. "22-06". Empty = any time.
    # Scans still run; only the shrink action waits.
    only_between_hours: str = ""

    @field_validator("only_between_hours")
    @classmethod
    def _check_window(cls, v: str) -> str:
        if not v:
            return v
        try:
            start, _, end = v.partition("-")
            if not (0 <= int(start) <= 23 and 0 <= int(end) <= 23):
                raise ValueError
        except ValueError:
            raise ValueError('expected "HH-HH", e.g. "22-06"') from None
        return v


class TranscodeConfig(BaseModel):
    enabled: bool = True
    video_codec: Literal["h264", "hevc", "copy"] = "h264"
    # Hardware encoder to use when available: qsv (Intel), nvenc, vaapi, none.
    hwaccel: Literal["none", "qsv", "nvenc", "vaapi", "videotoolbox"] = "none"
    vaapi_device: str = "/dev/dri/renderD128"
    crf: int = 20
    preset: str = "medium"
    audio_codec: Literal["aac", "ac3", "eac3", "copy"] = "aac"
    audio_bitrate: str = "192k"
    container: Literal["mkv", "mp4"] = "mkv"
    # Never re-encode a stream that already satisfies the target profile.
    copy_compatible_streams: bool = True
    keep_subtitles: bool = True
    # Replace the source once the output verifies clean, rather than leaving
    # two copies for the *arr to trip over.
    replace_original: bool = True
    max_concurrent: int = 1
    nice_level: int = 10
    # Abort a transcode that has produced nothing for this long.
    stall_timeout_seconds: int = 900


class Policy(BaseModel):
    """What unfuckarr does when a check fails.

    ``auto`` here means genuinely unattended: the defaults act without anyone
    clicking approve. Deletes still route through the recycle bin below so an
    automatic wrong call is recoverable.
    """

    corrupt_action: Literal["none", "flag", "transcode", "redownload"] = "redownload"
    # Corruption that transcoding can plausibly repair (container/index damage)
    # is worth one remux attempt before we throw the file away.
    try_repair_before_redownload: bool = True
    incompatible_action: Literal["none", "flag", "transcode", "redownload"] = "transcode"
    hygiene_action: Literal["none", "flag", "transcode"] = "flag"
    # A file that is merely large is not a fault, so this can never delete —
    # the literal type is the enforcement, exactly as for hygiene_action.
    oversize_action: Literal["none", "flag", "shrink"] = "shrink"
    # Blocklist the release in Sonarr/Radarr so the same broken file is not
    # grabbed straight back.
    blocklist_on_redownload: bool = True
    # Deletes move here first. 0 disables the bin and unlinks immediately.
    recycle_bin_days: int = 14
    recycle_bin_path: str = ""
    # Refuse to act on more than this many files in one scan. A mount that
    # disappears mid-scan makes every file look broken; this is the brake.
    max_actions_per_scan: int = 50
    # Shrinks have their own, much smaller cap and are counted separately.
    # One shrink is a quality search plus a full re-encode — hours, not the
    # seconds a remux takes — and unlike a repair, nothing is broken while it
    # waits for the next scan.
    max_shrinks_per_scan: int = 5
    # If more than this fraction of a library fails, stop and flag instead of
    # deleting — that is a mount problem, not a media problem.
    abort_if_failure_ratio_over: float = 0.5


class ScheduleConfig(BaseModel):
    scan_enabled: bool = True
    scan_interval_hours: int = 24
    scan_at_startup: bool = False
    # Skip files whose size and mtime are unchanged since the last clean pass.
    skip_unchanged: bool = True
    # Re-verify a clean file after this long anyway (bit rot, silent truncation).
    recheck_after_days: int = 90
    max_concurrent_probes: int = 2


class WatchFolder(BaseModel):
    path: str
    enabled: bool = True
    # Wait for the file to stop growing before probing it — an in-progress
    # copy or an unpacking rar looks exactly like a truncated file.
    settle_seconds: int = 60
    recursive: bool = True


class Settings(BaseModel):
    sonarr: ArrConfig = Field(default_factory=ArrConfig)
    radarr: ArrConfig = Field(default_factory=ArrConfig)
    emby: EmbyConfig = Field(default_factory=EmbyConfig)
    integrity: IntegrityConfig = Field(default_factory=IntegrityConfig)
    emby_compat: EmbyCompatConfig = Field(default_factory=EmbyCompatConfig)
    hygiene: HygieneConfig = Field(default_factory=HygieneConfig)
    efficiency: EfficiencyConfig = Field(default_factory=EfficiencyConfig)
    transcode: TranscodeConfig = Field(default_factory=TranscodeConfig)
    shrink: ShrinkConfig = Field(default_factory=ShrinkConfig)
    policy: Policy = Field(default_factory=Policy)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    watch_folders: list[WatchFolder] = Field(default_factory=list)
    # Extra library roots to sweep that no *arr knows about.
    extra_library_paths: list[str] = Field(default_factory=list)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    log_level: str = "INFO"
    # Set non-empty to require a token in the X-API-Key header / ?apikey=.
    api_key: str = ""


_lock = threading.RLock()
_settings: Settings | None = None


def _env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply UNFUCKARR_* environment variables over loaded config.

    Only the handful an Unraid template realistically sets. Everything else is
    the settings page's job.
    """
    mapping = {
        "UNFUCKARR_SONARR_URL": ("sonarr", "url"),
        "UNFUCKARR_SONARR_API_KEY": ("sonarr", "api_key"),
        "UNFUCKARR_RADARR_URL": ("radarr", "url"),
        "UNFUCKARR_RADARR_API_KEY": ("radarr", "api_key"),
        "UNFUCKARR_EMBY_URL": ("emby", "url"),
        "UNFUCKARR_EMBY_API_KEY": ("emby", "api_key"),
        "UNFUCKARR_API_KEY": ("api_key",),
        "UNFUCKARR_LOG_LEVEL": ("log_level",),
        # Where deleted and replaced files are kept. It belongs with the
        # volume mappings rather than on the settings page, because it is only
        # useful pointed at a path that was mounted into the container — and
        # whoever writes the mapping is the one who knows where that is. It
        # also wants to be on the same filesystem as the media (see
        # recycle.same_filesystem): otherwise every recycled file is copied
        # rather than renamed, which on Unraid means 40 GB remuxes landing on
        # the cache one at a time.
        "UNFUCKARR_RECYCLE_BIN_PATH": ("policy", "recycle_bin_path"),
        "UNFUCKARR_RECYCLE_BIN_DAYS": ("policy", "recycle_bin_days"),
    }
    for env, path in mapping.items():
        val = os.environ.get(env)
        if not val:
            continue
        node = data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = val
        # A URL and key arriving by env plainly means "use this service".
        if len(path) == 2 and path[1] in ("url", "api_key"):
            node.setdefault("enabled", True)

    watch = os.environ.get("UNFUCKARR_WATCH_FOLDERS", "")
    if watch and not data.get("watch_folders"):
        data["watch_folders"] = [
            {"path": p.strip()} for p in watch.split(",") if p.strip()
        ]
    return data


def load() -> Settings:
    global _settings
    with _lock:
        data: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
            except (OSError, json.JSONDecodeError):
                # A corrupt config must not stop the app booting — the settings
                # page is the only way the user can fix it.
                data = {}
        _settings = Settings.model_validate(_env_overrides(data))
        return _settings


def get() -> Settings:
    return _settings if _settings is not None else load()


def save(settings: Settings) -> Settings:
    global _settings
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings.model_dump(mode="json"), indent=2))
        tmp.replace(CONFIG_PATH)
        _settings = settings
        return _settings


def apply_path_mappings(path: str, mappings: list[dict[str, str]]) -> str:
    """Translate a remote service's path into a local one.

    Longest prefix wins so ``/tv`` and ``/tv/anime`` can both be mapped.
    """
    best: tuple[int, str] | None = None
    for m in mappings:
        src, dst = m.get("from", ""), m.get("to", "")
        if not src:
            continue
        if path == src or path.startswith(src.rstrip("/") + "/"):
            if best is None or len(src) > best[0]:
                best = (len(src), dst.rstrip("/") + path[len(src.rstrip("/")):])
    return best[1] if best else path
