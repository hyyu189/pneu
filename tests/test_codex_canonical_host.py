"""Canonical-host adoption: detect and configure, never patch.

The join switch is a semi-documented upstream internal, so every check here is
report-only and every failure mode has to be visible rather than silent.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import plistlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(ROOT))

import _rtcodex
from pneu_packaging import setup as harness_setup


def load_script(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


doctor = load_script("rt-doctor", "canonical_host_doctor")

SOCKET = Path("/tmp/roundtable-test/app-server-control.sock")


def managed(pid: int = 100) -> dict:
    return {
        "pid": pid,
        "command": f"/opt/codex app-server --listen unix://{SOCKET}",
    }


def private(pid: int = 200) -> dict:
    return {
        "pid": pid,
        "command": "/Applications/Codex.app/Contents/MacOS/codex app-server",
        "kind": "private-stdio",
    }


def render(**kwargs) -> str:
    report = doctor.Report()
    doctor.report_app_server_hosts(report, SOCKET, **kwargs)
    return report


def test_only_a_real_app_server_process_counts_as_a_host():
    assert _rtcodex._is_app_server_command(
        "/opt/codex app-server --listen unix:///tmp/a.sock"
    )
    assert _rtcodex._is_app_server_command(
        "/Applications/Codex.app/Contents/MacOS/codex app-server"
    )
    # The wake bridge names the app-server socket on its own command line; a
    # substring match would count pneu's own client as a competing host.
    assert not _rtcodex._is_app_server_command(
        "/usr/bin/python /opt/pneu/bin/rt-codex-wake --socket "
        "/tmp/roundtable-test/app-server-control.sock run"
    )


def test_dual_host_inventory_states_the_trust_domain(capsys):
    inventory = _rtcodex.AppServerHostInventory(
        managed=(managed(),),
        private=(private(),),
    )
    render(
        inventory=inventory,
        switch=(None, "CODEX_APP_SERVER_USE_LOCAL_DAEMON is not set"),
        headroom={"pid": 100, "open_files": 10, "limit": 4096},
    )
    output = capsys.readouterr().out

    assert "OK codex-hosts" in output
    assert "managed=1 other=1" in output
    assert "pid=200 kind=private-stdio" in output
    assert "machine-wide trust domain, not seat isolation" in output


def test_join_drift_is_reported_when_a_private_host_survives(capsys):
    inventory = _rtcodex.AppServerHostInventory(
        managed=(managed(),),
        private=(private(),),
    )
    render(
        inventory=inventory,
        switch=("1", "CODEX_APP_SERVER_USE_LOCAL_DAEMON=1"),
        headroom={"pid": 100, "open_files": 10, "limit": 4096},
    )
    output = capsys.readouterr().out

    assert "WARN codex-daemon-join" in output
    assert "has not joined the canonical daemon" in output
    assert "no longer be honored upstream" in output


def test_joined_desktop_reports_a_single_host(capsys):
    inventory = _rtcodex.AppServerHostInventory(managed=(managed(),))
    render(
        inventory=inventory,
        switch=("1", "CODEX_APP_SERVER_USE_LOCAL_DAEMON=1"),
        headroom={"pid": 100, "open_files": 10, "limit": 4096},
    )
    output = capsys.readouterr().out

    assert "OK codex-daemon-join" in output
    assert "no competing private app-server host" in output
    # Promotion unknown 2 is still open; doctor states it instead of implying
    # a graceful fallback nobody has observed.
    assert "is not established" in output


def test_unset_switch_is_healthy_and_offers_the_owned_fix(capsys):
    inventory = _rtcodex.AppServerHostInventory(managed=(managed(),))
    render(
        inventory=inventory,
        switch=(None, "CODEX_APP_SERVER_USE_LOCAL_DAEMON is not set"),
        headroom={"pid": 100, "open_files": 10, "limit": 4096},
    )
    output = capsys.readouterr().out

    assert "OK codex-daemon-join" in output
    assert "keeps its own private app-server host" in output
    assert "roundtable-setup apply --harness codex" in output


def test_low_descriptor_headroom_warns_before_the_daemon_fails(capsys):
    inventory = _rtcodex.AppServerHostInventory(managed=(managed(),))
    render(
        inventory=inventory,
        switch=("1", "CODEX_APP_SERVER_USE_LOCAL_DAEMON=1"),
        headroom={
            "pid": 100,
            "open_files": 200,
            "limit": 256,
            "limit_source": "launchd session default",
        },
    )
    output = capsys.readouterr().out

    assert "WARN codex-headroom" in output
    assert "close to its descriptor limit" in output


def test_healthy_headroom_reports_its_limit_source(capsys):
    inventory = _rtcodex.AppServerHostInventory(managed=(managed(),))
    render(
        inventory=inventory,
        switch=("1", "CODEX_APP_SERVER_USE_LOCAL_DAEMON=1"),
        headroom={
            "pid": 100,
            "open_files": 136,
            "limit": 4096,
            "limit_source": "managed plist",
        },
    )
    output = capsys.readouterr().out

    assert "OK codex-headroom" in output
    assert "open files=136 of 4096 (managed plist)" in output


def test_managed_app_server_asks_for_real_descriptor_headroom(tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("RT_CODEX_BIN", str(codex))
    payload = _rtcodex.app_server_plist(SOCKET, ensure_runtime=False)
    assert payload["SoftResourceLimits"]["NumberOfFiles"] == (
        _rtcodex.APP_SERVER_FILE_LIMIT
    )
    assert _rtcodex.APP_SERVER_FILE_LIMIT > 256


def test_join_agent_only_sets_the_upstream_switch():
    payload = _rtcodex.daemon_join_plist(SOCKET, ensure_runtime=False)
    assert payload["Label"] == _rtcodex.DAEMON_JOIN_LABEL
    assert payload["ProgramArguments"][1:] == [
        "setenv",
        _rtcodex.DAEMON_JOIN_VARIABLE,
        _rtcodex.DAEMON_JOIN_VALUE,
    ]
    assert payload["RunAtLoad"] is True
    # Never a service, never an environment snapshot, never the Desktop bundle.
    assert payload["KeepAlive"] is False
    assert "EnvironmentVariables" not in payload


def test_setup_owns_the_join_switch_and_unsets_it_before_removal():
    assert harness_setup.CODEX_DAEMON_JOIN_LABEL in harness_setup.CODEX_LABELS
    source = (ROOT / "pneu_packaging" / "setup.py").read_text()
    unset_index = source.index("_unset_codex_daemon_join(\n                owned")
    unload_index = source.index("_unload_codex_jobs(owned[\"codex\"])")
    remove_index = source.index("_remove_record(harness, home, owned[harness])")
    assert unset_index < unload_index < remove_index
