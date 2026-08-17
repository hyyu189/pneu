"""A Codex binding must not outlive the project registration it belongs to."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtruntime
from _rtlib import load_project_registry, register_project


def load_script(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


wake = load_script("rt-codex-wake", "binding_reclaim_wake")
pneu = load_script("pneu", "binding_reclaim_pneu")


@pytest.fixture
def host(tmp_path, monkeypatch):
    registry = tmp_path / "projects.yaml"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(wake, "RUNTIME_DIR", runtime)
    return {"registry": registry, "runtime": runtime}


def write_project(path: Path) -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n"
    )
    register_project(project)
    return project


def thread_payload(project: Path, thread_id: str = "thread-1") -> dict:
    return {
        "id": thread_id,
        "sessionId": "native-session-1",
        "cwd": str(project),
        "source": "cli",
        "threadSource": None,
        "parentThreadId": None,
        "ephemeral": False,
        "status": {"type": "idle"},
    }


def state_path(host: dict) -> Path:
    return host["runtime"] / "rt-codex-wake-state.json"


def bind(host: dict, project: Path, thread_id: str = "thread-1") -> dict:
    store = wake.StateStore(state_path(host))
    store.bind(project, thread_payload(project, thread_id))
    return store.bindings[str(project)]


def test_rt_projects_rm_releases_the_codex_binding(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind(host, project)
    assert wake.StateStore(state_path(host)).bindings

    environment = os.environ.copy()
    environment.update(
        {
            "RT_PROJECTS_FILE": str(host["registry"]),
            "RT_RUNTIME_DIR": str(host["runtime"]),
            "RT_CODEX_RUNTIME_DIR": str(host["runtime"]),
        }
    )
    removed = subprocess.run(
        [sys.executable, str(BIN / "rt-projects"), "rm", str(project)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert removed.returncode == 0, removed.stderr
    assert "released the Codex thread binding" in removed.stderr
    assert wake.StateStore(state_path(host)).bindings == {}


def test_rt_projects_rm_reports_an_unbind_failure_without_failing(host, tmp_path):
    project = write_project(tmp_path / "project")
    environment = os.environ.copy()
    environment.update(
        {
            "RT_PROJECTS_FILE": str(host["registry"]),
            "RT_RUNTIME_DIR": str(host["runtime"]),
            "RT_CODEX_RUNTIME_DIR": str(host["runtime"]),
        }
    )

    removed = subprocess.run(
        [sys.executable, str(BIN / "rt-projects"), "rm", str(project)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    # Nothing was bound, so the registry row is tombstoned and the advisory
    # stays quiet.
    assert removed.returncode == 0, removed.stderr
    assert "tombstoned" in removed.stdout
    assert "could not unbind" not in removed.stderr


def test_launcher_offers_a_binding_whose_registration_still_matches(host, tmp_path):
    project = write_project(tmp_path / "project")
    binding = bind(host, project)
    entries, _warnings = load_project_registry()
    entry = next(item for item in entries if item["root"] == project)
    binding.update(
        {
            "projectUuid": entry["uuid"],
            "projectRegisteredAt": entry["registered_at"],
        }
    )
    payload = json.loads(state_path(host).read_text())
    payload["bindings"][str(project)] = binding
    state_path(host).write_text(json.dumps(payload))

    assert pneu._bound_codex_thread(project, "codex") == "thread-1"


def test_launcher_discards_a_binding_from_a_previous_registration(host, tmp_path):
    project = write_project(tmp_path / "project")
    binding = bind(host, project)
    binding.update(
        {
            "projectUuid": "00000000-0000-4000-8000-000000000000",
            "projectRegisteredAt": "2026-01-01T00:00:00Z",
        }
    )
    payload = json.loads(state_path(host).read_text())
    payload["bindings"][str(project)] = binding
    state_path(host).write_text(json.dumps(payload))

    assert pneu._bound_codex_thread(project, "codex") is None


def test_launcher_keeps_a_binding_written_before_the_field_existed(host, tmp_path):
    project = write_project(tmp_path / "project")
    binding = bind(host, project)
    binding.pop("projectUuid", None)
    binding.pop("projectRegisteredAt", None)
    payload = json.loads(state_path(host).read_text())
    payload["bindings"][str(project)] = binding
    state_path(host).write_text(json.dumps(payload))

    assert pneu._bound_codex_thread(project, "codex") == "thread-1"
