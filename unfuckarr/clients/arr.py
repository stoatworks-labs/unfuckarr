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

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        if not self.cfg.url or not self.cfg.api_key:
            raise ArrError(f"{self.flavour} is not configured")
        url = f"{self.cfg.url}/api/v3/{path.lstrip('/')}"
        headers = {"X-Api-Key": self.cfg.api_key, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
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

    def rescan(self, entity_id: int) -> None:
        """Make the *arr re-read the folder — needed after we replace a file
        in place with a transcode, or its size and quality go stale."""
        body = ({"name": "RescanMovie", "movieId": entity_id}
                if self.flavour == "radarr"
                else {"name": "RescanSeries", "seriesId": entity_id})
        self._request("POST", "command", json=body)
