"""Visible terminal-surface selection for project-anchored seat launches."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TextIO


SURFACE_KINDS = frozenset({"herdr", "tmux", "print"})
LEASE_CONTEXT_ENV_NAMES = (
    "RT_PROJECT_ROOT",
    "RT_SESSION_ID",
    "RT_LEASE_REVISION",
)
TMUX_TARGET_FORMAT = "#{session_name}:#{window_index}.#{pane_index}"
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SurfaceError(RuntimeError):
    """A requested terminal surface cannot be selected or launched safely."""


@dataclass(frozen=True)
class SurfaceSelection:
    kind: str
    source: str
    executable: Path | None = None
    tmux_session: str | None = None


@dataclass(frozen=True)
class SurfaceLaunch:
    launched: bool
    command: str
    surface: dict[str, str] | None = None


def _selected_kind(raw: str, source: str) -> SurfaceSelection:
    value = raw.strip().lower()
    if value not in SURFACE_KINDS:
        expected = " | ".join(sorted(SURFACE_KINDS))
        raise SurfaceError(
            f"unknown surface {raw!r} from {source}; expected one of {expected}"
        )
    return SurfaceSelection(value, source)


def _which(name: str, environ: Mapping[str, str]) -> Path | None:
    found = shutil.which(name, path=environ.get("PATH"))
    return Path(found).resolve() if found else None


def _invoke(
    command: list[str],
    *,
    environ: Mapping[str, str],
    runner=subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        env=dict(environ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )


def _run_checked(
    surface: str,
    command: list[str],
    *,
    environ: Mapping[str, str],
    runner=subprocess.run,
) -> subprocess.CompletedProcess[str]:
    rendered = shlex.join(command)
    try:
        result = _invoke(command, environ=environ, runner=runner)
    except (OSError, subprocess.SubprocessError) as error:
        raise SurfaceError(
            f"{surface} surface command failed: {rendered}: {error}"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SurfaceError(
            f"{surface} surface command failed: {rendered} "
            f"(exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result


def _probe_tmux_session(
    environ: Mapping[str, str],
    *,
    runner=subprocess.run,
) -> tuple[Path | None, str | None]:
    executable = _which("tmux", environ)
    if executable is None:
        return None, None
    command = [str(executable), "list-clients", "-F", "#{client_session}"]
    try:
        result = _invoke(command, environ=environ, runner=runner)
    except (OSError, subprocess.SubprocessError):
        return executable, None
    if result.returncode != 0:
        return executable, None
    sessions = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return executable, sessions[0] if sessions else None


def detect_surface(
    explicit: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> SurfaceSelection:
    """Select the first requested or ambient terminal surface."""

    selected_environment = os.environ if environ is None else environ
    if explicit is not None:
        return _selected_kind(explicit, "--surface")
    configured = selected_environment.get("RT_SURFACE", "")
    if configured.strip():
        return _selected_kind(configured, "RT_SURFACE")
    if selected_environment.get("HERDR_ENV") == "1":
        return SurfaceSelection("herdr", "HERDR_ENV=1")
    if selected_environment.get("TMUX"):
        return SurfaceSelection("tmux", "TMUX")
    executable, session = _probe_tmux_session(
        selected_environment,
        runner=runner,
    )
    if executable is not None and session is not None:
        return SurfaceSelection(
            "tmux",
            "attached tmux client",
            executable=executable,
            tmux_session=session,
        )
    return SurfaceSelection("print", "fallback")


def launcher_shell_command(
    launcher: Path,
    agent_id: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Build a shell-safe launcher command with inherited seat fencing removed."""

    if not launcher.is_absolute():
        raise SurfaceError(f"seat launcher must be an absolute path: {launcher}")
    if not isinstance(agent_id, str) or not agent_id or "\0" in agent_id:
        raise SurfaceError("seat agent id must be a non-empty string without NUL")
    command = ["/usr/bin/env"]
    for name in LEASE_CONTEXT_ENV_NAMES:
        command.extend(["-u", name])
    for name, value in (environment or {}).items():
        if (
            not isinstance(name, str)
            or not ENVIRONMENT_NAME_RE.fullmatch(name)
            or name in LEASE_CONTEXT_ENV_NAMES
            or name == "RT_FROM"
        ):
            raise SurfaceError(f"invalid explicit launcher environment name: {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise SurfaceError(
                f"explicit launcher environment {name} must be a string without NUL"
            )
        command.append(f"{name}={value}")
    command.extend([f"RT_FROM={agent_id}", str(launcher)])
    return shlex.join(command)


