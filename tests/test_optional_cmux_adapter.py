import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

from _rtlib import register_project, resolve_project_mailbox


def registry_for(path):
    return path.resolve().parent / "projects.yaml"


def mailbox_for(project):
    return resolve_project_mailbox(project, registry_path=registry_for(project))


def no_cmux_env(**updates):
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(Path(sys.executable).resolve().parent),
            "CMUX_SURFACE_ID": "",
            "CODEX_THREAD_ID": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ROUNDTABLE_PROJECT_DIR": "",
            "RT_FALLBACK_PROJECT": "",
            "RT_FROM": "",
        }
    )
    env.update(updates)
    return env


def fake_cmux(tmp_path, body, exit_code=0):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    cmux = fake_bin / "cmux"
    cmux.write_text(
        "#!/bin/sh\n"
        'if [ -n "${RT_TEST_CMUX_SENTINEL:-}" ]; then\n'
        '  printf "called\\n" > "$RT_TEST_CMUX_SENTINEL"\n'
        "fi\n"
        f"printf '%s\\n' '{body}'\n"
        f"exit {exit_code}\n"
    )
    cmux.chmod(0o755)
    return f"{fake_bin}:{Path(sys.executable).resolve().parent}"


def run_tool(name, *args, cwd, env=None):
    environment = dict(env or no_cmux_env())
    environment["RT_PROJECTS_FILE"] = str(registry_for(cwd))
    return subprocess.run(
        [sys.executable, str(BIN / name), *args],
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write_project(path):
    state = path / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        f"""schema: roundtable.agents.v1
project: {path}
agents:
  codex:
    harness: codex
    instances:
      - id: codex
  claude:
    harness: claude-code
    instances:
      - id: claude
  hermes:
    harness: hermes-agent
    instances:
      - id: hermes
"""
    )
    register_project(path, path=registry_for(path))
    return state


def write_mail(state, msg_id, sender, target, body="test"):
    directory = mailbox_for(state.parent).inbox_dir / target / "new"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{msg_id}.md"
    path.write_text(
        f"[{sender.upper()}→{target.upper()} directive id={msg_id}] {body}"
    )
    return path


def test_inbox_lists_and_hides_drained_mail_without_cmux(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    msg_id = "20260718T120000Z-codex-to-claude-100"
    mail = write_mail(state, msg_id, "codex", "claude", "review this")

    listed = run_tool(
        "rt-inbox", "claude", "--format", "json", cwd=project
    )

    assert listed.returncode == 0, listed.stderr
    records = json.loads(listed.stdout)
    assert [(record["msg_id"], record["body"]) for record in records] == [
        (msg_id, "review this")
    ]
    assert "Traceback" not in listed.stderr

    current = mail.parents[1] / "cur"
    current.mkdir()
    mail.rename(current / mail.name)
    drained = run_tool("rt-inbox", "claude", cwd=project)

    assert drained.returncode == 0, drained.stderr
    assert drained.stdout == ""
    assert drained.stderr == ""


def test_inbox_without_identity_fails_cleanly_without_cmux(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)

    result = run_tool("rt-inbox", cwd=project)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "rt-inbox: could not infer agent; pass agent or set RT_FROM\n"
    )
    assert "Traceback" not in result.stderr
    assert not (state / "runtime.json").exists()


