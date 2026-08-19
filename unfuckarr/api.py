"""FastAPI application: JSON API, SSE stream, and the static web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, quality, recycle
from .clients.arr import ArrClient, ArrError
from .clients.emby import EmbyClient, EmbyError
from .config import Settings
from .remediation import shrink_blocked
from .service import service
from .state import bus, state

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VERSION = "1.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, config.load().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Worker threads publish onto this loop.
    bus.bind(asyncio.get_running_loop())
    service.start()
    try:
        yield
    finally:
        service.stop()


app = FastAPI(title="unfuckarr", version=VERSION, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")


async def require_key(request: Request) -> None:
    """Optional shared-secret auth. Unset by default — on a LAN behind Unraid
    there is usually nothing to protect against, and a mandatory key would just
    be written on a sticky note."""
    expected = config.get().api_key
    if not expected:
        return
    given = (request.headers.get("X-API-Key")
             or request.query_params.get("apikey", ""))
    if given != expected:
        raise HTTPException(status_code=401, detail="bad or missing API key")


api = FastAPI(dependencies=[Depends(require_key)])


def _row(r: Any) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys()}
    for field in ("last_result", "probe"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except (TypeError, json.JSONDecodeError):
                d[field] = None
    if d.get("arr_episode_ids"):
        try:
            d["arr_episode_ids"] = json.loads(d["arr_episode_ids"])
        except (TypeError, json.JSONDecodeError):
            d["arr_episode_ids"] = None
    return d


# -- status ---------------------------------------------------------------

@api.get("/status")
def get_status() -> dict[str, Any]:
    counts = {r["status"]: r["n"] for r in
              db.q("SELECT status, COUNT(*) n FROM files GROUP BY status")}
    libraries = [
        {"library": r["library"] or "Other", "total": r["n"],
         "ok": r["ok"], "corrupt": r["corrupt"], "incompatible": r["incompatible"],
         "hygiene": r["hygiene"], "oversized": r["oversized"],
         "unknown": r["unknown"], "missing": r["missing"],
         "bytes": r["bytes"] or 0}
        for r in db.q(
            """SELECT library,
                      COUNT(*) n,
                      SUM(status='ok') ok,
                      SUM(status='corrupt') corrupt,
                      SUM(status='incompatible') incompatible,
                      SUM(status='hygiene') hygiene,
                      SUM(status='oversized') oversized,
                      SUM(status IN ('unknown','error')) unknown,
                      SUM(status='missing') missing,
                      SUM(size) bytes
               FROM files GROUP BY library ORDER BY library""")
    ]
    return {
        "version": VERSION,
        "state": state.snapshot(),
        "counts": counts,
        "total": sum(counts.values()),
        "libraries": libraries,
        "recycle": recycle.usage(config.get().policy.recycle_bin_path),
        "watch_pending": service.watcher.pending,
        "shrink": shrink_summary(),
        "configured": bool(config.get().sonarr.enabled or config.get().radarr.enabled
                           or config.get().extra_library_paths),
    }


def shrink_summary() -> dict[str, Any]:
    """What space saving has actually happened, and whether it can happen.

    ``blocked`` matters more than it looks: the finding is raised, the policy
    says shrink, and then nothing happens — with no libvmaf, or with HEVC
    outside the target profile, that is the only place the reason is visible.
    """
    s = config.get()
    row = db.q1("SELECT COUNT(*) n, "
                "COALESCE(SUM(shrunk_from - size), 0) saved "
                "FROM files WHERE shrunk IS NOT NULL")
    skipped = db.q1("SELECT COUNT(*) n FROM files WHERE shrink_skipped IS NOT NULL")
    metric = quality.resolve_metric(s.shrink, s.ffmpeg_path)
    return {
        "enabled": s.shrink.enabled and s.policy.oversize_action == "shrink",
        "action": s.policy.oversize_action,
        "files": row["n"], "saved": row["saved"],
        "assessed_and_left": skipped["n"],
        "metric": metric.name if metric else None,
        "metric_binary": metric.binary if metric else None,
        "metric_is_estimate": bool(metric and metric.is_estimate),
        "target": metric.target if metric else None,
        "blocked": shrink_blocked(s),
    }


@api.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Server-sent events. One stream drives the whole UI."""
    queue = bus.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'event': 'hello', 'data': state.snapshot()})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # Comment frame: keeps proxies from closing an idle stream.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# -- files ----------------------------------------------------------------

