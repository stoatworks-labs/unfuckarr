"""Investigating a file when it fails to play in Emby."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from unfuckarr import db, playback
from unfuckarr.checks import CheckResult, Finding
from unfuckarr.playback import PlaybackWatcher, Tracker
from unfuckarr.probe import DecodeResult
from unfuckarr.remediation import Decision, Remediator

from .conftest import needs_ffmpeg

# Shaped on the logs Emby 4.10.2 wrote for the live failures, 2026-10-22:
# a Blu-ray image its ffmpeg could not open, and a transcode that worked.
FAILED_LOG = """\
http://host1/emby/videos/7166032/live.m3u8?DeviceId=ff30&MediaSourceId=mediasource_7166032&PlaySessionId=945bcb8e16034b988bcbfcdfb1926c23&api_key=x_secret1_x&VideoCodec=hevc,h264

{"Protocol":"File","Id":"mediasource_7166032","Path":"/mnt/user/media/Jason (1963) BR-DISK.iso","Container":"iso"}

>>>>>>  User policy for azlan121
        Allow Video Playback Transcoding: True

/bin/ffmpeg -loglevel +timing -y -copyts -i "/mnt/user/media/Jason (1963) BR-DISK.iso" -vn -an -sn

05:03:23.622 ffmpeg version 5.1-emby_2023_06_25_p6 Copyright (c) 2000-2022 the FFmpeg developers
05:03:23.622   built with gcc 13.4.0 (crosstool-NG 1.28.0)
05:03:23.622 Execution Date: 2026-10-22 05:03:23
05:03:23.666 /mnt/user/media/Jason (1963) BR-DISK.iso: Invalid data found when processing input
05:03:23.666 EXIT

======= ExtraData
H4sIAAAAAAAAA7VW227aQBD9FbRSpVbCmJQ0gbyREFIahVTNpQ9VZA322Ky6F2t3TaAR
---
"""

WORKED_LOG = """\
2026-10-22 05:34:51.992
Operating System: Linux version 6.18.38-Unraid
http://host1/emby/videos/2202407/main.m3u8?DeviceId=ff30&MediaSourceId=mediasource_2202407&StartTimeTicks=742519020&PlaySessionId=6ed29908b60943e3a8b46838c103d8f2&api_key=x_secret1_x

05:34:52.004 [matroska,webm @ 0x45c780] Could not find codec parameters for stream 3 (Subtitle: hdmv_pgs_subtitle (pgssub)): unspecified size
video:1954kB audio:106kB subtitle:0kB other streams:0kB global headers:2kB muxing overhead: unknown
encoded 94 frames in 2.15s (43.71 fps), 4081.72 kb/s, Avg QP:23.73
EXIT

