"""Policy, the safety brakes, and path mapping — the parts where a wrong
answer costs the user their library."""

from __future__ import annotations

import pytest

from unfuckarr import config, db
from unfuckarr.checks import CheckResult, Finding
from unfuckarr.config import apply_path_mappings
from unfuckarr.remediation import decide


def corrupt(code: str = "decode_errors") -> CheckResult:
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("integrity", code, "error", ""))
    return r


def incompatible() -> CheckResult:
    r = CheckResult(path="/media/x.avi")
    r.add(Finding("compat", "bad_container", "error", ""))
    return r


def untidy() -> CheckResult:
    r = CheckResult(path="/media/x.mkv")
    r.add(Finding("hygiene", "audio_missing_language", "warning", ""))
    return r


# -- decisions ------------------------------------------------------------

def test_default_policy_repairs_container_damage_before_redownloading(settings):
    d = decide(corrupt("decode_errors"), settings)
    assert d.action == "repair"


def test_unrecoverable_corruption_goes_straight_to_redownload(settings):
    d = decide(corrupt("zero_length"), settings)
    assert d.action == "redownload"


def test_repair_first_can_be_turned_off(settings):
    settings.policy.try_repair_before_redownload = False
    assert decide(corrupt("decode_errors"), settings).action == "redownload"


def test_incompatible_files_are_transcoded_not_deleted(settings):
    assert decide(incompatible(), settings).action == "transcode"


def test_hygiene_never_deletes(settings):
    """Even set to its most aggressive, a hygiene finding cannot pick
    redownload — the config type does not allow it."""
    assert decide(untidy(), settings).action == "flag"
    settings.policy.hygiene_action = "transcode"
    assert decide(untidy(), settings).action == "transcode"
    with pytest.raises(Exception):
        config.Policy(hygiene_action="redownload")


def test_clean_file_produces_no_action(settings):
    assert decide(CheckResult(path="/media/ok.mkv"), settings).action == "none"


def test_check_that_did_not_complete_does_nothing(settings):
    r = CheckResult(path="/media/x.mkv", error="ffprobe timed out")
    assert decide(r, settings).action == "none"


def test_corruption_outranks_incompatibility(settings):
    r = corrupt("zero_length")
    r.add(Finding("compat", "bad_container", "error", ""))
    assert decide(r, settings).action == "redownload"


def test_actions_can_be_disabled_entirely(settings):
    settings.policy.corrupt_action = "flag"
    settings.policy.incompatible_action = "none"
    assert decide(corrupt("zero_length"), settings).action == "flag"
    assert decide(incompatible(), settings).action == "none"


# -- the brakes -----------------------------------------------------------

def test_scan_aborts_when_most_of_the_library_fails(settings, monkeypatch):
    """An unmounted array makes every file look broken. Nothing must be
    deleted in that state."""
    from unfuckarr.remediation import Decision, Remediator
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    applied = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[0]["path"]) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)

    state.scan = ScanProgress(running=True, checked=10)
    pending = [({"path": f"/media/{i}.mkv"}, corrupt(), None,
                Decision("redownload", "corrupt")) for i in range(9)]
    out = scanner._remediate(settings, pending)

    assert "aborted" in out
    assert not applied, "nothing may be touched once the abort trips"


def test_abort_needs_more_than_a_handful_of_failures(settings, monkeypatch):
    """Three bad files out of four is a small library, not a broken mount."""
    from unfuckarr.remediation import Decision, Remediator
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    applied = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[0]["path"]) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)

    state.scan = ScanProgress(running=True, checked=4)
    pending = [({"path": f"/media/{i}.mkv"}, corrupt(), None,
                Decision("redownload", "corrupt")) for i in range(3)]
    out = scanner._remediate(settings, pending)
    assert "aborted" not in out
    assert len(applied) == 3


def test_action_cap_stops_a_runaway_scan(settings, monkeypatch):
    from unfuckarr.remediation import Decision, Remediator
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    settings.policy.max_actions_per_scan = 5
    settings.policy.abort_if_failure_ratio_over = 1.0

    applied = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[0]["path"]) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)

    state.scan = ScanProgress(running=True, checked=100)
    pending = [({"path": f"/media/{i}.mkv"}, corrupt("zero_length"), None,
                Decision("redownload", "corrupt")) for i in range(20)]
    scanner._remediate(settings, pending)
    assert len(applied) == 5


def test_flag_only_findings_do_not_count_against_the_cap(settings, monkeypatch):
    from unfuckarr.remediation import Decision, Remediator
    from unfuckarr.scanner import Scanner
    from unfuckarr.state import ScanProgress, state

    settings.policy.max_actions_per_scan = 2
    settings.policy.abort_if_failure_ratio_over = 1.0
    applied = []
    rem = Remediator(lambda: settings)
    monkeypatch.setattr(rem, "apply",
                        lambda *a, **k: applied.append(a[3].action) or {"ok": True})
    scanner = Scanner(lambda: settings, rem)

    state.scan = ScanProgress(running=True, checked=100)
    pending = [({"path": f"/media/{i}.mkv"}, untidy(), None,
                Decision("flag", "tidy")) for i in range(10)]
    scanner._remediate(settings, pending)
    assert len(applied) == 10


# -- path mapping ---------------------------------------------------------

def test_path_mapping_rewrites_a_prefix():
    m = [{"from": "/tv", "to": "/media/tv"}]
    assert apply_path_mappings("/tv/Show/S01E01.mkv", m) == "/media/tv/Show/S01E01.mkv"


def test_path_mapping_prefers_the_longest_match():
    m = [{"from": "/tv", "to": "/media/tv"},
         {"from": "/tv/anime", "to": "/media/anime"}]
    assert apply_path_mappings("/tv/anime/X/01.mkv", m) == "/media/anime/X/01.mkv"


def test_path_mapping_does_not_match_a_partial_directory_name():
    """/tv must not rewrite /tvshows."""
    m = [{"from": "/tv", "to": "/media/tv"}]
    assert apply_path_mappings("/tvshows/X.mkv", m) == "/tvshows/X.mkv"


def test_unmapped_paths_pass_through():
    assert apply_path_mappings("/movies/X.mkv", []) == "/movies/X.mkv"


# -- config ---------------------------------------------------------------

def test_settings_round_trip(tmp_path):
    s = config.load()
    s.sonarr.url = "http://sonarr:8989/"
    s.watch_folders = [config.WatchFolder(path="/downloads")]
    config.save(s)
    reloaded = config.load()
    assert reloaded.sonarr.url == "http://sonarr:8989"   # trailing slash stripped
    assert reloaded.watch_folders[0].path == "/downloads"


def test_env_seeds_config_and_enables_the_service(monkeypatch):
    monkeypatch.setenv("UNFUCKARR_RADARR_URL", "http://radarr:7878")
    monkeypatch.setenv("UNFUCKARR_RADARR_API_KEY", "abc123")
    s = config.load()
    assert s.radarr.enabled and s.radarr.api_key == "abc123"


def test_corrupt_config_file_does_not_stop_startup(tmp_path):
    config.CONFIG_PATH.write_text("{ this is not json")
    s = config.load()
    assert s.integrity.enabled is True


def test_env_watch_folders_are_parsed(monkeypatch):
    monkeypatch.setenv("UNFUCKARR_WATCH_FOLDERS", "/a, /b ")
    s = config.load()
    assert [f.path for f in s.watch_folders] == ["/a", "/b"]
