#!/usr/bin/env python3
"""Exercise ``pneu worktree open`` in one isolated live Herdr session.

The real Herdr backend drives a disposable fake Codex launcher. The launcher
claims and heartbeats a real pneu lease in an isolated runtime, then runs a
bounded drain/ack loop with the real ``rt-inbox`` / ``rt-ack`` tools. The lab
checks open -> active seat -> surface record -> mail loop without harness
credentials, and is structurally unable to stop the operator's live session.
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
STAGES = (
    "isolation",
    "open",
    "seat_active",
    "surface_record",
    "mail_delivered",
    "mail_drained",
    "mail_acked",
    "mail_archived",
    "mail_receipt",
    "teardown",
)
COPIED_TOOLS = (
    "_rtcodex.py",
    "_rtlib.py",
    "_rtruntime.py",
    "_rtsurface.py",
    "roundtable-init",
    "rt-ack",
    "rt-inbox",
    "rt-say",
    "rt-worktree",
)
HERDR_ENV_PREFIX = "HERDR_"


class LabError(RuntimeError):
    """The isolated Herdr journey did not complete safely."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        stages: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.stages = stages or {}


def _blank_stages() -> dict[str, str]:
    return {name: "pending" for name in STAGES}


def session_collision_reason(
    session: str,
    *,
    ambient: str,
    sessions: list[dict[str, Any]],
) -> str | None:
    """Explain why a requested session name is unsafe, or return None."""

    if ambient and session == ambient:
        return (
            f"refusing --session {session!r}: it is the ambient Herdr session "
            f"(HERDR_SESSION={ambient})"
        )
    for entry in sessions:
        name = entry.get("name")
        if name != session:
            continue
        if entry.get("running") is True:
            return (
                f"refusing --session {session!r}: a Herdr session with that "
                "name is already running"
            )
        return (
            f"refusing --session {session!r}: a Herdr session with that name "
            "already exists; this lab will only stop a session it created"
        )
    return None


def _without_herdr_env(environ: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith(HERDR_ENV_PREFIX)
    }


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
        raise LabError(
            f"command failed to run: {shlex.join(command)}: {error}",
            stage="open",
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LabError(
            f"command failed with exit {result.returncode}: {shlex.join(command)}"
            + (f": {detail}" if detail else ""),
            stage="open",
        )
    return result


def _json_result(
    result: subprocess.CompletedProcess[str],
    command: list[str],
    *,
    stage: str = "open",
) -> dict:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LabError(
            f"command returned invalid JSON: {shlex.join(command)}: {error}",
            stage=stage,
        ) from error
    if not isinstance(payload, dict):
        raise LabError(
            f"command returned non-object JSON: {shlex.join(command)}",
            stage=stage,
        )
    return payload


def _nested(value: Any, *names: str) -> Any:
    current = value
    for name in names:
        current = current.get(name) if isinstance(current, dict) else None
    return current


def _copy_source_tools(lab_root: Path, isolated: dict[str, str]) -> Path:
    lab_bin = lab_root / "source" / "bin"
    lab_bin.mkdir(parents=True)
    for name in COPIED_TOOLS:
        shutil.copy2(BIN / name, lab_bin / name)
    shutil.copytree(PROJECT_ROOT / "templates", lab_root / "source" / "templates")

    fake_launcher = lab_bin / "rt-codex"
    fake_launcher.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, {str(lab_bin)!r})
from _rtruntime import claim, release, update_wake

os.environ["RT_PROJECTS_FILE"] = {isolated["RT_PROJECTS_FILE"]!r}
os.environ["ROUNDTABLE_INSTALL_PREFIX"] = {isolated["ROUNDTABLE_INSTALL_PREFIX"]!r}
os.environ.setdefault("RT_RUNTIME_DIR", {isolated["RT_RUNTIME_DIR"]!r})
os.environ.setdefault("RT_CODEX_RUNTIME_DIR", {isolated["RT_CODEX_RUNTIME_DIR"]!r})

