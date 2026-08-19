# Independent architecture review for 1.4 (Codex)

> Status: current — the independent 1.4 review; read with its own cross-check, which refines it and wins where the two differ

## Scope and method

This review was completed before reading either `architecture-review-1.4.md` or
`architecture-review-1.4-track-collisions.md`.

I mapped every production and test file under `bin/`, `pneu_packaging/`,
`integrations/`, and `tests/`; enumerated imports, top-level definitions, file
sizes, and the longest functions/classes; and then read the implementation
paths named below. I read the mail send/inbox/ack path, registry/mailbox and
runtime APIs, launcher and onboarding flows, setup/install transaction engine,
Codex service/transport boundaries, all three integration entry points, and
the structure/helpers/test names of the large test modules. I did not read all
roughly 34,000 test lines or all of every state machine. I inspected
`_rtmigrate.py` only at the module/import-map level and make no proposal about
it, in accordance with the 2026-07-29 freeze.

The rankings below optimize first for safety and the cost of adding a harness,
then for deletion of accidental coupling. They are behavior-preserving and add
no dependency.

## Ranked findings

### 1. Put maildir publication and archival behind one safety kernel

**Anchors:** `bin/rt-say:588-657`, `bin/rt-inbox:350-412`,
`bin/rt-ack:79-279`, `bin/_rtlib.py:625-660`.

**Current shape.** The authoritative mailbox and its layout lock are correctly
centralized in `_rtlib`, but three commands independently implement the
filesystem commit beneath that lock. `rt-say` performs tmp/write/fsync/rename;
`rt-inbox` hard-links and unlinks quiet acknowledgements; `rt-ack` implements a
second, slightly richer hard-link/unlink archive operation. All three also own
directory-fsync helpers. The implementations are intentionally defensive, but
the safety contract is distributed: future changes must keep no-clobber,
same-inode retry, symlink rejection, fsync ordering, and error semantics aligned
by hand.

**Proposed shape.** Add a small internal module, for example
`_rtmaildir.py`, whose public operations are `publish_new(...)` and
`archive_new(...)`. Callers must pass a `ProjectMailbox` obtained while holding
the existing layout lock; the new module must not resolve layouts or acquire
locks itself. Preserve the exact current tmp-to-new atomic publication and
hard-link-then-unlink retry protocol. Parameterize only error type/tool label
and quiet filename policy. Make `rt-say`, `rt-inbox`, and `rt-ack` thin policy
callers. Do not generalize the fd/ownership checks or relax any admission gate.

**Blast radius.** New `bin/_rtmaildir.py`; `bin/rt-say`, `bin/rt-inbox`, and
`bin/rt-ack`; packaging module lists and installed-module checks in
`pyproject.toml`, `pneu_packaging/cli.py`, and `tests/test_packaging.py`;
maildir cases in `tests/test_rt_tooling.py` and
`tests/test_rt_tripwire_runtime.py`. Keep the registry/layout-lock tests
unchanged as black-box guards.

**Effort:** M. **Risk:** high unless done as literal code motion with
fault-injection tests. This is ranked first because divergent commit machinery
is a larger long-term risk than file size, not because the present algorithms
should be simplified.

### 2. Remove the keyboard compatibility implementation from the durable-send command body

**Anchors:** `bin/rt-say:659-941`, `bin/rt-say:945-1368`, and especially the
391-line dispatcher at `bin/rt-say:978-1368`.

**Current shape.** `rt-say` is both the core maildir sender and the archived
cmux keyboard adapter. Its main locked function parses both modes, establishes
identity for both, publishes and records durable mail, then returns early; the
second half resolves surfaces, watches cmux events, injects text, presses a key,
and writes legacy ledger transitions. Even though normal delivery does not use
cmux, the optional adapter's state, helpers, imports, and failure-mode global
remain in the core command.

**Proposed shape.** Keep the public `rt-say --legacy-nudge-only` compatibility
switch, but dispatch it immediately to a separate `_rtlegacy_nudge.py` module.
The default `rt-say` path should parse, authenticate, resolve one address,
publish through the maildir kernel, and persist the ledger/alarm. The legacy
module may reuse pure envelope/record constructors but must never be imported
on the normal path. Preserve legacy exit code 3 and every existing keyboard
behavior until the compatibility path is intentionally retired.

**Blast radius.** `bin/rt-say`; new `bin/_rtlegacy_nudge.py`; packaging lists;
the legacy/correlation cases in `tests/test_rt_tooling.py` and the baseline
guards in `tests/test_optional_cmux_adapter.py` and `pneu_packaging/smoke.py`.
Do this after finding 1, not concurrently, so maildir movement and legacy
movement do not obscure each other.

**Effort:** M. **Risk:** medium. This directly reinforces the product rule that
cmux is optional and makes the normal transport easier to reason about.

### 3. Establish one canonical harness specification for launcher and onboarding metadata

