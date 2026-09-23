"""Mutation tickets bind literal data; these are synthetic contract tests."""

import json
import os
import shlex
import subprocess
import sys

import pytest

from test_native_inbox_cli import cli, project, payload, bind_claude, call_main
from _rtnative import NativeQueryError


@pytest.mark.parametrize("literal", [
    r'"a\$HOME"', r'"a\`word\`"', r'"a\"quote\""', r'"a\\word"',
    r'"a\q"', r'a\$HOME', "'one'\"two\"'three'", "''", '"line\nnext"',
])
def test_literal_decoder_matches_posix_shell(literal):
    probe = shlex.join([sys.executable, "-c", "import json,sys;print(json.dumps(sys.argv[1:]))"])
    actual = subprocess.run(["/bin/sh", "-c", probe + " " + literal],
                            text=True, capture_output=True, check=True)
    assert cli.literal_words(literal) == json.loads(actual.stdout)


@pytest.mark.parametrize("body", [
    "simple", "", "--help", "--no-nudge", "--legacy-nudge-only", "--expect-reply=2h",
    "line one\nline two\n", 'both \'single\' and "double" quotes',
    "$(touch nope); `false` | cat > target & $HOME # [] * ? \\ end",
    "中文审阅：2 + 2 = 5。", "trailing\n\n",
])
def test_hook_binds_and_quotes_every_body_byte(project, monkeypatch, body):
    bind_claude(project)
    expected = {"target": "codex", "kind": "request", "body": body}
    calls = []
    def mutation(root, harness, native_id, operation, parameters, **kwargs):
        calls.append((root, harness, native_id, operation, parameters))
        return {"mutation": {"status": "committed"}}
    monkeypatch.setattr(cli, "mutate", mutation)
    request = payload(project, event="PreToolUse", tool_input={
        "command": shlex.join(["rt-native", "send", "codex", "request", body]),
        "description": "explicit send", "timeout": 5000,
    })
    output = cli.run_hook("claude", project, request)["hookSpecificOutput"]
    assert "permissionDecision" not in output
    updated = output["updatedInput"]
    assert updated["timeout"] == 5000
    assert updated["description"] == "explicit send"
    words = shlex.split(updated["command"])
    ticket = words[words.index("--ticket") + 1]
    record = json.loads((cli.ticket_directory() / (ticket + ".json")).read_text())
    assert record["parameters"] == expected
    # Exercise actual POSIX parsing of the rewritten argv, without executing
    # the tool. Any unquoted syntax would alter argv or execute in the test cwd.
    probe = shlex.join([sys.executable, "-c", "import sys,json;print(json.dumps(sys.argv[1:]))"])
    shell = subprocess.run(["/bin/sh", "-c", probe + " " + shlex.join(words)],
                           capture_output=True, text=True, check=True)
    assert json.loads(shell.stdout) == words
    cli.consume_ticket(project, ticket, "send", os.getpid(), expected)
    assert calls == [(project, "claude", "root-session", "send", expected)]


@pytest.mark.parametrize("operation,parameters,changed", [
    ("send", {"target": "codex", "kind": "request", "body": "original"}, {"target": "claude"}),
    ("send", {"target": "codex", "kind": "request", "body": "original"}, {"kind": "reply"}),
    ("send", {"target": "codex", "kind": "request", "body": "original"}, {"body": "replaced"}),
    ("ack", {"refs": ["ref-a"], "body": "done"}, {"refs": ["ref-b"]}),
    ("ack", {"refs": ["ref-a"], "body": "done"}, {"body": "not done"}),
])
def test_parameter_substitution_consumes_ticket_without_mutation(project, monkeypatch, operation, parameters, changed):
    bind_claude(project)
    monkeypatch.setattr(cli, "mutate", lambda *a, **kw: pytest.fail("unauthorized mutation"))
    ticket = cli.issue_ticket(project, "root-session", os.getpid(), operation, "call", parameters)
    with pytest.raises(NativeQueryError, match="invocation"):
        cli.consume_ticket(project, ticket, operation, os.getpid(), parameters | changed)
    with pytest.raises(NativeQueryError, match="already used"):
        cli.consume_ticket(project, ticket, operation, os.getpid(), parameters)


