"""Re-searching a file nothing can repair.

The whole point of this action is what it does *not* do. A redownload deletes
first and hopes; this asks the *arr for a better release and leaves the file
alone, because the *arr's own upgrade path already replaces it only on a
successful import. Every test here is really one of two questions: does it ask,
and is the file still on disk afterwards.
"""

from __future__ import annotations

import os
import time

import pytest

from unfuckarr import db
from unfuckarr.checks import CheckResult, Finding
from unfuckarr.clients.arr import ArrClient
from unfuckarr.config import ArrConfig
from unfuckarr.remediation import Decision, Remediator, decide


def reencoded() -> CheckResult:
    """A heavily re-encoded source: real, and nothing a rewrite can change."""
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("hygiene", "very_low_bitrate", "warning", "0.82 Mbps at 1080p"))
    return r


def image_subs() -> CheckResult:
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("hygiene", "image_subtitles_only", "warning", ""))
    return r


def refused_without_reasons() -> CheckResult:
    """Emby says no and will not say why — measured as 100% of its verdicts."""
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("emby", "no_direct_play", "error", "Emby did not say why", {}))
    return r


def fixable() -> CheckResult:
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("hygiene", "audio_missing_language", "warning", ""))
    return r


# -- the decision ---------------------------------------------------------

def test_unfixable_findings_still_only_flag_by_default(settings):
    """Spending someone's bandwidth is opt-in."""
    assert settings.policy.unfixable_action == "flag"
    assert decide(reencoded(), settings).action == "flag"


def test_unfixable_findings_are_researched_when_asked(settings):
    settings.policy.unfixable_action = "research"
    assert decide(reencoded(), settings).action == "research"
    assert decide(image_subs(), settings).action == "research"


def test_a_reasonless_emby_refusal_is_researched_too(settings):
    settings.policy.unfixable_action = "research"
    assert decide(refused_without_reasons(), settings).action == "research"


def test_a_fixable_finding_is_still_transcoded(settings):
    """The re-search must not steal work the transcoder can actually do."""
    settings.policy.unfixable_action = "research"
    settings.policy.hygiene_action = "transcode"
    assert decide(fixable(), settings).action == "transcode"


def test_unfixable_can_be_silenced_entirely(settings):
    settings.policy.unfixable_action = "none"
    assert decide(reencoded(), settings).action == "none"


# -- the action -----------------------------------------------------------

class FakeArr:
    flavour = "sonarr"

    def __init__(self, blocked: str | None = None):
        self.blocked = blocked
        self.searched: list[tuple[int, list[int]]] = []
        self.deleted: list[int] = []

    def upgrade_blocked(self, entity_id: int) -> str | None:
        return self.blocked

    def search(self, entity_id: int, episode_ids=None) -> None:
        self.searched.append((entity_id, list(episode_ids or [])))

    def delete_file(self, file_id: int) -> None:  # pragma: no cover - must not run
        self.deleted.append(file_id)


@pytest.fixture
def row(tmp_path):
    path = tmp_path / "ep.mkv"
    path.write_bytes(b"not really a video, but it is on disk")
    db.ex("INSERT INTO files (path, library, source, arr_id, arr_parent_id, title) "
          "VALUES (?,?,?,?,?,?)",
          (str(path), "TV", "sonarr", 11, 22, "Ep"))
    return {"path": str(path), "library": "TV", "source": "sonarr",
            "arr_id": 11, "arr_parent_id": 22, "arr_episode_ids": [33],
            "title": "Ep", "last_research": None}


def _remediator(settings, arr):
    rem = Remediator(lambda: settings)
    rem._arr_for = lambda file_row: arr
    return rem


def test_research_asks_the_arr_and_keeps_the_file(settings, row):
    arr = FakeArr()
    rem = _remediator(settings, arr)
    out = rem.apply(row, reencoded(), None,
                    Decision("research", "no rewrite can help"))
    assert out["ok"]
    assert arr.searched == [(22, [33])]
    # The two things that separate this from a redownload.
    assert arr.deleted == []
    assert os.path.exists(row["path"])


def test_research_records_when_it_asked(settings, row):
    rem = _remediator(settings, FakeArr())
    rem.apply(row, reencoded(), None, Decision("research", "x"))
    stored = db.q1("SELECT last_research, research_attempts FROM files WHERE path=?",
                   (row["path"],))
    assert stored["last_research"] > 0
    assert stored["research_attempts"] == 1


def test_a_file_in_its_cooldown_is_not_asked_about_again(settings, row):
    settings.policy.research_after_days = 30
    row["last_research"] = time.time() - 5 * 86400
    arr = FakeArr()
    rem = _remediator(settings, arr)
    out = rem.apply(row, reencoded(), None, Decision("research", "x"))
    assert arr.searched == []
    assert "waiting" in out["message"]


def test_the_cooldown_expires(settings, row):
    settings.policy.research_after_days = 30
    row["last_research"] = time.time() - 31 * 86400
    arr = FakeArr()
    rem = _remediator(settings, arr)
    rem.apply(row, reencoded(), None, Decision("research", "x"))
    assert arr.searched == [(22, [33])]


