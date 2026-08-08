"""Emby.

Two things are worth getting from Emby that ffprobe cannot tell us:

1. ``POST /Items/{id}/PlaybackInfo`` with a device profile makes the server
   itself decide whether it would direct play, and returns ``TranscodeReasons``
   when it would not. That is authoritative in a way a local codec table never
   is — it accounts for the server's own version, its ffmpeg build and its
   remux rules.
2. The activity log records real playback failures. A file that ffprobe likes
   and a user could not play is exactly the case worth surfacing.

Item lookup is by file path, which means an index of the whole library. Emby
returns ``Path`` on every item, so one recursive query builds it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import EmbyCompatConfig, EmbyConfig, apply_path_mappings
from ..checks import CheckResult, Finding
from ..checks.compat import resolve as resolve_profile

log = logging.getLogger(__name__)

# Emby's own words for why it would not direct play. Mapped to readable text;
# unknown reasons pass through verbatim rather than being swallowed.
TRANSCODE_REASONS = {
    "ContainerNotSupported": "container not supported by the client",
    "VideoCodecNotSupported": "video codec not supported",
    "AudioCodecNotSupported": "audio codec not supported",
    "SubtitleCodecNotSupported": "subtitle codec not supported",
    "AudioIsExternal": "audio is an external file",
    "SecondaryAudioNotSupported": "secondary audio not supported",
    "VideoProfileNotSupported": "video profile not supported",
    "VideoLevelNotSupported": "video level not supported",
    "VideoResolutionNotSupported": "resolution not supported",
    "VideoBitDepthNotSupported": "bit depth not supported",
    "VideoFramerateNotSupported": "frame rate not supported",
    "RefFramesNotSupported": "too many reference frames",
    "AnamorphicVideoNotSupported": "anamorphic video not supported",
    "InterlacedVideoNotSupported": "interlaced video not supported",
    "AudioChannelsNotSupported": "channel count not supported",
    "AudioProfileNotSupported": "audio profile not supported",
    "AudioSampleRateNotSupported": "sample rate not supported",
    "AudioBitDepthNotSupported": "audio bit depth not supported",
    "ContainerBitrateExceedsLimit": "bitrate exceeds the client limit",
    "VideoBitrateNotSupported": "video bitrate not supported",
    "AudioBitrateNotSupported": "audio bitrate not supported",
    "UnknownVideoStreamInfo": "Emby could not read the video stream",
    "UnknownAudioStreamInfo": "Emby could not read the audio stream",
    "DirectPlayError": "Emby reported a direct play error",
}


class EmbyError(RuntimeError):
    pass


class EmbyClient:
    def __init__(self, cfg: EmbyConfig, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout
        self._user_id: str | None = None
        self._index: dict[str, dict[str, Any]] = {}
        self._index_built: float = 0.0

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        if not self.cfg.url or not self.cfg.api_key:
            raise EmbyError("Emby is not configured")
        url = f"{self.cfg.url}/{path.lstrip('/')}"
        headers = {
            "X-Emby-Token": self.cfg.api_key,
            "Accept": "application/json",
            # Emby wants to know who is asking; without it some builds refuse
            # PlaybackInfo outright.
            "X-Emby-Authorization":
                'MediaBrowser Client="unfuckarr", Device="unfuckarr", '
                'DeviceId="unfuckarr", Version="1.0.0"',
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise EmbyError(f"Emby unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise EmbyError("Emby rejected the API key")
        if resp.status_code >= 400:
            raise EmbyError(f"Emby {method} {path} → {resp.status_code}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def ping(self) -> dict[str, Any]:
        info = self._request("GET", "System/Info")
        return {
            "ok": True,
            "version": info.get("Version", "?"),
            "name": info.get("ServerName", "Emby"),
        }

    def _admin_user_id(self) -> str | None:
        """PlaybackInfo is answered per user; any admin will do."""
        if self._user_id:
            return self._user_id
        users = self._request("GET", "Users") or []
        for u in users:
            if (u.get("Policy") or {}).get("IsAdministrator"):
                self._user_id = u.get("Id")
                return self._user_id
        if users:
            self._user_id = users[0].get("Id")
        return self._user_id

    def local_path(self, remote: str) -> str:
        return apply_path_mappings(remote, self.cfg.path_mappings)

    # -- library index ----------------------------------------------------

    def build_index(self, max_age: float = 600.0) -> dict[str, dict[str, Any]]:
        """Map local file path → {id, name, media_source_id}."""
        if self._index and time.time() - self._index_built < max_age:
            return self._index
        items = self._request("GET", "Items", params={
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode,Video",
            "Fields": "Path,MediaSources",
            "EnableTotalRecordCount": "false",
            "Limit": 100000,
        }) or {}
        index: dict[str, dict[str, Any]] = {}
        for item in items.get("Items", []):
            sources = item.get("MediaSources") or []
            # A stacked/multi-version item has one MediaSource per file, and
            # each carries its own path — index them all, not just Item.Path.
            if sources:
                for src in sources:
                    p = src.get("Path")
                    if p:
                        index[self.local_path(p)] = {
                            "id": item.get("Id"),
                            "name": item.get("Name", ""),
                            "media_source_id": src.get("Id"),
                        }
            elif item.get("Path"):
                index[self.local_path(item["Path"])] = {
                    "id": item.get("Id"),
                    "name": item.get("Name", ""),
                    "media_source_id": None,
                }
        self._index = index
        self._index_built = time.time()
        return index

    # -- the interesting bit ----------------------------------------------

    def device_profile(self, cfg: EmbyCompatConfig) -> dict[str, Any]:
        """Translate our target profile into an Emby DeviceProfile.

        Emby decides direct play against the *client's* declared capabilities,
        so describing our target profile as a client is what makes its answer
        mean what we want it to mean.
        """
        p = resolve_profile(cfg)
        video = ",".join(sorted(p.video))
        audio = ",".join(sorted(p.audio))
        conditions: list[dict[str, Any]] = [
            {"Condition": "LessThanEqual", "Property": "Height",
             "Value": str(p.max_height), "IsRequired": False},
        ]
        if not p.allow_10bit:
            conditions.append({"Condition": "LessThanEqual", "Property": "VideoBitDepth",
                               "Value": "8", "IsRequired": False})
        return {
            "Name": "unfuckarr",
            "MaxStreamingBitrate": 1_000_000_000,
            "DirectPlayProfiles": [
                {"Container": ",".join(sorted(p.containers)), "Type": "Video",
                 "VideoCodec": video, "AudioCodec": audio},
            ],
            "TranscodingProfiles": [
                {"Container": "ts", "Type": "Video", "VideoCodec": "h264",
                 "AudioCodec": "aac", "Protocol": "hls"},
            ],
            "CodecProfiles": [
                {"Type": "Video", "Codec": video, "Conditions": conditions},
            ],
            "SubtitleProfiles": [
                {"Format": "srt", "Method": "External"},
                {"Format": "subrip", "Method": "External"},
                {"Format": "ass", "Method": "Embed"},
                {"Format": "pgssub", "Method": "Embed"},
            ],
        }

    def playback_info(self, item_id: str, profile: dict[str, Any],
                      media_source_id: str | None = None) -> dict[str, Any] | None:
        body: dict[str, Any] = {"DeviceProfile": profile, "AutoOpenLiveStream": False}
        params: dict[str, Any] = {}
        user = self._admin_user_id()
        if user:
            params["UserId"] = user
        if media_source_id:
            params["MediaSourceId"] = media_source_id
        data = self._request("POST", f"Items/{item_id}/PlaybackInfo",
                             params=params, json=body)
        if not data:
            return None
        sources = data.get("MediaSources") or []
        if not sources:
            return None
        if media_source_id:
            for s in sources:
                if s.get("Id") == media_source_id:
                    return s
        return sources[0]

    def check_direct_play(self, path: str, cfg: EmbyCompatConfig,
                          result: CheckResult) -> bool:
        """Ask Emby whether it would direct play this file.

        Returns True when Emby answered (so the local codec model can be
        skipped), False when the file is unknown to Emby or the call failed.
        """
        index = self.build_index()
        entry = index.get(path)
        if entry is None:
            # Not an error: the file may be new, or outside Emby's libraries.
            result.add(Finding(
                "emby", "not_in_emby", "info",
                "Emby has no item for this path — library not scanned yet, "
                "or the path mapping is wrong",
            ))
            return False

        try:
            source = self.playback_info(
                entry["id"], self.device_profile(cfg), entry.get("media_source_id"))
        except EmbyError as exc:
            result.add(Finding("emby", "playback_info_failed", "info",
                               f"could not ask Emby: {exc}"))
            return False
        if source is None:
            result.add(Finding("emby", "no_media_source", "error",
                               "Emby knows the item but reports no playable media source"))
            return True

        if source.get("SupportsDirectPlay") or source.get("SupportsDirectStream"):
            return True

        reasons = source.get("TranscodeReasons") or []
        if isinstance(reasons, str):
            reasons = [r.strip() for r in reasons.split(",") if r.strip()]
        readable = [TRANSCODE_REASONS.get(r, r) for r in reasons] or \
                   ["Emby did not say why"]
        result.add(Finding(
            "emby", "no_direct_play", "error",
            "Emby would transcode this: " + "; ".join(readable),
            {"reasons": reasons},
        ))
        return True

    # -- activity log -----------------------------------------------------

    def playback_failures(self, since_hours: int = 168) -> list[dict[str, Any]]:
        """Entries from Emby's activity log that look like playback failures."""
        start = time.time() - since_hours * 3600
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
        data = self._request("GET", "System/ActivityLog/Entries",
                             params={"minDate": stamp, "Limit": 500}) or {}
        out = []
        for e in data.get("Items", []):
            sev = (e.get("Severity") or "").lower()
            text = f"{e.get('Name', '')} {e.get('Overview', '')} {e.get('ShortOverview', '')}"
            if sev in ("error", "fatal") or "failed" in text.lower():
                out.append({
                    "date": e.get("Date"),
                    "name": e.get("Name", ""),
                    "overview": e.get("Overview") or e.get("ShortOverview") or "",
                    "item_id": e.get("ItemId"),
                    "severity": sev or "error",
                })
        return out

    def refresh_item(self, item_id: str) -> None:
        """Re-read metadata after we replaced a file underneath Emby."""
        self._request("POST", f"Items/{item_id}/Refresh", params={
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
        })
