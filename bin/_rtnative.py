"""Explicit native-root inbox reads using the existing seat capability/fence.

The CLI establishes harness-specific per-call root identity. This module
associates that identity with the existing lease and reads durable mail only
while the association remains valid. It does not infer identity from cwd,
transcripts, a legacy wake binding, or a complete inherited RT_* environment.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from _rtlauncher import SelectionError, configured_sender_ids
from _rtruntime import (
    FenceRejected,
    RuntimeStateError,
    SeatAmbiguous,
    SeatOccupied,
    _ensure_private_dir,
    _existing_shared_lock,
    _locked,
    canonical_project,
    claim,
    inspect_seat,
    inspect_host_harness_seats,
    load_validated_lease,
    process_start_fingerprint,
    read_seat_capability,
    record_seat_capability,
    runtime_root,
    seat_shared_guard,
    validate_native_query_binding,
)


SCHEMA = "roundtable.native-query.v1"
HOST_BIND_LOCK = "native-query-bind.lock"


class NativeQueryError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _identity(project, harness, native_id):
    if harness not in {"claude", "codex"}:
        raise NativeQueryError("unsupported", "native harness is unsupported")
    if not isinstance(native_id, str) or not native_id or "\0" in native_id:
        raise NativeQueryError("unbound", "native root identity is missing")
    canonical = canonical_project(project)
    try:
        candidates = configured_sender_ids(canonical, harness)
    except (SelectionError, SystemExit, OSError) as error:
        raise NativeQueryError("unbound", "project has no valid native seat configuration") from error
    if len(candidates) != 1:
        raise NativeQueryError("unbound", "project must configure exactly one seat for this harness")
    return canonical, candidates[0]


def _environment_matches(project, token, environ):
    values = os.environ if environ is None else environ
    expected = {
        "RT_FROM": token.agent_id,
        "RT_SESSION_ID": token.session_id,
        "RT_LEASE_REVISION": token.revision,
    }
    for name, wanted in expected.items():
        if name in values and values[name] != wanted:
            raise NativeQueryError("conflict", f"{name} conflicts with the current native seat")
    for name in ("RT_PROJECT_ROOT", "ROUNDTABLE_PROJECT_DIR"):
        if name in values:
            raw = values[name]
            if not raw or Path(raw).expanduser().resolve() != project:
                raise NativeQueryError("conflict", f"{name} conflicts with the native project")


def _bound_capability(project, agent, harness, native_id, *, owner_pid=None):
    record = read_seat_capability(project, agent)
    if record is None or record.get("nativeQuery") is None:
        raise NativeQueryError("unbound", "native root binding is missing")
    native = record["nativeQuery"]
    if record["harness"] != harness or native["harness"] != harness:
        raise NativeQueryError("conflict", "native harness conflicts with the seat")
    if native["nativeSessionId"] != native_id:
        raise NativeQueryError("conflict", "calling native root does not own this seat")
    if owner_pid is not None and native["ownerPid"] != owner_pid:
        raise NativeQueryError("conflict", "calling native owner process does not own this seat")
    token = load_validated_lease(
        project, agent, record["roundtableSessionId"], record["leaseRevision"]
    )
    # Existing wake integration stores the native Codex thread in both the
    # capability's threadId and the lease's wake.nativeSessionId. These are
    # optional corroborating identities, never an alternative authorization
    # path. A legacy writer must not give this lease a second native owner.
    for field, value in (
        ("capability threadId", record.get("threadId")),
        ("lease wake.nativeSessionId", (token.record.get("wake") or {}).get("nativeSessionId")),
    ):
        if value is not None and value != native_id:
            raise NativeQueryError("conflict", f"{field} conflicts with the native root binding")
    if (
        token.harness != harness
        or native["ownerPid"] != token.owner_pid
        or native["ownerStart"] != token.owner_start
    ):
        raise NativeQueryError("stale", "native binding no longer matches the lease owner")
    return record, token


def _result(record, token):
    return {
        "schema": SCHEMA,
        "binding": {
            "status": "bound",
            "harness": token.harness,
            "nativeSessionId": record["nativeQuery"]["nativeSessionId"],
            "agentId": token.agent_id,
        },
        "lease": {
            "status": "active",
            "sessionId": token.session_id,
            "revision": token.revision,
        },
        "native_busy": "unknown",
    }


def _assert_unique_native_root(project, agent, harness, native_id):
    """Check the existing authority records; there is no second binding index.

    Caller holds the host native-binding lock before any project guard. Dead
    owner generations and superseded capabilities have no binding authority.
    An equal native ID on a different live owner is still a conflict: a PID
    cannot expand one native root into two projects.
    """
    for inspection in inspect_host_harness_seats(harness):
        token = inspection.token
        if token is None or inspection.status in {"vacant", "stale"}:
            continue
        record = read_seat_capability(token.project_root, token.agent_id)
        native = (record or {}).get("nativeQuery")
        if native is None or native["nativeSessionId"] != native_id:
            continue
        if (
            record["roundtableSessionId"] != token.session_id
            or record["leaseRevision"] != token.revision
        ):
            continue
        if inspection.status == "ambiguous":
            raise NativeQueryError("unsupported", "native root has an unverifiable host binding")
        if token.project_root != project or token.agent_id != agent:
            raise NativeQueryError("conflict", "native root is already bound to another project or seat")


def bind_native(
    project: Path | str,
    harness: str,
    native_id: str,
    owner_pid: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind a trusted native root; never displace an active owner.

    Claude SessionStart or explicit Codex native-shell bind is the mutation
    boundary. Query paths never call this function, claim a lease, renew one,
    or create a missing association.
    Host binding serialization precedes every project claim guard.
    """
    try:
        canonical, agent = _identity(project, harness, native_id)
        root = runtime_root()
        _ensure_private_dir(root)
        with _locked(root / HOST_BIND_LOCK):
            _assert_unique_native_root(canonical, agent, harness, native_id)
            return _bind_native_locked(
                canonical, harness, native_id, owner_pid, environ=environ
            )
    except NativeQueryError:
        raise
    except (RuntimeStateError, OSError, ValueError) as error:
        raise NativeQueryError("unsupported", str(error)) from error


