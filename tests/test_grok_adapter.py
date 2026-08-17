from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import _kit as kit


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

adapter = importlib.import_module("integrations.grok.roundtable")
import _rtlauncher  # noqa: E402
import _rtruntime  # noqa: E402


class _Process:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class _FakeClient:
    def __init__(self, *, prompt_error=None, initialize=None, session=None):
        self.process = _Process()
        self.prompt_error = prompt_error
        self.initialize_response = initialize or {"jsonrpc": "2.0", "id": 1, "result": {}}
        self.session_response = session or {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"sessionId": "grok-session"},
        }
        self.prompts = []
        self.closed = False
        self.permission_decisions = []

    def request(self, method, params, *, timeout):
        if method == "initialize":
            return self.initialize_response, {"method": method}
        if method == "session/new":
            return self.session_response, {"method": method}
        if method == "session/prompt":
            self.prompts.append(params)
            if self.prompt_error is not None:
                raise self.prompt_error
            return {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {"stopReason": "end_turn"},
            }, {"method": method}
        raise AssertionError(method)

    def close(self, *, force=False):
        self.closed = True
        self.process.returncode = -9 if force else -15


def _instance(tmp_path, factory, **kwargs):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime")
    instance = adapter.GrokAdapter(
        project,
        agent_id="grok",
        session_id="session-1",
        revision="1",
        isolation=isolation,
        executable=executable,
        api_key_resolver=lambda: "xai-test-key",
        client_factory=factory,
        **kwargs,
    )
    instance._lease_valid = lambda: None
    return instance


def test_auth_reader_uses_key_and_ignores_refresh_token(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"account": {"key": "xai-test-key", "refresh_token": "do-not-use"}}),
        encoding="utf-8",
    )
    assert adapter.resolve_grok_api_key({"GROK_AUTH_PATH": str(auth)}) == "xai-test-key"


def test_isolation_bounds_child_state_and_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime")
    environment = isolation.environment(
        project_root=project,
        executable=executable,
        api_key="xai-test-key",
    )
    environment["RT_GROK_BIN"] = str(executable)
    isolation.assert_isolated(environment)
    assert Path(environment["HOME"]).is_relative_to(isolation.root)
    assert Path(environment["GROK_HOME"]).is_relative_to(isolation.root)
    monkeypatch.setitem(environment, "GROK_HOME", str(tmp_path / "personal"))
    with pytest.raises(adapter.GrokError, match="GROK_HOME"):
        isolation.assert_isolated(environment)


def test_wake_prompt_and_opt_in_mailroom_policy_stay_exact(tmp_path):
    prompt = adapter.wake_prompt(tmp_path, "grok", ("one.md", "two.md"))
    assert "rt-inbox --fenced --archive-quiet-acks -f json" in prompt
    assert "RT_FROM=grok rt-ack <msg_id> handled" in prompt
    assert adapter._mail_command_allowed("rt-inbox -f json")
    assert adapter._mail_command_allowed("rt-inbox --fenced --archive-quiet-acks -f json")
    assert adapter._mail_command_allowed("RT_FROM=grok rt-ack message-id handled")
    assert not adapter._mail_command_allowed("pip install PyYAML")
    assert not adapter._mail_command_allowed("RT_FROM=grok rt-ack message-id; rm -rf x")


def test_default_wake_policy_is_full_permission_with_mailroom_opt_in():
    default_policy = adapter.wake_permission_policy(environment={})
    assert default_policy("pip install PyYAML")
    assert default_policy("git commit -am work")
    assert default_policy("rt-inbox -f json")

    restricted = adapter.wake_permission_policy(
        environment={"RT_GROK_WAKE_MAILROOM_ONLY": "1"}
    )
    assert restricted is adapter._mail_command_allowed
    assert not restricted("pip install PyYAML")


