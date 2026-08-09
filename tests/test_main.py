"""Bind-address resolution in ``__main__``."""

from __future__ import annotations

import sys

import pytest

from unfuckarr.__main__ import resolve_host

LOOPBACK = "lo0" if sys.platform == "darwin" else "lo"


def test_defaults_to_all_interfaces(monkeypatch):
    monkeypatch.delenv("UNFUCKARR_HOST", raising=False)
    monkeypatch.delenv("UNFUCKARR_BIND_INTERFACE", raising=False)
    assert resolve_host() == "0.0.0.0"


def test_host_env_is_respected(monkeypatch):
    monkeypatch.setenv("UNFUCKARR_HOST", "127.0.0.1")
    monkeypatch.delenv("UNFUCKARR_BIND_INTERFACE", raising=False)
    assert resolve_host() == "127.0.0.1"


def test_interface_resolves_and_beats_host(monkeypatch):
    monkeypatch.setenv("UNFUCKARR_HOST", "0.0.0.0")
    monkeypatch.setenv("UNFUCKARR_BIND_INTERFACE", LOOPBACK)
    assert resolve_host() == "127.0.0.1"


def test_missing_interface_fails_rather_than_binding_everywhere(monkeypatch):
    monkeypatch.setenv("UNFUCKARR_BIND_INTERFACE", "no-such-if0")
    monkeypatch.setenv("UNFUCKARR_BIND_WAIT", "0")
    with pytest.raises(SystemExit, match="no-such-if0"):
        resolve_host()