@pytest.mark.parametrize("suffix", [
    '"$HOME"', '"$(touch nope)"', '`touch nope`', 'body; true', 'body && true',
    'body\ntrue', 'body | cat', 'body > target', '*', '~', 'body # comment',
    '"`touch nope`"', '$(echo body)', "body \\\ntrue",
])
def test_mutation_hook_rejects_shell_evaluation(project, suffix):
    bind_claude(project)
    request = payload(project, event="PreToolUse", tool_input={
        "command": "rt-native send codex request " + suffix,
    })
    with pytest.raises(NativeQueryError):
        cli.run_hook("claude", project, request)
    assert not cli.ticket_directory().exists()


@pytest.mark.parametrize("operation,arguments,parameters", [
    ("send", ["codex", "request", "--help"], {"target": "codex", "kind": "request", "body": "--help"}),
    ("send", ["codex", "request", "a\nb"], {"target": "codex", "kind": "request", "body": "a\nb"}),
    ("ack", ["ref-a,ref-b", "--help"], {"refs": ["ref-a", "ref-b"], "body": "--help"}),
])
def test_rewritten_command_reaches_cli_with_exact_parameters(project, monkeypatch, capsys, operation, arguments, parameters):
    bind_claude(project)
    ticket = cli.issue_ticket(project, "root-session", os.getpid(), operation, "call", parameters)
    seen = []
    monkeypatch.setattr(cli, "mutate", lambda *a, **kw: seen.append(a[4]) or {"mutation": {"status": "committed"}})
    code, captured = call_main(monkeypatch, capsys, [operation, "--ticket", ticket, "--", *arguments])
    assert code == 0, captured
    assert seen == [parameters]


@pytest.mark.parametrize("status,code", [("committed", 0), ("partial", 1), ("unknown", 1), ("not_committed", 1)])
def test_mutation_cli_preserves_outcome_and_exit_status(project, monkeypatch, capsys, status, code):
    monkeypatch.setenv("CODEX_THREAD_ID", "root")
    monkeypatch.setenv("CODEX_SESSION_ID", "root")
    result = {"mutation": {"status": status, "message_id": "known-id"}}
    monkeypatch.setattr(cli, "mutate", lambda *a, **kw: result)
    actual, captured = call_main(monkeypatch, capsys, ["send", "claude", "request", "body"])
    assert actual == code
    assert json.loads(captured.out) == result


def test_lost_stdout_does_not_report_committed_send_as_unsent(project, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "root")
    monkeypatch.setenv("CODEX_SESSION_ID", "root")
    monkeypatch.setattr(cli, "mutate", lambda *a, **kw: {
        "mutation": {"status": "committed", "message_id": "known-id"},
    })
    original = sys.stdout
    class BrokenOutput:
        def write(self, text):
            raise OSError("lost response")
        def flush(self):
            pass
    monkeypatch.setattr(sys, "argv", ["rt-native", "send", "claude", "request", "body"])
    monkeypatch.setattr(sys, "stdout", BrokenOutput())
    assert cli.main() == 1
    monkeypatch.setattr(sys, "stdout", original)
    result = json.loads(capsys.readouterr().err)
    assert result["mutation"] == {"status": "committed", "message_id": "known-id"}
    assert result["response_error"] == "lost response"


@pytest.mark.parametrize("parameters", [["a,,b"], ["a,a"], [" a"], ["a", "body", "extra"]])
def test_ack_refs_are_exact_unique_and_bounded(parameters):
    with pytest.raises(NativeQueryError):
        cli.mutation_parameters("ack", parameters)


def test_rejected_ack_reports_requested_refs_without_claiming_commit(project, monkeypatch, capsys):
    code, captured = call_main(monkeypatch, capsys, ["ack", "ref-a,ref-b", "note"])
    assert code == 2
    assert json.loads(captured.out)["mutation"] == {
        "status": "not_committed", "refs": ["ref-a", "ref-b"],
    }
