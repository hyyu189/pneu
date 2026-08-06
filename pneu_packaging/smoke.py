"""Exercise the terminal-independent send, inbox, ack, and drain baseline."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
MESSAGE_ID_RE = re.compile(r"sent maildir-only (?P<id>\S+)")


class SmokeFailure(RuntimeError):
    pass


def default_bin_dir() -> Path:
    installed = Path(sys.executable).absolute().parent
    if (installed / "rt-say").is_file():
        return installed
    return SOURCE_ROOT / "bin"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the isolated terminal-independent Roundtable baseline."
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=default_bin_dir(),
        help="directory containing rt-say, rt-inbox, and rt-ack",
    )
    return parser.parse_args(argv)


def tool_command(bin_dir: Path, name: str) -> list[str]:
    path = (bin_dir / name).expanduser().absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SmokeFailure(f"missing executable: {path}")
    try:
        first_line = path.read_text(errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        first_line = ""
    if first_line.startswith("#!/usr/bin/env python"):
        return [sys.executable, str(path)]
    return [str(path)]


def load_rtlib(bin_dir: Path):
    """Load the resolver shipped beside the exact tools under smoke test."""

    library_path = (bin_dir / "_rtlib.py").expanduser().absolute()
    if not library_path.is_file():
        raise SmokeFailure(f"missing mailbox library: {library_path}")
    module_name = f"_roundtable_smoke_rtlib_{os.getpid()}"
    spec = importlib.util.spec_from_file_location(module_name, library_path)
    if spec is None or spec.loader is None:
        raise SmokeFailure(f"cannot load mailbox library: {library_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def run_tool(
    commands: dict[str, list[str]],
    name: str,
    *arguments: str,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [*commands[name], *arguments],
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SmokeFailure(
            f"{name} exited {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return result


def write_project(root: Path) -> None:
    state = root / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {root}\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n"
        "  claude:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        "      - id: claude\n"
    )


def smoke(bin_dir: Path) -> dict:
    rtlib = load_rtlib(bin_dir)
    commands = {
        name: tool_command(bin_dir, name)
        for name in ("rt-say", "rt-inbox", "rt-ack", "rt-projects")
    }
    with tempfile.TemporaryDirectory(prefix="roundtable-terminal-smoke-") as temporary:
        workspace = Path(temporary)
        home = workspace / "home"
        project = workspace / "project"
        home.mkdir()
        project.mkdir()
        write_project(project)

        environment = os.environ.copy()
        adapter_bin = workspace / "optional-adapter-bin"
        adapter_bin.mkdir()
        adapter_sentinel = workspace / "terminal-adapter-invoked"
        fake_cmux = adapter_bin / "cmux"
        fake_cmux.write_text(
            "#!/bin/sh\n"
            'printf "invoked\\n" > "$RT_SMOKE_ADAPTER_SENTINEL"\n'
            "exit 99\n"
        )
        fake_cmux.chmod(0o755)
        isolated_path = (
            f"{adapter_bin}:{Path(sys.executable).absolute().parent}:/usr/bin:/bin"
        )
        state = project / ".roundtable"
        (state / "runtime.json").write_text(
            json.dumps(
                {
                    "schema": "roundtable.runtime.v1",
                    "project": str(project),
                    "workspace_ref": "stale:workspace",
                    "agents": {
                        "codex": {"surface_ref": "stale:codex"},
                        "claude": {"surface_ref": "stale:claude"},
                    },
                }
            )
            + "\n"
        )
        environment.update(
            {
                "HOME": str(home),
                "PATH": isolated_path,
                "PYTHONDONTWRITEBYTECODE": "1",
                "ROUNDTABLE_PROJECT_DIR": "",
                "RT_FALLBACK_PROJECT": "",
                "RT_FROM": "codex",
                "RT_PROJECTS_FILE": str(workspace / "registry" / "projects.yaml"),
                "CMUX_SURFACE_ID": "",
                "CODEX_THREAD_ID": "",
                "RT_SMOKE_ADAPTER_SENTINEL": str(adapter_sentinel),
            }
        )
        run_tool(
            commands,
            "rt-projects",
            "add",
            str(project),
            cwd=project,
            environment=environment,
        )
        resolved = run_tool(
            commands,
            "rt-projects",
            "resolve",
            str(project),
            cwd=project,
            environment=environment,
        )
        mailbox = json.loads(resolved.stdout)
        if mailbox.get("layout") != "local":
            raise SmokeFailure("new smoke project did not resolve local layout")
        sent = run_tool(
            commands,
            "rt-say",
            "claude",
            "directive",
            "offline delivery smoke",
            cwd=project,
            environment=environment,
        )
        match = MESSAGE_ID_RE.search(sent.stdout)
        if match is None:
            raise SmokeFailure(f"could not parse rt-say output: {sent.stdout.strip()}")
        message_id = match.group("id")

        listed = run_tool(
            commands,
            "rt-inbox",
            "claude",
            "--format",
            "json",
            cwd=project,
            environment=environment,
        )
        records = json.loads(listed.stdout)
        if (
            not records
            or {record.get("msg_id") for record in records} != {message_id}
            or not any(
                record.get("delivery_source") == "maildir" for record in records
            )
        ):
            raise SmokeFailure(
                "recipient inbox does not contain the sent maildir message"
            )

        ack_environment = dict(environment)
        ack_environment["RT_FROM"] = ""
        run_tool(
            commands,
            "rt-ack",
            message_id,
            "smoke received",
            cwd=project,
            environment=ack_environment,
        )
        registry = Path(environment["RT_PROJECTS_FILE"])
        try:
            with rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
            ) as locked_mailbox:
                inbox_dir = locked_mailbox.inbox_dir
                ack_files = list(
                    (inbox_dir / "codex" / "new").glob("ack-*.md")
                )
                if (
                    len(ack_files) != 1
                    or f"refs={message_id}"
                    not in ack_files[0].read_text()
                ):
                    raise SmokeFailure(
                        "sender mailbox does not contain the expected quiet ack"
                    )

                new_path = (
                    inbox_dir / "claude" / "new" / f"{message_id}.md"
                )
                current = new_path.parents[1] / "cur"
                archived_path = current / new_path.name
                if new_path.exists() or not archived_path.is_file():
                    raise SmokeFailure(
                        "rt-ack did not archive the exact inbound message"
                    )
        except rtlib.ProjectRegistryError as error:
            raise SmokeFailure(f"mailbox inspection failed: {error}") from error
        drained = run_tool(
            commands,
            "rt-inbox",
            "claude",
            "--format",
            "json",
            cwd=project,
            environment=environment,
        )
        if json.loads(drained.stdout) != []:
            raise SmokeFailure("recipient inbox is not empty after drain")
        if adapter_sentinel.exists():
            raise SmokeFailure("terminal baseline invoked an optional adapter")

        return {
            "status": "passed",
            "profile": "terminal-baseline",
            "transport": "maildir",
            "terminal_emulator": "not-required",
            "optional_adapters_loaded": [],
            "message_id": message_id,
            "ack_files": len(ack_files),
            "recipient_inbox_after_drain": 0,
        }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        print(json.dumps(smoke(args.bin_dir), sort_keys=True))
        return 0
    except (SmokeFailure, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"roundtable terminal smoke failed: {error}", file=sys.stderr)
        return 1
