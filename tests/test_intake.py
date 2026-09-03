"""The intake watcher: downloads the *arr finished but will not import.

The load-bearing test in this file is
``test_a_healthy_complete_download_is_never_a_bad_release``. Everything else
protects a brake; that one protects the reason the module exists.
"""

from __future__ import annotations

import time

import httpx
import pytest

from unfuckarr import db, intake
from unfuckarr.clients.arr import ArrClient
from unfuckarr.config import ArrConfig
from unfuckarr.intake import (IntakeWatcher, QueueItem, abort_reason, inspect,
                              normalise, read_contents, triage)

from .conftest import needs_ffmpeg

# The genuine class, captured before any test patches it. Without this a
# second `_watcher()` in one test wraps the first test's lambda and
# httpx is handed `transport` twice.
_REAL_CLIENT = httpx.Client


# -- fixtures -------------------------------------------------------------

def queue_record(**kw):
    """A Sonarr 4.0 queue record, shaped as the live instance returns them."""
    record = {
        "id": 1674207477,
        "seriesId": 1102,
        "episodeId": 73628,
        "title": "Some.Show.S01E03.1080p.WEB.H264-GRP",
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importBlocked",
        "statusMessages": [],
        "errorMessage": "",
        "downloadId": "b91ec7f7-9571-4b41-8e8b-ddcf3a8c4fe5",
        "protocol": "usenet",
        "downloadClient": "SABnzbd",
        "indexer": "NZBgeek (Prowlarr)",
        "outputPath": "/downloads/Some.Show.S01E03",
        "size": 0,
        "sizeleft": 0,
        "added": "2026-09-03T17:22:19Z",
    }
    messages = kw.pop("messages", None)
    if messages is not None:
        record["statusMessages"] = [{"title": record["title"],
                                     "messages": list(messages)}]
    record.update(kw)
    return record


def item(**kw) -> QueueItem:
    return normalise(queue_record(**kw), "sonarr", [])


def _arr(monkeypatch, handler, flavour="sonarr") -> ArrClient:
    transport = httpx.MockTransport(handler)

    def client_factory(*a, **kw):
        kw.pop("transport", None)
        return _REAL_CLIENT(transport=transport, **kw)
    monkeypatch.setattr(httpx, "Client", client_factory)
    return ArrClient(ArrConfig(enabled=True, url="http://arr:8989", api_key="k"),
                     flavour)


# -- normalisation --------------------------------------------------------

def test_a_record_with_no_download_id_is_dropped():
    """The queue row's own id changes when the *arr restarts; the client's
    does not, and it is what `blocked_since` has to survive on."""
    assert normalise(queue_record(downloadId=""), "sonarr", []) is None


def test_status_messages_are_flattened_and_path_mapped():
    q = normalise(queue_record(messages=["one", "two"]), "sonarr",
                  [{"from": "/downloads", "to": "/mnt/dl"}])
    assert q.messages == ["one", "two"]
    assert q.local_path == "/mnt/dl/Some.Show.S01E03"
    assert q.output_path == "/downloads/Some.Show.S01E03"


# -- triage: pure, and can never condemn ----------------------------------

@pytest.mark.parametrize("state", sorted(intake.WORKING_STATES))
def test_an_arr_still_working_is_left_alone(state):
    assert triage(item(trackedDownloadState=state)).kind == "working"


def test_triage_can_never_return_bad_release():
    """Invariant 23. Only evidence from the files can condemn a release, so
    the pure half of the classifier must have no route to it at all."""
    shapes = [
        item(),
        item(trackedDownloadState="failed", status="failed"),
        item(messages=["No files found are eligible for import"]),
        item(errorMessage="something nobody has ever seen"),
        item(trackedDownloadState="importFailed"),
    ]
    assert all(triage(i).kind != "bad_release" for i in shapes)


def test_the_live_sonarr_id_match_message_is_read_as_manual():
    """Verified against Sonarr 4.0.19.2979 on 2026-09-03: three complete,
    importable 2 GB downloads carried exactly this, and the queue cleaner
    running beside unfuckarr had already struck them twice."""
    q = item(messages=[
        "Found matching series via grab history, but release was matched to "
        "series by ID. Automatic import is not possible. See the FAQ for details."
    ])
    verdict = triage(q)
    assert verdict.kind == "manual"


@pytest.mark.parametrize("message", [
    "Access to the path '/tv/Show' is denied",
    "Permission denied",
    "Not an upgrade for existing episode file(s)",
    "Unable to determine which series this file belongs to",
    "The disk is full",
])
def test_arr_side_problems_never_reach_the_files(message):
    """None of these is fixed by another copy, and the permissions one is the
    mount-has-gone-away shape — the single most expensive thing to misread."""
    assert triage(item(messages=[message])).kind == "manual"


