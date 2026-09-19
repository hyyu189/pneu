"""Maildir operations shared by legacy CLIs and explicitly authorized native calls.

Native callers retain their own authorization guard around mutate_mail. This
module owns no identity authority, watcher, transport, or alternate mailbox.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import stat
import time

from _rtlib import (
    MailEnvelopeError, format_mail_envelope, load_agents_doc,
    locked_project_mailbox_checked, parse_mail_envelope, parse_message_id,
)

AGENT_COMPONENT_RE = re.compile(r"^[a-z0-9#_-]+$")
MAIL_KIND_RE = re.compile(r"^[^\s\]\x00]+$")


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def lstat_or_none(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def validate_agent_component(value, label):
    if not isinstance(value, str) or not AGENT_COMPONENT_RE.fullmatch(value):
        raise ValueError(f"invalid {label} agent component")


def configured_target(root, target, *, required=True, runtime=None):
    """Resolve a configured target to (base agent, concrete instance)."""
    doc = load_agents_doc(root, "rt-say")
    agents = doc.get("agents") or {}
    if not isinstance(agents, dict):
        raise SystemExit(
            "rt-say: invalid agents configuration: agents must be a mapping"
        )
    exact_candidates = []
    base_candidates = []
    valid_names = set()
    for base, config in agents.items():
        if not isinstance(base, str) or not AGENT_COMPONENT_RE.fullmatch(base):
            raise SystemExit(
                "rt-say: invalid agents configuration: invalid base agent "
                f"{base!r}"
            )
        base_name = base
        valid_names.add(base_name)
        if not isinstance(config, dict):
            raise SystemExit(
                "rt-say: invalid agents configuration for "
                f"{base_name}: expected a mapping"
            )
        instances = config.get("instances")
        if instances is None or instances == []:
            instances = [{"id": base_name}]
        elif not isinstance(instances, list):
            raise SystemExit(
                "rt-say: invalid agents configuration for "
                f"{base_name}: instances must be a list"
            )
        instance_names = []
        for instance in instances:
            if isinstance(instance, str):
                instance_name = instance
            elif isinstance(instance, dict):
                instance_id = instance.get("id")
                if not isinstance(instance_id, str) or not instance_id:
                    raise SystemExit(
                        "rt-say: invalid agents configuration for "
                        f"{base_name}: every instance needs an id"
                    )
                instance_name = instance_id
            else:
                raise SystemExit(
                    "rt-say: invalid agents configuration for "
                    f"{base_name}: instance entries must be strings or mappings"
                )
            if not AGENT_COMPONENT_RE.fullmatch(instance_name):
                raise SystemExit(
                    "rt-say: invalid agents configuration for "
                    f"{base_name}: invalid instance id {instance_name!r}"
                )
            instance_names.append(instance_name)
            valid_names.add(instance_name)
            if instance_name == target:
                exact_candidates.append((base_name, instance_name))
        if base_name == target:
            base_candidates.append((base_name, instance_names))
    if len(exact_candidates) == 1:
        return exact_candidates[0]
    if len(exact_candidates) > 1:
        matches = ", ".join(
            f"{base}/{instance}"
            for base, instance in sorted(exact_candidates)
        )
        raise SystemExit(
            f"rt-say: ambiguous configured agent or instance {target}: "
            f"{matches}"
        )
    if len(base_candidates) > 1:
        matches = ", ".join(base for base, _instances in base_candidates)
        raise SystemExit(
            f"rt-say: ambiguous configured base agent {target}: {matches}"
        )
    if len(base_candidates) == 1:
        base_name, instance_names = base_candidates[0]
        if len(instance_names) == 1:
            return base_name, instance_names[0]
        raise SystemExit(
            f"rt-say: {target} has multiple instances; use one of: "
            + ", ".join(sorted(set(instance_names)))
        )
    if not required and target in ((runtime or {}).get("agents") or {}):
        return target, target
    rendered = ", ".join(sorted(valid_names)) or "none configured"
    raise SystemExit(
        f"rt-say: unknown agent or instance: {target}; use one of: {rendered}"
    )


def ensure_inbox_gitignore(inbox_dir):
    """Atomically install the inbox-local ignore rule, repairing partial files."""
    ignore_path = inbox_dir / ".gitignore"
    tmp_path = inbox_dir / f".gitignore.tmp.{os.getpid()}.{time.time_ns()}"
    created_tmp = False
    try:
        descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created_tmp = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write("*\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, ignore_path)
        created_tmp = False
        fsync_directory(inbox_dir)
    except OSError:
        if created_tmp:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def publish_maildir(inbox_dir, target, msg_id, content, *, quiet_ack=False):
    """Publish one immutable maildir message via tmp -> new atomic rename."""
    validate_agent_component(target, "inbox target")
    filename = f"{'ack-' if quiet_ack else ''}{msg_id}.md"
    if Path(filename).name != filename or "/" in filename or "\x00" in filename:
        raise SystemExit(f"rt-say: invalid inbox message filename: {filename}")

    target_dir = inbox_dir / target
    tmp_dir = target_dir / "tmp"
    new_dir = target_dir / "new"
    cur_dir = target_dir / "cur"
    tmp_path = tmp_dir / filename
    new_path = new_dir / filename
    created_tmp = False
    try:
        for path in (tmp_dir, new_dir, cur_dir):
            path.mkdir(parents=True, exist_ok=True)
        ensure_inbox_gitignore(inbox_dir)
        descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created_tmp = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if new_path.exists():
            raise FileExistsError(f"destination already exists: {new_path}")
        os.rename(tmp_path, new_path)
        created_tmp = False
        fsync_directory(new_dir)
    except OSError as error:
        if created_tmp:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise SystemExit(f"rt-say: failed to publish inbox message {msg_id}: {error}") from error
    return new_path


def archive_inbound(mailbox, ref, recipient):
    """Move one exact inbound ref from new/ to cur/ without clobbering.

    A hard-link followed by unlink gives us a no-clobber publication and a
    recoverable intermediate state. If a prior attempt stopped between those
    operations, both names identify the same inode and this retry completes the
    unlink. A source that disappears after the preflight is a compatibility
    no-op only when the archive copy is already a regular file.
    """

    mail_root = mailbox.inbox_dir
    inbox = mail_root / recipient
    new_dir = inbox / "new"
    cur_dir = inbox / "cur"
    source = new_dir / f"{ref}.md"
    destination = cur_dir / f"{ref}.md"

    for directory in (mail_root, inbox, new_dir):
        if directory.is_symlink():
            raise OSError(f"maildir path is a symlink: {directory}")
        if directory.exists() and not directory.is_dir():
            raise OSError(f"maildir path is not a directory: {directory}")

    source_stat = lstat_or_none(source)
    destination_stat = lstat_or_none(destination)
    if source_stat is None:
        if destination_stat is None:
            raise FileNotFoundError(
                f"inbound mail disappeared before archive: {source}"
            )
        if not stat.S_ISREG(destination_stat.st_mode):
            raise OSError(f"archive destination is not a regular file: {destination}")
        return
    if not stat.S_ISREG(source_stat.st_mode):
        raise OSError(f"inbound mail is not a regular file: {source}")

    cur_dir.mkdir(parents=True, exist_ok=True)
    if cur_dir.is_symlink() or not cur_dir.is_dir():
        raise OSError(f"archive directory is not a real directory: {cur_dir}")

    destination_stat = lstat_or_none(destination)
    if destination_stat is not None:
        if not stat.S_ISREG(destination_stat.st_mode):
            raise OSError(f"archive destination is not a regular file: {destination}")
        if (source_stat.st_dev, source_stat.st_ino) != (
            destination_stat.st_dev,
            destination_stat.st_ino,
        ):
            raise FileExistsError(
                f"refusing to overwrite conflicting archive: {destination}"
            )
        fsync_directory(cur_dir)
    else:
        os.link(source, destination, follow_symlinks=False)
        fsync_directory(cur_dir)

    source.unlink()
    fsync_directory(new_dir)


def _native_seat(project, address):
    """Resolve only this project's single configured Claude or Codex seat."""
    from _rtlauncher import CONFIG_HARNESSES, SelectionError, configured_sender_ids

    validate_agent_component(address, "native seat")
    try:
        base, concrete = configured_target(project, address)
        config = load_agents_doc(project, "rt-native")["agents"][base]
        harness = next((name for name in ("claude", "codex")
                        if config.get("harness") in CONFIG_HARNESSES[name]), None)
        if harness is None or configured_sender_ids(project, harness) != [concrete]:
            raise ValueError("native mail requires exactly one configured seat per supported harness")
    except (SystemExit, SelectionError) as error:
        raise ValueError(str(error)) from error
    return concrete