def test_authenticated_session_reuses_child_and_waits_for_exact_drain(tmp_path):
    client = _FakeClient()
    instance = _instance(tmp_path, lambda _command, **_kwargs: client)
    generations = [("message.md",), ()]
    instance._mail_generation = lambda: generations.pop(0)
    fences = []
    instance._fence_update = lambda **kwargs: fences.append(kwargs)

    result = instance.run_generation(("message.md",), prompt_timeout=1, drain_timeout=1)

    assert result.stop_reason == "end_turn"
    assert len(client.prompts) == 1
    assert client.prompts[0]["sessionId"] == "grok-session"
    assert fences[-1]["names"] == ("message.md",)


def test_child_death_restarts_once_and_preserves_mail_until_retry(tmp_path):
    first = _FakeClient(prompt_error=adapter.GrokUnavailableError("child died"))
    second = _FakeClient()
    clients = iter((first, second))
    instance = _instance(tmp_path, lambda _command, **_kwargs: next(clients))
    generations = [("message.md",), ("message.md",), ()]
    instance._mail_generation = lambda: generations.pop(0)
    instance._fence_update = lambda **_kwargs: None

    result = instance.run_generation(("message.md",), prompt_timeout=1, drain_timeout=1)

    assert result.stop_reason == "end_turn"
    assert first.closed
    assert len(second.prompts) == 1


def test_auth_failure_does_not_retry_or_start_a_second_child(tmp_path):
    bad = _FakeClient(
        initialize={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Incorrect API key provided"},
        }
    )
    calls = []
    instance = _instance(tmp_path, lambda _command, **_kwargs: calls.append(bad) or bad)
    with pytest.raises(adapter.GrokAuthenticationError):
        instance._start_client()
    assert calls == [bad]


def test_prompt_auth_failure_is_classified_and_child_is_closed(tmp_path):
    class BadPrompt(_FakeClient):
        def request(self, method, params, *, timeout):
            if method == "session/prompt":
                return {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "error": {
                        "code": -32603,
                        "data": {
                            "http_status": 403,
                            "message": "unauthenticated:bad-credentials",
                        },
                    },
                }, {"method": method}
            return super().request(method, params, timeout=timeout)

    client = BadPrompt()
    instance = _instance(tmp_path, lambda _command, **_kwargs: client)
    instance._mail_generation = lambda: ("message.md",)
    with pytest.raises(adapter.GrokAuthenticationError, match="wake turn"):
        instance.run_generation(("message.md",), prompt_timeout=1, drain_timeout=1)
    assert client.closed


def test_hung_prompt_is_bounded_and_does_not_ack(tmp_path):
    client = _FakeClient(prompt_error=adapter.GrokRunTimeout("hung"))
    instance = _instance(tmp_path, lambda _command, **_kwargs: client)
    instance._mail_generation = lambda: ("message.md",)
    with pytest.raises(adapter.GrokRunTimeout):
        instance.run_generation(("message.md",), prompt_timeout=0.01, drain_timeout=0.01)
    assert instance._mail_generation() == ("message.md",)
    assert client.closed


@pytest.mark.parametrize("bad_field", ["session_id", "agent_id", "revision"])
def test_stale_or_wrong_identity_is_refused_before_child_start(tmp_path, monkeypatch, bad_field):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    lease = _rtruntime.claim(project, "grok", "grok", owner_pid=os.getpid())
    try:
        executable = tmp_path / "grok"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        isolation = adapter.create_isolation(project, runtime_root=tmp_path / "grok-runtime")
        values = {
            "agent_id": "other",
            "session_id": "wrong-session",
                "revision": "wrong-revision",
        }
        instance = adapter.GrokAdapter(
            project,
            agent_id=values["agent_id"] if bad_field == "agent_id" else "grok",
            session_id=values["session_id"] if bad_field == "session_id" else lease.session_id,
            revision=values["revision"] if bad_field == "revision" else str(lease.revision),
            isolation=isolation,
            executable=executable,
            api_key_resolver=lambda: "xai-test-key",
        )
        started = []
        monkeypatch.setattr(instance, "_start_client", lambda: started.append(True))
        with pytest.raises(_rtruntime.FenceRejected):
            instance.run(once=True)
        assert started == []
    finally:
        _rtruntime.release(lease)