def test_an_unrecognised_block_is_flagged_not_acted_on():
    verdict = triage(item(messages=["Something new in Sonarr 5"]))
    assert verdict.kind == "unrecognised"
    assert verdict.actionable is False


# -- inspection: the evidence ---------------------------------------------

def test_a_missing_path_is_unrecognised_not_bad(settings):
    """"I could not look" is not "the release is bad" — invariant 17. On this
    system a wrong path mapping is not hypothetical: it has hidden 17,569 of
    17,715 files from Emby."""
    verdict = inspect(item(outputPath="/nope/does-not-exist"), settings)
    assert verdict.kind == "unrecognised"
    assert "does not exist" in verdict.reason


def test_an_empty_download_is_a_bad_release(settings, tmp_path):
    (tmp_path / "empty").mkdir()
    verdict = inspect(item(outputPath=str(tmp_path / "empty")), settings)
    assert verdict.kind == "bad_release"
    assert "nothing to import" in verdict.reason


def test_an_empty_dir_where_the_arr_saw_gigabytes_is_a_path_problem(
        settings, tmp_path):
    """The likeliest reading of "Sonarr recorded 2.2 GB and I see an empty
    directory" is a path mapping landing somewhere that merely exists, not a
    release that evaporated — and it is not worth a blocklist to find out."""
    (tmp_path / "empty").mkdir()
    verdict = inspect(item(outputPath=str(tmp_path / "empty"),
                           size=2_254_106_237), settings)
    assert verdict.kind == "unrecognised"
    assert "path mapping" in verdict.reason


@needs_ffmpeg
def test_a_download_far_short_of_its_release_size_is_bad(
        settings, tmp_path, video_factory):
    d = tmp_path / "short"
    d.mkdir()
    video_factory("part.mkv", seconds=4).rename(d / "Some.Show.S01E03.mkv")
    verdict = inspect(item(outputPath=str(d), size=2_254_106_237), settings)
    assert verdict.kind == "bad_release"
    assert "not the release" in verdict.reason


@needs_ffmpeg
def test_a_season_pack_is_not_read_as_a_sample(settings, tmp_path,
                                               video_factory):
    """Sonarr opens one queue record per episode and puts the *pack's* total
    size on each. Judging completeness by the largest single file therefore
    makes every episode of a twenty-part pack 5% of "its" release — and an
    earlier version of this module condemned all twenty."""
    d = tmp_path / "pack"
    d.mkdir()
    for n in range(6):
        video_factory(f"e{n}.mkv", seconds=5).rename(
            d / f"Some.Show.S01E0{n}.1080p.WEB.H264-GRP.mkv")
    pack_size = sum(f.stat().st_size for f in d.iterdir())
    verdict = inspect(item(outputPath=str(d), size=pack_size), settings)
    assert verdict.kind == "manual", verdict.reason


def test_a_still_packed_release_is_a_bad_release(settings, tmp_path):
    d = tmp_path / "packed"
    d.mkdir()
    for name in ("release.rar", "release.r00", "release.r01"):
        (d / name).write_bytes(b"x" * 10)
    verdict = inspect(item(outputPath=str(d)), settings)
    assert verdict.kind == "bad_release"
    assert "still packed" in verdict.reason
    assert verdict.evidence["archives"] == 3


def test_junk_only_is_a_bad_release(settings, tmp_path):
    d = tmp_path / "junk"
    d.mkdir()
    (d / "readme.nfo").write_text("scene notes")
    (d / "sums.sfv").write_text("x")
    verdict = inspect(item(outputPath=str(d)), settings)
    assert verdict.kind == "bad_release"


