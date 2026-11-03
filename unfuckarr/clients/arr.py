"""Sonarr and Radarr (v3 API).

Both share a shape, so one class covers them with a flavour flag. The parts
that matter and are easy to get wrong:

* Deleting a file via the *arr — not with ``os.unlink`` — is what makes the
  library update. Delete it from disk behind the *arr's back and Sonarr will
  happily leave the episode marked as present until its next rescan.
* Marking the grab failed (``/history/failed/{id}``) blocklists the release
  *and* triggers a search. Without it the indexer hands back the same broken
  file within the hour, which is the classic unattended-repair loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

import httpx

from ..config import ArrConfig, apply_path_mappings

log = logging.getLogger(__name__)

Flavour = Literal["sonarr", "radarr"]


class ArrError(RuntimeError):
    pass


class ArrClient:
    def __init__(self, cfg: ArrConfig, flavour: Flavour, timeout: float = 30.0):
        self.cfg = cfg
        self.flavour = flavour
        self.timeout = timeout
        # Quality profiles change about never and `upgrade_blocked` is asked
        # once per re-searched file, so they are fetched once per client.
        self._profiles: dict[int, dict[str, Any]] | None = None

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, timeout: float | None = None,
                 **kw: Any) -> Any:
        if not self.cfg.url or not self.cfg.api_key:
            raise ArrError(f"{self.flavour} is not configured")
        url = f"{self.cfg.url}/api/v3/{path.lstrip('/')}"
        headers = {"X-Api-Key": self.cfg.api_key, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                resp = client.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise ArrError(f"{self.flavour} unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise ArrError(f"{self.flavour} rejected the API key")
        if resp.status_code >= 400:
            raise ArrError(f"{self.flavour} {method} {path} → "
                           f"{resp.status_code}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def local_path(self, remote: str) -> str:
        return apply_path_mappings(remote, self.cfg.path_mappings)

    # -- reads ------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        status = self._request("GET", "system/status")
        return {
            "ok": True,
            "version": status.get("version", "?"),
            "name": status.get("instanceName", self.flavour),
        }

    def root_folders(self) -> list[str]:
        return [self.local_path(f["path"]) for f in self._request("GET", "rootfolder") or []]

    def library(self) -> list[dict[str, Any]]:
        """Every video file the *arr knows about, normalised.

        Keys: path, arr_id, arr_parent_id, arr_episode_ids, title,
        expected_runtime (seconds), size, quality.
        """
        return (self._sonarr_library() if self.flavour == "sonarr"
                else self._radarr_library())

    def _radarr_library(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for movie in self._request("GET", "movie") or []:
            mf = movie.get("movieFile")
            if not mf or not mf.get("path"):
                continue
            out.append({
                "path": self.local_path(mf["path"]),
                "arr_id": mf.get("id"),
                "arr_parent_id": movie.get("id"),
                "arr_episode_ids": None,
                "title": movie.get("title", ""),
                # Radarr reports runtime in minutes; 0 means it does not know.
                "expected_runtime": (movie.get("runtime") or 0) * 60,
                "size": mf.get("size") or 0,
                "quality": ((mf.get("quality") or {}).get("quality") or {}).get("name", ""),
                "source": "radarr",
                "library": "Movies",
            })
        return out

    def _sonarr_library(self) -> list[dict[str, Any]]:
        series_by_id = {s["id"]: s for s in self._request("GET", "series") or []}
        out: list[dict[str, Any]] = []
        for sid, series in series_by_id.items():
            files = self._request("GET", "episodefile", params={"seriesId": sid}) or []
            if not files:
                continue
            # Episode records carry the runtime and let us name the file
            # properly; one call per series rather than per file.
            episodes = self._request("GET", "episode", params={"seriesId": sid}) or []
            by_file: dict[int, list[dict[str, Any]]] = {}
            for ep in episodes:
                fid = ep.get("episodeFileId")
                if fid:
                    by_file.setdefault(fid, []).append(ep)
            for f in files:
                if not f.get("path"):
                    continue
                eps = by_file.get(f["id"], [])
                # A multi-episode file's expected runtime is the sum.
                runtime = sum(
                    (e.get("runtime") or series.get("runtime") or 0) for e in eps
                ) * 60
                label = series.get("title", "")
                if eps:
                    nums = ", ".join(
                        f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}"
                        for e in sorted(eps, key=lambda e: (e.get("seasonNumber", 0),
                                                            e.get("episodeNumber", 0)))
                    )
                    label = f"{label} — {nums}"
                out.append({
                    "path": self.local_path(f["path"]),
                    "arr_id": f.get("id"),
                    "arr_parent_id": sid,
                    "arr_episode_ids": [e["id"] for e in eps],
                    "title": label,
                    "expected_runtime": runtime,
                    "size": f.get("size") or 0,
                    "quality": ((f.get("quality") or {}).get("quality") or {}).get("name", ""),
                    "source": "sonarr",
                    "library": "TV",
                })
        return out

    # -- writes -----------------------------------------------------------

    def delete_file(self, file_id: int) -> None:
        endpoint = "moviefile" if self.flavour == "radarr" else "episodefile"
        self._request("DELETE", f"{endpoint}/{file_id}")

    def blocklist_last_grab(self, entity_id: int,
                            episode_ids: list[int] | None = None) -> bool:
        """Mark the release that produced this file as failed.

        Returns False when no matching grab is in history — common for files
        imported by hand, and not an error.
        """
        if self.flavour == "radarr":
            params = {"movieId": entity_id, "eventType": 1, "pageSize": 20}
            hist = self._request("GET", "history/movie", params=params) or []
            records = hist if isinstance(hist, list) else hist.get("records", [])
        else:
            records = []
            for eid in episode_ids or []:
                h = self._request("GET", "history",
                                  params={"episodeId": eid, "eventType": 1,
                                          "pageSize": 20}) or {}
                records.extend(h.get("records", h if isinstance(h, list) else []))

        grabs = [r for r in records if r.get("eventType") in ("grabbed", 1)]
        if not grabs:
            return False
        newest = max(grabs, key=lambda r: r.get("date", ""))
        # This endpoint blocklists the release and queues a replacement search.
        self._request("POST", f"history/failed/{newest['id']}")
        return True

    def upgrade_blocked(self, entity_id: int) -> str | None:
        """Why a re-search for this entity cannot produce a replacement.

        Returns None when a grab is at least possible, or a sentence naming
        the setting that forbids it. This exists because a search that cannot
        result in a grab is indistinguishable, from the outside, from one that
        simply found nothing — the *arr accepts the command, fans it out to
        every indexer, rejects every release it gets back, and logs nothing
        the caller can see. Spending that on a file whose profile forbids the
        upgrade outright is pure waste, and worse, it is waste that looks like
        work.

        Both branches were measured on the live install (2026-09-22): all six
        Radarr profiles had ``upgradeAllowed`` off, and five of the six Sonarr
        profiles had their cutoff pinned to the *lowest* quality they allow,
        which is the same refusal wearing a different hat — Sonarr upgrades
        only until it reaches the cutoff, so a cutoff at the bottom of the
        list is met by every file that exists.
        """
        entity = "movie" if self.flavour == "radarr" else "series"
        try:
            record = self._request("GET", f"{entity}/{entity_id}") or {}
            profile_id = record.get("qualityProfileId")
            if not profile_id:
                return None
            profile = self._quality_profile(int(profile_id))
        except ArrError:
            # Not knowing is not a reason to refuse: let the search happen and
            # let the *arr be the one that says no.
            return None
        if profile is None:
            return None
        name = profile.get("name") or profile_id

        if not profile.get("upgradeAllowed"):
            return (f"quality profile {name!r} has upgrades turned off, so "
                    f"{self.flavour} will not replace a file it already has")

        allowed = self._allowed_qualities(profile)
        cutoff = profile.get("cutoff")
        if allowed and cutoff is not None and allowed[0][0] == cutoff:
            return (f"quality profile {name!r} has its cutoff at {allowed[0][1]!r}, "
                    "the lowest quality it allows — every file already meets it, "
                    "so no release counts as an upgrade")
        return None

    def _quality_profile(self, profile_id: int) -> dict[str, Any] | None:
        if self._profiles is None:
            rows = self._request("GET", "qualityprofile") or []
            self._profiles = {int(p["id"]): p for p in rows if p.get("id")}
        return self._profiles.get(profile_id)

    @staticmethod
    def _allowed_qualities(profile: dict[str, Any]) -> list[tuple[int, str]]:
        """The profile's allowed qualities, worst first, as (id, name).

        A group (Sonarr's "WEB 1080p" holding WEBDL and WEBRip) carries the id
        the cutoff refers to, so it is flattened to one entry rather than to
        its members — comparing the cutoff against a member id would never
        match and the check would silently pass everything.
        """
        out: list[tuple[int, str]] = []
        for item in profile.get("items") or []:
            quality = item.get("quality")
            if quality is None:
                if item.get("id") and any(m.get("allowed") for m in item.get("items") or []):
                    out.append((int(item["id"]), item.get("name") or ""))
            elif item.get("allowed"):
                out.append((int(quality["id"]), quality.get("name") or ""))
        return out

    def search(self, entity_id: int, episode_ids: list[int] | None = None) -> None:
        if self.flavour == "radarr":
            body = {"name": "MoviesSearch", "movieIds": [entity_id]}
        elif episode_ids:
            body = {"name": "EpisodeSearch", "episodeIds": episode_ids}
        else:
            body = {"name": "SeriesSearch", "seriesId": entity_id}
        self._request("POST", "command", json=body)

    def queue(self, page_size: int = 200) -> list[dict[str, Any]]:
        """Every record in the download queue, raw.

        ``includeUnknown*Items`` matters: a download the *arr can no longer
        match to a series or movie is *precisely* the kind that gets stuck,
        and it is left out of the default response — so without these the
        queue looks healthier than it is.

        Paged, because a queue can genuinely be longer than one page after an
        indexer outage and a partial view would make the abort ratio in
        ``intake`` read against a denominator that is not the queue.
        """
        params: dict[str, Any] = {
            "pageSize": page_size,
            "includeUnknownSeriesItems": True,
            "includeUnknownMovieItems": True,
        }
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request("GET", "queue", params={**params, "page": page})
            if data is None:
                break
            if isinstance(data, list):
                out.extend(data)
                break
            records = data.get("records") or []
            out.extend(records)
            total = data.get("totalRecords") or 0
            if len(out) >= total or not records or page > 20:
                break
            page += 1
        return out

    def remove_from_queue(self, queue_id: int, blocklist: bool = True,
                          remove_from_client: bool = True,
                          skip_redownload: bool = False) -> None:
        """Cancel a queue item, optionally blocklisting the release.

        With ``blocklist`` on and ``skip_redownload`` off this is the queue
        equivalent of ``history/failed/{id}``: the *arr blocklists the release
        *and* queues a replacement search, which is what invariant 2 requires.
        Doing it in two calls instead would leave a window in which a plain
        search can re-grab the release we are in the middle of rejecting.

        Verified against Sonarr 4.0.19.2979 and the Radarr on this tailnet.
        """
        params = {
            "removeFromClient": remove_from_client,
            "blocklist": blocklist,
            "skipRedownload": skip_redownload,
        }
        self._request("DELETE", f"queue/{queue_id}", params=params)

    # -- replacing a file ourselves (see `replace.py`) --------------------

    def releases(self, entity_id: int,
                 episode_ids: list[int] | None = None) -> list[dict[str, Any]]:
        """Every release the indexers offer for this episode or movie.

        This is the interactive search, the one behind the person icon in the
        *arr's UI: it fans out to every indexer, waits for them all, and hands
        back each release with the *arr's reasons for refusing it — which is
        the part `replace.choose` needs, because on a library whose profiles
        forbid upgrades every single release comes back refused, and only the
        *reasons* say which of them are refused for a reason we can overrule.

        One episode at a time on Sonarr: asked by episode id it returns the
        releases parsed to that episode, plus season packs, which `choose`
        skips. A multi-episode file is asked about by its first episode.

        Slow — tens of seconds, and over a minute when an indexer is having a
        bad day — so it gets its own timeout rather than the client's 30 s.
        """
        params = ({"movieId": entity_id} if self.flavour == "radarr"
                  else {"episodeId": (episode_ids or [entity_id])[0]})
        return self._request("GET", "release", timeout=180.0, params=params) or []

    def grab(self, guid: str, indexer_id: int) -> dict[str, Any]:
        """Send one release from the last `releases` call to the download client.

        The *arr looks the release up in the cache that search filled (it keeps
        it for about half an hour), so this has to follow `releases` closely.
        It does not re-run its own decision: the UI lets a person grab a
        release it refused, and this is the same call.
        """
        return self._request("POST", "release", timeout=60.0,
                             json={"guid": guid, "indexerId": indexer_id}) or {}

    def history_for_download(self, download_id: str) -> list[dict[str, Any]]:
        """Every history event for one download: grabbed, imported, failed."""
        data = self._request("GET", "history", params={
            "downloadId": download_id, "pageSize": 50,
            "sortKey": "date", "sortDirection": "descending"}) or {}
        return data.get("records", []) if isinstance(data, dict) else list(data)

    def find_grab(self, entity_id: int, episode_ids: list[int] | None,
                  title: str, since: float) -> str | None:
        """The download id the *arr recorded for a grab we just made, by title.

        The grab call does not return one, and the download id is the only
        name a download keeps from the queue to the history to the import.
        Matched on the release title and on being newer than ``since``, so an
        earlier grab of the same release (a retry, a person) is not mistaken
        for this one.
        """
        if self.flavour == "radarr":
            hist = self._request("GET", "history/movie", params={
                "movieId": entity_id, "eventType": 1}) or []
            records = hist if isinstance(hist, list) else hist.get("records", [])
        else:
            records = []
            for eid in (episode_ids or [])[:1]:
                h = self._request("GET", "history", params={
                    "episodeId": eid, "eventType": 1, "pageSize": 20}) or {}
                records.extend(h.get("records", []) if isinstance(h, dict) else h)
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since - 120))
        for r in sorted(records, key=lambda r: r.get("date", ""), reverse=True):
            if (r.get("sourceTitle") == title and r.get("downloadId")
                    and (r.get("date") or "") >= cutoff):
                return str(r["downloadId"])
        return None

    def manual_import_candidates(self, download_id: str) -> list[dict[str, Any]]:
        """What the *arr would import from a finished download, file by file.

        The same list its Manual Import dialog shows, with each file's parsed
        episode or movie, quality, languages and the reasons the automatic
        import refused it. `filterExistingFiles` off, because "a file is
        already there" is exactly the case this is for.

        By download id **alone**. Given a `seriesId` as well, Sonarr 4.0.20
        lists the series' own folder and ignores the download: live,
        2026-11-03, it answered with every BattleBots episode in the
        library, the very file being replaced among them, so the check would
        have judged that file and thrown the download back for it.
        """
        return self._request("GET", "manualimport", timeout=120.0, params={
            "downloadId": download_id, "filterExistingFiles": False}) or []

    def manual_import(self, item: dict[str, Any], entity_id: int,
                      episode_ids: list[int] | None,
                      download_id: str) -> int | None:
        """Import one file from a download the *arr is holding. Returns the
        command id.

        A manual import is the *arr's own import, decided by a person: it
        moves the file into the library and replaces the existing one through
        its ordinary upgrade path, which removes the old file only once the
        new one is in place. Nothing here deletes anything.
        """
        entry: dict[str, Any] = {
            "path": item.get("path"),
            "folderName": item.get("folderName") or "",
            "quality": item.get("quality"),
            "languages": item.get("languages") or [],
            "releaseGroup": item.get("releaseGroup") or "",
            "indexerFlags": item.get("indexerFlags") or 0,
            "downloadId": download_id,
        }
        if self.flavour == "radarr":
            entry["movieId"] = entity_id
        else:
            entry["seriesId"] = entity_id
            entry["episodeIds"] = list(episode_ids or [])
            entry["releaseType"] = item.get("releaseType") or "singleEpisode"
        data = self._request("POST", "command", json={
            "name": "ManualImport", "importMode": "auto", "files": [entry]}) or {}
        return data.get("id")

    def command_status(self, command_id: int) -> dict[str, Any]:
        return self._request("GET", f"command/{command_id}") or {}

    def rescan(self, entity_id: int) -> None:
        """Make the *arr re-read the folder — needed after we replace a file
        in place with a transcode, or its size and quality go stale."""
        body = ({"name": "RescanMovie", "movieId": entity_id}
                if self.flavour == "radarr"
                else {"name": "RescanSeries", "seriesId": entity_id})
        self._request("POST", "command", json=body)