======= ExtraData
H4sIAAAAAAAAA+1ca2/buBL9K4KAC7SAE+thJ7YvAjRN+gg22QZx2v3QLQRapmwiekGinHgX
---
"""

SESSION = "945bcb8e16034b988bcbfcdfb1926c23"


def payload(event: str, ticks: int | None = 0, done: bool = False,
            path: str = "/mnt/user/media/film.mkv", item_id: str = "7166032",
            session: str = SESSION) -> dict:
    info = {"PlayedToCompletion": done, "PlaySessionId": session}
    if ticks is not None:
        info["PositionTicks"] = ticks
    return {
        "Event": event, "Title": "x", "User": {"Name": "azlan121"},
        "Item": {"Id": item_id, "Name": "Jason and the Argonauts", "Path": path,
                 "Type": "Movie"},
        "Session": {"Id": "s1", "DeviceName": "Google Chrome Windows",
                    "Client": "Emby Web"},
        "PlaybackInfo": info,
    }


def play(*a, **kw) -> playback.Play:
    return playback.parse(payload(*a, **kw))


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime(t))


@pytest.fixture
def fake_emby(monkeypatch, settings, tmp_path):
    """Emby's log endpoints, with the library mapped onto ``tmp_path``."""
    logs: dict[str, tuple[float, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/System/Logs":
            return httpx.Response(200, json=[
                {"Name": n, "DateCreated": _stamp(t), "Size": len(x)}
                for n, (t, x) in logs.items()])
        if path.startswith("/System/Logs/"):
            return httpx.Response(200, text=logs[path.rsplit("/", 1)[1]][1])
        if path == "/Items":
            return httpx.Response(200, json={"Items": []})
        return httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(
        transport=httpx.MockTransport(handler), **kw))
    settings.emby.enabled = True
    settings.emby.url = "http://emby:8096"
    settings.emby.api_key = "k"
    # The direct-play question is tested elsewhere; here Emby only has logs.
    settings.emby.use_playback_info = False
    settings.emby.path_mappings = [{"from": "/mnt/user/media", "to": str(tmp_path)}]
    return logs


def _watcher(settings, monkeypatch) -> tuple[PlaybackWatcher, list, threading.Event]:
    applied: list[tuple[str, Decision]] = []
    done = threading.Event()
    rem = Remediator(lambda: settings)

    def fake_apply(row, result, info, decision, cancel=None):
        applied.append((row["path"], decision))
        done.set()
        return {"action": decision.action, "ok": True}

    monkeypatch.setattr(rem, "apply", fake_apply)
    return PlaybackWatcher(lambda: settings, rem), applied, done


def _suspect(watcher: PlaybackWatcher, position: float = 0.0,
             path: str = "/mnt/user/media/film.mkv") -> playback.Suspect:
    watcher.tracker.start(play("playback.start", ticks=int(position * 1e7),
                                  path=path))
    suspect = watcher.tracker.stop(
        play("playback.stop", ticks=int((position + 2) * 1e7), path=path), 30)
    assert suspect is not None
    return suspect


def _library_file(path) -> None:
    db.ex("INSERT INTO files (path, library, source, arr_id, arr_parent_id, "
          "title, status) VALUES (?,?,?,?,?,?,?)",
          (str(path), "Movies", "radarr", 11, 22, "Film", "ok"))


# -- reading the webhook --------------------------------------------------

def test_a_stop_event_is_read_from_emby_s_notification_shape():
    play = playback.parse(payload("playback.stop", ticks=123_456_789))
    assert play.event == "playback.stop"
    assert play.item_id == "7166032"
    assert play.emby_path == "/mnt/user/media/film.mkv"
    assert play.device == "Google Chrome Windows" and play.user == "azlan121"
    assert play.position == pytest.approx(12.3456789)
    assert play.key == SESSION


def test_the_media_source_played_wins_over_the_item_s_path():
    """An item with two versions has one Path and two files; the webhook's
    MediaSource is the one that was actually played."""
    body = payload("playback.stop")
    body["PlaybackInfo"]["MediaSource"] = {"Id": "ms2",
                                           "Path": "/mnt/user/media/film.4k.mkv"}
    play = playback.parse(body)
    assert play.emby_path == "/mnt/user/media/film.4k.mkv"
    assert play.media_source_id == "ms2"


def test_an_episode_is_titled_with_its_series():
    body = payload("playback.stop")
    body["Item"].update({"Name": "Errors and Omissions", "SeriesName": "Suits",
                         "ParentIndexNumber": 1, "IndexNumber": 2})
    assert playback.parse(body).title == "Suits S01E02 – Errors and Omissions"


def test_anything_but_a_playback_event_is_ignored():
    assert playback.parse({"Event": "user.authenticated"}) is None
    assert playback.parse({}) is None


def test_the_webhook_body_is_read_as_json_or_as_emby_s_multipart_form():
    doc = payload("playback.stop")
    raw = json.dumps(doc).encode()
    assert playback.webhook_payload(raw, "application/json") == doc

    form = (b"--b\r\nContent-Disposition: form-data; name=\"data\"\r\n"
            b"Content-Type: application/json\r\n\r\n" + raw + b"\r\n--b--\r\n")
    assert playback.webhook_payload(form, "multipart/form-data; boundary=b") == doc

    assert playback.webhook_payload(b"not json", "application/json") is None
    assert playback.webhook_payload(b"[1, 2]", "application/json") is None


# -- what counts as a suspect ---------------------------------------------

def _tracker():
    now = [1000.0]
    return Tracker(clock=lambda: now[0]), now


def test_a_play_that_stops_at_once_is_a_suspect():
    tracker, now = _tracker()
    tracker.start(play("playback.start"))
    now[0] += 2
    suspect = tracker.stop(play("playback.stop", ticks=0), 30)
    assert suspect is not None and suspect.seconds == 0 and suspect.started == 1000.0


def test_a_play_that_got_somewhere_or_finished_is_not():
    tracker, now = _tracker()
    tracker.start(play("playback.start"))
    now[0] += 3000
    assert tracker.stop(play("playback.stop", ticks=2900 * 10**7), 30) is None

    tracker.start(play("playback.start"))
    now[0] += 5
    assert tracker.stop(play("playback.stop", ticks=5 * 10**7, done=True), 30) is None


def test_a_failed_resume_is_measured_from_where_it_resumed():
    """Forty minutes in and dead at once is a five-second play."""
    tracker, now = _tracker()
    tracker.start(play("playback.start", ticks=2400 * 10**7))
    now[0] += 5
    suspect = tracker.stop(play("playback.stop", ticks=2405 * 10**7), 30)
    assert suspect is not None and suspect.seconds == 5


def test_a_play_paused_and_walked_away_from_is_not_a_failure():
    tracker, now = _tracker()
    tracker.start(play("playback.start"))
    now[0] += playback.ABANDONED_SECONDS + 1
    assert tracker.stop(play("playback.stop", ticks=4 * 10**7), 30) is None


def test_a_stop_without_its_start_is_measured_from_the_top_of_the_file():
    """A restart between the two loses the start. That can miss a failed
    resume; it must never invent one."""
    tracker, _ = _tracker()
    assert tracker.stop(play("playback.stop", ticks=2405 * 10**7), 30) is None
    suspect = tracker.stop(play("playback.stop", ticks=0), 30)
    assert suspect is not None and suspect.started is None
    # And with neither a start nor a position there is nothing to go on.
    assert tracker.stop(play("playback.stop", ticks=None), 30) is None


# -- Emby's transcoder log ------------------------------------------------

def test_a_transcoder_that_could_not_open_the_file_is_read_as_failed():
    t = playback.read_log("ffmpeg-transcode-a_1.txt", FAILED_LOG)
    assert t.failed and not t.finished
    assert t.error.endswith("Invalid data found when processing input")
    assert t.item_id == "7166032" and t.play_session_id == SESSION


def test_a_transcode_that_worked_is_not_failed_by_its_warnings():
    """Live, a good transcode logs "Could not find codec parameters" for a PGS
    subtitle. Judging a log by its error-looking lines would call it broken."""
    t = playback.read_log("ffmpeg-transcode-b_1.txt", WORKED_LOG)
    assert t.finished and not t.failed
    assert t.item_id == "2202407"


def test_a_transcode_stopped_part_way_is_not_failed():
    """The viewer stopping kills the transcoder mid-run: no summary, and no
    error either. That is not evidence against the file."""
    killed = WORKED_LOG.split("video:")[0] + "05:34:53.000 frame=  40 fps=0.0\n"
    t = playback.read_log("ffmpeg-transcode-c_1.txt", killed)
    assert not t.finished and not t.failed


def test_the_logs_for_a_play_are_found_by_item_and_session(fake_emby, settings):
    now = time.time()
    fake_emby["ffmpeg-transcode-a_1.txt"] = (now - 3, FAILED_LOG)
    fake_emby["ffmpeg-transcode-other_1.txt"] = (now - 3, WORKED_LOG)
    fake_emby["ffmpeg-transcode-old_1.txt"] = (now - 7200, FAILED_LOG)
    fake_emby["embyserver.txt"] = (now, "server log")

    from unfuckarr.clients.emby import EmbyClient
    suspect = playback.Suspect(play=playback.parse(payload("playback.stop")),
                               started=now - 5, stopped=now, seconds=0)
    found = playback.find_transcoder(EmbyClient(settings.emby), suspect)
    assert [t.log for t in found] == ["ffmpeg-transcode-a_1.txt"]


# -- the investigation ----------------------------------------------------

@needs_ffmpeg
def test_a_clean_file_whose_transcoder_failed_is_recorded_and_left_alone(
        fake_emby, settings, video_factory, tmp_path, monkeypatch):
    """Emby's ffmpeg failing on a file that reads cleanly here points at Emby,
    not the file — and a short play is never a finding on its own."""
    clip = video_factory("film.mkv", seconds=20)
    _library_file(clip)
    fake_emby["ffmpeg-transcode-a_1.txt"] = (time.time(), FAILED_LOG)
    watcher, applied, _ = _watcher(settings, monkeypatch)

    row = watcher.investigate(_suspect(watcher))

    assert row["path"] == str(clip)
    assert row["transcoder"] == "ffmpeg-transcode-a_1.txt"
    assert row["error"].endswith("Invalid data found when processing input")
    assert "reads cleanly" in row["verdict"]
    assert row["action"] in ("none", "flag") and applied == []

    found = db.q1("SELECT severity, detail FROM findings WHERE path=? AND "
                  "code='emby_playback_failed' AND resolved IS NULL", (str(clip),))
    assert found["severity"] == "info"
    assert "Google Chrome Windows" in found["detail"]
    assert db.q1("SELECT status FROM files WHERE path=?",
                 (str(clip),))["status"] != "corrupt"
    stored = db.q1("SELECT * FROM playback WHERE id=?", (row["id"],))
    assert stored["device"] == "Google Chrome Windows" and stored["user"] == "azlan121"


def _broken_where_it_stopped(monkeypatch, errors: int = 12) -> list:
    calls = []

    def fake(source, ffmpeg="ffmpeg", start=None, duration=None, **kw):
        calls.append((start, duration))
        return DecodeResult(errors=errors, messages=["[h264] corrupt macroblock"])

    monkeypatch.setattr(playback, "decode_check_full", fake)
    return calls


@needs_ffmpeg
def test_decode_errors_where_it_stopped_are_acted_on(
        fake_emby, settings, video_factory, monkeypatch):
    """The file condemning itself, at the place the viewer could not get past:
    the one check that knows where to look."""
    clip = video_factory("film.mkv", seconds=40)
    _library_file(clip)
    calls = _broken_where_it_stopped(monkeypatch)
    watcher, applied, done = _watcher(settings, monkeypatch)

    row = watcher.investigate(_suspect(watcher, position=25))

    assert calls == [(pytest.approx(7), pytest.approx(40))], \
        "the window is a sample either side of where it stopped"
    assert row["verdict"].startswith("broken where it stopped")
    assert row["action"] == "redownload"
    assert done.wait(5)
    assert applied[0][0] == str(clip) and applied[0][1].action == "redownload"
    assert db.q1("SELECT status FROM files WHERE path=?",
                 (str(clip),))["status"] == "corrupt"


@needs_ffmpeg
def test_a_play_that_died_inside_the_first_sample_is_not_decoded_twice(
        fake_emby, settings, video_factory, monkeypatch):
    clip = video_factory("film.mkv", seconds=40)
    _library_file(clip)
    calls = _broken_where_it_stopped(monkeypatch)
    watcher, _, _ = _watcher(settings, monkeypatch)
    watcher.investigate(_suspect(watcher, position=0))
    assert calls == []


def test_the_hook_acts_on_one_file_at_a_time(settings, monkeypatch):
    """A conversion takes no slot in the remediator, so three discs failing in
    a row — Jason, Prometheus and Totoro in Chrome, 2026-10-22 — would have
    been three MakeMKV rips reading the array at once."""
    rem = Remediator(lambda: settings)
    started: list[str] = []
    running, peak = [0], [0]
    lock = threading.Lock()
    first_in, release, all_done = (threading.Event(), threading.Event(),
                                   threading.Event())

    def slow_apply(row, result, info, decision, cancel=None):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            started.append(row["path"])
        first_in.set()
        release.wait(5)
        with lock:
            running[0] -= 1
            if len(started) == 3:
                all_done.set()
        return {"action": decision.action, "ok": True}

    monkeypatch.setattr(rem, "apply", slow_apply)
    watcher = PlaybackWatcher(lambda: settings, rem)
    discs = [f"/media/{name}.iso" for name in ("Jason", "Prometheus", "Totoro")]
    for path in discs:
        watcher._actions.put(({"path": path}, CheckResult(path=path), None,
                              Decision("convert", "disc image")))
    watcher._ensure_actor()

    assert first_in.wait(5)
    time.sleep(0.3)
    assert started == discs[:1], "the second waits for the first to finish"
    release.set()
    assert all_done.wait(5)
    assert started == discs and peak[0] == 1


def test_a_remux_is_never_offered_on_the_stopping_place_alone():
    """`_confirm_fixed` re-checks with the ordinary samples, which never look
    where the play stopped, so a remux would be recorded as a fix nobody saw
    (invariant 9). And a shrink is no answer to a film that will not start."""
    result = CheckResult(path="/x.mkv")
    result.add(Finding("integrity", "playback_decode_errors", "error", "bad"))
    assert playback._not_for_this(Decision("repair", "r"), result).action == "flag"
    assert playback._not_for_this(Decision("redownload", "r"), result).action \
        == "redownload"
    assert playback._not_for_this(Decision("shrink", "s"), CheckResult("/y.mkv")) \
        .action == "flag"
    result.add(Finding("integrity", "decode_errors", "error", "bad"))
    assert playback._not_for_this(Decision("repair", "r"), result).action == "repair"


@needs_ffmpeg
def test_the_brakes_turn_an_action_into_a_flag(
        fake_emby, settings, video_factory, monkeypatch):
    clip = video_factory("film.mkv", seconds=40)
    _library_file(clip)
    _broken_where_it_stopped(monkeypatch)
    settings.playback.quiet_minutes = 0

    settings.playback.action = "flag"
    watcher, applied, _ = _watcher(settings, monkeypatch)
    row = watcher.investigate(_suspect(watcher, position=25))
    assert row["action"] == "flag" and "set to flag" in row["detail"]["not_acted"]

    settings.playback.action = "fix"
    settings.playback.max_actions_per_day = 0
    row = watcher.investigate(_suspect(watcher, position=25))
    assert row["action"] == "flag" and "the cap is 0" in row["detail"]["not_acted"]
    assert applied == []


@needs_ffmpeg
def test_a_file_outside_the_library_is_investigated_but_not_touched(
        fake_emby, settings, video_factory, monkeypatch):
    clip = video_factory("film.mkv", seconds=40)
    _broken_where_it_stopped(monkeypatch)
    watcher, applied, _ = _watcher(settings, monkeypatch)
    row = watcher.investigate(_suspect(watcher, position=25))
    assert row["action"] == "flag" and "not in unfuckarr's library" in \
        row["detail"]["not_acted"]
    assert applied == []
    assert db.q1("SELECT 1 FROM files WHERE path=?", (str(clip),)) is None


@needs_ffmpeg
def test_a_retry_straight_after_is_recorded_without_a_second_look(
        fake_emby, settings, video_factory, monkeypatch):
    """Live, the same film was tried twice within two seconds."""
    clip = video_factory("film.mkv", seconds=20)
    _library_file(clip)
    watcher, _, _ = _watcher(settings, monkeypatch)
    watcher.investigate(_suspect(watcher))

    monkeypatch.setattr(playback, "check_file",
                        lambda *a, **k: pytest.fail("checked a second time"))
    row = watcher.investigate(_suspect(watcher))
    assert row["verdict"] == "repeat"
    assert db.q1("SELECT COUNT(*) n FROM playback")["n"] == 2


def test_a_path_that_maps_nowhere_is_said_so(fake_emby, settings, monkeypatch):
    watcher, applied, _ = _watcher(settings, monkeypatch)
    row = watcher.investigate(_suspect(watcher, path="/elsewhere/film.mkv"))
    assert "not on disk here" in row["verdict"] and applied == []


# -- the scan -------------------------------------------------------------

def test_a_disc_that_failed_to_play_is_converted_first(settings, monkeypatch):
    """Biggest first left the discs people were trying to watch weeks back in a
    queue that drains at two a night."""
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    applied: list[str] = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[0]["path"]) or {"ok": True})
    state.scan = ScanProgress(running=True, checked=10)
    settings.policy.max_conversions_per_scan = 1

    class Info:
        def __init__(self, size):
            self.size = size

    db.ex("INSERT INTO playback (ts, path, verdict, action) VALUES (?,?,?,?)",
          (time.time(), "/media/Totoro.iso", "disc image", "flag"))
    pending = [({"path": p}, CheckResult(path=p), Info(size),
                Decision("convert", "disc image"))
               for p, size in (("/media/Big.iso", 90e9), ("/media/Totoro.iso", 30e9))]
    Scanner(lambda: settings, rem)._remediate(settings, pending, population=10)
    assert applied == ["/media/Totoro.iso"]