@needs_ffmpeg
def test_an_unopenable_video_is_a_bad_release(settings, tmp_path):
    """ffprobe refusing the only video is evidence, and it is exactly the
    evidence this application already exists to gather."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / "film.mkv").write_bytes(b"\x00" * 400_000)
    verdict = inspect(item(outputPath=str(d)), settings)
    assert verdict.kind == "bad_release"
    assert "will not open" in verdict.reason


@needs_ffmpeg
def test_a_named_sample_is_a_bad_release(settings, tmp_path, video_factory):
    d = tmp_path / "withsample"
    d.mkdir()
    clip = video_factory("sample.mkv", seconds=4)
    clip.rename(d / "some.release-sample.mkv")
    verdict = inspect(item(outputPath=str(d)), settings)
    assert verdict.kind == "bad_release"
    assert "sample" in verdict.reason


@needs_ffmpeg
def test_a_healthy_complete_download_is_never_a_bad_release(
        settings, tmp_path, video_factory):
    """The reason this module exists.

    A complete, openable, full-length video that the *arr will not take is the
    *arr's problem. Another copy blocks in exactly the same way, so removing
    and blocklisting throws away good media and burns a clean release — which
    is what the string-matching tools beside it were about to do to three real
    downloads on this system.
    """
    d = tmp_path / "good"
    d.mkdir()
    clip = video_factory("episode.mkv", seconds=8)
    clip.rename(d / "Some.Show.S01E03.1080p.WEB.H264-GRP.mkv")
    (d / "release.nfo").write_text("notes")

    verdict = inspect(item(outputPath=str(d), messages=["Something unfamiliar"]),
                      settings)
    assert verdict.kind == "manual"
    assert verdict.actionable is False
    assert "manual import" in verdict.reason
    assert verdict.evidence["duration"] > 0


@needs_ffmpeg
def test_the_largest_video_is_the_one_judged(settings, tmp_path, video_factory):
    """A release with a sample *and* the feature is a good release."""
    d = tmp_path / "both"
    d.mkdir()
    (d / "Sample").mkdir()
    video_factory("s.mkv", seconds=3).rename(d / "Sample" / "sample.mkv")
    video_factory("f.mkv", seconds=9).rename(d / "feature.mkv")
    verdict = inspect(item(outputPath=str(d)), settings)
    assert verdict.kind == "manual", "the feature is present and fine"
    assert verdict.evidence["largest"].endswith("feature.mkv")


@needs_ffmpeg
def test_a_release_that_is_only_a_sample_is_bad(settings, tmp_path,
                                                video_factory):
    d = tmp_path / "sampleonly"
    d.mkdir()
    (d / "Sample").mkdir()
    video_factory("s.mkv", seconds=3).rename(d / "Sample" / "sample.mkv")
    verdict = inspect(item(outputPath=str(d)), settings)
    assert verdict.kind == "bad_release"
    assert "sample" in verdict.reason


def test_read_contents_accepts_a_single_file(tmp_path):
    """Usenet single-file grabs hand the *arr the file, not a directory."""
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 100)
    contents = read_contents(str(f))
    assert contents.videos == [str(f)]


def test_read_contents_ignores_dotfiles(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / ".hidden.mkv").write_bytes(b"x")
    assert read_contents(str(d)).empty


# -- the brakes -----------------------------------------------------------

def test_the_abort_ratio_trips_when_most_of_the_queue_is_blocked(settings):
    """SABnzbd stopping makes every import fail in the same pass. Blocklisting
    on that empties the library's future in one sweep, and unlike a delete it
    cannot be undone by putting a file back."""
    cfg = settings.intake
    assert abort_reason(8, 10, cfg) is not None
    assert abort_reason(2, 10, cfg) is None


def test_the_abort_ratio_has_a_floor_that_the_file_side_does_not_need(settings):
    """A library has thousands of files, so a ratio means something on its
    own. A queue often has three, and two of three is 67% of nothing."""
    cfg = settings.intake
    assert abort_reason(2, 2, cfg) is None
    assert abort_reason(3, 3, cfg) is None
    assert abort_reason(4, 4, cfg) is not None


def test_intake_can_never_delete(settings):
    """The literal type is the enforcement, exactly as for hygiene_action."""
    from unfuckarr.config import IntakeConfig
    allowed = IntakeConfig.model_fields["action"].annotation
    assert set(getattr(allowed, "__args__", ())) == {"flag", "fix"}


# -- the pass -------------------------------------------------------------

def _patch(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def client_factory(*a, **kw):
        kw.pop("transport", None)
        return _REAL_CLIENT(transport=transport, **kw)
    monkeypatch.setattr(httpx, "Client", client_factory)


def _watcher(settings, monkeypatch, records, removed=None):
    """An IntakeWatcher wired to a fake Sonarr holding `records`."""
    settings.sonarr = ArrConfig(enabled=True, url="http://sonarr:8989",
                                api_key="k")
    settings.radarr = ArrConfig(enabled=False)

    def handler(request):
        if request.method == "DELETE":
            if removed is not None:
                removed.append(str(request.url))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"records": records,
                                         "totalRecords": len(records)})

    _patch(monkeypatch, handler)
    return IntakeWatcher(lambda: settings)


def _ripen(seconds: int = 7200) -> None:
    """Backdate every tracked item past the min-blocked timer."""
    db.ex("UPDATE intake SET blocked_since = ?", (time.time() - seconds,))


def test_a_freshly_blocked_import_is_left_to_retry(settings, monkeypatch,
                                                   tmp_path):
    """The *arr retries imports on its own schedule and most blocks clear
    themselves. Acting on a snapshot acts on downloads that were about to
    import."""
    d = tmp_path / "rel"
    d.mkdir()
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))])
    result = watcher.run_pass()
    assert result.blocked == 1
    assert result.ripe == 0, "not yet past min_blocked_minutes"
    assert result.acted == 0


def test_flag_mode_classifies_and_touches_nothing(settings, monkeypatch,
                                                  tmp_path):
    d = tmp_path / "empty-release"
    d.mkdir()
    removed: list[str] = []
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))], removed)
    watcher.run_pass()
    _ripen()
    result = watcher.run_pass()

    assert result.bad == 1
    assert result.acted == 0
    assert removed == [], "flag is the default and it must not act"
    row = db.q1("SELECT * FROM intake")
    assert row["verdict"] == "bad_release"
    assert row["acted"] is None


def test_fix_mode_removes_blocklists_and_re_searches(settings, monkeypatch,
                                                     tmp_path):
    """One DELETE, carrying blocklist and *not* skipping the redownload —
    invariant 2 in its queue form. Two calls would leave a window in which a
    plain search re-grabs the release being rejected."""
    d = tmp_path / "empty-release"
    d.mkdir()
    settings.intake.action = "fix"
    removed: list[str] = []
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))], removed)
    watcher.run_pass()
    _ripen()
    result = watcher.run_pass()

    assert result.acted == 1
    assert len(removed) == 1
    url = removed[0]
    assert "/api/v3/queue/1674207477" in url
    assert "blocklist=true" in url
    assert "removeFromClient=true" in url
    assert "skipRedownload=false" in url
    assert db.q1("SELECT acted FROM intake")["acted"] is not None


@needs_ffmpeg
def test_fix_mode_still_will_not_touch_a_good_download(settings, monkeypatch,
                                                       tmp_path, video_factory):
    """The policy says fix; the evidence says the file is fine. The evidence
    wins, because `fix` is permission to act on a bad release, not on a
    verdict the classifier never reached."""
    d = tmp_path / "good"
    d.mkdir()
    video_factory("e.mkv", seconds=8).rename(d / "Some.Show.S01E03.mkv")
    settings.intake.action = "fix"
    removed: list[str] = []
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))], removed)
    watcher.run_pass()
    _ripen()
    result = watcher.run_pass()

    assert result.manual == 1 and result.acted == 0
    assert removed == []


def test_the_abort_ratio_stops_a_pass_acting(settings, monkeypatch, tmp_path):
    settings.intake.action = "fix"
    records = []
    for n in range(6):
        d = tmp_path / f"r{n}"
        d.mkdir()
        records.append(queue_record(id=1000 + n, downloadId=f"hash{n}",
                                    outputPath=str(d)))
    removed: list[str] = []
    watcher = _watcher(settings, monkeypatch, records, removed)
    watcher.run_pass()
    _ripen()
    result = watcher.run_pass()

    assert result.aborted is not None
    assert result.bad == 6, "still classified — the flagging is the point"
    assert result.acted == 0 and removed == []


def test_the_per_pass_cap_holds(settings, monkeypatch, tmp_path):
    settings.intake.action = "fix"
    settings.intake.max_actions_per_pass = 2
    # Ten items, four of them fine, so the abort ratio does not trip first.
    records = []
    for n in range(6):
        d = tmp_path / f"bad{n}"
        d.mkdir()
        records.append(queue_record(id=1000 + n, downloadId=f"bad{n}",
                                    outputPath=str(d)))
    for n in range(6):
        records.append(queue_record(id=2000 + n, downloadId=f"ok{n}",
                                    trackedDownloadState="downloading",
                                    status="downloading"))
    removed: list[str] = []
    watcher = _watcher(settings, monkeypatch, records, removed)
    watcher.run_pass()
    _ripen()
    result = watcher.run_pass()

    assert result.aborted is None
    assert result.acted == 2
    assert len(removed) == 2


def test_a_paused_service_acts_on_nothing(settings, monkeypatch, tmp_path):
    from unfuckarr.state import state as app_state

    d = tmp_path / "empty-release"
    d.mkdir()
    settings.intake.action = "fix"
    removed: list[str] = []
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))], removed)
    watcher.run_pass()
    _ripen()
    app_state.paused = True
    try:
        result = watcher.run_pass()
    finally:
        app_state.paused = False
    assert result.bad == 1 and result.acted == 0 and removed == []


def test_an_item_is_only_acted_on_once(settings, monkeypatch, tmp_path):
    d = tmp_path / "empty-release"
    d.mkdir()
    settings.intake.action = "fix"
    removed: list[str] = []
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))], removed)
    watcher.run_pass()
    _ripen()
    watcher.run_pass()
    watcher.run_pass()
    assert len(removed) == 1


def test_an_unreachable_arr_does_not_retire_what_it_was_tracking(
        settings, monkeypatch, tmp_path):
    """An unreachable Sonarr must not read as an empty Sonarr. If it did,
    every tracked item would be marked gone, `blocked_since` would be lost,
    and the min-blocked timer would restart from zero on reconnection —
    turning an outage into a reset of every brake in the module."""
    d = tmp_path / "rel"
    d.mkdir()
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))])
    watcher.run_pass()
    assert db.q1("SELECT gone FROM intake")["gone"] is None

    def dead(request):
        raise httpx.ConnectError("no route to host")
    _patch(monkeypatch, dead)
    result = watcher.run_pass()

    assert result.errors, "the failure must be reported"
    assert db.q1("SELECT gone FROM intake")["gone"] is None


def test_leaving_the_queue_is_recorded_not_forgotten(settings, monkeypatch,
                                                     tmp_path):
    d = tmp_path / "rel"
    d.mkdir()
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))])
    watcher.run_pass()
    watcher = _watcher(settings, monkeypatch, [])
    watcher.run_pass()
    row = db.q1("SELECT * FROM intake")
    assert row is not None, "the row survives so 'did we act' stays answerable"
    assert row["gone"] is not None


def test_blocked_since_survives_a_changed_queue_id(settings, monkeypatch,
                                                   tmp_path):
    """The *arr renumbers its queue on restart. The timer is anchored to the
    download client's id precisely so a Sonarr update does not reset it."""
    d = tmp_path / "rel"
    d.mkdir()
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))])
    watcher.run_pass()
    first = db.q1("SELECT blocked_since FROM intake")["blocked_since"]

    watcher = _watcher(settings, monkeypatch,
                       [queue_record(id=999999, outputPath=str(d))])
    watcher.run_pass()
    row = db.q1("SELECT blocked_since, queue_id FROM intake")
    assert row["blocked_since"] == first
    assert row["queue_id"] == 999999