**Anchors:** `bin/_rtlauncher.py:36-77`, `bin/pneu:42-67`,
`bin/pneu:636-681`, `bin/pneu:749-897`, and
`pneu_packaging/setup.py:36-48`.

**Current shape.** Harness command names, labels, aliases, executable override
variables, configuration spellings, display order, add-seat spelling, anchor
rules, and setup eligibility are spread across several dictionaries and
conditionals. `pneu` imports two launcher maps but still maintains its own
`HARNESS_ORDER` and `HARNESS_CONFIG`; setup keeps a third, deliberately smaller
tuple. Adding a harness therefore means finding all duplicated registrations
before any harness-specific protocol work begins.

**Proposed shape.** Create a stdlib-only immutable `HarnessSpec` registry with
the declarative facts shared by launch and onboarding: stable id, display name,
config name and accepted harness spellings, executable name/override, menu
order, whether a project anchor is mandatory, primer class, and setup support
class. Keep protocol behavior as named functions in the existing modules; do
not turn it into callbacks hidden in data. `pneu`, `_rtlauncher`, doctor family
normalization, and setup selection should consume the same registry and assert
that every supported id has the required implementation.

**Blast radius.** New `bin/_rtharness.py`; `bin/_rtlauncher.py`, `bin/pneu`,
`bin/rt-doctor`, and `pneu_packaging/setup.py`; packaging lists; launcher,
onboarding, setup, and each optional-adapter test. No change to
`agents.yaml` schema or wire names.

**Effort:** M. **Risk:** medium. The leverage is high for the next harness
workstream, but a registry must expose existing differences rather than pretend
all harnesses have the same lifecycle.

### 4. Convert setup's harness branches into planners feeding the existing transaction engine

**Anchors:** harness preparation at `pneu_packaging/setup.py:1012-1577`,
dispatch at `pneu_packaging/setup.py:1579-1593`, validation at
`pneu_packaging/setup.py:1623-1903`, application/path collection at
`pneu_packaging/setup.py:1931-2032`, and remove/detection dispatch at
`pneu_packaging/setup.py:2288-2348`.

**Current shape.** The setup module has excellent load-bearing transaction
machinery: owned-path validation, mutation lock, exact snapshots, atomic
writes, rollback, and explicit external-state reporting. Harness-specific
knowledge is nevertheless woven through every transaction phase. Each new
managed integration requires edits to prepare, validate, apply, mutation-path
enumeration, removal, detection, upgrade planning, and source preflight. That
is a collision-prone extension surface inside the safety engine.

**Proposed shape.** Keep `_mutation_lock`, `_snapshot_paths`,
`_rollback_snapshots`, `_atomic_write`, ownership validation, and rollback
reporting as the only executor. Move each harness's prepare/validate/remove
logic into a planner that returns a typed/declarative mutation plan: paths and
expected ownership, before/after bytes, modes, links, backups, and any explicit
external operation. The common engine preflights and snapshots the union, then
executes it exactly as today. Codex LaunchAgent unload/reload remains a named
special external step with its current partial-rollback semantics; it should
not be forced into a generic file operation.

**Blast radius.** Primarily `pneu_packaging/setup.py`, likely split into a
transaction module plus three harness planner modules; packaging metadata;
`tests/test_harness_setup.py`, `tests/test_codex_service_preflight.py`, and
packaging/release tests. Existing failure-injection tests are acceptance gates,
not candidates for simplification.

**Effort:** L. **Risk:** high. Sequence this after finding 3. It materially
reduces the cost of the next fully managed harness, but only if performed as
behavior-pinned extraction; the transaction guarantees must not be rewritten.

### 5. Use one pure project-ancestor discovery function

**Anchors:** canonical checks at `bin/_rtlib.py:3603-3612`; duplicates at
`bin/_rtlauncher.py:1072-1077`, `bin/rt-wait-inbox:96-102`,
`bin/rt-stop-gate:26-32`, `bin/rt-startup-advisory:46-57`, and
`bin/rt-doctor:710-714`.

**Current shape.** At least five modules repeat the same ancestor walk. Four
resolve the start path and call `is_project_root`; doctor performs a raw
`agents.yaml.is_file()` check instead. `find_project_root` adds override,
fallback, and optional cmux policies on top of yet another walk. The duplication
is small, but these are identity/anchor boundaries where semantic drift is
undesirable.

**Proposed shape.** Add `project_at_or_above(start) -> Path | None` to
`_rtlib`, with the current canonicalization and home/filesystem-root rejection
provided by `is_project_root`. Keep `find_project_root` as the CLI policy layer
for overrides/fallback/cmux. Import the pure ancestor helper everywhere else.
Callers retain their current error messages and configured-root cross-checks.

**Blast radius.** `_rtlib`, `_rtlauncher`, doctor, wait-inbox, stop-gate,
startup-advisory, and their focused tests. **Effort:** S. **Risk:** low to
medium because startup hooks must preserve calm no-op behavior.

