#!/usr/bin/env python3
"""Exercise ``pneu worktree open`` in one isolated live Herdr session.

The real Herdr backend drives a disposable fake Codex launcher. The launcher
claims and heartbeats a real pneu lease in an isolated runtime, so the lab
checks open -> active seat -> surface record without harness credentials.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BIN = PROJECT_ROOT / "bin"
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COPIED_TOOLS = (
    "_rtcodex.py",
    "_rtlib.py",
    "_rtruntime.py",
    "_rtsurface.py",
    "roundtable-init",
    "rt-worktree",
)


class LabError(RuntimeError):
    """The isolated Herdr journey did not complete safely."""


def _run(
    command: list[str],
    *,
    cwd: Path,
    environ: dict[str, str],
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environ,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LabError(f"command failed to run: {shlex.join(command)}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LabError(
            f"command failed with exit {result.returncode}: {shlex.join(command)}"
            + (f": {detail}" if detail else "")
        )
    return result


def _json_result(result: subprocess.CompletedProcess[str], command: list[str]) -> dict:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LabError(
            f"command returned invalid JSON: {shlex.join(command)}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise LabError(f"command returned non-object JSON: {shlex.join(command)}")
    return payload


def _nested(value: Any, *names: str) -> Any:
    current = value
    for name in names:
        current = current.get(name) if isinstance(current, dict) else None
    return current


def _copy_source_tools(lab_root: Path) -> Path:
    lab_bin = lab_root / "source" / "bin"
    lab_bin.mkdir(parents=True)
    for name in COPIED_TOOLS:
        shutil.copy2(BIN / name, lab_bin / name)
    shutil.copytree(PROJECT_ROOT / "templates", lab_root / "source" / "templates")

    fake_launcher = lab_bin / "rt-codex"
    fake_launcher.write_text(
        f"""#!{sys.executable}
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, {str(lab_bin)!r})
from _rtruntime import claim, release, update_wake

token = claim(Path.cwd(), os.environ["RT_FROM"], "codex")
stopping = False

def stop(_signum, _frame):
    global stopping
    stopping = True

for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, stop)

try:
    while not stopping:
        update_wake(
            token.project_root,
            token.agent_id,
            token.session_id,
            token.revision,
            watcher_pid=os.getpid(),
        )
        time.sleep(1.0)
finally:
    release(token)