def test_going_back_to_downloading_clears_the_timer(settings, monkeypatch,
                                                    tmp_path):
    d = tmp_path / "rel"
    d.mkdir()
    watcher = _watcher(settings, monkeypatch,
                       [queue_record(outputPath=str(d))])
    watcher.run_pass()
    assert db.q1("SELECT blocked_since FROM intake")["blocked_since"]

    watcher = _watcher(settings, monkeypatch,
                       [queue_record(trackedDownloadState="downloading",
                                     outputPath=str(d))])
    watcher.run_pass()
    assert db.q1("SELECT blocked_since FROM intake")["blocked_since"] is None


def test_a_never_act_phrase_forces_manual(settings, monkeypatch, tmp_path):
    d = tmp_path / "empty-release"
    d.mkdir()
    settings.intake.action = "fix"
    settings.intake.never_act_phrases = ["my private tracker"]
    removed: list[str] = []
    watcher = _watcher(
        settings, monkeypatch,
        [queue_record(outputPath=str(d),
                      messages=["Blocked by my private tracker rules"])],
        removed)
    watcher.run_pass()
    _ripen()
    result = watcher.run_pass()
    assert result.manual == 1 and removed == []


# -- the client -----------------------------------------------------------

def test_queue_asks_for_unknown_items(monkeypatch):
    """A download the *arr can no longer match is exactly the kind that gets
    stuck, and it is left out of the default response."""
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"records": [], "totalRecords": 0})
    client = _arr(monkeypatch, handler)
    client.queue()
    assert "includeUnknownSeriesItems=true" in seen[0]
    assert "includeUnknownMovieItems=true" in seen[0]


def test_queue_pages(monkeypatch):
    pages = {
        1: {"records": [{"downloadId": "a"}, {"downloadId": "b"}],
            "totalRecords": 3},
        2: {"records": [{"downloadId": "c"}], "totalRecords": 3},
    }

    def handler(request):
        page = int(dict(request.url.params).get("page", 1))
        return httpx.Response(200, json=pages.get(page, {"records": [],
                                                         "totalRecords": 3}))
    client = _arr(monkeypatch, handler)
    assert len(client.queue()) == 3