# -- the endpoint ---------------------------------------------------------

@pytest.fixture
def client(monkeypatch, settings):
    from fastapi.testclient import TestClient

    from unfuckarr import api as api_mod
    from unfuckarr.service import service
    monkeypatch.setattr(service, "start", lambda: None)
    monkeypatch.setattr(service, "stop", lambda: None)
    with TestClient(api_mod.app) as c:
        yield c


def test_the_webhook_queues_a_suspect_and_answers_at_once(client, monkeypatch):
    from unfuckarr.service import service
    seen = threading.Event()
    monkeypatch.setattr(service.playback, "investigate", lambda s: seen.set())

    assert client.post("/api/emby/webhook",
                       json=payload("playback.start")).json() == {"ok": True}
    r = client.post("/api/emby/webhook", json=payload("playback.stop"))
    assert r.json() == {"ok": True, "suspect": True, "queued": True}
    assert seen.wait(5)

    other = client.post("/api/emby/webhook", json={"Event": "library.new"})
    assert other.json()["ignored"] == "not a playback event"
    assert client.post("/api/emby/webhook", content=b"nonsense",
                       headers={"content-type": "application/json"}).status_code == 400


def test_the_webhook_takes_the_api_key_in_the_url(client):
    from unfuckarr import config
    config.get().api_key = "secret"
    assert client.post("/api/emby/webhook",
                       json=payload("playback.start")).status_code == 401
    assert client.post("/api/emby/webhook?apikey=secret",
                       json=payload("playback.start")).status_code == 200


