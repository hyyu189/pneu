"""Stage 2: a seat drives its recorded surface explicitly, or refuses.

The rule these tests defend is narrow and absolute: pneu never fabricates
``HERDR_ENV=1``.  That variable asserts the caller is genuinely inside a Herdr
pane and changes ``--current`` semantics for every thread on the host, so a
process that is not in a pane either addresses the recorded pane through a
broker that genuinely is, or it fails closed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtsurface
from _rtsurface import SurfaceError


def herdr_surface() -> dict[str, str]:
    return {
        "kind": "herdr",
        "pane": "pane-7",
        "workspace": "ws-1",
        "endpoint": "/tmp/herdr.sock",
    }


def test_launch_environment_capture_keeps_only_explicit_identifiers():
    captured = _rtsurface.capability_surface_from_environment(
        {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "pane-7",
            "HERDR_WORKSPACE_ID": "ws-1",
            "HERDR_TAB_ID": "tab-2",
            "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
            "HOME": "/home/example",
            "PATH": "/usr/bin",
            "AWS_SECRET_ACCESS_KEY": "secret",
        }
    )

    assert captured == {
        "kind": "herdr",
        "pane": "pane-7",
        "workspace": "ws-1",
        "tab": "tab-2",
        "endpoint": "/tmp/herdr.sock",
    }


def test_tmux_capture_records_the_pane_and_its_socket():
    captured = _rtsurface.capability_surface_from_environment(
        {"TMUX": "/private/tmp/tmux-501/default,1234,0", "TMUX_PANE": "%3"}
    )

    assert captured == {
        "kind": "tmux",
        "target": "%3",
        "endpoint": "/private/tmp/tmux-501/default",
    }


def test_no_ambient_surface_records_nothing():
    assert _rtsurface.capability_surface_from_environment({}) is None
    assert (
        _rtsurface.capability_surface_from_environment({"HERDR_ENV": "1"}) is None
    )


def test_address_is_explicit_never_current():
    assert _rtsurface.capability_surface_arguments(herdr_surface()) == [
        "--pane",
        "pane-7",
    ]
    assert _rtsurface.capability_surface_arguments(
        {"kind": "tmux", "target": "%3"}
    ) == ["-t", "%3"]


def test_a_process_outside_a_pane_refuses_without_a_broker():
    with pytest.raises(SurfaceError) as error:
        _rtsurface.capability_surface_command(
            herdr_surface(),
            ["pane", "layout", "--pane", "pane-7"],
            environ={"PATH": "/usr/bin"},
        )
    assert "never fabricates HERDR_ENV=1" in str(error.value)


def test_broker_executes_the_explicit_address(tmp_path):
    broker = tmp_path / "herdr-broker"
    broker.write_text("#!/bin/sh\nexit 0\n")
    broker.chmod(0o755)

    command = _rtsurface.capability_surface_command(
        herdr_surface(),
        ["pane", "layout", "--pane", "pane-7"],
        environ={"PATH": "/usr/bin", "RT_HERDR_BROKER": str(broker)},
    )

    assert command == [
        str(broker),
        "pane",
        "layout",
        "--pane",
        "pane-7",
    ]


def test_broker_must_be_an_absolute_executable(tmp_path):
    with pytest.raises(SurfaceError) as error:
        _rtsurface.capability_surface_command(
            herdr_surface(),
            ["pane", "layout"],
            environ={"PATH": "/usr/bin", "RT_HERDR_BROKER": "herdr"},
        )
    assert "must be an absolute path" in str(error.value)

    missing = tmp_path / "not-executable"
    missing.write_text("")
    with pytest.raises(SurfaceError) as error:
        _rtsurface.capability_surface_command(
            herdr_surface(),
            ["pane", "layout"],
            environ={"PATH": "/usr/bin", "RT_HERDR_BROKER": str(missing)},
        )
    assert "not an executable file" in str(error.value)


def test_a_vanished_pane_fails_closed_with_a_clear_diagnostic(tmp_path):
    broker = tmp_path / "herdr-broker"
    broker.write_text("#!/bin/sh\necho 'pane pane-7 not found' >&2\nexit 1\n")
    broker.chmod(0o755)

    with pytest.raises(SurfaceError) as error:
        _rtsurface.probe_capability_surface(
            herdr_surface(),
            environ={"PATH": "/usr/bin", "RT_HERDR_BROKER": str(broker)},
        )
    detail = str(error.value)
    assert "recorded herdr surface is no longer available" in detail
    assert "pane pane-7 not found" in detail


def test_a_live_pane_probes_clean(tmp_path):
    broker = tmp_path / "herdr-broker"
    broker.write_text("#!/bin/sh\nexit 0\n")
    broker.chmod(0o755)

    _rtsurface.probe_capability_surface(
        herdr_surface(),
        environ={"PATH": "/usr/bin", "RT_HERDR_BROKER": str(broker)},
    )


def test_no_pneu_code_fabricates_a_herdr_environment():
    """The daemon must never assert it is inside a Herdr pane."""

    offenders = []
    for path in sorted(BIN.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "HERDR_ENV" not in line:
                continue
            stripped = line.strip()
            # Reading the variable is how a launcher tells the truth about its
            # own process; writing it is how a daemon would lie about one.
            if any(
                token in stripped
                for token in ('HERDR_ENV"] =', "HERDR_ENV'] =", 'HERDR_ENV="1"')
            ):
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert offenders == []