token = claim(Path.cwd(), os.environ["RT_FROM"], "codex")
os.environ["RT_PROJECT_ROOT"] = str(token.project_root)
os.environ["RT_FROM"] = token.agent_id
os.environ["RT_SESSION_ID"] = token.session_id
os.environ["RT_LEASE_REVISION"] = str(token.revision)

inbox_tool = {str(lab_bin / "rt-inbox")!r}
ack_tool = {str(lab_bin / "rt-ack")!r}
status_path = Path({str(lab_root / "mail-status.json")!r})
status = {{"drained": [], "acked": []}}
stopping = False


def stop(_signum, _frame):
    global stopping
    stopping = True


def write_status():
    payload = json.dumps(status, indent=2, sort_keys=True)
    temporary = status_path.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(status_path)


def drain_once():
    listing = subprocess.run(
        [
            sys.executable,
            inbox_tool,
            "--fenced",
            "--archive-quiet-acks",
            "-f",
            "json",
        ],
        cwd=token.project_root,
        env=os.environ,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    if listing.returncode != 0:
        return
    try:
        payload = json.loads(listing.stdout)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, list):
        return
    ids = []
    seen = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        msg_id = item.get("msg_id")
        if kind in {{"sync-ack", "malformed"}}:
            continue
        if not isinstance(msg_id, str) or not msg_id or msg_id.startswith("ack-"):
            continue
        if msg_id in seen:
            continue
        seen.add(msg_id)
        ids.append(msg_id)
    if not ids:
        return
    for msg_id in ids:
        if msg_id not in status["drained"]:
            status["drained"].append(msg_id)
    write_status()
    acked = subprocess.run(
        [sys.executable, ack_tool, "--fenced", ",".join(ids)],
        cwd=token.project_root,
        env=os.environ,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    if acked.returncode != 0:
        return
    for msg_id in ids:
        if msg_id not in status["acked"]:
            status["acked"].append(msg_id)
    write_status()


for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, stop)

try:
    write_status()
    while not stopping:
        update_wake(
            token.project_root,
            token.agent_id,
            token.session_id,
            token.revision,
            watcher_pid=os.getpid(),
        )
        drain_once()
        time.sleep(0.2)
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
        "  claude:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        "      - id: claude\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n",
        encoding="utf-8",
    )
    return repository, target


def _herdr_environ(environ: dict[str, str]) -> dict[str, str]:
    return _without_herdr_env(environ)


def _herdr(
    executable: Path,
    session: str,
    arguments: list[str],
    *,
    cwd: Path,
    environ: dict[str, str],
    timeout: float = 30.0,
    stage: str = "open",
) -> subprocess.CompletedProcess[str]:
    try:
        return _run(
            [str(executable), "--session", session, *arguments],
            cwd=cwd,
            environ=_herdr_environ(environ),
            timeout=timeout,
        )
    except LabError as error:
        raise LabError(str(error), stage=stage, stages=error.stages) from error


def _herdr_global(
    executable: Path,
    arguments: list[str],
    *,
    cwd: Path,
    environ: dict[str, str],
    timeout: float = 30.0,
    stage: str = "isolation",
) -> subprocess.CompletedProcess[str]:
    try:
        return _run(
            [str(executable), *arguments],
            cwd=cwd,
            environ=_herdr_environ(environ),
            timeout=timeout,
        )
    except LabError as error:
        raise LabError(str(error), stage=stage, stages=error.stages) from error


def _list_sessions(
    executable: Path,
    *,
    cwd: Path,
    environ: dict[str, str],
) -> list[dict[str, Any]]:
    command = [str(executable), "session", "list", "--json"]
    result = _herdr_global(
        executable,
        ["session", "list", "--json"],
        cwd=cwd,
        environ=environ,
        stage="isolation",
    )
    payload = _json_result(result, command, stage="isolation")
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        raise LabError(
            "herdr session list did not return a sessions array",
            stage="isolation",
        )
    return [entry for entry in sessions if isinstance(entry, dict)]


