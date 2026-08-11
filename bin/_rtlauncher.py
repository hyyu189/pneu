"""Shared project selector for Roundtable harness launchers."""

from __future__ import annotations

import importlib.util
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
    inspect_seat,
    release,
    runtime_root,
)


COMMANDS = {
    "claude": ["claude"],
    "codex": ["codex", "--remote", "unix://"],
    "hermes": ["hermes"],
    # OpenClaw is launched through rt-openclaw-wake, not attached to a
    # pre-existing native Gateway service.
    "openclaw": ["openclaw"],
    # Grok is launched through rt-grok-wake, not attached to a shared leader.
    "grok": ["grok"],
}
HARNESS_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "grok": "Grok Build",
}
HARNESS_INSTALL_HINTS = {
    "claude": "Install Claude Code from Anthropic, then run pneu again",
    "codex": "Install Codex CLI from OpenAI, then run pneu again",
    "hermes": "Install Hermes Agent, then run pneu again",
    "openclaw": "Install OpenClaw using its vendor installer, then run pneu again",
    "grok": "Install Grok Build using its vendor installer, then run pneu again",
}
EXECUTABLE_OVERRIDES = {
    "claude": "RT_CLAUDE_BIN",
    "hermes": "RT_HERMES_BIN",
    "openclaw": "RT_OPENCLAW_BIN",
    "grok": "RT_GROK_BIN",
}
CONFIG_HARNESSES = {
    "claude": frozenset({"claude", "claude-code"}),
    "codex": frozenset({"codex"}),
    "hermes": frozenset({"hermes", "hermes-agent"}),
    "openclaw": frozenset({"openclaw", "openclaw-gateway"}),
    "grok": frozenset({"grok", "grok-build"}),
}
CMUX_SHIM_PARTS = frozenset({"cmux-cli-shims"})
CMUX_WRAPPER_NAMES = frozenset({"cmux-claude-wrapper", "cmux-codex-wrapper"})
AGENT_ID_RE = re.compile(r"^[a-z0-9#_-]+$")
HERMES_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
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
CODEX_CLI_OPTIONS_WITH_VALUES = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--remote",
        "--remote-auth-token-env",
        "-i",
        "--image",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
    }
)
CODEX_SHORT_OPTIONS_WITH_ATTACHED_VALUES = (
    "-c",
    "-i",
    "-m",
    "-p",
    "-s",
    "-C",
    "-a",
)


class SelectionError(RuntimeError):
    pass


_ADAPTER_MODULES = {}


def _adapter_module(harness: str):
    """Load the installed adapter module used by an optional harness resolver."""

    relative = {
        "openclaw": Path("integrations/openclaw/roundtable/__init__.py"),
        "grok": Path("integrations/grok/roundtable/__init__.py"),
    }[harness]
    module = _ADAPTER_MODULES.get(harness)
    if module is not None:
        return module
    version_root = Path(__file__).resolve().parent.parent
    candidates = (
        version_root / relative,
        version_root / "share" / "pneu" / relative,
    )
    source = next((candidate for candidate in candidates if candidate.is_file()), None)
    if source is None:
        raise SelectionError(
            f"rt-{harness}: the installed {HARNESS_LABELS[harness]} adapter is missing; "
            "reinstall pneu before launching this seat"
        )
    module_name = f"_pneu_{harness}_resolver"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise SelectionError(
            f"rt-{harness}: cannot load the installed {HARNESS_LABELS[harness]} adapter; "
            "reinstall pneu before launching this seat"
        )
    module = importlib.util.module_from_spec(spec)
    # Dataclasses and other introspection helpers expect an executing module
    # to be registered under its spec name.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _ADAPTER_MODULES[harness] = module
    return module


def _adapter_harness_bin(harness: str) -> Path:
    resolver_name = {
        "openclaw": "resolve_openclaw_bin",
        "grok": "resolve_grok_bin",
    }[harness]
    try:
        return Path(getattr(_adapter_module(harness), resolver_name)()).resolve()
    except SelectionError:
        raise
    except (OSError, RuntimeError) as error:
        raise SelectionError(f"rt-{harness}: {error}") from error