def _real_directories(mailbox, agent):
    """Do not follow a maildir component replaced by a link."""
    inbox = mailbox.inbox_dir / agent
    for directory in (mailbox.inbox_dir, inbox, inbox / "tmp", inbox / "new", inbox / "cur"):
        info = lstat_or_none(directory)
        if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)):
            raise ValueError("native maildir component is not a real directory")
    return inbox


def _exact_copies(mailbox, agent, filename):
    """Read exact immutable new/cur evidence, rejecting conflicting copies."""
    inbox = _real_directories(mailbox, agent)
    copies = []
    for lifecycle in ("new", "cur"):
        path = inbox / lifecycle / filename
        info = lstat_or_none(path)
        if info is None:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("native mail evidence is not a regular file")
        copies.append((lifecycle, path, info))
    if len({(info.st_dev, info.st_ino) for _, _, info in copies}) > 1:
        raise ValueError("conflicting new/cur copies of native mail")
    if not copies:
        return None, set()
    return copies[0][1].read_text(encoding="utf-8"), {part for part, _, _ in copies}


def _new_id(sender, target):
    return (f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{sender}-to-{target}-{secrets.token_hex(12)}")


def _publish_observed(mailbox, sender, target, kind, body, *, quiet_ack=False):
    """Preserve the intended ID and reconcile the publication after an error.

    Maildir is the only commit witness. A thrown/lost publication response
    does not authorize another send. Missing or unreadable evidence after an
    attempted publication is unknown, even if no file is currently visible.
    """
    msg_id = _new_id(sender, target)
    content = format_mail_envelope(sender, target, kind, msg_id, body,
                                   origin_uuid=mailbox.project_uuid)
    result = {"status": "committed", "message_id": msg_id}
    try:
        publish_maildir(mailbox.inbox_dir, target, msg_id, content, quiet_ack=quiet_ack)
    except (Exception, SystemExit) as error:
        result["error"] = str(error)
        filename = f"{'ack-' if quiet_ack else ''}{msg_id}.md"
        try:
            observed, _ = _exact_copies(mailbox, target, filename)
            result["status"] = "committed" if observed == content else "unknown"
        except (OSError, ValueError):
            result["status"] = "unknown"
    return result


def _inbound(mailbox, project, agent, ref):
    identity = parse_message_id(ref, recipient=agent)
    if identity is None:
        raise ValueError("ACK requires an exact message ID addressed to this seat")
    sender, _ = identity
    if sender == agent or _native_seat(project, sender) != sender:
        raise ValueError("ACK original sender must be the other configured native seat")
    content, copies = _exact_copies(mailbox, agent, ref + ".md")
    if content is None:
        raise ValueError("ACK original message is missing from this seat")
    try:
        envelope = parse_mail_envelope(content)
    except MailEnvelopeError as error:
        raise ValueError("ACK original envelope is invalid") from error
    if (envelope is None or envelope["msg_id"] != ref
            or envelope["from"] != sender or envelope["to"] != agent
            or envelope["origin_uuid"] != mailbox.project_uuid):
        raise ValueError("ACK original envelope does not prove this project and seat")
    if envelope["kind"] == "sync-ack":
        raise ValueError("ACK-of-ACK is not permitted")
    return {"ref": ref, "sender": sender, "copies": copies}


def _receipt_evidence(mailbox, agent, original_sender, refs):
    """Find quiet receipts by exact envelope authority and refs, never ledger."""
    wanted = set(refs)
    inbox = _real_directories(mailbox, original_sender)
    filenames = set()
    for lifecycle in ("new", "cur"):
        directory = inbox / lifecycle
        if directory.exists():
            filenames.update(path.name for path in directory.glob("ack-*.md"))
    found = {}
    for filename in sorted(filenames):
        content, _ = _exact_copies(mailbox, original_sender, filename)
        try:
            envelope = parse_mail_envelope(content)
        except MailEnvelopeError as error:
            raise ValueError("quiet receipt evidence has an invalid envelope") from error
        if envelope is None:
            raise ValueError("quiet receipt evidence has an invalid envelope")
        match = re.match(r"^refs=([^\s]+)(?:\s|$)", envelope["body"])
        receipt_refs = match.group(1).split(",") if match else []
        overlap = wanted.intersection(receipt_refs)
        if not overlap:
            continue
        if (envelope["kind"] != "sync-ack" or envelope["from"] != agent
                or envelope["to"] != original_sender
                or envelope["origin_uuid"] != mailbox.project_uuid
                or filename != f"ack-{envelope['msg_id']}.md"
                or parse_message_id(envelope["msg_id"], recipient=original_sender)
                != (agent, original_sender)
                or len(receipt_refs) != len(set(receipt_refs))
                or any(parse_message_id(ref, recipient=agent) != (original_sender, agent)
                       for ref in receipt_refs)):
            raise ValueError("quiet receipt does not prove the requested ACK route")
        receipt = {"message_id": envelope["msg_id"], "refs": receipt_refs}
        for ref in overlap:
            if ref in found and found[ref]["message_id"] != receipt["message_id"]:
                raise ValueError("multiple quiet receipts claim the same ACK ref")
            found[ref] = receipt
    return found


def _ack(mailbox, project, agent, refs, body):
    # Preflight the entire batch before the first receipt or archive mutation.
    inbound = [_inbound(mailbox, project, agent, ref) for ref in refs]
    by_sender = defaultdict(list)
    for item in inbound:
        by_sender[item["sender"]].append(item["ref"])
    evidence = {}
    for sender, group_refs in by_sender.items():
        evidence.update(_receipt_evidence(mailbox, agent, sender, group_refs))
    result = {"status": "committed", "refs": refs, "receipts": [], "archived": []}
    seen_receipts = set()
    for receipt in evidence.values():
        if receipt["message_id"] not in seen_receipts:
            result["receipts"].append(receipt)
            seen_receipts.add(receipt["message_id"])
    # An exact cur copy already proves completed archival. Preserve the legacy
    # retry contract even if its receipt has since been removed externally.
    result["archived"] = [item["ref"] for item in inbound if item["copies"] == {"cur"}]
    groups = defaultdict(list)
    for item in inbound:
        if "cur" not in item["copies"] and item["ref"] not in evidence:
            groups[item["sender"]].append(item["ref"])
    for target, group_refs in groups.items():
        visible_body = "refs=" + ",".join(group_refs)
        if body:
            visible_body += " " + body
        publication = _publish_observed(mailbox, agent, target, "sync-ack", visible_body,
                                        quiet_ack=True)
        receipt = {"message_id": publication["message_id"], "refs": group_refs,
                   "status": publication["status"]}
        result["receipts"].append(receipt)
        if publication.get("error"):
            result["error"] = publication["error"]
        if publication["status"] != "committed":
            result["status"] = "partial" if evidence or result["archived"] else "unknown"
            return result
        for ref in group_refs:
            evidence[ref] = receipt
    for item in inbound:
        if item["copies"] == {"cur"}:
            continue
        try:
            archive_inbound(mailbox, item["ref"], agent)
        except (Exception, SystemExit) as error:
            # Archival can have committed before its final fsync/response.
            try:
                _, copies = _exact_copies(mailbox, agent, item["ref"] + ".md")
                if copies == {"cur"}:
                    result["archived"].append(item["ref"])
            except (OSError, ValueError):
                pass
            result["status"] = "committed" if set(result["archived"]) == set(refs) else "partial"
            result["error"] = str(error)
            return result
        result["archived"].append(item["ref"])
    return result


def mutate_mail(project, agent, operation, parameters):
    """Mutate under the caller's native guard and a single project layout lock.

    Validation failures raise ValueError before any mail write. Once publish
    starts, results retain known IDs and committed/partial/unknown evidence.
    The exclusive layout section serializes competing native mutations and
    never nests another mailbox or runtime lock. There is no native wake.
    """
    project = Path(project).resolve()
    if not isinstance(parameters, dict):
        raise ValueError("native mail parameters must be an object")
    if _native_seat(project, agent) != agent:
        raise ValueError("native sender must be its exact configured seat")
    body = parameters.get("body")
    if not isinstance(body, str) or "\0" in body:
        raise ValueError("native message body must be text without NUL")
    if operation == "send":
        if set(parameters) != {"target", "kind", "body"}:
            raise ValueError("send requires target, kind and body")
        target = _native_seat(project, parameters["target"])
        if target == agent:
            raise ValueError("native self-send is not permitted")
        kind = parameters["kind"]
        if not isinstance(kind, str) or not MAIL_KIND_RE.fullmatch(kind):
            raise ValueError("native message kind must be one non-empty token")
        if kind.lower() == "sync-ack":
            raise ValueError("sync-ack is reserved for explicit ACK")
    elif operation == "ack":
        if set(parameters) != {"refs", "body"}:
            raise ValueError("ACK requires refs and body")
        refs = parameters["refs"]
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError("ACK refs must be a non-empty list of distinct message IDs")
        if any(part.startswith("refs=") for part in body.split()):
            raise ValueError("ACK note must not contain a refs token")
        for ref in refs:
            if parse_message_id(ref, recipient=agent) is None:
                raise ValueError("ACK requires exact original message IDs for this seat")
    else:
        raise ValueError("unsupported native mail operation")
    result = None
    try:
        with locked_project_mailbox_checked(project, exclusive=True) as mailbox:
            _real_directories(mailbox, agent)
            if operation == "send":
                _real_directories(mailbox, target)
                result = dict(_publish_observed(mailbox, agent, target, kind, body),
                              target=target, kind=kind)
            else:
                result = _ack(mailbox, project, agent, refs, body)
    except (Exception, SystemExit) as error:
        if result is None:
            raise
        # Unlock/close failure cannot erase the already observed mail commit.
        previous = result.get("error")
        result["error"] = (previous + "; " if previous else "") + str(error)
    return result
