from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.herdr_open_lab import (  # noqa: E402
    STAGES,
    _lab_payload,
    _without_herdr_env,
    session_collision_reason,
)


def test_session_collision_refuses_ambient_name():
    reason = session_collision_reason(
        "pneu-dev",
        ambient="pneu-dev",
        sessions=[],
    )
    assert reason is not None
    assert "ambient Herdr session" in reason


def test_session_collision_refuses_running_session():
    reason = session_collision_reason(
        "live",
        ambient="pneu-dev",
        sessions=[{"name": "live", "running": True}],
    )
    assert reason is not None
    assert "already running" in reason


def test_session_collision_refuses_existing_stopped_session():
    reason = session_collision_reason(
        "demo",
        ambient="pneu-dev",
        sessions=[{"name": "demo", "running": False}],
    )
    assert reason is not None
    assert "already exists" in reason


def test_session_collision_allows_fresh_name():
    assert (
        session_collision_reason(
            "pneu-lab-fresh",
            ambient="pneu-dev",
            sessions=[
                {"name": "pneu-dev", "running": True},
                {"name": "demo", "running": False},
            ],
        )
        is None
    )


def test_herdr_env_is_stripped_from_lab_commands():
    cleaned = _without_herdr_env(
        {
            "HERDR_ENV": "1",
            "HERDR_SESSION": "pneu-dev",
            "HERDR_SOCKET_PATH": "/tmp/pneu-dev.sock",
            "PATH": "/usr/bin",
            "RT_RUNTIME_DIR": "/tmp/runtime",
        }
    )
    assert "HERDR_ENV" not in cleaned
    assert "HERDR_SESSION" not in cleaned
    assert "HERDR_SOCKET_PATH" not in cleaned
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["RT_RUNTIME_DIR"] == "/tmp/runtime"


def test_failure_payload_names_stage_and_is_json_serializable():
    stages = {name: "pending" for name in STAGES}
    stages["isolation"] = "failed"
    payload = _lab_payload(
        status="failed",
        session="pneu-lab-x",
        stage="isolation",
        reason="refusing --session 'pneu-dev': it is the ambient Herdr session",
        stages=stages,
    )
    encoded = json.dumps(payload, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["status"] == "failed"
    assert decoded["stage"] == "isolation"
    assert decoded["session"] == "pneu-lab-x"
    assert decoded["reason"].startswith("refusing --session")
    assert decoded["stages"]["isolation"] == "failed"
    assert decoded["stages"]["teardown"] == "pending"