def _candidate_paths(harness: str) -> list[Path]:
    """Return the same visible executable locations used for menu diagnostics."""

    override_name = EXECUTABLE_OVERRIDES.get(harness)
    candidates: list[Path] = []
    if override_name and os.environ.get(override_name):
        candidates.append(Path(os.environ[override_name]).expanduser())
    home = Path.home()
    if harness == "codex":
        code_home = os.environ.get("CODEX_HOME")
        if code_home:
            candidates.append(Path(code_home).expanduser() / "packages/standalone/current/codex")
    candidates.extend(
        (
            home / ".local" / "bin" / COMMANDS[harness][0],
            home / ".npm-global" / "bin" / COMMANDS[harness][0],
        )
    )
    candidates.extend(
        Path(directory).expanduser() / COMMANDS[harness][0]
        for directory in os.environ.get("PATH", "").split(os.pathsep)
        if directory
    )
    return candidates


def harness_unavailable_detail(harness: str, error: str | Exception) -> str:
    """Render a menu-safe explanation with the next action a user can take."""

    binary = COMMANDS[harness][0]
    override = EXECUTABLE_OVERRIDES.get(harness)
    existing = next(
        (candidate for candidate in _candidate_paths(harness) if os.path.lexists(candidate)),
        None,
    )
    if existing is not None and not _executable(existing):
        state = f"the {binary} executable at {existing} is missing or not executable"
    else:
        state = f"missing executable `{binary}` (it is not installed)"
    override_hint = f"; or set {override} to its executable path" if override else ""
    return f"{state}. {HARNESS_INSTALL_HINTS[harness]}{override_hint}."


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

    if harness in {"openclaw", "grok"}:
        return _adapter_harness_bin(harness)

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
        f"rt-{harness}: "
        f"{harness_unavailable_detail(harness, 'executable not found')}"
    )


def _hermes_root() -> Path:
    """Mirror Hermes' POSIX root selection without importing its environment."""

    native_root = (Path.home() / ".hermes").expanduser()
    configured = os.environ.get("HERMES_HOME", "").strip()
    if not configured:
        return native_root
    selected = Path(configured).expanduser()
    try:
        selected.resolve(strict=False).relative_to(native_root.resolve(strict=False))
        return native_root
    except (OSError, RuntimeError, ValueError):
        pass
    if selected.parent.name == "profiles":
        return selected.parent.parent
    return selected


def _hermes_profile(argv: list[str]) -> str | None:
    """Return an explicit Hermes profile selector when one is present."""

    try:
        end = argv.index("--")
    except ValueError:
        option_argv = argv
    else:
        option_argv = argv[:end]
    for index, argument in enumerate(option_argv):
        if argument.startswith("--profile="):
            profile = argument.partition("=")[2] or None
            break
        if argument in {"--profile", "-p"}:
            profile = (
                option_argv[index + 1]
                if index + 1 < len(option_argv)
                else None
            )
            break
    else:
        return None
    if profile is None or not HERMES_PROFILE_RE.fullmatch(profile):
        raise SelectionError(
            f"rt-hermes: invalid Hermes profile {profile!r}; expected "
            "^[a-z0-9][a-z0-9_-]{0,63}$"
        )
    return profile


def hermes_credential_paths(argv: list[str]) -> tuple[Path, ...]:
    """Return the credential stores read by the installed Hermes flow.

    Hermes treats the active ``auth.json`` as the profile source of truth and
    also reads a cross-profile Nous OAuth store.  This preflight deliberately
    checks presence only; Hermes remains responsible for parsing and refresh.
    """

    root = _hermes_root()
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    active_home = Path(configured_home).expanduser() if configured_home else root
    profile = _hermes_profile(argv)
    if profile:
        active_home = root / "profiles" / profile
    shared_override = os.environ.get("HERMES_SHARED_AUTH_DIR", "").strip()
    shared = (
        Path(shared_override).expanduser()
        if shared_override
        else root / "shared"
    )
    candidates = (active_home / "auth.json", shared / "nous_auth.json")
    return tuple(dict.fromkeys(path.resolve(strict=False) for path in candidates))