def _running_names(sessions: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for entry in sessions:
        name = entry.get("name")
        if isinstance(name, str) and entry.get("running") is True:
            names.add(name)
    return names


def _refuse_session_collision(
    executable: Path,
    session: str,
    *,
    cwd: Path,
    environ: dict[str, str],
) -> list[dict[str, Any]]:
    ambient = os.environ.get("HERDR_SESSION", "").strip()
    sessions = _list_sessions(executable, cwd=cwd, environ=environ)
    reason = session_collision_reason(
        session,
        ambient=ambient,
        sessions=sessions,
    )
    if reason is not None:
        raise LabError(reason, stage="isolation")
    return sessions


def _start_owned_server(
    executable: Path,
    session: str,
    *,
    cwd: Path,
    environ: dict[str, str],
    timeout: float,
    log_path: Path,
    on_spawned=None,
) -> subprocess.Popen[str]:
    command = [str(executable), "--session", session, "server"]
    try:
        log_handle = log_path.open("w", encoding="utf-8")
    except OSError as error:
        raise LabError(
            f"cannot write herdr server log {log_path}: {error}",
            stage="open",
        ) from error
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_herdr_environ(environ),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as error:
        log_handle.close()
        raise LabError(
            f"command failed to run: {shlex.join(command)}: {error}",
            stage="open",
        ) from error
    if on_spawned is not None:
        on_spawned(process)

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                detail = ""
                try:
                    detail = log_path.read_text(encoding="utf-8").strip()
                except OSError:
                    detail = ""
                raise LabError(
                    f"herdr server exited {code}: {shlex.join(command)}"
                    + (f": {detail}" if detail else ""),
                    stage="open",
                )
            if session in _running_names(
                _list_sessions(executable, cwd=cwd, environ=environ)
            ):
                return process
            time.sleep(0.1)
        raise LabError(
            f"herdr server did not become running within {timeout:.1f}s: "
            f"{shlex.join(command)}",
            stage="open",
        )
    except Exception:
        _reap_process(process)
        raise
    finally:
        log_handle.close()


def _reap_process(process: subprocess.Popen[str], timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            return


def _assert_owned_session_started(
    executable: Path,
    session: str,
    *,
    cwd: Path,
    environ: dict[str, str],
    before_running: set[str],
) -> None:
    sessions = _list_sessions(executable, cwd=cwd, environ=environ)
    after_running = _running_names(sessions)
    missing = sorted(before_running - after_running)
    if missing:
        raise LabError(
            "starting the lab session changed other running Herdr sessions: "
            f"no longer running {missing}",
            stage="isolation",
        )
    unexpected = sorted(after_running - before_running - {session})
    if unexpected:
        raise LabError(
            "starting the lab session started unexpected Herdr sessions: "
            f"{unexpected}",
            stage="isolation",
        )
    if session not in after_running:
        raise LabError(
            f"lab session {session!r} is not running after server start",
            stage="isolation",
        )


def _teardown_owned_session(
    executable: Path,
    session: str,
    *,
    cwd: Path,
    environ: dict[str, str],
) -> None:
    sessions = _list_sessions(executable, cwd=cwd, environ=environ)
    names = {
        entry.get("name")
        for entry in sessions
        if isinstance(entry.get("name"), str)
    }
    if session not in names:
        return
    running = _running_names(sessions)
    if session in running:
        _herdr_global(
            executable,
            ["session", "stop", session],
            cwd=cwd,
            environ=environ,
            stage="teardown",
        )
    _herdr_global(
        executable,
        ["session", "delete", session],
        cwd=cwd,
        environ=environ,
        stage="teardown",
    )


def _wait_until(
    predicate,
    *,
    timeout: float,
    error: str,
    stage: str,
    interval: float = 0.1,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise LabError(error, stage=stage)


def _regular_mail(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix == ".md"
        and not path.name.startswith("ack-")
    )


def _receipts(*directories: Path) -> list[Path]:
    found: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        found.extend(
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.name.startswith("ack-")
            and path.suffix == ".md"
        )
    return sorted(found)


def _mail_status(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {"drained": [], "acked": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"drained": [], "acked": []}
    if not isinstance(payload, dict):
        return {"drained": [], "acked": []}
    drained = payload.get("drained")
    acked = payload.get("acked")
    return {
        "drained": [item for item in drained if isinstance(item, str)]
        if isinstance(drained, list)
        else [],
        "acked": [item for item in acked if isinstance(item, str)]
        if isinstance(acked, list)
        else [],
    }


def _wait_for_runtime_cleanup(target: Path, timeout: float) -> None:
    sys.path.insert(0, str(BIN))
    from _rtruntime import inspect_seat, reclaim_project_runtime

    deadline = time.monotonic() + timeout
    last = inspect_seat(target, "codex")
    while last.status in {"active_healthy", "active_unhealthy", "ambiguous"}:
        if time.monotonic() >= deadline:
            raise LabError(
                "Herdr stopped but the isolated fake seat did not retire: "
                f"{last.status} ({last.detail})",
                stage="teardown",
            )
        time.sleep(0.1)
        last = inspect_seat(target, "codex")
    reclaimed = reclaim_project_runtime(target)
    if reclaimed.blockers:
        raise LabError(
            "isolated runtime teardown was blocked: "
            + "; ".join(reclaimed.blockers),
            stage="teardown",
        )


def _restore_environment(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _fail(tracker: dict[str, str], stage: str, message: str) -> None:
    tracker[stage] = "failed"
    raise LabError(message, stage=stage, stages=dict(tracker))


def _pass(tracker: dict[str, str], stage: str) -> None:
    tracker[stage] = "passed"


def _lab_payload(
    *,
    status: str,
    session: str,
    stage: str,
    reason: str | None,
    stages: dict[str, str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "reason": reason,
        "session": session,
        "stage": stage,
        "stages": stages,
        "status": status,
    }
    if extra:
        payload.update(extra)
    return payload


def run_lab(executable: Path, session: str, timeout: float) -> dict[str, Any]:
    tracker = _blank_stages()
    with tempfile.TemporaryDirectory(prefix="pneu-herdr-open-lab-") as raw_root:
        lab_root = Path(raw_root).resolve()
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
        lab_bin = _copy_source_tools(lab_root, isolated)
        previous = {name: os.environ.get(name) for name in isolated}
        os.environ.update(isolated)
        environ = dict(os.environ)
        repository: Path | None = None
        target: Path | None = None
        owned_session = False
        server_process: subprocess.Popen[str] | None = None
        teardown_error: Exception | None = None
        extra: dict[str, Any] = {}
        report: dict[str, Any] | None = None
        try:
            before = _refuse_session_collision(
                executable,
                session,
                cwd=lab_root,
                environ=environ,
            )
            before_running = _running_names(before)
            _pass(tracker, "isolation")

            repository, target = _create_projects(lab_root, lab_bin, environ)

            def _mark_owned(process: subprocess.Popen[str]) -> None:
                nonlocal owned_session, server_process
                server_process = process
                owned_session = True

            server_process = _start_owned_server(
                executable,
                session,
                cwd=repository,
                environ=environ,
                timeout=min(timeout, 20.0),
                log_path=lab_root / "herdr-server.log",
                on_spawned=_mark_owned,
            )
            _assert_owned_session_started(
                executable,
                session,
                cwd=repository,
                environ=environ,
                before_running=before_running,
            )
            create_args = [
                "workspace",
                "create",
                "--cwd",
                str(repository),
                "--env",
                f"RT_PROJECTS_FILE={isolated['RT_PROJECTS_FILE']}",
                "--env",
                f"RT_RUNTIME_DIR={isolated['RT_RUNTIME_DIR']}",
                "--env",
                f"RT_CODEX_RUNTIME_DIR={isolated['RT_CODEX_RUNTIME_DIR']}",
                "--env",
                f"ROUNDTABLE_INSTALL_PREFIX={isolated['ROUNDTABLE_INSTALL_PREFIX']}",
                "--env",
                "ROUNDTABLE_ONBOARDING_SUBPROCESS=1",
                "--env",
                f"RT_WORKTREE_OPEN_TIMEOUT={isolated['RT_WORKTREE_OPEN_TIMEOUT']}",
            ]
            create_command = [str(executable), "--session", session, *create_args]
            created = _herdr(
                executable,
                session,
                create_args,
                cwd=repository,
                environ=environ,
                stage="open",
            )
            payload = _json_result(created, create_command)
            pane_id = _nested(payload, "result", "root_pane", "pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                _fail(tracker, "open", "Herdr workspace response has no root pane id")

            open_command = " ".join(
                [
                    "env",
                    f"RT_PROJECTS_FILE={shlex.quote(isolated['RT_PROJECTS_FILE'])}",
                    f"RT_RUNTIME_DIR={shlex.quote(isolated['RT_RUNTIME_DIR'])}",
                    f"RT_CODEX_RUNTIME_DIR={shlex.quote(isolated['RT_CODEX_RUNTIME_DIR'])}",
                    f"ROUNDTABLE_INSTALL_PREFIX={shlex.quote(isolated['ROUNDTABLE_INSTALL_PREFIX'])}",
                    "ROUNDTABLE_ONBOARDING_SUBPROCESS=1",
                    f"RT_WORKTREE_OPEN_TIMEOUT={shlex.quote(isolated['RT_WORKTREE_OPEN_TIMEOUT'])}",
                    shlex.join(
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
                    ),
                ]
            )
            _herdr(
                executable,
                session,
                ["pane", "run", pane_id, open_command],
                cwd=repository,
                environ=environ,
                stage="open",
            )
            try:
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
                    stage="open",
                )
            except LabError as error:
                snapshot = ""
                try:
                    read = _herdr(
                        executable,
                        session,
                        ["pane", "read", pane_id],
                        cwd=repository,
                        environ=environ,
                        stage="open",
                    )
                    snapshot = (read.stdout or read.stderr).strip()
                except LabError as read_error:
                    snapshot = f"<pane read failed: {read_error}>"
                _fail(
                    tracker,
                    "open",
                    f"{error}; pane {pane_id} output: {snapshot or '<empty>'}",
                )
            _pass(tracker, "open")

            sys.path.insert(0, str(BIN))
            from _rtlib import resolve_project_mailbox_checked
            from _rtruntime import inspect_seat, seat_paths

            inspection = inspect_seat(target, "codex")
            surface_path = seat_paths(target, "codex", root=runtime).surface
            if inspection.status != "active_healthy":
                _fail(
                    tracker,
                    "seat_active",
                    f"opened seat is not active_healthy: {inspection.status} "
                    f"({inspection.detail})",
                )
            _pass(tracker, "seat_active")
            extra["seat"] = inspection.status
            extra["project"] = str(target)
            if not surface_path.is_file():
                _fail(
                    tracker,
                    "surface_record",
                    f"open wrote no advisory surface record: {surface_path}",
                )
            surface = json.loads(surface_path.read_text(encoding="utf-8"))
            extra["surface"] = surface.get("surface")
            _pass(tracker, "surface_record")

            mailbox = resolve_project_mailbox_checked(target)
            receiver_new = mailbox.inbox_dir / "codex" / "new"
            receiver_cur = mailbox.inbox_dir / "codex" / "cur"
            sender_new = mailbox.inbox_dir / "claude" / "new"
            sender_cur = mailbox.inbox_dir / "claude" / "cur"
            status_path = lab_root / "mail-status.json"
            mail_wait = min(timeout, 15.0)

            send_env = dict(environ)
            send_env["RT_FROM"] = "claude"
            sent = _run(
                [
                    sys.executable,
                    str(lab_bin / "rt-say"),
                    "--no-nudge",
                    "codex",
                    "fyi",
                    "isolated herdr lab mail-loop probe",
                ],
                cwd=target,
                environ=send_env,
            )
            delivered = _regular_mail(receiver_new)
            if not delivered:
                _wait_until(
                    lambda: bool(_regular_mail(receiver_new)),
                    timeout=mail_wait,
                    error=(
                        "rt-say returned but no regular mail appeared in the "
                        f"codex new/ mailbox: {(sent.stdout or sent.stderr).strip()}"
                    ),
                    stage="mail_delivered",
                )
                delivered = _regular_mail(receiver_new)
            msg_name = delivered[0].name
            msg_id = delivered[0].stem
            extra["msg_id"] = msg_id
            _pass(tracker, "mail_delivered")

            _wait_until(
                lambda: msg_id in _mail_status(status_path)["drained"]
                or not (receiver_new / msg_name).exists(),
                timeout=mail_wait,
                error=f"fake launcher did not drain {msg_id}",
                stage="mail_drained",
            )
            _pass(tracker, "mail_drained")

            _wait_until(
                lambda: msg_id in _mail_status(status_path)["acked"],
                timeout=mail_wait,
                error=f"fake launcher did not rt-ack {msg_id}",
                stage="mail_acked",
            )
            _pass(tracker, "mail_acked")

            _wait_until(
                lambda: (receiver_cur / msg_name).is_file()
                and not (receiver_new / msg_name).exists(),
                timeout=mail_wait,
                error=f"acked message was not archived to cur/: {msg_id}",
                stage="mail_archived",
            )
            _pass(tracker, "mail_archived")

            _wait_until(
                lambda: bool(_receipts(sender_new, sender_cur)),
                timeout=mail_wait,
                error="quiet receipt did not reach the sender mailbox",
                stage="mail_receipt",
            )
            extra["receipt"] = _receipts(sender_new, sender_cur)[0].name
            _pass(tracker, "mail_receipt")

            report = _lab_payload(
                status="passed",
                session=session,
                stage="teardown",
                reason=None,
                stages=tracker,
                extra=extra,
            )
        except LabError as error:
            if error.stages:
                tracker.update(error.stages)
            if error.stage in tracker and tracker[error.stage] != "passed":
                tracker[error.stage] = "failed"
            error.stages = dict(tracker)
            raise
        finally:
            if owned_session and repository is not None:
                try:
                    _teardown_owned_session(
                        executable,
                        session,
                        cwd=repository,
                        environ=environ,
                    )
                    if target is not None:
                        _wait_for_runtime_cleanup(target, min(timeout, 10.0))
                    _pass(tracker, "teardown")
                    if report is not None:
                        report["stages"] = dict(tracker)
                        report["stage"] = "teardown"
                except Exception as error:  # preserve a primary lab failure
                    tracker["teardown"] = "failed"
                    teardown_error = error
                    if report is not None:
                        report["stages"] = dict(tracker)
                finally:
                    if server_process is not None:
                        _reap_process(server_process)
            _restore_environment(previous)
            if teardown_error is not None and sys.exc_info()[0] is None:
                if isinstance(teardown_error, LabError):
                    teardown_error.stages = dict(tracker)
                    raise teardown_error
                raise LabError(
                    str(teardown_error),
                    stage="teardown",
                    stages=dict(tracker),
                ) from teardown_error
        return report if report is not None else _lab_payload(
            status="failed",
            session=session,
            stage="unknown",
            reason="lab returned no result",
            stages=tracker,
        )


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
        stage = getattr(error, "stage", "unknown")
        stages = getattr(error, "stages", None) or _blank_stages()
        if stage in stages and stages[stage] != "passed":
            stages[stage] = "failed"
        print(
            json.dumps(
                _lab_payload(
                    status="failed",
                    session=args.session,
                    stage=str(stage),
                    reason=str(error),
                    stages=stages,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        print(f"herdr-open-lab: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
