"""Maildir mutation contracts using isolated projects, not native acceptance."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sys

import pytest

import _kit as kit

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import _rtmail as mail
from _rtlib import format_mail_envelope, parse_mail_envelope, register_project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    root = tmp_path / "project"
    kit.write_project(root, (kit.CODEX, kit.CLAUDE), project='"."')
    register_project(root)
    return root


def send(project, body="Review this text", *, sender="claude", target="codex", kind="question"):
    return mail.mutate_mail(project, sender, "send", {"target": target, "kind": kind, "body": body})


def ack(project, refs, body="", *, sender="codex"):
    return mail.mutate_mail(project, sender, "ack", {"refs": refs, "body": body})


def inbox(project, agent, lifecycle="new"):
    return project / ".roundtable" / "inbox" / agent / lifecycle


def snapshot(project):
    root = project / ".roundtable" / "inbox"
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("body", ["", "--help", "--no-nudge", "--fenced",
                                 "line 1\n\"quoted\" 'single' $HOME $(touch ignored); & | > < `cmd`\n结束"])
def test_send_preserves_body_as_data(project, body):
    sent = send(project, body)
    assert sent["status"] == "committed"
    original = inbox(project, "codex") / (sent["message_id"] + ".md")
    assert parse_mail_envelope(original.read_text())["body"] == body
    assert len(list(inbox(project, "codex").glob("*.md"))) == 1


def test_each_send_is_a_new_delivery_not_automatic_retry(project):
    first, second = send(project), send(project)
    assert first["message_id"] != second["message_id"]
    assert len(list(inbox(project, "codex").glob("*.md"))) == 2


def test_send_reconciles_publication_when_response_is_lost(project, monkeypatch):
    real_publish = mail.publish_maildir

    def publish_then_lose(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise BrokenPipeError("synthetic publication response lost")

    monkeypatch.setattr(mail, "publish_maildir", publish_then_lose)
    result = send(project)
    assert result["status"] == "committed"
    assert result["message_id"]
    assert "response lost" in result["error"]
    assert len(list(inbox(project, "codex").glob("*.md"))) == 1


def test_send_preserves_known_id_when_outcome_cannot_be_proven(project, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable evidence")

    monkeypatch.setattr(mail, "publish_maildir", unavailable)
    result = send(project)
    assert result["status"] == "unknown"
    assert result["message_id"]
    assert not list(inbox(project, "codex").glob("*.md"))


@pytest.mark.parametrize("operation", ["send", "ack"])
def test_layout_guard_release_failure_keeps_committed_outcome(project, monkeypatch, operation):
    ref = send(project)["message_id"] if operation == "ack" else None
    real_guard = mail.locked_project_mailbox_checked

    @contextmanager
    def failed_release(*args, **kwargs):
        with real_guard(*args, **kwargs) as mailbox:
            yield mailbox
        raise OSError("synthetic layout close failure")

    monkeypatch.setattr(mail, "locked_project_mailbox_checked", failed_release)
    result = send(project) if operation == "send" else ack(project, [ref])
    assert result["status"] == "committed"
    assert "close failure" in result["error"]
    if operation == "send":
        assert (inbox(project, "codex") / (result["message_id"] + ".md")).exists()
    else:
        assert result["archived"] == [ref]
        assert result["receipts"][0]["message_id"]


@pytest.mark.parametrize("sender,target,kind", [
    ("claude", "codex@other", "question"), ("claude", "missing", "question"),
    ("claude", "claude", "question"), ("missing", "codex", "question"),
    ("claude", "codex", "sync-ack"), ("claude", "codex", "SYNC-ACK"),
    ("claude", "codex", "two words"), ("claude", "codex", "bad]"),
])
def test_send_invalid_route_or_kind_never_publishes(project, sender, target, kind):
    before = snapshot(project)
    with pytest.raises(ValueError):
        send(project, sender=sender, target=target, kind=kind)
    assert snapshot(project) == before


def test_send_rejects_other_harness(project):
    config = project / ".roundtable" / "agents.yaml"
    config.write_text(config.read_text().replace("harness: codex", "harness: hermes"))
    with pytest.raises(ValueError, match="supported harness"):
        send(project)


def test_ack_archives_exact_original_and_returns_receipt(project):
    ref = send(project)["message_id"]
    untouched = send(project, "leave pending")["message_id"]
    result = ack(project, [ref], "Reviewed: no spelling errors")
    assert result["status"] == "committed"
    assert result["refs"] == result["archived"] == [ref]
    assert (inbox(project, "codex", "cur") / (ref + ".md")).exists()
    assert not (inbox(project, "codex") / (ref + ".md")).exists()
    assert (inbox(project, "codex") / (untouched + ".md")).exists()
    receipt = result["receipts"][0]
    quiet = inbox(project, "claude") / ("ack-" + receipt["message_id"] + ".md")
    envelope = parse_mail_envelope(quiet.read_text())
    assert envelope["from"] == "codex" and envelope["to"] == "claude"
    assert envelope["kind"] == "sync-ack"
    assert envelope["body"] == f"refs={ref} Reviewed: no spelling errors"


def test_complete_ack_retry_does_not_send_another_receipt(project):
    ref = send(project)["message_id"]
    first = ack(project, [ref])
    before = snapshot(project)
    second = ack(project, [ref], "second annotation must not create a new ACK")
    assert second["status"] == "committed"
    assert second["archived"] == [ref]
    assert second["receipts"][0]["message_id"] == first["receipts"][0]["message_id"]
    assert snapshot(project) == before


def test_receipt_published_archive_failure_recovers_without_duplicate(project, monkeypatch):
    ref = send(project)["message_id"]
    real_archive = mail.archive_inbound

    def fail_archive(*args):
        raise OSError("synthetic archive failure")

    monkeypatch.setattr(mail, "archive_inbound", fail_archive)
    partial = ack(project, [ref])
    assert partial["status"] == "partial"
    assert partial["archived"] == []
    assert len(partial["receipts"]) == 1
    assert (inbox(project, "codex") / (ref + ".md")).exists()
    monkeypatch.setattr(mail, "archive_inbound", real_archive)
    recovered = ack(project, [ref])
    assert recovered["status"] == "committed"
    assert recovered["archived"] == [ref]
    assert recovered["receipts"][0]["message_id"] == partial["receipts"][0]["message_id"]
    assert len(list(inbox(project, "claude").glob("ack-*.md"))) == 1


def test_receipt_response_loss_still_archives_original(project, monkeypatch):
    ref = send(project)["message_id"]
    real_publish = mail.publish_maildir

    def publish_then_lose(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise BrokenPipeError("synthetic response lost")

    monkeypatch.setattr(mail, "publish_maildir", publish_then_lose)
    result = ack(project, [ref])
    assert result["status"] == "committed"
    assert result["archived"] == [ref]
    assert len(list(inbox(project, "claude").glob("ack-*.md"))) == 1


def test_receipt_unknown_does_not_archive_original(project, monkeypatch):
    ref = send(project)["message_id"]

    def unavailable(*args, **kwargs):
        raise OSError("synthetic transport loss")

    monkeypatch.setattr(mail, "publish_maildir", unavailable)
    result = ack(project, [ref])
    assert result["status"] == "unknown"
    assert result["archived"] == []
    assert result["receipts"][0]["message_id"]
    assert result["receipts"][0]["status"] == "unknown"
    assert (inbox(project, "codex") / (ref + ".md")).exists()


def test_archive_response_loss_is_reconciled(project, monkeypatch):
    ref = send(project)["message_id"]
    real_archive = mail.archive_inbound

    def archive_then_lose(*args):
        real_archive(*args)
        raise OSError("synthetic archive response lost")

    monkeypatch.setattr(mail, "archive_inbound", archive_then_lose)
    result = ack(project, [ref])
    assert result["status"] == "committed"
    assert result["archived"] == [ref]


def test_partial_batch_receipt_can_recover_a_subset(project, monkeypatch):
    refs = [send(project, str(index))["message_id"] for index in range(2)]
    real_archive = mail.archive_inbound

    def fail_archive(*args):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(mail, "archive_inbound", fail_archive)
    first = ack(project, refs)
    monkeypatch.setattr(mail, "archive_inbound", real_archive)
    assert first["status"] == "partial"
    for ref in refs:
        recovered = ack(project, [ref])
        assert recovered["status"] == "committed"
        assert recovered["receipts"][0]["refs"] == refs
    assert len(list(inbox(project, "claude").glob("ack-*.md"))) == 1


@pytest.mark.parametrize("bad_ref", ["../escape", "ack-20260101T000000Z-claude-to-codex-1",
                                     "20260101T000000Z-claude-to-claude-1",
                                     "20260101T000000Z-claude-to-codex-missing"])
def test_ack_batch_preflight_refuses_invalid_refs_before_any_write(project, bad_ref):
    good = send(project)["message_id"]
    before = snapshot(project)
    with pytest.raises(ValueError):
        ack(project, [good, bad_ref])
    assert snapshot(project) == before


@pytest.mark.parametrize("field,value", [("kind", "sync-ack"), ("to", "claude"),
                                         ("from", "codex"), ("msg_id", "other"),
                                         ("origin_uuid", "00000000-0000-4000-8000-000000000077")])
def test_ack_rejects_wrong_original_envelope(project, field, value):
    ref = send(project)["message_id"]
    path = inbox(project, "codex") / (ref + ".md")
    env = parse_mail_envelope(path.read_text())
    env[field] = value
    path.write_text(format_mail_envelope(env["from"], env["to"], env["kind"], env["msg_id"],
                                         env["body"], origin_uuid=env["origin_uuid"]))
    before = snapshot(project)
    with pytest.raises(ValueError):
        ack(project, [ref])
    assert snapshot(project) == before


def test_duplicate_receipt_evidence_is_ambiguous(project):
    ref = send(project)["message_id"]
    result = ack(project, [ref])
    quiet = next(inbox(project, "claude").glob("ack-*.md"))
    envelope = parse_mail_envelope(quiet.read_text())
    duplicate_id = mail._new_id("codex", "claude")
    duplicate = inbox(project, "claude") / ("ack-" + duplicate_id + ".md")
    duplicate.write_text(format_mail_envelope("codex", "claude", "sync-ack", duplicate_id,
                                             envelope["body"], origin_uuid=envelope["origin_uuid"]))
    before = snapshot(project)
    with pytest.raises(ValueError, match="multiple quiet receipts"):
        ack(project, [ref])
    assert snapshot(project) == before
    assert result["archived"] == [ref]


@pytest.mark.parametrize("field,value", [("from", "claude"), ("to", "codex"),
                                         ("origin_uuid", "00000000-0000-4000-8000-000000000077")])
def test_wrong_quiet_receipt_authority_blocks_retry(project, field, value):
    ref = send(project)["message_id"]
    ack(project, [ref])
    path = next(inbox(project, "claude").glob("ack-*.md"))
    env = parse_mail_envelope(path.read_text())
    env[field] = value
    path.write_text(format_mail_envelope(env["from"], env["to"], env["kind"], env["msg_id"],
                                         env["body"], origin_uuid=env["origin_uuid"]))
    before = snapshot(project)
    with pytest.raises(ValueError, match="requested ACK route"):
        ack(project, [ref])
    assert snapshot(project) == before


def test_interrupted_same_inode_archive_is_finished_without_receipt(project):
    ref = send(project)["message_id"]
    source = inbox(project, "codex") / (ref + ".md")
    destination = inbox(project, "codex", "cur") / source.name
    os.link(source, destination)
    result = ack(project, [ref])
    assert result["status"] == "committed"
    assert result["archived"] == [ref]
    assert not source.exists() and destination.exists()
    assert not list(inbox(project, "claude").glob("ack-*.md"))


def test_concurrent_native_ack_calls_publish_one_receipt(project):
    ref = send(project)["message_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ack(project, [ref]), range(2)))
    assert all(result["status"] == "committed" for result in results)
    assert len(list(inbox(project, "claude").glob("ack-*.md"))) == 1


def test_symlink_maildir_is_rejected_before_write(project, tmp_path):
    send(project)
    target = inbox(project, "codex")
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)
    before = {path.name: path.read_bytes() for path in moved.iterdir()}
    with pytest.raises(ValueError, match="real directory"):
        send(project)
    assert {path.name: path.read_bytes() for path in moved.iterdir()} == before