def test_inbox_ignores_stale_runtime_and_does_not_probe_cmux(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    (state / "runtime.json").write_text("[]\n")
    path = fake_cmux(tmp_path, "[]")
    sentinel = tmp_path / "cmux-called"

    result = run_tool(
        "rt-inbox",
        cwd=project,
        env=no_cmux_env(PATH=path, RT_TEST_CMUX_SENTINEL=str(sentinel)),
    )

    assert result.returncode == 1
    assert result.stderr == (
        "rt-inbox: could not infer agent; pass agent or set RT_FROM\n"
    )
    assert "Traceback" not in result.stderr
    assert not sentinel.exists()


def test_inbox_uses_unique_codex_thread_identity_without_cmux(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    msg_id = "20260718T120000Z-claude-to-codex-106"
    write_mail(state, msg_id, "claude", "codex", "remote inbox")

    result = run_tool(
        "rt-inbox",
        "--format",
        "json",
        cwd=project,
        env=no_cmux_env(CODEX_THREAD_ID="thread-from-app-server"),
    )

    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    assert [record["msg_id"] for record in records] == [msg_id]


def test_core_project_fallback_does_not_probe_cmux(tmp_path):
    project = tmp_path / "project"
    write_project(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    path = fake_cmux(tmp_path, "{}")
    sentinel = tmp_path / "cmux-called"

    result = run_tool(
        "rt-inbox",
        "claude",
        "--format",
        "json",
        cwd=outside,
        env=no_cmux_env(
            PATH=path,
            RT_FALLBACK_PROJECT=str(project),
            RT_TEST_CMUX_SENTINEL=str(sentinel),
        ),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert not sentinel.exists()


def test_missing_core_project_does_not_probe_cmux(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    path = fake_cmux(tmp_path, "{}")
    sentinel = tmp_path / "cmux-called"

    result = run_tool(
        "rt-inbox",
        "claude",
        cwd=outside,
        env=no_cmux_env(
            PATH=path,
            RT_TEST_CMUX_SENTINEL=str(sentinel),
        ),
    )

    assert result.returncode == 1
    assert "not in a roundtable project" in result.stderr
    assert not sentinel.exists()


def test_cmux_diagnostics_fail_cleanly_when_adapter_is_unavailable(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)

    refresh = run_tool("rt-refresh", cwd=project)
    resolve = run_tool("rt-resolve", "codex", cwd=project)

    assert refresh.returncode == 1
    assert refresh.stdout == ""
    assert refresh.stderr == (
        "rt-refresh: cmux adapter unavailable (No such file or directory); "
        "maildir delivery is unaffected\n"
    )
    assert resolve.returncode == 1
    assert resolve.stdout == ""
    assert resolve.stderr == (
        "rt-resolve: cmux adapter unavailable (No such file or directory); "
        "maildir delivery is unaffected\n"
    )
    assert "Traceback" not in refresh.stderr + resolve.stderr
    assert not (state / "runtime.json").exists()


@pytest.mark.parametrize(
    ("body", "exit_code"),
    [("not-json", 0), ("connection failed", 69)],
)
def test_cmux_diagnostics_fail_cleanly_when_adapter_is_unhealthy(
    tmp_path, body, exit_code
):
    project = tmp_path / "project"
    state = write_project(project)
    path = fake_cmux(tmp_path, body, exit_code)
    env = no_cmux_env(PATH=path)

    refresh = run_tool("rt-refresh", cwd=project, env=env)
    resolve = run_tool("rt-resolve", "codex", cwd=project, env=env)

    assert refresh.returncode == 1
    assert refresh.stderr == (
        "rt-refresh: cmux adapter unavailable or unhealthy; "
        "maildir delivery is unaffected\n"
    )
    assert resolve.returncode == 1
    assert resolve.stderr == (
        "rt-resolve: cmux adapter unavailable or unhealthy; "
        "maildir delivery is unaffected\n"
    )
    assert "Traceback" not in refresh.stderr + resolve.stderr
    assert not (state / "runtime.json").exists()


def test_ack_infers_sender_from_message_recipient_without_cmux(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    original = "20260718T120000Z-codex-to-claude-101"
    mail = write_mail(state, original, "codex", "claude")

    result = run_tool("rt-ack", original, "received", cwd=project)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("sent maildir-only ")
    ack_files = list(
        (mailbox_for(project).inbox_dir / "codex" / "new").glob("ack-*.md")
    )
    assert len(ack_files) == 1
    ack = ack_files[0].read_text()
    assert ack.startswith("[CLAUDE→CODEX sync-ack id=")
    assert f"refs={original} received" in ack
    archived = mail.parents[1] / "cur" / mail.name
    assert not mail.exists()
    assert archived.read_text() == (
        f"[CODEX→CLAUDE directive id={original}] test"
    )
    assert "Traceback" not in result.stderr


def test_ack_failure_never_archives_inbound_mail(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    mailbox = mailbox_for(project)
    original = "20260718T120001Z-codex-to-claude-106"
    mail = write_mail(state, original, "codex", "claude")
    (mailbox.inbox_dir / "codex").write_text("blocks ack delivery")

    result = run_tool("rt-ack", original, cwd=project)

    assert result.returncode != 0
    assert "failed to publish inbox message" in result.stderr
    assert mail.is_file()
    assert not (mail.parents[1] / "cur" / mail.name).exists()


def test_ack_archives_only_exact_batch_refs(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    first = "20260718T120002Z-codex-to-claude-107"
    second = "20260718T120003Z-codex-to-claude-108"
    unrelated = "20260718T120004Z-codex-to-claude-109"
    first_mail = write_mail(state, first, "codex", "claude", "first")
    second_mail = write_mail(state, second, "codex", "claude", "second")
    unrelated_mail = write_mail(state, unrelated, "codex", "claude", "keep")

    result = run_tool("rt-ack", f"{first},{second}", cwd=project)

    assert result.returncode == 0, result.stderr
    cur = first_mail.parents[1] / "cur"
    assert (cur / first_mail.name).is_file()
    assert (cur / second_mail.name).is_file()
    assert not first_mail.exists()
    assert not second_mail.exists()
    assert unrelated_mail.is_file()
    assert not (cur / unrelated_mail.name).exists()


def test_ack_accepts_archived_but_rejects_unproven_missing_ref(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    archived_ref = "20260718T120005Z-codex-to-claude-110"
    missing_ref = "20260718T120006Z-codex-to-claude-111"
    mail = write_mail(state, archived_ref, "codex", "claude", "archived")
    cur = mail.parents[1] / "cur"
    cur.mkdir()
    archived = cur / mail.name
    mail.rename(archived)

    archived_result = run_tool("rt-ack", archived_ref, cwd=project)
    missing_result = run_tool("rt-ack", missing_ref, cwd=project)

    assert archived_result.returncode == 0, archived_result.stderr
    assert missing_result.returncode != 0
    assert "cannot determine origin for missing inbound ref" in missing_result.stderr
    assert archived.read_text().endswith(" archived")
    assert not mail.exists()
    assert not (cur / f"{missing_ref}.md").exists()


def test_ack_finishes_interrupted_same_inode_archive(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    original = "20260718T120007Z-codex-to-claude-112"
    mail = write_mail(state, original, "codex", "claude", "recover")
    cur = mail.parents[1] / "cur"
    cur.mkdir()
    archived = cur / mail.name
    os.link(mail, archived)
    assert mail.stat().st_ino == archived.stat().st_ino

    result = run_tool("rt-ack", original, cwd=project)

    assert result.returncode == 0, result.stderr
    assert not mail.exists()
    assert archived.read_text().endswith(" recover")


def test_ack_rejects_archive_conflict_before_sending_confirmation(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    original = "20260718T120008Z-codex-to-claude-113"
    mail = write_mail(state, original, "codex", "claude", "new copy")
    cur = mail.parents[1] / "cur"
    cur.mkdir()
    conflict = cur / mail.name
    conflict.write_text("different archived copy")

    result = run_tool("rt-ack", original, cwd=project)

    assert result.returncode != 0
    assert "conflicting new/cur copies" in result.stderr
    assert mail.read_text().endswith(" new copy")
    assert conflict.read_text() == "different archived copy"
    ack_files = list(
        (mailbox_for(project).inbox_dir / "codex" / "new").glob("ack-*.md")
    )
    assert ack_files == []


def test_ack_rejects_path_traversal_ref_before_delivery(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    mailbox = mailbox_for(project)
    unsafe = "20260718T120009Z-codex-to-claude-../../outside"
    # Make the mailbox and traversal components exist. The lexical guard must
    # still reject before recipient discovery performs any joined-path lstat.
    new_dir = mailbox.inbox_dir / "claude" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / unsafe.split("/", 1)[0]).mkdir()
    (new_dir.parent / "outside.md").mkdir()

    result = run_tool("rt-ack", unsafe, cwd=project)

    assert result.returncode != 0
    assert result.stderr == f"rt-ack: cannot parse msg_id: {unsafe}\n"
    assert (new_dir.parent / "outside.md").is_dir()


def test_ack_validated_sender_does_not_invoke_ambient_cmux(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    original = "20260718T120000Z-codex-to-claude-105"
    write_mail(state, original, "codex", "claude")
    runtime = {
        "schema": "roundtable.runtime.v1",
        "project": str(project),
        "agents": {
            "hermes": {
                "workspace_ref": "workspace:1",
                "surface_ref": "surface:3",
                "pane_ref": "pane:3",
            }
        },
        "surfaces": [],
    }
    (state / "runtime.json").write_text(json.dumps(runtime))
    path = fake_cmux(
        tmp_path,
        '{"caller":{"workspace_ref":"workspace:1","surface_ref":"surface:3"}}',
    )
    sentinel = tmp_path / "cmux-called"

    result = run_tool(
        "rt-ack",
        original,
        cwd=project,
        env=no_cmux_env(
            PATH=path,
            CMUX_SURFACE_ID="surface-uuid",
            RT_FROM="claude",
            RT_TEST_CMUX_SENTINEL=str(sentinel),
        ),
    )

    assert result.returncode == 0, result.stderr
    ack_file = next(
        (mailbox_for(project).inbox_dir / "codex" / "new").glob("ack-*.md")
    )
    assert ack_file.read_text().startswith("[CLAUDE→CODEX sync-ack id=")
    assert not sentinel.exists()


def test_ack_rejects_explicit_sender_mismatch_before_delivery(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    mailbox = mailbox_for(project)
    original = "20260718T120000Z-codex-to-claude-102"
    write_mail(state, original, "codex", "claude")

    result = run_tool(
        "rt-ack",
        original,
        cwd=project,
        env=no_cmux_env(RT_FROM="codex"),
    )

    assert result.returncode == 1
    assert result.stderr == (
        "rt-ack: RT_FROM=codex does not match message recipient claude\n"
    )
    assert not (mailbox.inbox_dir / "codex").exists()


def test_ack_rejects_mixed_recipients_before_delivery(tmp_path):
    project = tmp_path / "project"
    state = write_project(project)
    mailbox = mailbox_for(project)
    claude_ref = "20260718T120000Z-codex-to-claude-103"
    hermes_ref = "20260718T120001Z-codex-to-hermes-104"
    write_mail(state, claude_ref, "codex", "claude")
    write_mail(state, hermes_ref, "codex", "hermes")

    result = run_tool(
        "rt-ack", f"{claude_ref},{hermes_ref}", cwd=project
    )

    assert result.returncode == 1
    assert result.stderr == (
        "rt-ack: refs target multiple recipients: claude, hermes; "
        "acknowledge one recipient at a time\n"
    )
    assert not (mailbox.inbox_dir / "codex").exists()
