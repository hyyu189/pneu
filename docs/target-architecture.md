# pneu architecture — target state

> Status: current direction, rebaselined 2026-08-27. Sequenced in
> [`roadmap.md`](roadmap.md); adapter specifics in
> [`harness-adapters.md`](harness-adapters.md). Where this differs from the
> current code or older prose, the difference is an intentional target — the
> as-built truth stays in [`architecture.md`](architecture.md).

## Three planes

- **Data plane** — durable delivery. Atomic publication into the recipient
  mailbox is the fact; it works with no target session, no daemon, no
  multiplexer, no network, no Git.
- **Control plane** — the local runtime's ephemeral facts: seat owner and
  lease revision, native-session binding, adapter state and health, surface
  capabilities, reply alarms, takeover evidence. Never inferred from
  committed documents.
- **Surfaces** — pure views and navigation: detect and render fresh, mutate
  nothing by being looked at, jump/focus/open on explicit action.

## Corrected ontology lands concept-first

The implementation already distinguishes stable mailbox identity, one pneu
session ownership term, an optional native thread id, a lease revision, and
advisory surfaces — it is closer to the target ontology (seat = workspace ×
harness family; the session occupies it) than the older prose was. The
baseline therefore corrects the conceptual contract first; runtime field
renames follow only where they improve correctness, never as bulk churn.

## One home per fact

Every load-bearing fact gets exactly one module; consumers import, never
re-derive:

- `_rtharness.py` — the single stdlib-only harness registry/descriptor:
  executable aliases, default args, permission modes, config locations,
  native protocol, evidence version. Consumers: launcher, front door,
  worktree, rc-host, doctor, setup.
- `_rtmail.py` — the wake-predicate vocabulary with an explicit strictness
  enum, preserving the real strictness variants.
- `_rtsafeio.py` — the two named atomic-write contracts (bootstrap publisher
  vs owned-rewrite); strength stays visible at each call site, never a
  global merge.
- One project-ancestor primitive in `_rtlib` replacing the private copies.
- A maildir mutation kernel (`publish_new` / `archive_new`, lock-free,
  callers hold the layout lock) extracted as literal code motion with fault
  injection — never in the same cycle as wake-predicate, layout-lock, or
  lease changes.

**The packaging boundary is fixed before the broad split**: helper packages
or generated manifests instead of today's flat exact module list, so adding
or splitting a module stops requiring manifest, build, import-smoke, and
test edits in lockstep.

## Deliberately deferred abstraction

- The Codex subsystem's reusable supervised-service shape (state
  classification, converge/repair, launch intent, readiness) stays under its
  Codex nouns until a second supervised-service harness proves the boundary.
- The `rt-doctor` probe registry and setup planners are layered over the
  unchanged transaction engine after the registry exists.
- A generic adapter plugin contract is derived only after the concrete
  Codex, Herdr, and DeepSeek integrations (see
  [`harness-adapters.md`](harness-adapters.md)).

## Do not rewrite

The mail envelope; layout migration (`_rtmigrate.py` is frozen); fencing and
ownership checks; the rt-ack→rt-say process boundary (it *is* the lock
discipline); the setup transaction core (ownership, snapshots, backup,
rollback); the migration/mailbox crash matrices; `_rtsurface.py`;
`roundtable-init`'s transaction behavior (its *scaffold content* changes in
roadmap Phase 3). Tests that pin these contracts are protecting real
invariants; tests that pin wording or source shape are not, and are replaced
with contract tests as touched.
