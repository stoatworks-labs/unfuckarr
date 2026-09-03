"""API surface and the watch-folder settle logic."""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from unfuckarr import config, db


@pytest.fixture
def client(monkeypatch, settings):
    # The app's lifespan starts real watchers and a scheduler thread; the API
    # tests only need the routes, so it is stubbed out.
    from unfuckarr import api as api_mod
    from unfuckarr.service import service
    monkeypatch.setattr(service, "start", lambda: None)
    monkeypatch.setattr(service, "stop", lambda: None)
    with TestClient(api_mod.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_status_reports_empty_library(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["configured"] is False, "no *arr and no paths means unconfigured"


def test_files_filter_and_paginate(client):
    for i in range(5):
        db.ex("INSERT INTO files (path, library, title, status) VALUES (?,?,?,?)",
              (f"/media/{i}.mkv", "Movies", f"Film {i}",
               "corrupt" if i < 2 else "ok"))
    assert client.get("/api/files?status=corrupt").json()["total"] == 2
    assert client.get("/api/files?q=Film 3").json()["total"] == 1
    page = client.get("/api/files?limit=2&offset=0").json()
    assert len(page["files"]) == 2 and page["total"] == 5


def test_files_are_ordered_worst_first(client):
    db.ex("INSERT INTO files (path, status) VALUES ('/a.mkv','ok')")
    db.ex("INSERT INTO files (path, status) VALUES ('/b.mkv','corrupt')")
    files = client.get("/api/files").json()["files"]
    assert files[0]["path"] == "/b.mkv"


def test_settings_round_trip_through_the_api(client):
    body = client.get("/api/settings").json()
    body["sonarr"]["url"] = "http://sonarr:8989"
    body["sonarr"]["enabled"] = True
    body["watch_folders"] = [{"path": "/downloads", "enabled": True,
                              "settle_seconds": 30, "recursive": True}]
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200
    assert config.get().sonarr.url == "http://sonarr:8989"
    assert config.get().watch_folders[0].settle_seconds == 30


def test_invalid_settings_are_rejected(client):
    body = client.get("/api/settings").json()
    body["integrity"]["depth"] = "extremely thorough"
    assert client.put("/api/settings", json=body).status_code == 422


def test_api_key_gates_the_api_when_set(client):
    body = client.get("/api/settings").json()
    body["api_key"] = "secret"
    client.put("/api/settings", json=body)
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-API-Key": "secret"}).status_code == 200
    assert client.get("/api/status?apikey=secret").status_code == 200


def test_browse_lists_directories_only(client, tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "file.mkv").write_bytes(b"x")
    body = client.get(f"/api/browse?path={root}").json()
    assert [d["name"] for d in body["directories"]] == ["sub"]
    assert body["parent"] == str(tmp_path)


def test_browse_rejects_a_non_directory(client, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x")
    assert client.get(f"/api/browse?path={p}").status_code == 404


def test_shrink_settings_round_trip(client):
    body = client.get("/api/settings").json()
    body["shrink"]["quality"] = "excellent"
    body["shrink"]["only_between_hours"] = "22-06"
    body["efficiency"]["target_mbps"] = {"2160": 30.0, "1080": 9.0}
    body["policy"]["oversize_action"] = "flag"
    assert client.put("/api/settings", json=body).status_code == 200
    assert config.get().shrink.quality == "excellent"
    assert config.get().efficiency.target_mbps["1080"] == 9.0
    assert config.get().policy.oversize_action == "flag"


def test_a_malformed_shrink_window_is_rejected(client):
    """"22:00-06:00" is the obvious thing to type and would silently never
    match, so it has to fail loudly at the settings page instead."""
    body = client.get("/api/settings").json()
    body["shrink"]["only_between_hours"] = "10pm-6am"
    assert client.put("/api/settings", json=body).status_code == 422


def test_status_reports_what_shrinking_has_saved(client):
    db.ex("INSERT INTO files (path, library, status, size, shrunk, shrunk_from, "
          "shrink_score, shrink_metric) VALUES (?,?,?,?,?,?,?,?)",
          ("/media/a.mkv", "Movies", "ok", 1_000, 123.0, 4_000, 96.0, "vmaf"))
    db.ex("INSERT INTO files (path, library, status, shrink_skipped) "
          "VALUES (?,?,?,?)", ("/media/b.mkv", "Movies", "ok", "already efficient"))

    sh = client.get("/api/status").json()["shrink"]
    assert sh["files"] == 1 and sh["saved"] == 3_000
    assert sh["assessed_and_left"] == 1
    assert sh["action"] == "shrink"


def test_status_says_why_nothing_is_being_shrunk(client):
    """Otherwise it is invisible: the finding is raised, the policy says
    shrink, and nothing happens."""
    s = config.get()
    s.emby_compat.target_profile = "conservative"      # H.264 only
    assert "not in the target Emby profile" in \
        client.get("/api/status").json()["shrink"]["blocked"]


def test_shrink_estimate_needs_a_file_on_disk(client):
    r = client.post("/api/shrink/estimate?path=/media/gone.mkv")
    assert r.status_code == 404


def test_shrink_is_an_action_the_api_accepts(client):
    """A 404 for the unknown *file* means the action itself got through."""
    r = client.post("/api/files/action?path=/media/x.mkv&action=shrink")
    assert r.status_code == 404


def test_unknown_action_is_rejected(client):
    r = client.post("/api/files/action?path=/x.mkv&action=rm-rf")
    assert r.status_code == 400


def test_connection_test_reports_a_failure_without_raising(client, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route to host")
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = client.post("/api/settings/test?service_name=sonarr",
                    json={"enabled": True, "url": "http://nope:8989", "api_key": "k"})
    assert r.status_code == 200 and r.json()["ok"] is False


# -- watch folder settle --------------------------------------------------

def test_a_growing_file_is_not_reported_until_it_stops(tmp_path, settings, monkeypatch):
    """A file still being copied looks exactly like a truncated one. The
    settle timer is the only thing that tells them apart."""
    from unfuckarr.config import WatchFolder
    from unfuckarr.watcher import WatchManager

    ready = []
    folder = WatchFolder(path=str(tmp_path), settle_seconds=1)
    settings.watch_folders = [folder]
    wm = WatchManager(lambda: settings, lambda p, f: ready.append(p))

    target = tmp_path / "movie.mkv"
    target.write_bytes(b"x" * 1000)
    wm._touch(str(target), folder)

    # Still growing — the clock restarts, so nothing fires.
    time.sleep(1.1)
    target.write_bytes(b"x" * 2000)
    wm._settle_loop_once = None
    _tick(wm)
    assert ready == []

    # Now it holds steady past the settle window.
    time.sleep(1.1)
    _tick(wm)
    assert ready == [str(target)]


def _tick(wm):
    """Run one pass of the settle check without the sleeping loop."""
    import os
    now = time.time()
    out = []
    with wm._lock:
        for path, (last, size, folder) in list(wm._pending.items()):
            try:
                current = os.path.getsize(path)
            except OSError:
                wm._pending.pop(path, None)
                continue
            if current != size:
                wm._pending[path] = (now, current, folder)
                continue
            if now - last >= folder.settle_seconds:
                wm._pending.pop(path, None)
                out.append((path, folder))
    for path, folder in out:
        wm._on_ready(path, folder)


def test_partial_download_suffixes_are_ignored(tmp_path, settings):
    from unfuckarr.config import WatchFolder
    from unfuckarr.watcher import _Handler

    touched = []
    handler = _Handler(WatchFolder(path=str(tmp_path)), touched.append)
    for name in ("movie.mkv.part", "movie.mkv.!qb", "archive.rar", ".hidden.mkv",
                 "notes.txt"):
        handler._consider(str(tmp_path / name))
    assert touched == []

    handler._consider(str(tmp_path / "movie.mkv"))
    assert touched == [str(tmp_path / "movie.mkv")]


def test_last_scan_time_survives_a_restart(settings):
    """It lives in memory, so without restoring it from the scans table a
    container restart reports "No scan yet" and — worse — pushes the next
    scheduled scan out by a full interval. A nightly restart would mean the
    schedule never fires."""
    import time

    from unfuckarr.service import Service
    from unfuckarr.state import state

    finished = time.time() - 3600
    db.ex("INSERT INTO scans (started, finished, trigger) VALUES (?,?,?)",
          (finished - 60, finished, "scheduled"))

    state.last_scan_finished = None
    svc = Service()
    svc._restore_last_scan()
    assert state.last_scan_finished == finished

    settings.schedule.scan_interval_hours = 24
    svc._recompute_next_scan()
    # Due 24h after the last scan, not 24h after boot.
    assert abs(state.next_scan_at - (finished + 86400)) < 2


def test_restore_is_a_no_op_with_no_completed_scans(settings):
    from unfuckarr.service import Service
    from unfuckarr.state import state

    db.ex("INSERT INTO scans (started, trigger) VALUES (?,?)", (1.0, "manual"))
    state.last_scan_finished = None
    Service()._restore_last_scan()
    assert state.last_scan_finished is None


def test_walk_skips_incomplete_and_metadata_directories(tmp_path):
    from unfuckarr.scanner import walk_video_files

    (tmp_path / ".grab").mkdir()
    (tmp_path / ".grab" / "partial.mkv").write_bytes(b"x")
    (tmp_path / "extrafanart").mkdir()
    (tmp_path / "extrafanart" / "clip.mkv").write_bytes(b"x")
    (tmp_path / "Film.mkv").write_bytes(b"x")

    found = list(walk_video_files(str(tmp_path)))
    assert found == [str(tmp_path / "Film.mkv")]


# -- the download queue ---------------------------------------------------

def _intake_row(client, **kw):
    row = {
        "source": "sonarr", "download_id": "hash1", "queue_id": 7,
        "title": "Some.Show.S01E01", "state": "importBlocked",
        "verdict": "manual", "reason": "wants a manual import",
        "evidence": '{"videos": 1}', "first_seen": time.time(),
        "last_seen": time.time(),
    }
    row.update(kw)
    cols = ", ".join(row)
    db.ex(f"INSERT INTO intake ({cols}) VALUES "
          f"({', '.join('?' * len(row))})", tuple(row.values()))


def test_intake_lists_and_decodes_its_json(client):
    _intake_row(client)
    r = client.get("/api/intake")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["evidence"] == {"videos": 1}


def test_intake_hides_downloads_that_have_left_the_queue(client):
    _intake_row(client, download_id="gone", gone=time.time())
    assert client.get("/api/intake?live=true").json() == []
    assert len(client.get("/api/intake?live=false").json()) == 1


def test_intake_filters_by_verdict(client):
    _intake_row(client, download_id="a", verdict="manual")
    _intake_row(client, download_id="b", verdict="bad_release")
    assert len(client.get("/api/intake?verdict=bad_release").json()) == 1


def test_status_carries_an_intake_summary(client):
    _intake_row(client, download_id="a", verdict="bad_release")
    body = client.get("/api/status").json()
    assert body["intake"]["bad"] == 1


def test_acting_on_a_departed_download_is_rejected(client):
    _intake_row(client, download_id="gone", gone=time.time())
    r = client.post("/api/intake/act?source=sonarr&download_id=gone")
    assert r.status_code == 409


def test_acting_on_an_unknown_download_is_a_404(client):
    r = client.post("/api/intake/act?source=sonarr&download_id=nope")
    assert r.status_code == 404


def test_ignoring_marks_it_manual(client):
    _intake_row(client, verdict="unrecognised")
    r = client.post("/api/intake/ignore?source=sonarr&download_id=hash1")
    assert r.status_code == 200
    assert db.q1("SELECT verdict FROM intake")["verdict"] == "manual"


def test_intake_settings_round_trip(client):
    payload = client.get("/api/settings").json()
    payload["intake"]["action"] = "fix"
    payload["intake"]["min_blocked_minutes"] = 45
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 200
    assert config.get().intake.action == "fix"
    assert config.get().intake.min_blocked_minutes == 45
