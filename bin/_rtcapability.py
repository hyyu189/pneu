"""Generic seat-capability resolution for daemon-executed pneu tools.

A Codex client and the tool processes it spawns live in different process
trees: the app-server owns the tools, so ambient ``RT_*`` fence variables never
survive the trip.  Identity is therefore bound out-of-band at launch and
resolved here by lookup:

    native ``CODEX_THREAD_ID`` -> exact thread binding -> active lease
    -> seat-capability record (surface)

Every step is revalidated at every use.  A superseded lease, a changed
``bindingRevision``, a project/cwd mismatch, or a thread that is not the bound
seat thread (a ``/btw`` side child, a fork, a fresh thread) resolves to
nothing rather than to a weaker capability.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from _rtruntime import (
    FenceRejected,
    LeaseToken,
    RuntimeStateError,
    canonical_project,
    inspect_seat,
    load_validated_lease,
    read_seat_capability,
    runtime_root,
)


CODEX_WAKE_STATE_SCHEMA = "roundtable.codex-wake.v1"
CODEX_WAKE_STATE_NAME = "rt-codex-wake-state.json"
CODEX_WAKE_STATE_MAX_BYTES = 4 * 1024 * 1024
NATIVE_THREAD_ENV = "CODEX_THREAD_ID"
FENCE_ENV_NAMES = (
    "RT_PROJECT_ROOT",
    "RT_FROM",
    "RT_SESSION_ID",
    "RT_LEASE_REVISION",
)


class CapabilityError(RuntimeError):
    """Seat capability cannot be resolved from the current host state."""


class CapabilityUnavailable(CapabilityError):
    """No native identity is present, so there is nothing to resolve."""


@dataclass(frozen=True)
class SeatCapability:
    """One resolved seat identity plus its optional surface capability.

    ``thread_id`` and ``binding_revision`` are absent for a harness whose
    session-local transport already carried the fence; they are always present
    when identity was resolved from a native Codex thread.
    """

    token: LeaseToken
    thread_id: str | None = None
    binding_revision: str | None = None
    surface: dict[str, str] | None = None

    @property
    def project_root(self) -> Path:
        return self.token.project_root

    @property
    def agent_id(self) -> str:
        return self.token.agent_id

    def environment(self) -> dict[str, str]:
        return {
            "RT_PROJECT_ROOT": str(self.token.project_root),
            "RT_FROM": self.token.agent_id,
            "RT_SESSION_ID": self.token.session_id,
            "RT_LEASE_REVISION": str(self.token.revision),
        }


def codex_wake_state_path(root: Path | None = None) -> Path:
    return (runtime_root() if root is None else Path(root)) / CODEX_WAKE_STATE_NAME


def _read_state(path: Path) -> dict[str, Any] | None:
    """Read the bridge's binding state without following an unsafe path."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CapabilityError(f"cannot read Codex binding state {path}: {error}") from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CapabilityError(f"Codex binding state is not a regular file: {path}")
            if info.st_uid != os.getuid():
                raise CapabilityError(
                    f"Codex binding state owner uid {info.st_uid} != {os.getuid()}: {path}"
                )
            if info.st_size > CODEX_WAKE_STATE_MAX_BYTES:
                raise CapabilityError(f"Codex binding state is oversized: {path}")
            raw = handle.read(CODEX_WAKE_STATE_MAX_BYTES + 1)
    except OSError as error:
        raise CapabilityError(f"cannot read Codex binding state {path}: {error}") from error
    if len(raw) > CODEX_WAKE_STATE_MAX_BYTES:
        raise CapabilityError(f"Codex binding state is oversized: {path}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CapabilityError(f"invalid Codex binding state {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != CODEX_WAKE_STATE_SCHEMA:
        raise CapabilityError(f"invalid Codex binding state schema in {path}")
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        raise CapabilityError(f"invalid Codex binding table in {path}")
    return payload


def codex_binding(
    project: Path | str,
    *,
    state_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return the stored binding for one exact project, or ``None``."""

    canonical = canonical_project(project)
    payload = _read_state(
        codex_wake_state_path() if state_path is None else Path(state_path)
    )
    if payload is None:
        return None
    binding = payload.get("bindings", {}).get(str(canonical))
    if not isinstance(binding, dict):
        return None
    if binding.get("project") != str(canonical):
        raise CapabilityError(
            f"stored Codex binding does not describe {canonical}"
        )
    return dict(binding)


def _binding_text(binding: Mapping[str, Any], name: str) -> str:
    value = binding.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        raise CapabilityError(f"stored Codex binding field {name} is invalid")
    return value


def resolve_codex_capability(
    project: Path | str,
    thread_id: str,
    *,
    state_path: Path | None = None,
) -> SeatCapability:
    """Resolve one native Codex thread to its fenced seat, or fail closed."""

    if not isinstance(thread_id, str) or not thread_id or "\0" in thread_id:
        raise CapabilityError("native Codex thread id is invalid")
    canonical = canonical_project(project)
    binding = codex_binding(canonical, state_path=state_path)
    if binding is None:
        raise CapabilityError(
            f"no Codex thread binding is recorded for {canonical}"
        )
    bound_thread = _binding_text(binding, "threadId")
    if bound_thread != thread_id:
        # A /btw side child, an ordinary fork, or a second native thread in the
        # same cwd is not the seat.  The control boundary is the exact bound
        # thread, so this resolves to nothing rather than to the seat.
        raise CapabilityError(
            f"thread {thread_id} is not the bound Codex seat thread for {canonical}"
        )
    agent_id = _binding_text(binding, "agent")
    binding_revision = _binding_text(binding, "bindingRevision")
    session_id = binding.get("roundtableSessionId")
    lease_revision = binding.get("leaseRevision")
    if not isinstance(session_id, str) or not session_id or lease_revision is None:
        raise CapabilityError(
            f"stored Codex binding for {canonical} carries no lease identity; "
            "relaunch the seat with rt-codex so the binding is fenced"
        )
    try:
        token = load_validated_lease(
            canonical,
            agent_id,
            session_id,
            str(lease_revision),
        )
        inspection = inspect_seat(canonical, agent_id)
    except FenceRejected as error:
        raise CapabilityError(
            f"Codex seat lease for {canonical} is superseded or stale: {error}"
        ) from error
    except (OSError, RuntimeStateError) as error:
        raise CapabilityError(
            f"cannot validate the Codex seat lease for {canonical}: {error}"
        ) from error
    if token.harness != "codex":
        raise CapabilityError(
            f"bound seat for {canonical} is harness {token.harness!r}, not codex"
        )
    if not inspection.status.startswith("active_") or inspection.token is None:
        raise CapabilityError(
            f"bound Codex seat for {canonical} is not active: {inspection.status} "
            f"({inspection.detail})"
        )
    if (
        inspection.token.session_id != token.session_id
        or str(inspection.token.revision) != str(token.revision)
    ):
        raise CapabilityError(
            f"Codex seat lease for {canonical} changed during validation"
        )

    surface = None
    try:
        record = read_seat_capability(canonical, agent_id)
    except (OSError, RuntimeStateError) as error:
        raise CapabilityError(
            f"seat capability record for {canonical} is unusable: {error}"
        ) from error
    if record is not None:
        # Absence is tolerated (a binding created before this record existed is
        # still fenced by the lease), but a record that disagrees with the live
        # binding or lease is a fail-closed conflict, never a downgrade.
        recorded_thread = record.get("threadId")
        recorded_binding = record.get("bindingRevision")
        if (
            record.get("roundtableSessionId") != token.session_id
            or str(record.get("leaseRevision")) != str(token.revision)
            or (recorded_thread is not None and recorded_thread != thread_id)
            or (
                recorded_binding is not None
                and recorded_binding != binding_revision
            )
        ):
            raise CapabilityError(
                f"seat capability record for {canonical} does not match the "
                "current binding and lease"
            )
        surface = record.get("surface")
    return SeatCapability(
        token=token,
        thread_id=thread_id,
        binding_revision=binding_revision,
        surface=surface,
    )


def native_thread_id(environ: Mapping[str, str] | None = None) -> str | None:
    selected = os.environ if environ is None else environ
    value = selected.get(NATIVE_THREAD_ENV, "")
    rendered = value.strip() if isinstance(value, str) else ""
    return rendered or None


def resolve_native_capability(
    project: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    state_path: Path | None = None,
) -> SeatCapability:
    """Resolve this tool process's seat from its native thread identity."""

    thread_id = native_thread_id(environ)
    if thread_id is None:
        raise CapabilityUnavailable(
            f"{NATIVE_THREAD_ENV} is not set in this process"
        )
    return resolve_codex_capability(project, thread_id, state_path=state_path)


def resolve_ambient_capability(
    project: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> SeatCapability:
    """Resolve a seat from ambient fence variables that survived transit.

    Claude and Hermes seats keep their launcher environment, so their fence is
    already in the process.  It is still revalidated against the live lease
    here; an inherited variable is a claim, never proof.
    """

    selected = os.environ if environ is None else environ
    canonical = canonical_project(project)
    agent_id = (selected.get("RT_FROM") or "").strip().lower()
    session_id = (selected.get("RT_SESSION_ID") or "").strip()
    revision = (selected.get("RT_LEASE_REVISION") or "").strip()
    if not (agent_id and session_id and revision):
        raise CapabilityUnavailable(
            "no ambient seat fence is present in this process"
        )
    configured_root = (selected.get("RT_PROJECT_ROOT") or "").strip()
    if configured_root and Path(configured_root) != canonical:
        raise CapabilityError(
            f"fenced project {configured_root} does not match {canonical}"
        )
    try:
        token = load_validated_lease(canonical, agent_id, session_id, revision)
        inspection = inspect_seat(canonical, agent_id)
    except FenceRejected as error:
        raise CapabilityError(
            f"seat lease for {agent_id!r} in {canonical} is superseded or "
            f"stale: {error}"
        ) from error
    except (OSError, RuntimeStateError) as error:
        raise CapabilityError(
            f"cannot validate the seat lease for {canonical}: {error}"
        ) from error
    if not inspection.status.startswith("active_"):
        raise CapabilityError(
            f"seat {agent_id!r} in {canonical} is not active: "
            f"{inspection.status} ({inspection.detail})"
        )
    try:
        record = read_seat_capability(canonical, agent_id)
    except (OSError, RuntimeStateError) as error:
        raise CapabilityError(
            f"seat capability record for {canonical} is unusable: {error}"
        ) from error
    surface = None
    thread_id = None
    binding_revision = None
    if record is not None:
        if record.get("roundtableSessionId") != token.session_id or str(
            record.get("leaseRevision")
        ) != str(token.revision):
            raise CapabilityError(
                f"seat capability record for {canonical} belongs to another "
                "lease generation"
            )
        surface = record.get("surface")
        thread_id = record.get("threadId")
        binding_revision = record.get("bindingRevision")
    return SeatCapability(
        token=token,
        thread_id=thread_id,
        binding_revision=binding_revision,
        surface=surface,
    )


def resolve_seat_capability(
    project: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    state_path: Path | None = None,
) -> SeatCapability:
    """Resolve this process's seat by whichever transport actually reached it."""

    if native_thread_id(environ) is not None:
        return resolve_native_capability(
            project,
            environ=environ,
            state_path=state_path,
        )
    return resolve_ambient_capability(project, environ=environ)


def backfill_environment(
    capability: SeatCapability,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Publish the resolved fence into this single process only.

    Nothing is written to the daemon's global environment or to a thread
    config: the resolved values are re-derived on every call, so a lease
    rotation is visible immediately instead of surviving in a snapshot.
    """

    selected = os.environ if environ is None else environ
    values = capability.environment()
    selected.update(values)
    return values