def _bind_native_locked(project, harness, native_id, owner_pid, *, environ):
    try:
        canonical, agent = _identity(project, harness, native_id)
        fingerprint = process_start_fingerprint(owner_pid)
        native = validate_native_query_binding({
            "harness": harness,
            "nativeSessionId": native_id,
            "source": "session-start" if harness == "claude" else "native-shell",
            "ownerPid": owner_pid,
            "ownerStart": fingerprint,
        })
        inspection = inspect_seat(canonical, agent)
        if inspection.status in {"active_healthy", "active_unhealthy"}:
            # This is idempotence only: an active legacy/wake lease or another
            # root cannot be upgraded through a coincident PID or cwd.
            record, token = _bound_capability(
                canonical, agent, harness, native_id, owner_pid=owner_pid
            )
            if record["nativeQuery"] != native:
                raise NativeQueryError("conflict", "native owner generation changed")
            with seat_shared_guard(canonical, agent, token.session_id, token.revision):
                record, token = _bound_capability(
                    canonical, agent, harness, native_id, owner_pid=owner_pid
                )
                _environment_matches(canonical, token, environ)
                return _result(record, token)
        if inspection.status == "ambiguous":
            raise NativeQueryError("unsupported", "current seat ownership cannot be verified")
        # A supplied stale or conflicting ambient fence must not create a new
        # seat on its way to failing. A native hook normally has no RT fence.
        values = os.environ if environ is None else environ
        if any(name in values for name in ("RT_SESSION_ID", "RT_LEASE_REVISION")):
            raise NativeQueryError("conflict", "ambient fence has no matching active native binding")
        if "RT_FROM" in values and values["RT_FROM"] != agent:
            raise NativeQueryError("conflict", "RT_FROM conflicts with configured native seat")
        for name in ("RT_PROJECT_ROOT", "ROUNDTABLE_PROJECT_DIR"):
            if name in values and (
                not values[name] or Path(values[name]).expanduser().resolve() != canonical
            ):
                raise NativeQueryError("conflict", f"{name} conflicts with native project")
        token = claim(canonical, agent, harness, owner_pid=owner_pid)
        with seat_shared_guard(canonical, agent, token.session_id, token.revision):
            record_seat_capability(
                canonical,
                agent,
                harness,
                session_id=token.session_id,
                revision=token.revision,
                native_query=native,
                claim_lock_held=True,
            )
            record, token = _bound_capability(
                canonical, agent, harness, native_id, owner_pid=owner_pid
            )
            return _result(record, token)
    except NativeQueryError:
        raise
    except SeatOccupied as error:
        raise NativeQueryError("conflict", "native seat is already occupied") from error
    except FenceRejected as error:
        raise NativeQueryError("stale", str(error)) from error
    except (SeatAmbiguous, RuntimeStateError, OSError, ValueError) as error:
        raise NativeQueryError("unsupported", str(error)) from error


def _read_inbox(project, agent, environ):
    child_env = dict(os.environ)
    if environ is not None:
        child_env.update(environ)
    child_env["ROUNDTABLE_PROJECT_DIR"] = str(project)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("rt-inbox")), agent, "-f", "json"],
        cwd=project,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise NativeQueryError("unsupported", "durable inbox could not be read: " + completed.stderr.strip())
    try:
        messages = json.loads(completed.stdout)
    except ValueError as error:
        raise NativeQueryError("unsupported", "durable inbox returned invalid data") from error
    if not isinstance(messages, list):
        raise NativeQueryError("unsupported", "durable inbox returned invalid data")
    return messages


def query(
    project: Path | str,
    harness: str,
    native_id: str,
    operation: str,
    *,
    environ: Mapping[str, str] | None = None,
    owner_pid: int | None = None,
) -> dict[str, Any]:
    """Read status/inbox under the current fence without changing mail state."""
    if operation not in {"inbox", "status"}:
        raise NativeQueryError("unsupported", "native query operation is unsupported")
    try:
        canonical, agent = _identity(project, harness, native_id)
        record, token = _bound_capability(
            canonical, agent, harness, native_id, owner_pid=owner_pid
        )
        # Existing-lock acquisition never creates or repairs runtime state.
        # Reading under the same host lock catches pre-existing conflicting
        # mappings and excludes a native bind until the snapshot is complete.
        with _existing_shared_lock(runtime_root() / HOST_BIND_LOCK):
            _assert_unique_native_root(canonical, agent, harness, native_id)
            with seat_shared_guard(canonical, agent, token.session_id, token.revision):
                record, token = _bound_capability(
                    canonical, agent, harness, native_id, owner_pid=owner_pid
                )
                _environment_matches(canonical, token, environ)
                result = _result(record, token)
                if operation == "inbox":
                    result["messages"] = _read_inbox(canonical, agent, environ)
                current, _ = _bound_capability(
                    canonical, agent, harness, native_id, owner_pid=owner_pid
                )
                if current["nativeQuery"] != record["nativeQuery"]:
                    raise NativeQueryError("stale", "native binding changed during the query")
                return result
    except NativeQueryError:
        raise
    except FenceRejected as error:
        raise NativeQueryError("stale", str(error)) from error
    except (RuntimeStateError, OSError, ValueError) as error:
        raise NativeQueryError("unsupported", str(error)) from error