def test_switched_off_it_answers_and_does_nothing(client):
    from unfuckarr import config
    config.get().playback.enabled = False
    r = client.post("/api/emby/webhook", json=payload("playback.stop"))
    assert r.json()["ignored"] == "playback investigation is switched off"


def test_failed_plays_are_listed_and_shown_on_the_file(client):
    db.ex("INSERT INTO files (path, status) VALUES ('/media/a.mkv', 'ok')")
    db.ex("INSERT INTO playback (ts, path, title, verdict, action, detail) "
          "VALUES (?,?,?,?,?,?)",
          (time.time(), "/media/a.mkv", "A", "repeat", "none", '{"x": 1}'))
    listed = client.get("/api/playback").json()
    assert listed[0]["title"] == "A" and listed[0]["detail"] == {"x": 1}
    detail = client.get("/api/files/detail?path=/media/a.mkv").json()
    assert detail["playback"][0]["verdict"] == "repeat"


def test_a_config_from_before_this_still_loads(scratch_config):
    """The live config.json carries the old `read_activity_log` switch."""
    from unfuckarr import config
    (scratch_config / "config.json").write_text(json.dumps(
        {"emby": {"enabled": True, "read_activity_log": True}}))
    s = config.load()
    assert s.emby.enabled and s.playback.enabled
    assert "read_activity_log" not in s.emby.model_dump()