def _grok_launch_fixture(tmp_path, monkeypatch, user_argv, extra_env=None):
    project = tmp_path / "project"
    kit.write_project(project, [kit.GROK], project=kit.PROJECT_DOT_BARE)
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    mailbox = (tmp_path / "mail" / "inbox" / "grok" / "new").absolute()
    runtime = (tmp_path / "runtime").absolute()
    user_home = (tmp_path / "user-home").absolute()
    observed = {}

    for name in (
        *_rtlauncher.LEASE_ENV_NAMES,
        "RT_RUNTIME_DIR",
        "RT_CODEX_RUNTIME_DIR",
        "RT_GROK_NO_PRIMER",
        "RT_GROK_ISOLATION_ROOT",
        "GROK_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RT_FROM", "grok")
    monkeypatch.setenv("HOME", str(user_home))
    for name, value in (extra_env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda path: observed.setdefault("cwd", path))
    monkeypatch.setattr(_rtlauncher, "set_launch_identity", lambda *_args: "grok")
    monkeypatch.setattr(
        _rtlauncher,
        "normalize_runtime_environment",
        lambda: (
            _rtlauncher.os.environ.update(
                {
                    "RT_RUNTIME_DIR": str(runtime),
                    "RT_CODEX_RUNTIME_DIR": str(runtime),
                }
            )
            or runtime
        ),
    )
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: executable)
    monkeypatch.setattr(_rtlauncher, "preflight_grok_credentials", lambda: None)
    monkeypatch.setattr(_rtlauncher, "grok_seat_maildir", lambda *_args: mailbox)
    monkeypatch.setattr(_rtlauncher, "_same_process_lease", lambda *_args: None)
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, harness: SimpleNamespace(
            project_root=root.resolve(),
            agent_id=agent_id,
            session_id=f"{harness}-session",
            revision=7,
        ),
    )

    def fake_exec(path, argv):
        observed["path"] = path
        observed["argv"] = argv
        observed["environment"] = {
            name: _rtlauncher.os.environ.get(name)
            for name in (
                *_rtlauncher.LEASE_ENV_NAMES,
                "RT_RUNTIME_DIR",
                "RT_CODEX_RUNTIME_DIR",
                "HOME",
                "RT_GROK_ISOLATION_ROOT",
                "GROK_HOME",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "TMPDIR",
            )
        }
        raise RuntimeError("exec captured")

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_exec)
    with pytest.raises(RuntimeError, match="exec captured"):
        _rtlauncher.launch("grok", user_argv)
    return observed, executable, mailbox, runtime, user_home


