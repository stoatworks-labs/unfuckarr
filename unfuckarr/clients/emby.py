"""Emby.

Two things are worth getting from Emby that ffprobe cannot tell us:

1. ``POST /Items/{id}/PlaybackInfo`` with a device profile makes the server
   itself decide whether it would direct play, and returns ``TranscodeReasons``
   when it would not. That is authoritative in a way a local codec table never
   is — it accounts for the server's own version, its ffmpeg build and its
   remux rules.
2. Its own ffmpeg's log of a play that failed. Emby records no playback
   failure anywhere — measured on a live 4.10.2 server, eight months of
   activity log held only starts, stops and logins — so a failure has to be
   inferred from a play that stopped as soon as it began (see playback.py),
   and the transcoder's log is the evidence of what went wrong. Emby keeps
   those logs for about a day, so they are read as the play stops.

Item lookup is by file path, which means an index of the whole library. Emby
returns ``Path`` on every item, so one recursive query builds it.
"""

from __future__ import annotations

import logging
import os
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
        resp = self._send(method, path, **kw)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
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
        return resp

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

    def remote_path(self, local: str) -> str:
        """The path Emby knows a local file by: the mappings run backwards."""
        reverse = [{"from": m.get("to", ""), "to": m.get("from", "")}
                   for m in self.cfg.path_mappings]
        return apply_path_mappings(local, reverse)

    def media_replaced(self, old: str, new: str) -> None:
        """Tell Emby a file it knows was rewritten, or replaced by another.

        Without it Emby goes on describing the old file until its own library
        scan reaches the folder, and every verdict asked of it meanwhile is
        about a file that has gone (`_stale`). Sent as Sonarr's own Emby
        connection sends it, so it costs one request and no library index.
        """
        if old == new:
            updates = [{"Path": self.remote_path(new), "UpdateType": "Modified"}]
        else:
            updates = [{"Path": self.remote_path(old), "UpdateType": "Deleted"},
                       {"Path": self.remote_path(new), "UpdateType": "Created"}]
        self._send("POST", "Library/Media/Updated", json={"Updates": updates})

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
        skipped), False when the file is unknown to Emby, the call failed, or
        Emby's answer is about a file that is no longer the one on disk.
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

        stale = self._stale(path, source)
        if stale is not None:
            # Emby is answering about a file that has since been replaced, so
            # its "no" is about the old one. Live, 2026-11-03: Westworld
            # S04E04 had its AC-3 default added at 04:42, and at 18:26 Emby
            # still listed one DTS track and the old 12,099,700,816 bytes
            # against 12,369,013,883 on disk — refused, and with nothing left
            # for the local table to find, flagged as "nothing in the file
            # looks wrong". 226 of the 328 silent refusals that day were the
            # default audio track, and the stale ones among them had been
            # fixed already. Ask it to look again, and let the local table
            # speak for the file until it has.
            try:
                self.refresh_item(entry["id"], self._remote_path(source))
            except EmbyError as exc:
                log.debug("could not ask Emby to re-read %s: %s", path, exc)
            result.add(Finding(
                "emby", "emby_stale", "info",
                f"Emby has not re-read this file since it changed ({stale}) "
                "— asked it to; its verdict counts again once it has",
            ))
            return False

        if source.get("SupportsDirectPlay") or source.get("SupportsDirectStream"):
            return True

        reasons = source.get("TranscodeReasons") or []
        if isinstance(reasons, str):
            reasons = [r.strip() for r in reasons.split(",") if r.strip()]
        data: dict[str, Any] = {"reasons": reasons}
        if reasons:
            readable = [TRANSCODE_REASONS.get(r, r) for r in reasons]
            detail = "Emby would transcode this: " + "; ".join(readable)
        else:
            found = self.diagnose(entry, cfg)
            if found:
                data = {"reasons": found, "inferred": True}
                detail = ("Emby would transcode this: "
                          + "; ".join(TRANSCODE_REASONS.get(r, r) for r in found)
                          + " (Emby did not say; found by asking again with "
                            "the rest of the profile relaxed)")
            else:
                data = {"reasons": [], "inferred": True}
                # Kept word for word: it is the phrase the UI and the notes
                # have always used for this verdict.
                detail = "Emby would transcode this: Emby did not say why"
        result.add(Finding("emby", "no_direct_play", "error", detail, data))
        return True

    # -- finding out why --------------------------------------------------

    # The four parts of the device profile a direct-play verdict can turn on,
    # each with the reason Emby would have given had it given one. The names
    # are Emby's own, so `transcode.plan` reads an inferred reason exactly as
    # it reads one Emby supplied.
    DIAGNOSABLE = (
        ("container", "ContainerNotSupported"),
        ("video", "VideoCodecNotSupported"),
        ("audio", "AudioCodecNotSupported"),
        ("subtitles", "SubtitleCodecNotSupported"),
    )

    # Every subtitle format Emby names, embedded. A client claiming to render
    # all of them is a client that never needs one burned in.
    ALL_SUBTITLES = ("srt", "subrip", "ass", "ssa", "vtt", "pgssub", "dvdsub",
                     "dvbsub", "vobsub", "sub", "teletext", "mov_text", "smi")

    def diagnose(self, entry: dict[str, Any], cfg: EmbyCompatConfig) -> list[str]:
        """Why Emby refused a file it would not explain, asked of Emby itself.

        Emby 4.9.5 and 4.10.2 answer every refusal with an empty
        `TranscodeReasons` (2026-08-27, 63 of 63; 2026-11-03, 328 of 328), and
        a verdict with no reason has no plan behind it. But the profile is
        ours, so the question can be put again: relax all four parts, which
        should play, then put each part back on its own. A part that turns
        the answer back to "no" is a reason, and a file with two problems —
        an AVI is both the container and the codec — names both, where
        relaxing one part at a time would find neither.

        Measured on the live library, 2026-11-03, relaxing one part at a time:
        323 of the 328 refusals came down to a single part (226 the default
        audio track, 88 an MPEG-TS container, 8 a default forced DVD subtitle,
        1 video). The other five were an AVI and four disc rips, which is why
        this puts parts back rather than taking them away. Five calls, and only for a file Emby has already refused. An empty
        list means even the fully relaxed profile was refused, or Emby
        stopped answering part-way, and the caller says so rather than guess.
        """
        base = self.device_profile(cfg)
        parts = [name for name, _ in self.DIAGNOSABLE]
        try:
            if not self._plays(entry, self._relaxed(base, parts)):
                return []
            found = []
            for name, reason in self.DIAGNOSABLE:
                others = [p for p in parts if p != name]
                if not self._plays(entry, self._relaxed(base, others)):
                    found.append(reason)
            return found
        except EmbyError as exc:
            log.debug("diagnosing %s stopped: %s", entry.get("name"), exc)
            return []

    def _plays(self, entry: dict[str, Any], profile: dict[str, Any]) -> bool:
        source = self.playback_info(entry["id"], profile,
                                    entry.get("media_source_id"))
        return bool(source and (source.get("SupportsDirectPlay")
                                or source.get("SupportsDirectStream")))

    @classmethod
    def _relaxed(cls, base: dict[str, Any], parts: list[str]) -> dict[str, Any]:
        """The device profile with the named parts accepting anything."""
        profile = {**base,
                   "DirectPlayProfiles": [dict(p) for p in base["DirectPlayProfiles"]],
                   "CodecProfiles": list(base["CodecProfiles"]),
                   "SubtitleProfiles": list(base["SubtitleProfiles"])}
        direct = profile["DirectPlayProfiles"][0]
        if "container" in parts:
            direct["Container"] = ""
        if "video" in parts:
            direct["VideoCodec"] = ""
            profile["CodecProfiles"] = []
        if "audio" in parts:
            direct["AudioCodec"] = ""
        if "subtitles" in parts:
            profile["SubtitleProfiles"] = [{"Format": f, "Method": "Embed"}
                                           for f in cls.ALL_SUBTITLES]
        return profile

    @staticmethod
    def _stale(path: str, source: dict[str, Any]) -> str | None:
        """How Emby's record of this file differs from the file, or None.

        The size, because it is the one thing Emby records that any rewrite
        changes and that costs a `stat` to compare. A remux that only flips a
        flag moves it by a few bytes, which is still a different number.
        """
        recorded = source.get("Size")
        try:
            actual = os.path.getsize(path)
        except OSError:
            return None
        if not recorded or int(recorded) == actual:
            return None
        return f"Emby has {int(recorded):,} bytes on record, the file is {actual:,}"

    @staticmethod
    def _remote_path(source: dict[str, Any]) -> str | None:
        return source.get("Path") or None

    # -- server logs ------------------------------------------------------

    def log_files(self) -> list[dict[str, Any]]:
        """Emby's log directory: ``Name``, ``DateCreated`` (UTC), ``Size``.

        The transcoder writes one ``ffmpeg-transcode-*`` or ``ffmpeg-remux-*``
        file per attempt, and Emby prunes them within about a day.
        """
        data = self._request("GET", "System/Logs") or []
        if isinstance(data, dict):
            data = data.get("Items") or []
        return [d for d in data if isinstance(d, dict) and d.get("Name")]

    def log_text(self, name: str) -> str:
        return self._send("GET", f"System/Logs/{name}").text

    def item_path(self, item_id: str, media_source_id: str | None = None) -> str | None:
        """The local path of one item, for a webhook that did not carry it."""
        for path, entry in self.build_index().items():
            if entry["id"] != item_id:
                continue
            if not media_source_id or entry.get("media_source_id") in (
                    None, media_source_id):
                return path
        return None

    def refresh_item(self, item_id: str, remote_path: str | None = None) -> None:
        """Ask Emby to re-read a file we replaced underneath it.

        Both ways in: the media-updated notice, which is what Sonarr's own
        Emby connection sends, and an item refresh. Neither has been seen to
        work yet. On 2026-11-03 Emby queued both ("will be refreshed") behind a
        library scan that had sat at 90% for days, and Westworld S04E04's
        record had not changed half an hour later, with a full refresh tried
        as well. So the refresh is the default mode, not a full one — this is
        sent for every stale file on every pass, and a full refresh is a
        metadata lookup online each time — and `check_direct_play` measures
        the staleness rather than assuming it has gone.
        """
        if remote_path:
            self._send("POST", "Library/Media/Updated", json={
                "Updates": [{"Path": remote_path, "UpdateType": "Modified"}]})
        self._request("POST", f"Items/{item_id}/Refresh", params={
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
        })
