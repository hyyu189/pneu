"""CLI/hook identity contracts in an isolated runtime, not native smoke proof."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtnative
import _rtruntime
from _rtlib import register_project


loader = importlib.machinery.SourceFileLoader("native_inbox_cli", str(BIN / "rt-native"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = cli
loader.exec_module(cli)


@pytest.fixture
def project(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    for name in (
        "RT_PROJECT_ROOT", "ROUNDTABLE_PROJECT_DIR", "RT_FROM", "RT_SESSION_ID",
        "RT_LEASE_REVISION", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_TOOL_USE_ID", "RT_FALLBACK_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "project"
    state = root / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        "agents:\n"
        "  codex:\n    harness: codex\n"
        "  claude:\n    harness: claude\n"
    )
    register_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "native_owner", lambda harness: os.getpid())
    # This fixture supplies a trusted native hook transport. Actual kernel
    # transport attestation is covered separately from synthetic payloads.
    monkeypatch.setattr(cli, "require_hook_transport", lambda harness, owner: None)
    return root.resolve()


def payload(project, *, native_id="root-session", event="SessionStart", **fields):
    value = {
        "hook_event_name": event, "session_id": native_id, "cwd": str(project),
        "source": "startup",
    }
    if event == "PreToolUse":
        value.update(tool_name="Bash", tool_use_id="query-call", tool_input={"command": "rt-native inbox"})
    value.update(fields)
    return value


def bind_claude(project):
    cli.run_hook("claude", project, payload(project))


def call_main(monkeypatch, capsys, arguments, stdin=""):
    monkeypatch.setattr(sys, "argv", ["rt-native", *arguments])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main()
    captured = capsys.readouterr()
    return code, captured


def test_codex_current_call_requires_both_native_identifiers():
    assert cli.codex_identity({"CODEX_THREAD_ID": "root", "CODEX_SESSION_ID": "root"}) == "root"
    for environment in (
        {}, {"CODEX_THREAD_ID": "root"}, {"CODEX_SESSION_ID": "root"},
        {"CODEX_THREAD_ID": "child", "CODEX_SESSION_ID": "root"},
    ):
        with pytest.raises(_rtnative.NativeQueryError):
            cli.codex_identity(environment)


def test_claude_hook_identity_is_not_replaced_by_inherited_codex_environment(project, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "launcher-parent")
    monkeypatch.setenv("CODEX_SESSION_ID", "launcher-parent")

    cli.run_hook("claude", project, payload(project))

    record = _rtruntime.read_seat_capability(project, "claude")
    assert record["nativeQuery"]["nativeSessionId"] == "root-session"


@pytest.mark.parametrize("field,value", [
    ("agent_id", "subagent"), ("parent_thread_id", "parent"),
    ("parentThreadId", "parent"), ("forked_from_id", "parent"),
    ("forkedFromId", "parent"), ("source_thread_id", "parent"),
    ("sourceThreadId", "parent"), ("ephemeral", True),
])
def test_child_session_start_cannot_claim_root_seat(project, field, value):
    with pytest.raises(_rtnative.NativeQueryError):
        cli.run_hook("claude", project, payload(project, **{field: value}))
    assert _rtruntime.inspect_seat(project, "claude").status == "vacant"


def test_same_cwd_other_claude_session_cannot_get_root_ticket(project):
    bind_claude(project)
    with pytest.raises(_rtnative.NativeQueryError, match="root"):
        cli.run_hook("claude", project, payload(project, native_id="other-root", event="PreToolUse"))
    assert not cli.ticket_directory().exists()


def test_claude_subagent_cannot_get_root_ticket(project):
    bind_claude(project)
    with pytest.raises(_rtnative.NativeQueryError, match="subagent"):
        cli.run_hook("claude", project, payload(project, event="PreToolUse", agent_id="child"))
    assert not cli.ticket_directory().exists()


@pytest.mark.parametrize("command", [
    "rt-native inbox; true", "rt-native inbox && true", "rt-native inbox\n",
    "rt-native $(echo inbox)", "rt-native inbox --all", "rt-native inbox claude",
    "rt-native inbox --archive-quiet-acks", "rt-native inbox # comment", "echo rt-native inbox",
])
def test_hook_does_not_authorize_shell_syntax_or_extra_flags(command):
    assert cli.query_command(command) is None


def test_pretooluse_changes_only_exact_query_and_preserves_native_permission(project):
    bind_claude(project)
    request = payload(project, event="PreToolUse")
    request["tool_input"].update(timeout=1234, description="read my inbox")

    result = cli.run_hook("claude", project, request)

    output = result["hookSpecificOutput"]
    assert "permissionDecision" not in output
    updated = output["updatedInput"]
    assert updated["timeout"] == 1234
    assert updated["description"] == "read my inbox"
    command = shlex.split(updated["command"])
    ticket = command[-1]
    assert command[-2] == "--ticket"
    record = json.loads((cli.ticket_directory() / (ticket + ".json")).read_text())
    assert record["toolUseId"] == request["tool_use_id"]
    first = cli.consume_ticket(project, ticket, "inbox", os.getpid())
    assert first["binding"]["nativeSessionId"] == "root-session"
    assert first["messages"] == []
    with pytest.raises(_rtnative.NativeQueryError, match="already used"):
        cli.consume_ticket(project, ticket, "inbox", os.getpid())


@pytest.mark.parametrize("mismatch", ["operation", "project", "owner", "expired"])
def test_failed_ticket_invocation_is_consumed(project, tmp_path, monkeypatch, mismatch):
    bind_claude(project)
    ticket = cli.issue_ticket(project, "root-session", os.getpid(), "inbox", tool_use_id="query-call")
    root, operation, owner = project, "inbox", os.getpid()
    if mismatch == "operation":
        operation = "status"
    elif mismatch == "project":
        root = tmp_path / "other"
    elif mismatch == "owner":
        owner += 1
    else:
        monkeypatch.setattr(cli.time, "time", lambda: float("inf"))

    with pytest.raises(_rtnative.NativeQueryError):
        cli.consume_ticket(root, ticket, operation, owner)
    assert not list(cli.ticket_directory().iterdir())


def test_ticket_cannot_survive_lease_replacement(project):
    bind_claude(project)
    ticket = cli.issue_ticket(project, "root-session", os.getpid(), "status", tool_use_id="query-call")
    old = _rtruntime.inspect_seat(project, "claude").token
    _rtruntime.release(old)
    _rtnative.bind_native(project, "claude", "root-session", os.getpid())

    with pytest.raises(_rtnative.NativeQueryError, match="RT_SESSION_ID"):
        cli.consume_ticket(project, ticket, "status", os.getpid())


@pytest.mark.parametrize("field", ["RT_SESSION_ID", "RT_LEASE_REVISION", "CLAUDE_CODE_SESSION_ID"])
def test_ticket_does_not_override_conflicting_native_or_lease_identity(project, monkeypatch, field):
    bind_claude(project)
    ticket = cli.issue_ticket(project, "root-session", os.getpid(), "status", tool_use_id="query-call")
    monkeypatch.setenv(field, "other-session")

    with pytest.raises(_rtnative.NativeQueryError, match="disagree|conflict"):
        cli.consume_ticket(project, ticket, "status", os.getpid())
    assert os.environ[field] == "other-session"


def test_direct_claude_inherited_fence_cannot_bypass_hook_ticket(project, monkeypatch, capsys):
    bind_claude(project)
    token = _rtruntime.inspect_seat(project, "claude").token
    for name, value in {
        "RT_FROM": "claude", "RT_PROJECT_ROOT": str(project),
        "RT_SESSION_ID": token.session_id, "RT_LEASE_REVISION": token.revision,
        "CLAUDE_CODE_SESSION_ID": "root-session",
    }.items():
        monkeypatch.setenv(name, value)

    code, captured = call_main(monkeypatch, capsys, ["inbox"])

    assert code == 2
    result = json.loads(captured.out)
    assert result["binding"]["status"] == "unsupported"
    assert "messages" not in result


@pytest.mark.parametrize("raw", ["[]", "null", "12"])
def test_nonobject_hook_payload_fails_closed_without_traceback(project, monkeypatch, capsys, raw):
    code, captured = call_main(monkeypatch, capsys, ["hook", "claude", "--project", str(project)], raw)
    assert code in {0, 2}
    result = json.loads(captured.err)
    assert result["binding"]["status"] == "unsupported"
    assert _rtruntime.inspect_seat(project, "claude").status == "vacant"


@pytest.mark.parametrize("lease", [None, [], "invalid"])
def test_malformed_ticket_lease_is_rejected_and_consumed(project, lease):
    bind_claude(project)
    ticket = cli.issue_ticket(project, "root-session", os.getpid(), "status", tool_use_id="query-call")
    path = cli.ticket_directory() / (ticket + ".json")
    record = json.loads(path.read_text())
    record["lease"] = lease
    path.write_text(json.dumps(record))

    with pytest.raises((_rtnative.NativeQueryError, _rtruntime.RuntimeStateError)):
        cli.consume_ticket(project, ticket, "status", os.getpid())
    assert not list(cli.ticket_directory().iterdir())


@pytest.mark.parametrize("command", [
    "rt-native hook claude --project .",
    "rt-native hooks claude --project .",
    "rt-native bind",
    "rt-native inbox --ticket " + "a" * 64,
    "rt-native status --ticket " + "a" * 64,
    "rt-native inbox --archive-quiet-acks",
    "rt-native inbox another-agent",
])
@pytest.mark.parametrize("agent_id", [None, "child"])
def test_real_pretooluse_rejects_managed_internal_entrypoints_and_extra_arguments(
    project, command, agent_id
):
    bind_claude(project)
    request = payload(project, event="PreToolUse", agent_id=agent_id)
    request["tool_input"]["command"] = command

    with pytest.raises(_rtnative.NativeQueryError):
        cli.run_hook("claude", project, request)
    assert not cli.ticket_directory().exists()


def test_pretooluse_requires_native_tool_use_id(project):
    bind_claude(project)
    request = payload(project, event="PreToolUse")
    request.pop("tool_use_id")

    with pytest.raises(_rtnative.NativeQueryError):
        cli.run_hook("claude", project, request)
    assert not cli.ticket_directory().exists()


@pytest.mark.parametrize("command", ["rg rt-native docs", "cat bin/rt-native", "echo rt-native"])
@pytest.mark.parametrize("agent_id", [None, "child"])
def test_hook_leaves_unrelated_native_bash_work_unchanged(project, command, agent_id):
    request = payload(project, event="PreToolUse", agent_id=agent_id)
    request["tool_input"]["command"] = command

    assert cli.run_hook("claude", project, request) == {}
    assert _rtruntime.inspect_seat(project, "claude").status == "vacant"
    assert not cli.ticket_directory().exists()


def test_forged_session_start_cannot_bind_without_native_hook_transport(project, monkeypatch):
    def untrusted_transport(*_):
        raise _rtnative.NativeQueryError("unsupported", "untrusted hook transport")
    monkeypatch.setattr(cli, "require_hook_transport", untrusted_transport)

    with pytest.raises(_rtnative.NativeQueryError, match="transport"):
        cli.run_hook("claude", project, payload(project))
    assert _rtruntime.inspect_seat(project, "claude").status == "vacant"


def test_forged_pretooluse_cannot_mint_ticket_without_native_hook_transport(project, monkeypatch):
    bind_claude(project)
    def untrusted_transport(*_):
        raise _rtnative.NativeQueryError("unsupported", "untrusted hook transport")
    monkeypatch.setattr(cli, "require_hook_transport", untrusted_transport)

    with pytest.raises(_rtnative.NativeQueryError, match="transport"):
        cli.run_hook("claude", project, payload(project, event="PreToolUse"))
    assert not cli.ticket_directory().exists()


def test_codex_hook_payload_cannot_bind_a_seat(project):
    with pytest.raises(_rtnative.NativeQueryError) as error:
        cli.run_hook("codex", project, payload(project))
    assert error.value.status == "unsupported"
    assert _rtruntime.inspect_seat(project, "codex").status == "vacant"


def test_codex_bind_uses_only_current_native_root_identity(project, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "root-session")
    monkeypatch.setenv("CODEX_SESSION_ID", "root-session")
    code, captured = call_main(monkeypatch, capsys, ["bind"])

    assert code == 0, captured.err
    result = json.loads(captured.out)
    assert result["binding"]["nativeSessionId"] == "root-session"
    record = _rtruntime.read_seat_capability(project, "codex")
    assert record["nativeQuery"]["source"] == "native-shell"
    assert record["nativeQuery"]["ownerPid"] == os.getpid()


def test_codex_subagent_cannot_explicitly_bind_a_root_seat(project, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "subagent-session")
    monkeypatch.setenv("CODEX_SESSION_ID", "root-session")
    code, captured = call_main(monkeypatch, capsys, ["bind"])

    assert code == 2
    assert json.loads(captured.out)["binding"]["status"] == "unsupported"
    assert _rtruntime.inspect_seat(project, "codex").status == "vacant"


@pytest.mark.parametrize("arguments", [
    ["--agent", "codex"], ["--session-id", "root-session"],
    ["--project", "."], ["codex"], ["--ticket", "a" * 64],
])
def test_codex_bind_accepts_no_free_form_identity_or_ticket(project, monkeypatch, capsys, arguments):
    monkeypatch.setattr(cli, "bind_native", lambda *_args, **_kwargs: pytest.fail("unexpected bind"))
    monkeypatch.setattr(sys, "argv", ["rt-native", "bind", *arguments])

    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    capsys.readouterr()


def test_initial_codex_bind_cannot_be_routed_to_another_worktree_by_environment(
    project, tmp_path, monkeypatch, capsys
):
    other = (tmp_path / "other-worktree").resolve()
    state = other / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text((project / ".roundtable" / "agents.yaml").read_text())
    register_project(other)
    monkeypatch.setenv("CODEX_THREAD_ID", "root-session")
    monkeypatch.setenv("CODEX_SESSION_ID", "root-session")
    monkeypatch.setenv("ROUNDTABLE_PROJECT_DIR", str(other))

    code, captured = call_main(monkeypatch, capsys, ["bind"])

    assert code == 2
    result = json.loads(captured.out)
    assert result["binding"]["status"] == "conflict"
    assert "ROUNDTABLE_PROJECT_DIR" in result["error"]
    for root in (project, other):
        assert _rtruntime.inspect_seat(root, "codex").status == "vacant"
        assert _rtruntime.read_seat_capability(root, "codex") is None


@pytest.mark.parametrize("action", ["bind", "inbox", "status"])
@pytest.mark.parametrize("environment", ["RT_FALLBACK_PROJECT", "ROUNDTABLE_PROJECT_DIR"])
def test_native_cli_requires_cwd_project_anchor_despite_environment_route(
    project, tmp_path, monkeypatch, capsys, action, environment
):
    unanchored = tmp_path / "without-project-anchor"
    unanchored.mkdir()
    monkeypatch.chdir(unanchored)
    monkeypatch.setenv("CODEX_THREAD_ID", "root-session")
    monkeypatch.setenv("CODEX_SESSION_ID", "root-session")
    monkeypatch.setenv(environment, str(project))

    code, captured = call_main(monkeypatch, capsys, [action])

    assert code == 2
    result = json.loads(captured.out)
    assert result["binding"]["status"] == "unbound"
    assert "messages" not in result
    assert _rtruntime.inspect_seat(project, "codex").status == "vacant"
    assert _rtruntime.read_seat_capability(project, "codex") is None
    assert not _rtruntime.runtime_root().exists()