""",
        encoding="utf-8",
    )
    fake_launcher.chmod(0o755)
    return lab_bin


def _create_projects(
    lab_root: Path,
    lab_bin: Path,
    environ: dict[str, str],
) -> tuple[Path, Path]:
    repository = lab_root / "repo"
    repository.mkdir()
    _run(["git", "init", "-q", "-b", "main"], cwd=repository, environ=environ)
    _run(
        ["git", "config", "user.name", "pneu Herdr lab"],
        cwd=repository,
        environ=environ,
    )
    _run(
        ["git", "config", "user.email", "pneu@example.invalid"],
        cwd=repository,
        environ=environ,
    )
    (repository / "README.md").write_text("isolated Herdr lab\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=repository, environ=environ)
    _run(["git", "commit", "-qm", "fixture"], cwd=repository, environ=environ)
    _run(
        [
            sys.executable,
            str(lab_bin / "rt-worktree"),
            "add",
            "target",
            "--repo",
            str(repository),
            "--yes",
        ],
        cwd=repository,
        environ=environ,
    )
    target = lab_root / "repo-worktree" / "target"
    (target / ".roundtable" / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        "project: .\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n",
        encoding="utf-8",
    )
    return repository, target


def _herdr(
    executable: Path,
    session: str,
    arguments: list[str],
    *,
    cwd: Path,
    environ: dict[str, str],
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [str(executable), "--session", session, *arguments],
        cwd=cwd,
        environ=environ,
        timeout=timeout,
    )


def _wait_for_runtime_cleanup(target: Path, timeout: float) -> None:
    sys.path.insert(0, str(BIN))
    from _rtruntime import inspect_seat, reclaim_project_runtime

    deadline = time.monotonic() + timeout
    last = inspect_seat(target, "codex")
    while last.status in {"active_healthy", "active_unhealthy", "ambiguous"}:
        if time.monotonic() >= deadline:
            raise LabError(
                "Herdr stopped but the isolated fake seat did not retire: "
                f"{last.status} ({last.detail})"
            )
        time.sleep(0.1)
        last = inspect_seat(target, "codex")
    reclaimed = reclaim_project_runtime(target)
    if reclaimed.blockers:
        raise LabError(
            "isolated runtime teardown was blocked: " + "; ".join(reclaimed.blockers)
        )


def _restore_environment(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def run_lab(executable: Path, session: str, timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pneu-herdr-open-lab-") as raw_root:
        lab_root = Path(raw_root).resolve()
        lab_bin = _copy_source_tools(lab_root)
        runtime = lab_root / "runtime"
        state_root = lab_root / "state"
        state_root.mkdir(mode=0o700)
        isolated = {
            "RT_PROJECTS_FILE": str(state_root / "projects.yaml"),
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
            "ROUNDTABLE_INSTALL_PREFIX": str(state_root),
            "ROUNDTABLE_ONBOARDING_SUBPROCESS": "1",
            "RT_WORKTREE_OPEN_TIMEOUT": str(timeout),
        }
        previous = {name: os.environ.get(name) for name in isolated}
        os.environ.update(isolated)
        environ = dict(os.environ)
        repository: Path | None = None
        target: Path | None = None
        server_started = False
        teardown_error: Exception | None = None
        try:
            repository, target = _create_projects(lab_root, lab_bin, environ)
            _herdr(
                executable,
                session,
                ["server", "start"],
                cwd=repository,
                environ=environ,
            )
            server_started = True
            create_command = [str(executable), "--session", session, "workspace", "create"]
            created = _herdr(
                executable,
                session,
                ["workspace", "create"],
                cwd=repository,
                environ=environ,
            )
            payload = _json_result(created, create_command)
            pane_id = _nested(payload, "result", "root_pane", "pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                raise LabError("Herdr workspace response has no root pane id")

            open_command = shlex.join(
                [
                    sys.executable,
                    str(lab_bin / "rt-worktree"),
                    "open",
                    "target",
                    "--seat",
                    "codex",
                    "--surface",
                    "herdr",
                ]
            )
            _herdr(
                executable,
                session,
                ["pane", "run", pane_id, open_command],
                cwd=repository,
                environ=environ,
            )
            _herdr(
                executable,
                session,
                [
                    "pane",
                    "wait-output",
                    pane_id,
                    "--match",
                    "worktree opened:",
                    "--timeout",
                    str(int(timeout * 1000)),
                ],
                cwd=repository,
                environ=environ,
                timeout=timeout + 5,
            )

            sys.path.insert(0, str(BIN))
            from _rtruntime import inspect_seat, seat_paths

            inspection = inspect_seat(target, "codex")
            surface_path = seat_paths(target, "codex", root=runtime).surface
            if inspection.status != "active_healthy":
                raise LabError(
                    f"opened seat is not active_healthy: {inspection.status} "
                    f"({inspection.detail})"
                )
            if not surface_path.is_file():
                raise LabError(f"open wrote no advisory surface record: {surface_path}")
            surface = json.loads(surface_path.read_text(encoding="utf-8"))
            return {
                "status": "passed",
                "session": session,
                "project": str(target),
                "seat": inspection.status,
                "surface": surface.get("surface"),
            }
        finally:
            if server_started and repository is not None and target is not None:
                try:
                    _herdr(
                        executable,
                        session,
                        ["server", "stop"],
                        cwd=repository,
                        environ=environ,
                    )
                    _wait_for_runtime_cleanup(target, min(timeout, 10.0))
                except Exception as error:  # preserve a primary lab failure
                    teardown_error = error
            _restore_environment(previous)
            if teardown_error is not None and sys.exc_info()[0] is None:
                raise teardown_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the live named-session Herdr worktree-open lab",
    )
    parser.add_argument(
        "--herdr",
        type=Path,
        help="Herdr executable (default: resolve herdr from PATH)",
    )
    parser.add_argument(
        "--session",
        default=f"pneu-lab-{os.getpid()}-{uuid.uuid4().hex[:8]}",
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args(argv)
    if not SESSION_RE.fullmatch(args.session):
        parser.error("--session must use only letters, digits, dot, underscore, or dash")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    executable = args.herdr or (
        Path(found).resolve() if (found := shutil.which("herdr")) else None
    )
    if executable is None or not executable.is_file() or not os.access(executable, os.X_OK):
        parser.error("Herdr is unavailable; install it or pass --herdr PATH")
    try:
        result = run_lab(executable, args.session, args.timeout)
    except (LabError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"herdr-open-lab: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