@api.get("/files")
def list_files(
    status: str | None = None,
    library: str | None = None,
    q: str | None = None,
    limit: int = Query(200, le=2000),
    offset: int = 0,
) -> dict[str, Any]:
    where, params = [], []
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if library and library != "all":
        where.append("library = ?")
        params.append(library)
    if q:
        where.append("(path LIKE ? OR title LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.q1(f"SELECT COUNT(*) n FROM files {clause}", params)["n"]
    rows = db.q(
        f"""SELECT * FROM files {clause}
            ORDER BY CASE status WHEN 'corrupt' THEN 0 WHEN 'incompatible' THEN 1
                                 WHEN 'hygiene' THEN 2 WHEN 'error' THEN 3
                                 WHEN 'missing' THEN 4 ELSE 5 END,
                     library, path
            LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    )
    return {"total": total, "files": [_row(r) for r in rows]}


@api.get("/files/detail")
def file_detail(path: str) -> dict[str, Any]:
    row = db.q1("SELECT * FROM files WHERE path = ?", (path,))
    if row is None:
        raise HTTPException(404, "unknown file")
    findings = [
        {k: f[k] for k in f.keys()}
        for f in db.q("SELECT * FROM findings WHERE path = ? ORDER BY created DESC "
                      "LIMIT 100", (path,))
    ]
    jobs = [
        {k: j[k] for k in j.keys()}
        for j in db.q("SELECT * FROM jobs WHERE path = ? ORDER BY created DESC "
                      "LIMIT 20", (path,))
    ]
    return {"file": _row(row), "findings": findings, "jobs": jobs,
            "exists": os.path.exists(path)}


@api.post("/files/recheck")
def recheck(path: str, act: bool = False) -> dict[str, Any]:
    if not os.path.exists(path):
        raise HTTPException(404, "file is not on disk")
    return service.recheck(path, act=act)


@api.post("/files/action")
def force_action(path: str, action: str) -> dict[str, Any]:
    if action not in ("transcode", "repair", "shrink", "redownload", "flag"):
        raise HTTPException(400, f"unknown action {action}")
    try:
        return service.force_action(path, action)
    except FileNotFoundError:
        raise HTTPException(404, "unknown file") from None


@api.post("/files/cancel")
def cancel_action(path: str) -> dict[str, Any]:
    return {"cancelled": service.remediator.cancel(path)}


@api.post("/shrink/estimate")
def shrink_estimate(path: str) -> dict[str, Any]:
    """Measure what a shrink would save on one file, and change nothing.

    Returns as soon as the search has started; the result arrives on the event
    stream as ``shrink_estimate`` and is written to the activity log. The
    search takes minutes, which is too long to hold an HTTP request open for.
    """
    if not os.path.exists(path):
        raise HTTPException(404, "file is not on disk")
    if not service.estimate_shrink(path):
        raise HTTPException(409, "an estimate is already running")
    return {"started": True}


# -- scans ----------------------------------------------------------------

@api.post("/scan/start")
def scan_start(library: str | None = None) -> dict[str, Any]:
    paths = None
    if library and library != "all":
        paths = [r["path"] for r in
                 db.q("SELECT path FROM files WHERE library = ?", (library,))]
    started = service.start_scan("manual", paths)
    if not started:
        raise HTTPException(409, "a scan is already running")
    return {"started": True}


@api.post("/scan/stop")
def scan_stop() -> dict[str, Any]:
    service.stop_scan()
    return {"stopping": True}


@api.post("/pause")
def set_pause(paused: bool) -> dict[str, Any]:
    state.paused = paused
    bus.publish("state", state.snapshot())
    db.log("paused" if paused else "resumed", "info")
    return {"paused": paused}


@api.get("/scans")
def list_scans(limit: int = 25) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in
            db.q("SELECT * FROM scans ORDER BY started DESC LIMIT ?", (limit,))]


# -- activity and jobs ----------------------------------------------------

@api.get("/activity")
def activity(limit: int = Query(100, le=1000), level: str | None = None) -> list[dict[str, Any]]:
    if level and level != "all":
        rows = db.q("SELECT * FROM activity WHERE level = ? ORDER BY ts DESC LIMIT ?",
                    (level, limit))
    else:
        rows = db.q("SELECT * FROM activity ORDER BY ts DESC LIMIT ?", (limit,))
    return [{k: r[k] for k in r.keys()} for r in rows]


@api.get("/jobs")
def jobs(state_filter: str | None = None, limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
    if state_filter and state_filter != "all":
        rows = db.q("SELECT * FROM jobs WHERE state = ? ORDER BY created DESC LIMIT ?",
                    (state_filter, limit))
    else:
        rows = db.q("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
    return [{k: r[k] for k in r.keys()} for r in rows]


# -- recycle bin ----------------------------------------------------------

@api.get("/recycle")
def recycle_list(limit: int = 200) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in
            db.q("SELECT * FROM recycle ORDER BY deleted DESC LIMIT ?", (limit,))]


@api.post("/recycle/restore")
def recycle_restore(id: int) -> dict[str, Any]:
    try:
        return {"restored": recycle.restore(id)}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None


@api.post("/recycle/empty")
def recycle_empty() -> dict[str, Any]:
    # Retention of 0 days in the sweep means "everything older than now".
    return {"removed": recycle.sweep(days=1 / 86400,
                                     configured_bin=config.get().policy.recycle_bin_path)}


# -- settings -------------------------------------------------------------

@api.get("/settings")
def get_settings() -> dict[str, Any]:
    return config.get().model_dump(mode="json")


@api.put("/settings")
def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        settings = Settings.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - pydantic error text is the message
        raise HTTPException(422, str(exc)) from None
    config.save(settings)
    service.reload()
    db.log("settings_saved", "info")
    return settings.model_dump(mode="json")


@api.post("/settings/test")
def test_connection(service_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Try credentials from the settings form without saving them first."""
    try:
        if service_name in ("sonarr", "radarr"):
            from .config import ArrConfig
            cfg = ArrConfig.model_validate(payload)
            info = ArrClient(cfg, service_name).ping()  # type: ignore[arg-type]
            roots = ArrClient(cfg, service_name).root_folders()  # type: ignore[arg-type]
            return {**info, "root_folders": roots}
        if service_name == "emby":
            from .config import EmbyConfig
            client = EmbyClient(EmbyConfig.model_validate(payload))
            info = client.ping()
            index = client.build_index(max_age=0)
            return {**info, "items_indexed": len(index)}
    except (ArrError, EmbyError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    raise HTTPException(400, f"unknown service {service_name}")


@api.get("/browse")
def browse(path: str = "/") -> dict[str, Any]:
    """Directory picker for the watch-folder setting.

    Read-only and directories only — the settings page needs to choose a path
    that exists inside the container, and typing it blind is how people end up
    watching a folder that was never mounted.
    """
    p = Path(path or "/")
    if not p.is_dir():
        raise HTTPException(404, f"{path} is not a directory in this container")
    try:
        entries = sorted(
            (e for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name.lower(),
        )
    except PermissionError:
        raise HTTPException(403, f"cannot read {path}") from None
    return {
        "path": str(p),
        "parent": str(p.parent) if p != p.parent else None,
        "directories": [{"name": e.name, "path": str(e)} for e in entries],
    }


@api.get("/services")
def services() -> dict[str, Any]:
    return service.refresh_services()


app.mount("/api", api)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": VERSION, "uptime": time.time() - state.started}


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:  # pragma: no cover - only when the image is built wrong
    @app.get("/")
    def missing_ui() -> JSONResponse:
        return JSONResponse({"error": f"web assets missing at {WEB_DIR}"}, 500)
