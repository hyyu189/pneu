"""Shared project selector for Roundtable harness launchers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from _rtlib import (
    ProjectRegistryError,
    active_project_entries,
    emit_registry_warnings,
    is_project_root,
    load_agents_doc,
    load_project_registry,
    resolve_project_mailbox_checked,
)
from _rtruntime import (
    RuntimeStateError,
    SeatAmbiguous,
    SeatOccupied,
    arm_codex_launch_intent,
    claim,
    runtime_root,
)


COMMANDS = {
    "claude": ["claude"],
    "codex": ["codex", "--remote", "unix://"],
    "hermes": ["hermes"],
}
EXECUTABLE_OVERRIDES = {
    "claude": "RT_CLAUDE_BIN",
    "hermes": "RT_HERMES_BIN",
}
CONFIG_HARNESSES = {
    "claude": frozenset({"claude", "claude-code"}),
    "codex": frozenset({"codex"}),
    "hermes": frozenset({"hermes", "hermes-agent"}),
}
CMUX_SHIM_PARTS = frozenset({"cmux-cli-shims"})
CMUX_WRAPPER_NAMES = frozenset({"cmux-claude-wrapper", "cmux-codex-wrapper"})
AGENT_ID_RE = re.compile(r"^[a-z0-9#_-]+$")
LEASE_ENV_NAMES = (
    "RT_PROJECT_ROOT",
    "RT_FROM",
    "RT_SESSION_ID",
    "RT_LEASE_REVISION",
)
LEASE_CONTEXT_ENV_NAMES = tuple(
    name for name in LEASE_ENV_NAMES if name != "RT_FROM"
)
CODEX_TOOL_ENV_NAMES = (
    *LEASE_ENV_NAMES,
    "RT_RUNTIME_DIR",
    "RT_CODEX_RUNTIME_DIR",
)


class SelectionError(RuntimeError):
    pass


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _is_cmux_shim(path: Path) -> bool:
    candidates = [path.absolute()]
    try:
        candidates.append(path.resolve())
    except OSError:
        pass
    for candidate in candidates:
        parts = {part.lower() for part in candidate.parts}
        if parts & CMUX_SHIM_PARTS:
            return True
        if candidate.name.lower() in CMUX_WRAPPER_NAMES:
            return True
    return False


def harness_bin(harness: str) -> Path:
    """Resolve a real harness executable without depending on cmux PATH shims."""
    if harness == "codex":
        try:
            from _rtcodex import CodexRuntimeError, codex_bin

            return codex_bin()
        except CodexRuntimeError as error:
            raise SelectionError(f"rt-codex: {error}") from error

    override_name = EXECUTABLE_OVERRIDES[harness]
    override = os.environ.get(override_name)
    if override:
        selected = Path(override).expanduser().absolute()
        if _executable(selected) and not _is_cmux_shim(selected):
            return selected
        if _executable(selected):
            raise SelectionError(
                f"rt-{harness}: {override_name} points to a cmux wrapper: {selected}"
            )
        raise SelectionError(
            f"rt-{harness}: {override_name} is not executable: {selected}"
        )

    executable_name = COMMANDS[harness][0]
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / executable_name,
        home / ".npm-global" / "bin" / executable_name,
    ]
    if harness == "hermes":
        candidates.append(
            home / ".hermes" / "hermes-agent" / "venv" / "bin" / executable_name
        )
    candidates.extend(
        Path(directory).expanduser() / executable_name
        for directory in os.environ.get("PATH", "").split(os.pathsep)
        if directory
    )
    seen = set()
    for candidate in candidates:
        selected = candidate.absolute()
        key = str(selected)
        if key in seen:
            continue
        seen.add(key)
        if _executable(selected) and not _is_cmux_shim(selected):
            return selected
    raise SelectionError(
        f"rt-{harness}: could not find a non-cmux {executable_name} executable; "
        f"set {override_name}"
    )


def configured_sender_ids(root: Path, harness: str) -> list[str]:
    document = load_agents_doc(root, f"rt-{harness}")
    if not isinstance(document, dict):
        raise SelectionError(
            f"rt-{harness}: {root / '.roundtable' / 'agents.yaml'} is not a mapping"
        )
    agents = document.get("agents") or {}
    if not isinstance(agents, dict):
        raise SelectionError(
            f"rt-{harness}: agents in {root / '.roundtable' / 'agents.yaml'} "
            "is not a mapping"
        )
    ids = []
    for agent_name, config in agents.items():
        if not isinstance(config, dict):
            continue
        if config.get("harness") not in CONFIG_HARNESSES[harness]:
            continue
        instances = config.get("instances")
        if not isinstance(instances, list) or not instances:
            instances = [{"id": agent_name}]
        for instance in instances:
            instance_id = (
                instance.get("id") if isinstance(instance, dict) else instance
            )
            if not isinstance(instance_id, str) or not AGENT_ID_RE.fullmatch(
                instance_id
            ):
                raise SelectionError(
                    f"rt-{harness}: configured instance id {instance_id!r} "
                    "must match ^[a-z0-9#_-]+$"
                )
            if instance_id not in ids:
                ids.append(instance_id)
    return ids


def set_launch_identity(root: Path | None, harness: str) -> str | None:
    existing = os.environ.get("RT_FROM")
    if root is None:
        return existing or None
    candidates = configured_sender_ids(root, harness)
    if existing:
        if existing not in candidates:
            rendered = ", ".join(candidates) or "none"
            raise SelectionError(
                f"rt-{harness}: RT_FROM={existing!r} is not configured for "
                f"{harness} in {root} (configured: {rendered})"
            )
        return existing
    if len(candidates) == 1:
        os.environ["RT_FROM"] = candidates[0]
        return candidates[0]
    if len(candidates) > 1:
        rendered = ", ".join(candidates)
        raise SelectionError(
            f"rt-{harness}: multiple configured instances ({rendered}); "
            "set RT_FROM to the instance to launch"
        )
    return None


def claim_launch_seat(root: Path | None, harness: str, agent_id: str | None):
    if root is None:
        return None
    if not agent_id:
        raise SelectionError(
            f"rt-{harness}: no configured {harness} instance in {root}; "
            "add one to .roundtable/agents.yaml or set RT_FROM"
        )
    try:
        token = claim(root, agent_id, harness)
    except SeatOccupied as error:
        status = error.inspection.status
        condition = "unhealthy" if status == "active_unhealthy" else "active"
        owner = getattr(getattr(error.inspection, "token", None), "agent_id", None)
        occupied = owner if isinstance(owner, str) and owner else agent_id
        request_detail = (
            f"; requested seat {agent_id!r}"
            if occupied != agent_id
            else ""
        )
        raise SelectionError(
            f"rt-{harness}: seat {occupied!r} is {condition} in {root}"
            f"{request_detail}; "
            f"{error.inspection.detail}"
        ) from error
    except SeatAmbiguous as error:
        raise SelectionError(
            f"rt-{harness}: seat {agent_id!r} has ambiguous runtime state in "
            f"{root}; {error.inspection.detail}"
        ) from error
    except RuntimeStateError as error:
        raise SelectionError(
            f"rt-{harness}: could not claim seat {agent_id!r} in {root}: {error}"
        ) from error

    environment = {
        "RT_PROJECT_ROOT": str(token.project_root),
        "RT_FROM": token.agent_id,
        "RT_SESSION_ID": token.session_id,
        "RT_LEASE_REVISION": str(token.revision),
    }
    os.environ.update(environment)
    return token


def normalize_runtime_environment() -> Path:
    """Expose one absolute runtime root to launchers and remote tool processes."""
    try:
        selected = runtime_root().expanduser().absolute()
    except RuntimeStateError as error:
        raise SelectionError(f"invalid Roundtable runtime root: {error}") from error
    rendered = str(selected)
    os.environ["RT_RUNTIME_DIR"] = rendered
    os.environ["RT_CODEX_RUNTIME_DIR"] = rendered
    return selected


def codex_seat_overrides() -> list[str]:
    arguments = []
    for name in CODEX_TOOL_ENV_NAMES:
        value = os.environ.get(name)
        if value is None:
            raise SelectionError(
                f"rt-codex: missing required tool environment variable {name}"
            )
        arguments.extend(
            [
                "-c",
                f"shell_environment_policy.set.{name}={json.dumps(value)}",
            ]
        )
    return arguments


def append_codex_seat_overrides(argv: list[str]) -> list[str]:
    overrides = codex_seat_overrides()
    try:
        separator = argv.index("--")
    except ValueError:
        return [*argv, *overrides]
    return [*argv[:separator], *overrides, *argv[separator:]]


def anchor_codex_project(root: Path, argv: list[str]) -> list[str]:
    """Make the selected project the explicit native Codex working root.

    Roundtable's seat identity is keyed by the canonical project path, so a
    caller-provided ``-C``/``--cd`` cannot be allowed to make the native
    thread disagree with its lease.  Arguments after ``--`` are prompt
    literals rather than CLI options and must remain untouched.
    """

    try:
        separator = argv.index("--")
    except ValueError:
        option_argv = argv
    else:
        option_argv = argv[:separator]
    for argument in option_argv:
        if (
            argument in {"-C", "--cd"}
            or argument.startswith("-C")
            or argument.startswith("--cd=")
        ):
            raise SelectionError(
                "rt-codex: -C/--cd is managed by Roundtable's selected "
                "project; choose the project through `roundtable`, or use "
                "native `codex` for a different working root"
            )
    return ["-C", str(root.expanduser().resolve()), *argv]


def clear_unanchored_lease_context() -> None:
    for name in LEASE_CONTEXT_ENV_NAMES:
        os.environ.pop(name, None)


def scrub_inherited_seat_environment(harness: str) -> None:
    """Discard a seat environment inherited from another Roundtable session.

    Any lease-context variable proves this shell already sits inside a
    Roundtable-launched seat, so every inherited seat value is that caller's
    identity rather than launch intent — including ``RT_FROM``, which must not
    leak into the new seat's identity selection or claim. ``RT_FROM`` set on
    its own remains the documented explicit multi-instance selection.
    """
    if not any(os.environ.get(name) for name in LEASE_CONTEXT_ENV_NAMES):
        return
    for name in LEASE_ENV_NAMES:
        os.environ.pop(name, None)
    print(
        f"rt-{harness}: advisory: ignoring Roundtable seat environment "
        "inherited from the launching session",
        file=sys.stderr,
    )


def _confirm_codex_reload(status, *, stdin=None, stderr=None) -> bool:
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    if not stdin.isatty():
        return False
    print(
        "Roundtable needs to reload its Codex app-server before launch.",
        file=stderr,
    )
    print(f"  {status.detail}", file=stderr)
    print(
        "  This can disconnect Codex sessions attached to that service.",
        file=stderr,
    )
    answer = _read_choice(stdin, stderr, "Reload now? [y/N]: ").lower()
    if answer not in {"", "n", "no", "y", "yes"}:
        raise SelectionError(
            f"rt-codex: expected yes or no for service reload, got {answer!r}"
        )
    return answer in {"y", "yes"}


def preflight_codex_services(*, ready_action=None) -> None:
    """Prepare services and publish the Codex seat under the host repair lock."""

    try:
        from _rtcodex import (
            CodexRuntimeError,
            codex_launch_preflight,
        )

        codex_launch_preflight(
            confirm_reload=_confirm_codex_reload,
            ready_action=ready_action,
        )
    except CodexRuntimeError as error:
        raise SelectionError(f"rt-codex: {error}") from error


def project_at_or_above(start: Path) -> Path | None:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if is_project_root(candidate):
            return candidate
    return None


def _read_choice(stdin, stderr, prompt: str) -> str:
    print(prompt, end="", file=stderr, flush=True)
    value = stdin.readline()
    if value == "":
        raise SelectionError("input closed while selecting a Roundtable project")
    return value.strip()


def _choose_git(stdin, stderr, harness: str) -> bool:
    answer = _read_choice(
        stdin, stderr, "Initialize Git too? [y/N]: "
    ).lower()
    if answer not in {"", "n", "no", "y", "yes"}:
        raise SelectionError(
            f"rt-{harness}: expected yes or no for Git, got {answer!r}"
        )
    return answer in {"y", "yes"}


def _preflight_launch_project(root: Path, harness: str) -> Path:
    """Validate durable identity and layout before any seat can be claimed."""

    try:
        mailbox = resolve_project_mailbox_checked(root)
    except ProjectRegistryError as error:
        raise SelectionError(
            f"rt-{harness}: project registration preflight failed for "
            f"{root}: {error}"
        ) from error
    return mailbox.project_root


def choose_launch_cwd(
    harness: str,
    *,
    cwd: Path | None = None,
    stdin=None,
    stderr=None,
    init_runner=subprocess.run,
) -> Path | None:
    """Return the canonical project root to launch from, or None if unanchored."""
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    anchored = project_at_or_above(cwd)
    if anchored is not None:
        return _preflight_launch_project(anchored, harness)
    if not stdin.isatty():
        raise SelectionError(
            f"rt-{harness}: not in a Roundtable project and stdin is not a TTY"
        )

    entries, warnings = load_project_registry()
    emit_registry_warnings(warnings, stream=stderr, tool=f"rt-{harness}")
    roots = [
        entry["root"]
        for entry in active_project_entries(entries, available_only=True)
    ]
    print("Roundtable projects:", file=stderr)
    for index, root in enumerate(roots, 1):
        print(f"  {index}) {root}", file=stderr)
    can_setup_here = cwd not in {Path.home().resolve(), Path(cwd.anchor)}
    setup_here_index = len(roots) + 1 if can_setup_here else None
    create_index = len(roots) + 1 + int(can_setup_here)
    unanchored_index = create_index + 1 if harness != "codex" else None
    if setup_here_index is not None:
        print(
            f"  {setup_here_index}) Set up this folder safely: {cwd}",
            file=stderr,
        )
    print(f"  {create_index}) Create a new project in {cwd}", file=stderr)
    if unanchored_index is not None:
        print(f"  {unanchored_index}) Start without a project anchor", file=stderr)
    else:
        print(
            "  Roundtable Codex requires a project anchor; use native `codex` "
            "for an unanchored session.",
            file=stderr,
        )

    raw = _read_choice(stdin, stderr, "Select: ")
    try:
        selected = int(raw)
    except ValueError as error:
        raise SelectionError(f"rt-{harness}: invalid selection: {raw!r}") from error
    if 1 <= selected <= len(roots):
        return _preflight_launch_project(roots[selected - 1], harness)
    if setup_here_index is not None and selected == setup_here_index:
        init = Path(__file__).resolve().parent / "roundtable-init"
        command = [str(init), "--here"]
        if _choose_git(stdin, stderr, harness):
            command.append("--git")
        result = init_runner(command, cwd=cwd, check=False)
        if result.returncode != 0:
            raise SelectionError(
                f"rt-{harness}: roundtable-init failed with exit {result.returncode}"
            )
        if not is_project_root(cwd):
            raise SelectionError(
                f"rt-{harness}: roundtable-init did not configure {cwd}"
            )
        return _preflight_launch_project(cwd, harness)
    if selected == create_index:
        name = _read_choice(stdin, stderr, "New project name: ")
        if not name:
            raise SelectionError(f"rt-{harness}: project name cannot be empty")
        init = Path(__file__).resolve().parent / "roundtable-init"
        command = [str(init), name, "--parent", str(cwd)]
        if _choose_git(stdin, stderr, harness):
            command.append("--git")
        result = init_runner(command, check=False)
        if result.returncode != 0:
            raise SelectionError(
                f"rt-{harness}: roundtable-init failed with exit {result.returncode}"
            )
        root = (cwd / name).resolve()
        if not is_project_root(root):
            raise SelectionError(
                f"rt-{harness}: roundtable-init did not create {root}"
            )
        return _preflight_launch_project(root, harness)
    if unanchored_index is not None and selected == unanchored_index:
        print(
            f"rt-{harness}: advisory: starting without a Roundtable project anchor from {cwd}",
            file=stderr,
        )
        return None
    raise SelectionError(f"rt-{harness}: selection out of range: {selected}")


def launch(harness: str, argv: list[str]) -> int:
    if harness not in COMMANDS:
        raise SelectionError(f"unknown Roundtable harness: {harness}")
    scrub_inherited_seat_environment(harness)
    selected = choose_launch_cwd(harness)
    if selected is not None:
        os.chdir(selected)
    root = selected or project_at_or_above(Path.cwd())
    if root is None:
        clear_unanchored_lease_context()
        if harness == "codex":
            raise SelectionError(
                "rt-codex: Roundtable Codex requires a Roundtable project anchor "
                "so its host service lease and native-thread binding are safe; "
                "choose or initialize a project, or run native `codex` directly"
            )
    agent_id = set_launch_identity(root, harness)
    if root is not None or harness == "codex":
        normalize_runtime_environment()
    executable = harness_bin(harness)
    codex_argv = (
        anchor_codex_project(root, argv)
        if harness == "codex" and root is not None
        else argv
    )
    if harness == "codex":
        # Service repair must happen before publishing a seat lease.  A
        # declined/deferred reload therefore cannot strand an occupied seat.
        # The final READY recheck and claim share the host repair lock, so a
        # concurrent reload cannot slip between them.
        def claim_and_arm_codex():
            token = claim_launch_seat(root, harness, agent_id)
            try:
                arm_codex_launch_intent(token)
            except (RuntimeStateError, OSError) as error:
                raise SelectionError(
                    f"rt-codex: could not arm native-thread binding: {error}"
                ) from error
            return token

        preflight_codex_services(ready_action=claim_and_arm_codex)
    else:
        claim_launch_seat(root, harness, agent_id)
    command = [*COMMANDS[harness]]
    if harness == "claude" and root is not None and not argv:
        # A bare Claude launch may open the user's FleetView/Remote Control
        # surface instead of creating a chat.  That surface does not run the
        # claimed seat's SessionStart hook, so it cannot own the inbox
        # tripwire.  Roundtable's bare-seat contract is a fresh addressable
        # chat; explicit native arguments remain untouched.
        command.extend(["--session-id", str(uuid.uuid4())])
    if harness == "hermes" and not argv:
        command.append("--tui")
    if harness == "codex" and root is not None:
        command.extend(append_codex_seat_overrides(codex_argv))
    else:
        command.extend(argv)
    command[0] = str(executable)
    os.execv(command[0], command)
    return 127


def main(harness: str) -> int:
    try:
        return launch(harness, sys.argv[1:])
    except SelectionError as error:
        print(error, file=sys.stderr)
        return 2