def test_a_profile_that_forbids_upgrades_is_not_queried(settings, row):
    """The *arr accepts the command and silently rejects every release, so
    the refusal has to be caught here or it looks like work."""
    arr = FakeArr(blocked="quality profile 'HD-1080p' has upgrades turned off")
    rem = _remediator(settings, arr)
    out = rem.apply(row, reencoded(), None, Decision("research", "x"))
    assert arr.searched == []
    assert "upgrades turned off" in out["message"]
    # Still recorded, so the cap is not spent on it again tomorrow.
    assert db.q1("SELECT last_research FROM files WHERE path=?",
                 (row["path"],))["last_research"] > 0


def test_a_file_no_arr_owns_is_left_alone(settings, row):
    row["arr_parent_id"] = None
    arr = FakeArr()
    rem = _remediator(settings, arr)
    out = rem.apply(row, reencoded(), None, Decision("research", "x"))
    assert arr.searched == []
    assert "no Sonarr or Radarr entry" in out["message"]


# -- reading the quality profile ------------------------------------------

def _client(profile: dict, monkeypatch) -> ArrClient:
    c = ArrClient(ArrConfig(enabled=True, url="http://x", api_key="k"), "sonarr")
    c._profiles = {7: profile}
    monkeypatch.setattr(c, "_request",
                        lambda method, path, **kw: {"qualityProfileId": 7})
    return c


def _profile(name: str, upgrade: bool, cutoff: int, allowed: list[tuple[int, str, bool]]):
    return {
        "id": 7, "name": name, "upgradeAllowed": upgrade, "cutoff": cutoff,
        "items": [{"quality": {"id": i, "name": n}, "allowed": a}
                  for i, n, a in allowed],
    }


def test_upgrades_disabled_is_reported(monkeypatch):
    p = _profile("HD-1080p", False, 1,
                 [(1, "HDTV-1080p", True), (2, "Bluray-1080p", True)])
    assert "upgrades turned off" in _client(p, monkeypatch).upgrade_blocked(5)


def test_a_cutoff_at_the_bottom_is_reported(monkeypatch):
    """Sonarr upgrades only until it reaches the cutoff, so a cutoff at the
    worst allowed quality is met by every file that exists."""
    p = _profile("HD-1080p", True, 1,
                 [(1, "HDTV-1080p", True), (2, "Bluray-1080p", True)])
    blocked = _client(p, monkeypatch).upgrade_blocked(5)
    assert blocked is not None and "lowest quality it allows" in blocked


def test_a_cutoff_above_the_floor_allows_the_search(monkeypatch):
    p = _profile("Any", True, 2,
                 [(1, "HDTV-1080p", True), (2, "Bluray-1080p", True)])
    assert _client(p, monkeypatch).upgrade_blocked(5) is None


def test_a_disallowed_quality_does_not_count_as_the_floor(monkeypatch):
    """The cutoff is compared against what the profile *allows*; a disabled
    entry below it must not make the cutoff look raised."""
    p = _profile("Any", True, 2,
                 [(1, "SDTV", False), (2, "Bluray-1080p", True)])
    blocked = _client(p, monkeypatch).upgrade_blocked(5)
    assert blocked is not None and "lowest quality it allows" in blocked


# -- pacing ---------------------------------------------------------------

def _pending(n: int, last_research=None):
    from unfuckarr.remediation import Decision
    return [({"path": f"/media/{i}.mkv", "arr_parent_id": 1,
              "arr_episode_ids": [i], "last_research": last_research},
             reencoded(), None, Decision("research", "no rewrite can help"))
            for i in range(n)]


def test_a_pass_asks_for_no_more_than_the_cap(settings, monkeypatch):
    """Each re-search is a query fanned out to every indexer. Firing one per
    stuck file — 1,476 of them on the library this was built against — is how
    an account gets banned."""
    from unfuckarr.remediation import Remediator
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    settings.policy.max_researches_per_scan = 5
    asked = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: asked.append(a[0]["path"]) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)

    state.scan = ScanProgress(running=True, checked=200)
    out = scanner._remediate(settings, _pending(50), population=200)

    assert len(asked) == 5
    assert out["researches"] == 5


def test_files_in_their_cooldown_do_not_spend_a_slot(settings, monkeypatch):
    """Otherwise the first five files in the backlog absorb the cap every
    night for a month and nothing behind them is ever asked about."""
    from unfuckarr.remediation import Decision, Remediator
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    settings.policy.max_researches_per_scan = 5
    settings.policy.research_after_days = 30
    asked = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: asked.append(a[0]["path"]) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)

    recent = time.time() - 86400
    pending = [({"path": f"/media/cooling{i}.mkv", "arr_parent_id": 1,
                 "arr_episode_ids": [i], "last_research": recent},
                reencoded(), None, Decision("research", "x")) for i in range(10)]
    pending += _pending(3)

    state.scan = ScanProgress(running=True, checked=200)
    scanner._remediate(settings, pending, population=200)

    assert sorted(asked) == ["/media/0.mkv", "/media/1.mkv", "/media/2.mkv"]


def test_a_research_never_trips_the_abort_brake(settings, monkeypatch):
    """The brake catches a library that has just broken. A re-search does not
    touch a byte on disk, so it cannot be what broke it."""
    from unfuckarr.remediation import Remediator
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    settings.policy.max_researches_per_scan = 100
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply", lambda *a, **k: {"ok": True})
    scanner = Scanner(lambda: settings, rem)

    state.scan = ScanProgress(running=True, checked=10)
    out = scanner._remediate(settings, _pending(9), population=10)

    assert "aborted" not in out