### 6. Split test domains and extract only stable test builders

**Anchors:** `tests/test_rt_tooling.py:31-590` followed by tests through line
5465; `tests/test_rt_codex.py:33-98` followed by bridge, WebSocket, launchd,
version, ownership, and doctor tests through line 2991; duplicated project and
script helpers in `tests/test_seat_lifecycle.py:26-83`,
`tests/test_rt_doctor_lease.py:21-64`, and
`tests/test_rt_tripwire_runtime.py:28-142`.

**Current shape.** Two large test files each cover several independently owned
subsystems, and many files carry local variants of `write_project`,
`load_script`, `run_tool`, registry construction, and lease environment setup.
Local helpers sometimes intentionally encode different fixtures, so blind
deduplication would reduce clarity; the problem is that the stable baseline and
the domain-specific variation are not separated.

**Proposed shape.** Split `test_rt_tooling.py` by durable send/ack, cross-project
addressing, malformed mail, and legacy cmux; split `test_rt_codex.py` by bridge
state machine, WebSocket/RPC, launchd/service ownership, and doctor integration.
Create a small `tests/support/` package only for exact, stable primitives:
isolated environment, executable loader, canonical project/registry builder,
and fenced lease builder. Keep scenario builders beside their tests.

**Blast radius.** Test files only, plus any test discovery configuration needed
for the support package. **Effort:** M. **Risk:** low if done as mechanical
movement with the full suite before and after. This improves ownership and
reduces track collisions without changing production architecture.

### 7. If the parked lab adapters survive D14, extract only their shared fenced-generation shell

**Anchors:** Grok isolation/generation/fence handling at
`integrations/grok/roundtable/__init__.py:184-333` and `:569-844`; OpenClaw
counterparts at `integrations/openclaw/roundtable/__init__.py:101-314` and
`:595-948`.

**Current shape.** The protocols differ substantially (ACP child versus
authenticated Gateway), but the modules duplicate project-key derivation,
isolation-root construction, executable resolution rules, fenced lease
validation/update/clear, exact non-ack generation scanning, drain polling, and
the outer once/loop lifecycle. There is a real divergence: Grok rejects an
isolation root inside the project at `integrations/grok/roundtable/__init__.py:265-277`,
while OpenClaw's `create_isolation` at
`integrations/openclaw/roundtable/__init__.py:202-228` does not. With an
explicit runtime root beneath the checkout, OpenClaw can therefore create
adapter state in the project, contrary to the isolation intent.

**Proposed shape.** Do not refactor these parked adapters now. If D14 retains
either as product code, first add the missing OpenClaw outside-project guard.
If both remain, extract a narrow `FencedGenerationSupervisor` responsible only
for lease validation, generation snapshots, wake-state updates, and drain
deadline. Leave credentials, child environment, transport, protocol parsing,
and terminal success entirely adapter-owned.

**Blast radius.** Conditional on D14: both adapter modules and their unit,
mutation, and interop-lab tests. The immediate guard would touch only OpenClaw
and its focused tests. **Effort:** S for the guard; M for the shared shell.
**Risk:** low for the guard, medium for extraction. This is Tier 3/conditional
because the code may shrink or leave.

## Areas I would leave alone in 1.4

- **`bin/_rtmigrate.py`:** frozen. I made no refactoring recommendation.
- **`bin/_rtruntime.py`:** large but domain-coherent around host-local fenced
  lease state, reply expectations, wake ownership, and Codex launch intent.
  Its lock ordering and revision checks are more important than reducing its
  line count. Extract only after a concrete feature establishes a stable seam.
- **`bin/_rtcodex.py` and `bin/rt-codex-wake`:** already separate protocol/
  service plumbing from bridge policy. Both are large, but another split now
  would move a highly stateful safety boundary without clear harness-expansion
  leverage.
- **Registry and layout-lock core in `bin/_rtlib.py`:** it is large because it
  carries identity upgrade, fd-based validation, lock ordering, and registry
  compare-and-swap. Findings 1 and 5 remove peripheral responsibilities around
  it; they do not simplify those guarantees.
- **The setup transaction core:** finding 4 isolates it; it does not replace or
  relax owned-path validation, snapshots, backups, manifests, rollback, or
  partial external-state reporting.

## Suggested sequence

1. Test file split/support extraction (finding 6), so subsequent moves have
   smaller ownership surfaces.
2. Canonical project discovery (finding 5).
3. Maildir safety kernel (finding 1), with fault injection.
4. Legacy cmux extraction (finding 2), in a separate cycle from step 3.
5. Harness specification registry (finding 3).
6. Setup planners over the unchanged transaction executor (finding 4).
7. Reassess the parked adapter shell only after D14 (finding 7).

Never combine maildir commit extraction with a change to layout locking, lease
validation, or migration. Never combine setup planner extraction with changes
to snapshot/rollback semantics or live LaunchAgent behavior.