def _json_output(
    surface: str,
    command: list[str],
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SurfaceError(
            f"{surface} surface returned invalid JSON for command "
            f"{shlex.join(command)}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise SurfaceError(
            f"{surface} surface returned non-object JSON for command "
            f"{shlex.join(command)}"
        )
    return payload


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _layout_dimensions(value: Any) -> tuple[float, float] | None:
    """Find a width/height pair without depending on unrelated JSON ordering."""

    if isinstance(value, dict):
        for width_name, height_name in (
            ("width", "height"),
            ("columns", "rows"),
            ("cols", "rows"),
        ):
            width = _positive_number(value.get(width_name))
            height = _positive_number(value.get(height_name))
            if width is not None and height is not None:
                return width, height
        for child in value.values():
            dimensions = _layout_dimensions(child)
            if dimensions is not None:
                return dimensions
    elif isinstance(value, list):
        for child in value:
            dimensions = _layout_dimensions(child)
            if dimensions is not None:
                return dimensions
    return None


def _launch_herdr(
    selection: SurfaceSelection,
    *,
    tree: Path,
    command: str,
    environ: Mapping[str, str],
    runner=subprocess.run,
) -> SurfaceLaunch:
    if environ.get("HERDR_ENV") != "1":
        raise SurfaceError(
            "herdr surface requires HERDR_ENV=1; this process is not inside a "
            "Herdr-managed pane"
        )
    caller_pane = environ.get("HERDR_PANE_ID", "").strip()
    if not caller_pane:
        raise SurfaceError(
            "herdr surface requires HERDR_PANE_ID for the calling pane"
        )
    executable = selection.executable or _which("herdr", environ)
    if executable is None:
        raise SurfaceError(
            "herdr surface is selected but the `herdr` executable is unavailable"
        )

    layout_command = [
        str(executable),
        "pane",
        "layout",
        "--pane",
        caller_pane,
    ]
    layout_result = _run_checked(
        "herdr",
        layout_command,
        environ=environ,
        runner=runner,
    )
    layout = _json_output("herdr", layout_command, layout_result)
    dimensions = _layout_dimensions(layout)
    if dimensions is None:
        raise SurfaceError(
            "herdr surface response did not contain pane width and height for "
            f"command {shlex.join(layout_command)}"
        )
    width, height = dimensions
    direction = "right" if width >= 2 * height else "down"

    split_command = [
        str(executable),
        "pane",
        "split",
        "--current",
        "--direction",
        direction,
        "--cwd",
        str(tree),
        "--no-focus",
    ]
    split_result = _run_checked(
        "herdr",
        split_command,
        environ=environ,
        runner=runner,
    )
    split = _json_output("herdr", split_command, split_result)
    result_object = split.get("result")
    pane_object = result_object.get("pane") if isinstance(result_object, dict) else None
    pane_id = pane_object.get("pane_id") if isinstance(pane_object, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        raise SurfaceError(
            "herdr surface response did not contain .result.pane.pane_id for "
            f"command {shlex.join(split_command)}"
        )

    run_command = [str(executable), "pane", "run", pane_id, command]
    _run_checked(
        "herdr",
        run_command,
        environ=environ,
        runner=runner,
    )
    return SurfaceLaunch(
        True,
        command,
        {"kind": "herdr", "pane": pane_id},
    )


def _required_tmux_session(
    executable: Path,
    environ: Mapping[str, str],
    *,
    runner=subprocess.run,
) -> str:
    command = [str(executable), "list-clients", "-F", "#{client_session}"]
    result = _run_checked(
        "tmux",
        command,
        environ=environ,
        runner=runner,
    )
    sessions = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not sessions:
        raise SurfaceError(
            "tmux surface found no attached client after command "
            f"{shlex.join(command)}"
        )
    return sessions[0]


def _launch_tmux(
    selection: SurfaceSelection,
    *,
    tree: Path,
    command: str,
    environ: Mapping[str, str],
    runner=subprocess.run,
) -> SurfaceLaunch:
    executable = selection.executable or _which("tmux", environ)
    if executable is None:
        raise SurfaceError(
            "tmux surface is selected but the `tmux` executable is unavailable"
        )
    if environ.get("TMUX"):
        launch_command = [
            str(executable),
            "split-window",
            "-d",
            "-P",
            "-F",
            TMUX_TARGET_FORMAT,
            "-c",
            str(tree),
            command,
        ]
    else:
        session = selection.tmux_session or _required_tmux_session(
            executable,
            environ,
            runner=runner,
        )
        launch_command = [
            str(executable),
            "new-window",
            "-d",
            "-P",
            "-F",
            TMUX_TARGET_FORMAT,
            "-t",
            session,
            "-c",
            str(tree),
            command,
        ]
    result = _run_checked(
        "tmux",
        launch_command,
        environ=environ,
        runner=runner,
    )
    target = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip()),
        "",
    )
    if not target:
        raise SurfaceError(
            "tmux surface returned no created target for command "
            f"{shlex.join(launch_command)}"
        )
    return SurfaceLaunch(
        True,
        command,
        {"kind": "tmux", "target": target},
    )


def launch_surface(
    selection: SurfaceSelection,
    *,
    tree: Path,
    launcher: Path,
    agent_id: str,
    launcher_environment: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO,
    runner=subprocess.run,
) -> SurfaceLaunch:
    """Launch one configured seat on the selected surface or print its command."""

    selected_environment = os.environ if environ is None else environ
    if not tree.is_absolute():
        raise SurfaceError(f"worktree root must be an absolute path: {tree}")
    command = launcher_shell_command(
        launcher,
        agent_id,
        environment=launcher_environment,
    )
    if selection.kind == "herdr":
        return _launch_herdr(
            selection,
            tree=tree,
            command=command,
            environ=selected_environment,
            runner=runner,
        )
    if selection.kind == "tmux":
        return _launch_tmux(
            selection,
            tree=tree,
            command=command,
            environ=selected_environment,
            runner=runner,
        )
    if selection.kind == "print":
        printed = f"cd {shlex.quote(str(tree))} && {command}"
        print("status: not launched, printed", file=stdout)
        print(f"command: {printed}", file=stdout)
        return SurfaceLaunch(False, printed)
    raise SurfaceError(f"unsupported surface selection: {selection.kind!r}")
