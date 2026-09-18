# PRINCIPLES

> Status: current, rebaselined 2026-08-27. This file is the ranked
> constitution: when two documents disagree, the higher-ranked principle
> decides. It records principles, not decisions — individual rulings live in
> [`docs/adr/`](docs/adr/). This revision supersedes the 2026-08-17
> constitution; the substantive change is principle 2's corrected seat
> definition (see ADR 0002).

The list is ranked. Principle 1 outranks principle 2, and so on.

## 1. Native experience first

The product is one experience: **open your harness the way you always do, and
mail finds you there.** Everything else in this repository — the maildir, the
leases, the fences, the wake bridges — exists to deliver that sentence.

The acceptance test for any harness adaptation:

> Does mail reach the session the human is actually in?

A mechanism that instead spawns a headless replacement session, drains the
mailbox there, and reports success has failed this test. It did not adapt the
harness; it replaced the human's seat with a robot and answered a question
nobody asked. Headless and oneshot agent processes remain perfectly
legitimate *inside* a harness (subagents, teammates, internal automation);
they are simply not user-facing seats.

## 2. A seat is workspace × harness family; a native session occupies it

> **seat = one stable, workspace-scoped address for one harness family.**

The canonical key is `(workspace_id, harness_family)`, and in pneu 1.x one
workspace holds at most one top-level seat per harness family. The native
session — a Codex thread, a Claude Code session — *occupies* the seat; it is
not the seat itself. Surfaces (terminal, Desktop, phone) display the session;
several clients driving one bound thread are one occupied seat.

The distinction carries the product:

- mail is addressed to the seat and remains durable while no session runs;
- wake is sent to the currently bound native session;
- focus/jump is sent to one of the surfaces showing that session;
- a resumed or replacement session may reoccupy the same seat.

Review, implementation, research, and planning are **transient task
purposes, not identities**. A purpose may appear in message metadata or on
the switchboard, never in the seat key or a persistent roster. The default
parallel unit is a change/workspace — one independently mergeable change
normally gets one worktree, where several harness families may collaborate
with freely changing roles.

## 3. The delivery core and the wake adapters obey different rules

**Delivery** needs no daemon, no multiplexer, no account, and no network. The
atomic write of a message file into the recipient's `new/` directory *is* the
delivery. An offline seat loses nothing; the core works in an ordinary
terminal.

**Wake** is an adapter layered on top and may use a harness-owned service.
The harness owns its runtime; native use must not depend on a replacement
host or a global client-routing override. Historical service machinery is
not evidence of current native attachment support, or authorization to
restore it (ADR 0005). Delivery remains independent of such services.

Delivery, notification, wake, seen, acknowledged, and replied are distinct
states and must never be collapsed into one "sent". The UI never claims "the
agent is working" merely because delivery succeeded.

## 4. Thin control-plane boundary

pneu owns local identity, seat addressing, durable mail, occupancy and
leases, native-session binding, wake/resume policy, surface navigation, and
host-integration ownership with clean reversal. It does not own model calls,
the agent loop, transcripts, tool execution or approval, editors or
terminals, harness-internal subagents, task decomposition, an issue tracker,
or a mandatory cloud service.

The boundary test:

> If a feature requires pneu to reconstruct or render the agent conversation
> in order to work, it belongs to the harness or to an adjacent product, not
> to pneu core.

When a harness provides a maintained native control protocol, use it — never
replace it with a lowest-common-denominator universal agent protocol
(ADR 0003).

## 5. State and support-claim discipline

Display surfaces detect and render fresh on every run, and never mutate state
by being looked at. Project state changes only by explicit acts; ephemeral
coordination facts (owner, lease revision, binding, adapter health) live in
the local runtime, never inferred from committed documents.

A support claim requires a live, end-to-end smoke on a real environment.
Fixtures, unit tests, source inspection, and version-number comparisons are
evidence for *design*, never for *support* (应然 vs 实然).
[`docs/compatibility.md`](docs/compatibility.md) is the one home for what has
actually been exercised. Two corollaries:

- **"We do not use it" does not lower the bar** — it removes the validation
  path, which *raises* it.
- **A shipped surface has no zero-cost parking state.** Retain, keep
  shipping, or stop shipping; doing nothing ships it again.

The same discipline applies to documentation: a statement earns the label
"current" only after someone checked it against code or a release artifact.
Support is a capability matrix, never one boolean per harness.

## 6. Project integration is minimal and reversible

Default project adoption creates only the smallest pneu-owned anchor needed
for identity and roster. Awareness blocks and collaboration conventions are
explicit, marker-owned, reversible modules — never a default, never a
dependency of delivery, binding, or wake (ADR 0004). Host setup previews its
mutations, tracks ownership, and reverses cleanly; pneu never mutates vendor
configuration or user repository documents beyond its marked fragments.