def preflight_hermes_credentials(argv: list[str]) -> None:
    if os.environ.get("RT_HERMES_SKIP_AUTH_CHECK") == "1":
        return
    candidates = hermes_credential_paths(argv)
    if any(path.is_file() for path in candidates):
        return
    rendered = ", ".join(str(path) for path in candidates)
    raise SelectionError(
        "rt-hermes: Hermes credential files are missing "
        f"(checked {rendered}). Run `hermes` once outside pneu to complete "
        "the browser login, then relaunch the seat. This preflight checks "
        "file presence only; a present-but-stale credential can still fail "
        "inside Hermes itself. Set RT_HERMES_SKIP_AUTH_CHECK=1 only to bypass "
        "this check intentionally."
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
    token = _same_process_lease(root, harness, agent_id)
    try:
        if token is None:
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


def _same_process_lease(
    root: Path | None,
    harness: str,
    agent_id: str,
):
    """Return an active lease already owned by this launcher process.

    A resume-shaped launcher can be re-entered by a harness lifecycle path
    before the original process reaches ``exec``.  Claiming the logical seat a
    second time is not safe: the lease file can advance while the child still
    receives the first claim's environment.  Reusing only a lease whose
    validated owner PID is this process makes that path idempotent while
    preserving the normal fail-closed claim behavior for every other owner.
    """

    if root is None or not agent_id:
        return None
    try:
        inspection = inspect_seat(root, agent_id)
    except RuntimeStateError:
        return None
    token = inspection.token
    if (
        inspection.status not in {"active_healthy", "active_unhealthy"}
        or token is None
        or token.agent_id != agent_id
        or token.harness != harness
        or token.owner_pid != os.getpid()
    ):
        return None
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


def openclaw_adapter_bin() -> Path:
    """Resolve the managed OpenClaw wake bridge beside this launcher."""

    candidate = Path(__file__).resolve().parent / "rt-openclaw-wake"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    raise SelectionError(
        "rt-openclaw: managed rt-openclaw-wake is unavailable; reinstall "
        "Roundtable or use the source-tree launcher"
    )


def grok_adapter_bin() -> Path:
    """Resolve the managed Grok ACP wake bridge beside this launcher."""

    candidate = Path(__file__).resolve().parent / "rt-grok-wake"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    raise SelectionError(
        "rt-grok: managed rt-grok-wake is unavailable; reinstall Roundtable "
        "or use the source-tree launcher"
    )


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


CODEX_SEAT_PRIMER = (
    "[roundtable] Seat activation turn. Do not call tools, inspect files, "
    "or modify the workspace. Reply exactly: ready."
)


def codex_seat_primer_args(argv: list[str]) -> list[str]:
    """Launcher-primed first turn for a bare project-anchored Codex launch.

    Codex dispatches its queued SessionStart hook only from a turn, so a
    seat that never receives one stays unbound.  On the verified standalone
    Codex 0.146 a positional prompt auto-submits: the real first turn
    materializes the rollout and fires the hook before model sampling,
    completing the fenced native-thread binding with no human message.
    Other releases keep the existing live launch gates; per the version
    policy this is a documented tested combination, not a version-range
    support claim.

    The text must remain a no-action instruction: it runs as a genuine
    model turn, and under a full-auto approval configuration an inviting
    prompt would mean unattended workspace changes.  Explicit native
    arguments disable the primer entirely so prompts, flags, and
    subcommands pass through untouched; RT_CODEX_NO_PRIMER=1 is the
    emergency opt-out.  The model's reply is never a binding health
    source — the host runtime record is.
    """

    if codex_primer_skip_reason(argv) is not None:
        return []
    return ["--", CODEX_SEAT_PRIMER]


def codex_primer_skip_reason(argv: list[str]) -> str | None:
    if argv:
        return "native arguments were supplied"
    if os.environ.get("RT_CODEX_NO_PRIMER") == "1":
        return "RT_CODEX_NO_PRIMER=1"
    return None


def print_codex_primer_skip_advisory(argv: list[str]) -> None:
    reason = codex_primer_skip_reason(argv)
    if reason is None:
        return
    print(
        "rt-codex: IMPORTANT: Codex activation primer skipped because "
        f"{reason}. This seat will not arm or bind until its first turn; "
        "interact with it once (or resume it) to arm.",
        file=sys.stderr,
    )


def claude_remote_control_args(
    root: Path | None,
    agent_id: str | None,
    argv: list[str],
) -> list[str]:
    """Name an anchored Claude Remote Control session after its pneu seat."""

    if root is None or not agent_id or os.environ.get("RT_CLAUDE_NO_RC") == "1":
        return []
    try:
        separator = argv.index("--")
    except ValueError:
        option_argv = argv
    else:
        option_argv = argv[:separator]
    if any(
        argument == "--remote-control"
        or argument.startswith("--remote-control=")
        for argument in option_argv
    ):
        return []
    return ["--remote-control", f"{agent_id}@{root.name}"]


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
    inherited = ", ".join(LEASE_CONTEXT_ENV_NAMES)
    print(
        f"rt-{harness}: this shell inherited {inherited} from another Roundtable "
        "seat; clearing them before starting a new seat so mail cannot be sent "
        "as the wrong agent",
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
    def parse(answer):
        answer = answer.lower()
        if answer not in {"", "n", "no", "y", "yes"}:
            raise SelectionError(
                f"rt-codex: expected yes or no for service reload, got {answer!r}"
            )
        return answer in {"y", "yes"}

    return _read_choice(stdin, stderr, "Reload now? [y/N]: ", parse=parse)


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


def _codex_option_end(argv: list[str], index: int) -> int:
    """Return the index after one supported Codex option and its value."""

    argument = argv[index]
    if argument in CODEX_CLI_OPTIONS_WITH_VALUES:
        if index + 1 >= len(argv):
            raise SelectionError(
                f"rt-codex: option {argument} is missing its value"
            )
        return index + 2
    if argument.startswith("--") and "=" in argument:
        return index + 1
    if any(
        argument.startswith(prefix) and argument != prefix
        for prefix in CODEX_SHORT_OPTIONS_WITH_ATTACHED_VALUES
    ):
        return index + 1
    return index + 1


def codex_resume_thread_id(argv: list[str]) -> str | None:
    """Return the explicit thread ID from any supported resume CLI shape."""

    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            return None
        if argument.startswith("-"):
            index = _codex_option_end(argv, index)
            continue
        if argument != "resume":
            return None
        index += 1
        break
    else:
        return None

    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            index += 1
            break
        if argument.startswith("-"):
            index = _codex_option_end(argv, index)
            continue
        return argument
    if index < len(argv) and argv[index]:
        return argv[index]
    raise SelectionError(
        "rt-codex: resumed Roundtable seats require an explicit thread ID "
        "as `rt-codex resume <thread-id>` so its recorded project can be "
        "validated before the seat is claimed; use native `codex resume` "
        "for the picker or --last flow"
    )


def preflight_codex_resume(project: Path, argv: list[str]) -> None:
    """Read and validate an explicit resume target before publishing a lease."""

    thread_id = codex_resume_thread_id(argv)
    if thread_id is None:
        return
    try:
        from _rtcodex import (
            DEFAULT_SOCKET,
            AppServerClient,
            CodexRuntimeError,
            require_thread_project_cwd,
        )

        client = AppServerClient(DEFAULT_SOCKET)
        try:
            result = client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
        finally:
            client.close()
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise CodexRuntimeError("thread/read returned an invalid thread")
        thread = result["thread"]
        if thread.get("id") != thread_id:
            raise CodexRuntimeError(
                f"thread/read returned {thread.get('id')!r}, expected {thread_id!r}"
            )
        require_thread_project_cwd(
            project,
            thread,
            expected_thread_id=thread_id,
        )
    except (CodexRuntimeError, OSError, TimeoutError) as error:
        raise SelectionError(f"rt-codex: resume refused: {error}") from error


def project_at_or_above(start: Path) -> Path | None:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if is_project_root(candidate):
            return candidate
    return None


PROMPT_ATTEMPTS = 3
ONBOARDING_SUBPROCESS_ENV = "ROUNDTABLE_ONBOARDING_SUBPROCESS"


def _read_choice(stdin, stderr, prompt: str, *, parse=None):
    """Read one menu value, giving invalid interactive input bounded retries."""

    last_error = None
    for attempt in range(PROMPT_ATTEMPTS):
        print(prompt, end="", file=stderr, flush=True)
        value = stdin.readline()
        if value == "":
            if last_error is not None:
                raise last_error
            raise SelectionError(
                "input closed while selecting a Roundtable project"
            )
        value = value.strip()
        if parse is None:
            return value
        try:
            return parse(value)
        except SelectionError as error:
            last_error = error
            if attempt + 1 == PROMPT_ATTEMPTS:
                raise
            print(f"{error}; please try again.", file=stderr)
    raise last_error


def run_onboarding_init(init_runner, command, **kwargs):
    """Mark a launcher-owned init subprocess so it can omit its next steps."""

    previous = os.environ.get(ONBOARDING_SUBPROCESS_ENV)
    os.environ[ONBOARDING_SUBPROCESS_ENV] = "1"
    try:
        return init_runner(command, **kwargs)
    finally:
        if previous is None:
            os.environ.pop(ONBOARDING_SUBPROCESS_ENV, None)
        else:
            os.environ[ONBOARDING_SUBPROCESS_ENV] = previous


def _choose_git(stdin, stderr, harness: str) -> bool:
    def parse(answer):
        answer = answer.lower()
        if answer not in {"", "n", "no", "y", "yes"}:
            raise SelectionError(
                f"rt-{harness}: expected yes or no for Git, got {answer!r}"
            )
        return answer in {"y", "yes"}

    return _read_choice(
        stdin, stderr, "Initialize Git too? [y/N]: ", parse=parse
    )


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


def _project_ready_recovery(error, harness: str, root: Path) -> SelectionError:
    return SelectionError(
        f"{error}; project already created at {root}; run roundtable again "
        "and choose it from the list"
    )


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
    unanchored_index = (
        create_index + 1
        if harness not in {"codex", "openclaw", "grok"}
        else None
    )
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

    def parse_selection(raw):
        try:
            selected = int(raw)
        except ValueError as error:
            raise SelectionError(
                f"rt-{harness}: invalid selection: {raw!r}"
            ) from error
        if not 1 <= selected <= create_index + int(unanchored_index is not None):
            raise SelectionError(
                f"rt-{harness}: selection out of range: {selected}"
            )
        return selected

    selected = _read_choice(
        stdin, stderr, "Select: ", parse=parse_selection
    )
    if 1 <= selected <= len(roots):
        return _preflight_launch_project(roots[selected - 1], harness)
    if setup_here_index is not None and selected == setup_here_index:
        init = Path(__file__).resolve().parent / "roundtable-init"
        command = [str(init), "--here"]
        if _choose_git(stdin, stderr, harness):
            command.append("--git")
        result = run_onboarding_init(
            init_runner, command, cwd=cwd, check=False
        )
        if result.returncode != 0:
            if is_project_root(cwd):
                raise SelectionError(
                    f"rt-{harness}: project already created at {cwd}; run "
                    "roundtable again and choose it from the list"
                )
            raise SelectionError(
                f"rt-{harness}: roundtable-init failed with exit {result.returncode}"
            )
        if not is_project_root(cwd):
            raise SelectionError(
                f"rt-{harness}: roundtable-init did not configure {cwd}"
            )
        try:
            return _preflight_launch_project(cwd, harness)
        except SelectionError as error:
            if is_project_root(cwd):
                raise _project_ready_recovery(error, harness, cwd) from error
            raise
    if selected == create_index:
        def parse_name(name):
            if not name:
                raise SelectionError(
                    f"rt-{harness}: project name cannot be empty"
                )
            return name

        name = _read_choice(
            stdin, stderr, "New project name: ", parse=parse_name
        )
        init = Path(__file__).resolve().parent / "roundtable-init"
        command = [str(init), name, "--parent", str(cwd)]
        if _choose_git(stdin, stderr, harness):
            command.append("--git")
        root = (cwd / name).resolve()
        result = run_onboarding_init(init_runner, command, check=False)
        if result.returncode != 0:
            if is_project_root(root):
                raise SelectionError(
                    f"rt-{harness}: project already created at {root}; run "
                    "roundtable again and choose it from the list"
                )
            raise SelectionError(
                f"rt-{harness}: roundtable-init failed with exit {result.returncode}"
            )
        if not is_project_root(root):
            raise SelectionError(
                f"rt-{harness}: roundtable-init did not create {root}"
            )
        try:
            return _preflight_launch_project(root, harness)
        except SelectionError as error:
            if is_project_root(root):
                raise _project_ready_recovery(error, harness, root) from error
            raise
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
        if harness == "openclaw":
            raise SelectionError(
                "rt-openclaw: Roundtable OpenClaw requires a Roundtable project "
                "anchor so its Gateway lease and durable mailbox are safe; "
                "choose or initialize a project, or run native `openclaw` directly"
            )
        if harness == "grok":
            raise SelectionError(
                "rt-grok: Roundtable Grok requires a project anchor so its ACP "
                "lease and durable mailbox are safe; choose or initialize a "
                "project, or run native `grok` directly"
            )
    agent_id = set_launch_identity(root, harness)
    if root is not None or harness in {"codex", "openclaw", "grok"}:
        normalize_runtime_environment()
    executable = harness_bin(harness)
    if harness == "hermes" and root is not None:
        # Authentication recovery requires a browser login outside the Hermes
        # TUI, so refuse before a Roundtable seat lease can be stranded.
        preflight_hermes_credentials(argv)
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
            preflight_codex_resume(root, argv)
            token = claim_launch_seat(root, harness, agent_id)
            try:
                arm_codex_launch_intent(token)
            except (RuntimeStateError, OSError) as error:
                try:
                    released = release(token)
                except (RuntimeStateError, OSError) as release_error:
                    clear_unanchored_lease_context()
                    raise SelectionError(
                        "rt-codex: could not arm native-thread binding "
                        f"({error}) and could not release the claimed seat: "
                        f"{release_error}"
                    ) from error
                clear_unanchored_lease_context()
                if not released:
                    raise SelectionError(
                        "rt-codex: could not arm native-thread binding "
                        f"({error}) and the claimed seat was no longer "
                        "releasable"
                    ) from error
                raise SelectionError(
                    f"rt-codex: could not arm native-thread binding: {error}"
                ) from error
            return token

        preflight_codex_services(ready_action=claim_and_arm_codex)
    else:
        claim_launch_seat(root, harness, agent_id)
    if harness == "openclaw":
        # Transfer the already-claimed lease to the managed adapter in this
        # same PID. The native OpenClaw CLI is never allowed to discover or
        # attach to the user's personal Gateway.
        os.environ["RT_OPENCLAW_BIN"] = str(executable)
        adapter = openclaw_adapter_bin()
        command = [str(adapter), "--gateway-bin", str(executable), *argv]
        os.execv(command[0], command)
        return 127
    if harness == "grok":
        # Transfer the already-claimed lease to the managed ACP adapter in
        # this same PID. The native Grok CLI is never allowed to discover or
        # attach to the user's personal state or a shared leader.
        os.environ["RT_GROK_BIN"] = str(executable)
        adapter = grok_adapter_bin()
        command = [str(adapter), "--grok-bin", str(executable), *argv]
        os.execv(command[0], command)
        return 127
    command = [*COMMANDS[harness]]
    if harness == "claude" and root is not None and not argv:
        # A bare Claude launch may open the user's FleetView/Remote Control
        # surface instead of creating a chat.  That surface does not run the
        # claimed seat's SessionStart hook, so it cannot own the inbox
        # tripwire.  Roundtable's bare-seat contract is a fresh addressable
        # chat. Explicit native launch modes remain intact; the managed Remote
        # Control identity is composed below unless the caller opts out.
        command.extend(["--session-id", str(uuid.uuid4())])
    if harness == "hermes" and not argv:
        command.append("--tui")
    if harness == "codex" and root is not None:
        print_codex_primer_skip_advisory(argv)
        command.extend(append_codex_seat_overrides(codex_argv))
        command.extend(codex_seat_primer_args(argv))
    elif harness == "claude":
        command.extend(argv)
        command.extend(claude_remote_control_args(root, agent_id, argv))
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
    except KeyboardInterrupt:
        print(
            f"rt-{harness}: cancelled by user (Ctrl-C); "
            "no agent session was launched.",
            file=sys.stderr,
        )
        return 130