def test_grok_credential_preflight_is_presence_only(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_bytes(b"\xffnot-readable-json")

    adapter_env = {"HOME": str(tmp_path / "home"), "GROK_AUTH_PATH": str(auth)}
    _rtlauncher.preflight_grok_credentials(adapter_env)
    _rtlauncher.preflight_grok_credentials({"XAI_API_KEY": "present-only"})

    with pytest.raises(_rtlauncher.SelectionError, match="credentials are missing"):
        _rtlauncher.preflight_grok_credentials({"HOME": str(tmp_path / "missing")})


def test_bare_grok_launcher_execs_native_tui_with_exact_monitor_primer(
    tmp_path, monkeypatch, capsys
):
    observed, executable, mailbox, runtime, user_home = _grok_launch_fixture(
        tmp_path, monkeypatch, []
    )
    prompt = _rtlauncher.GROK_SEAT_PRIMER_TEMPLATE.format(
        maildir=mailbox,
        inbox=BIN / "rt-inbox",
        ack=BIN / "rt-ack",
    )

    assert _rtlauncher.GROK_SEAT_PRIMER_TEMPLATE == (
        "[pneu] Grok seat activation turn. Use only the `monitor` tool to "
        "create exactly one background task with `persistent: true` watching "
        "the absolute directory {maildir}. For every event, start a turn that "
        "runs `{inbox} --fenced --archive-quiet-acks -f json`, acts on every "
        "non-ack message, then runs `{ack} --fenced <id>[,<id>...]` exactly "
        "once for the handled IDs. On this activation turn, do not inspect "
        "files, modify the workspace, or use any other tool. After creating "
        "the monitor, reply exactly: ready."
    )
    assert observed["path"] == str(executable)
    assert observed["argv"] == [str(executable), prompt]
    assert observed["cwd"] == (tmp_path / "project")
    assert observed["environment"] == {
        "RT_PROJECT_ROOT": str((tmp_path / "project").resolve()),
        "RT_FROM": "grok",
        "RT_SESSION_ID": "grok-session",
        "RT_LEASE_REVISION": "7",
        "RT_RUNTIME_DIR": str(runtime),
        "RT_CODEX_RUNTIME_DIR": str(runtime),
        "HOME": str(user_home),
        "RT_GROK_ISOLATION_ROOT": None,
        "GROK_HOME": None,
        "XDG_CONFIG_HOME": None,
        "XDG_DATA_HOME": None,
        "XDG_CACHE_HOME": None,
        "TMPDIR": None,
    }
    assert "activation primer skipped" not in capsys.readouterr().err
    assert "rt-grok-wake" not in observed["argv"]


def test_explicit_grok_arguments_pass_through_and_disable_primer(
    tmp_path, monkeypatch, capsys
):
    observed, executable, *_rest = _grok_launch_fixture(
        tmp_path, monkeypatch, ["--resume", "native-session"]
    )

    assert observed["argv"] == [str(executable), "--resume", "native-session"]
    assert _rtlauncher.GROK_SEAT_PRIMER_TEMPLATE not in observed["argv"]
    advisory = capsys.readouterr().err
    assert "IMPORTANT: Grok monitor activation primer skipped" in advisory
    assert "native arguments were supplied" in advisory
    assert "after every resume" in advisory


def test_rt_grok_no_primer_is_an_explicit_bare_opt_out(
    tmp_path, monkeypatch, capsys
):
    observed, executable, *_rest = _grok_launch_fixture(
        tmp_path,
        monkeypatch,
        [],
        extra_env={"RT_GROK_NO_PRIMER": "1"},
    )

    assert observed["argv"] == [str(executable)]
    advisory = capsys.readouterr().err
    assert "RT_GROK_NO_PRIMER=1" in advisory
    assert "will not auto-wake" in advisory


def test_grok_seat_path_is_pinned_away_from_internal_acp_supervisor():
    # "Seat path" means launch() *and everything it calls*, not launch()'s own
    # body. Checking only the body lets the supervisor reference move one call
    # deeper and keep this test green, which is weaker than the name claims.
    # (The original text slice from "def launch(" had the opposite problem: it
    # ran to end of file and absorbed unrelated definitions below.)
    launcher = BIN / "_rtlauncher.py"
    seat_path = kit.reachable_definitions(launcher, "launch")
    seat_source = kit.reachable_source(launcher, "launch")

    assert "launch" in seat_path
    assert "grok_seat_primer_args" in seat_path, seat_path
    assert "def main(" not in seat_source
    for marker in ("grok_adapter_bin", '"--grok-bin"', "rt-grok-wake"):
        assert marker not in seat_source


def test_internal_grok_lab_help_is_explicit():
    result = subprocess.run(
        [sys.executable, str(BIN / "rt-grok-wake"), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    help_text = " ".join(result.stdout.split())
    assert "INTERNAL LAB TOOL" in help_text
    assert "Normal Grok seats use the native TUI via rt-grok" in help_text
