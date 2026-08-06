#!/usr/bin/env python3
"""Run the credentialed Grok adapter lab in a disposable temp project.

The caller supplies an already-authorized ``XAI_API_KEY`` in the process
environment.  This lab never reads, prints, stores, or copies the host auth
file.  Every Grok-owned HOME, XDG directory, runtime record, mailbox, and
ACP event log is rooted below ``--lab-root``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BIN = PROJECT_ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import _rtruntime  # noqa: E402
from _rtlib import resolve_project_mailbox  # noqa: E402
from integrations.grok.roundtable import (  # noqa: E402
    GrokAdapter,
    create_isolation,
    _scrub,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _safe_lab_root(value: Path | None) -> Path:
    if value is None:
        return Path(tempfile.mkdtemp(prefix="rt-grok-stage25-"))
    root = value.expanduser().resolve()
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/private/tmp")}
    if not any(root == candidate or candidate in root.parents for candidate in temp_roots):
        raise SystemExit("--lab-root must be under the system temp directory")
    if root == PROJECT_ROOT or PROJECT_ROOT in root.parents:
        raise SystemExit("refusing to use the checkout or a checkout child as lab root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _environment(root: Path, registry: Path) -> dict[str, str]:
    environment = dict(os.environ)
    runtime = root / "runtime"
    environment.update(
        {
            "HOME": str(root / "home"),
            "GROK_HOME": str(root / "grok-home"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "TMPDIR": str(root / "tmp"),
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
            "RT_PROJECTS_FILE": str(registry),
            "PATH": str(BIN) + os.pathsep + environment.get("PATH", os.defpath),
        }
    )
    for name in (
        "HOME",
        "GROK_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
    ):
        Path(environment[name]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return environment


def _run(args: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        # Do not persist or print vendor output: it is not needed for the
        # evidence contract and could contain provider-sensitive material.
        raise RuntimeError(f"command failed with exit {completed.returncode}: {args[0]}")


def _bootstrap(root: Path, environment: dict[str, str]) -> Path:
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    _run([str(BIN / "roundtable-init"), "--here", "--no-git"], cwd=project, environment=environment)
    state = project / ".roundtable"
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        'project: "."\n'
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n"
        "  grok:\n"
        "    harness: grok-build\n"
        "    instances:\n"
        "      - id: grok\n",
        encoding="utf-8",
    )
    (state / "agents.yaml").chmod(0o600)
    environment["RT_PROJECT_ROOT"] = str(project)
    environment["ROUNDTABLE_PROJECT_DIR"] = str(project)
    return project


def _send(project: Path, environment: dict[str, str], body: str) -> None:
    sender = dict(environment)
    sender["RT_FROM"] = "codex"
    _run(
        [
            str(BIN / "rt-say"),
            "--no-nudge",
            "grok",
            "directive",
            body,
        ],
        cwd=project,
        environment=sender,
    )


def _inbox_snapshot(project: Path, environment: dict[str, str], registry: Path) -> dict[str, Any]:
    mailbox = resolve_project_mailbox(project, registry_path=registry)
    new_dir = mailbox.inbox_dir / "grok" / "new"
    cur_dir = mailbox.inbox_dir / "grok" / "cur"
    new_names = sorted(
        path.name
        for path in new_dir.iterdir()
        if not path.name.startswith((".", "ack-"))
    ) if new_dir.is_dir() else []
    cur_names = sorted(path.name for path in cur_dir.iterdir()) if cur_dir.is_dir() else []
    return {
        "new": new_names,
        "cur_count": len(cur_names),
        "cur_names": cur_names,
    }


def _permission_summary(adapter: GrokAdapter) -> list[dict[str, Any]]:
    client = adapter.client
    if client is None:
        return []
    return [
        {
            "allowed": bool(item.get("allowed")),
            "command": _scrub(str(item.get("command") or "")),
        }
        for item in client.permission_decisions
    ]


def _secret_appears(root: Path, secret: str) -> bool:
    needle = secret.encode("utf-8")
    return any(
        path.is_file() and needle in path.read_bytes()
        for path in root.rglob("*")
    )


def run_lab(*, grok: Path, lab_root: Path, kill_before_second: bool) -> Path:
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("XAI_API_KEY must be supplied by the caller for this lab")
    registry = lab_root / "projects.yaml"
    environment = _environment(lab_root, registry)
    project = _bootstrap(lab_root, environment)
    # The adapter and runtime library must resolve every project/runtime path
    # from the disposable lab environment as well as child subprocesses.
    os.environ.update(environment)
    lease = _rtruntime.claim(project, "grok", "grok", owner_pid=os.getpid())
    isolation = create_isolation(project, runtime_root=lab_root / "adapter-runtime")
    adapter = GrokAdapter(
        project,
        agent_id="grok",
        session_id=lease.session_id,
        revision=lease.revision,
        isolation=isolation,
        executable=grok,
        api_key_resolver=lambda: api_key,
        prompt_timeout=180.0,
        drain_timeout=180.0,
        max_restarts=1,
    )
    result: dict[str, Any] = {
        "schema": "roundtable.grok_adapter_stage25_lab.v1",
        "generations": [],
        "kill_before_second": kill_before_second,
        "lease_revision_present": bool(lease.revision),
        "isolation_root_relative": str(isolation.root.relative_to(lab_root)),
    }
    try:
        adapter._fence_update()
        for index, body in enumerate(
            (
                "GROK_ADAPTER_E2E_A: read this durable message, do the requested Roundtable acknowledgement, and reply briefly.",
                "GROK_ADAPTER_E2E_B: read this second durable message, do the requested Roundtable acknowledgement, and reply briefly.",
            ),
            start=1,
        ):
            _send(project, environment, body)
            before = _inbox_snapshot(project, environment, registry)
            names = tuple(before["new"])
            if len(names) != 1:
                raise RuntimeError(f"expected one exact new-mail name, got {len(names)}")
            if index == 2 and kill_before_second:
                adapter.stop(force=True)
            run = adapter.run_generation(names)
            after = _inbox_snapshot(project, environment, registry)
            result["generations"].append(
                {
                    "index": index,
                    "message_name": names[0],
                    "session_id_present": bool(run.session_id),
                    "stop_reason": run.stop_reason,
                    "new_after": after["new"],
                    "cur_count_after": after["cur_count"],
                }
            )
        result["permission_decisions"] = _permission_summary(adapter)
        result["final_mail"] = _inbox_snapshot(project, environment, registry)
        result["child_alive_before_close"] = bool(
            adapter.client is not None and adapter.client.process.poll() is None
        )
    finally:
        try:
            if adapter.wake_claimed:
                adapter._clear_fence()
        finally:
            adapter.stop()
            _rtruntime.release(lease)
    result["secret_not_written_to_lab"] = not _secret_appears(lab_root, api_key)
    if not result["secret_not_written_to_lab"]:
        raise RuntimeError("credential material was found in the disposable lab")
    result_path = lab_root / "result.json"
    result_path.write_text(_json(result) + "\n", encoding="utf-8")
    return result_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grok_adapter_stage25_lab")
    parser.add_argument("--grok", type=Path, required=True)
    parser.add_argument("--lab-root", type=Path)
    parser.add_argument("--kill-before-second", action="store_true")
    args = parser.parse_args(argv)
    grok = args.grok.expanduser().resolve()
    if not grok.is_file() or not os.access(grok, os.X_OK):
        raise SystemExit(f"grok executable is not runnable: {grok}")
    root = _safe_lab_root(args.lab_root)
    result_path = run_lab(
        grok=grok,
        lab_root=root,
        kill_before_second=args.kill_before_second,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    print(
        _json(
            {
                "schema": result["schema"],
                "result": str(result_path),
                "generation_count": len(result["generations"]),
                "final_new_count": len(result["final_mail"]["new"]),
                "secret_not_written_to_lab": result["secret_not_written_to_lab"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
