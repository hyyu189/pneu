# Codex capability-binding architecture

> Status: historical record — the decided model, implemented in 1.4 track T1; the shipped behavior is docs/compatibility.md "Codex capability binding"

Status: product/architecture proposal only. No implementation. Incorporates
the operator's product rulings routed 2026-08-14. This document supersedes
the B1-versus-B2 comparison in `handoff/archive/codex-canonical-daemon-proposal.md`
(the capability model below is the decided shape); that document's Proposal A
(canonical shared daemon) and its upstream asks remain standing.

## Product rulings (fixed points)

1. **The bound thread is the seat's native control entry.** Once capability
   resolution works through `CODEX_THREAD_ID`, any client driving the exact
   bound thread — TUI, Desktop, phone, or Remote — is operating the same
   pneu seat. That is the desired compatibility. Do not segregate by client
   origin; the goal is to make the pneu communication network compatible
   with native Codex Desktop/Remote control, not to block a native client
   from taking over the same legitimate thread.
2. **The control boundary is the exact bound thread.** Other threads on the
   same daemon are not the seat. If Remote enrollment can select several
   bound threads, it can correspondingly operate those seats — that is part
   of the chosen personal trust domain, stated plainly rather than disguised
   as isolation.
3. **Fence semantics are harness-uniform.** The fence exists to stop stale
   sessions/leases, cross-project and cross-seat confusion, and concurrent
   handoffs — identically for Codex, Claude, and Hermes. Codex is only
   harder because its client and its tool processes live in different
   process trees, so ambient `RT_*` transport breaks in transit; Claude's
   SessionStart/Stop hooks and the Hermes plugin have session-local
   transport and are simply never seen failing.

## Architecture: two-stage capability binding

Environment backfill into the daemon is not the model. Identity and
capability are bound out-of-band and resolved by lookup:

**Stage 1 — launch side (a child of the real user shell).**
`rt-codex` can see the true launch environment. It scrubs `RT_*` inherited
from any previous seat, selects project/agent, claims a fresh lease
(new revision), and writes a **private launch intent**: the lease identity
plus a minimal allowlisted surface capability (see Herdr rules below). Then
it execs `codex --remote`.

**Stage 2 — daemon side.** After `thread/start` or `thread/resume`, the
SessionStart path — which by design distrusts hook/tool-shell `RT_*` —
obtains `CODEX_THREAD_ID` and cwd and binds that thread to the launch
intent/lease by compare-and-swap. The wake bridge then reads the app-server
thread and re-validates cwd and lease before the binding is durable.

**Resolution path for every subsequent pneu tool call:** native
`CODEX_THREAD_ID` (injected by the engine into each tool process) → exact
binding → active lease validation → surface capability. No step expects the
daemon to have inherited anything from the launch shell.

Existing skeleton already in source, to be extended rather than redone:
`_rtlauncher.claim_launch_seat` (scrub + claim + fresh `RT_*`),
`arm_codex_launch_intent` (private lease intent), `rt-codex-session-start`
(intent restore, ambient-`RT_*` distrust), `rt-codex-wake` (thread/cwd/lease
validation before binding). The new work is a **generic capability
resolver** shared by the fenced tools, reply-alarm writes, and surface
consumers.

## Herdr surface capability rules

- Never fabricate `HERDR_ENV=1` inside the shared daemon. That variable
  asserts "the caller is genuinely inside a Herdr pane" and changes
  `--current` semantics; faking it machine-wide would corrupt those
  semantics for every thread.
- At launch, capture only minimal, explicitly addressable surface data:
  `kind=herdr`, pane id, the necessary workspace/tab/session identifiers, or
  a private socket endpoint. Never store the full environment, `HOME`,
  `PATH`, or tokens.
- Store this surface intent with the lease; once the thread binds, associate
  it with `threadId` + `bindingRevision` + `roundtableSessionId` +
  `leaseRevision`.
- At use time, revalidate the full fence chain first, then operate through
  the explicit pane/endpoint. If the Herdr CLI can only work from an ambient
  socket, delegate execution to a small local broker that genuinely runs in
  a Herdr environment; never pollute the app-server's global environment.

## Environment backfill: last-mile compatibility only

When an external command genuinely requires environment variables, a wrapper
first validates binding and lease via `CODEX_THREAD_ID`, then injects the
allowlisted variables into that single child process. Thread-config
injection is never treated as identity truth: the explicit-remote transport
does not reliably forward `shell_environment_policy`, and any snapshot goes
stale.

## Lifecycle rules

- Desktop, Remote, and TUI clients of the **same thread** share the seat.
- `resume` keeps the binding.
- `clear` migrates the seat only after a new SessionStart/CAS completes
  under the same active lease.
- `/btw` side threads, forks, and new threads inherit **nothing** by
  default; capability requires an explicit rebind.
- Fail-closed triggers, immediate: lease superseded, `bindingRevision`
  changed, project/cwd mismatch, or the recorded pane/endpoint no longer
  exists.

## Capability record schema

Today's `surface.json` stores only kind and pane/target, is advisory, and —
although lease-validated at write time — carries no thread, binding, or
lease revision, so it can go stale across lease generations. The proposal:
either extend that schema or (recommended) add a **private seat-capability
record** in the runtime agents directory keyed to the lease, carrying the
association tuple above. Resolution must jointly validate the record against
the **current** binding and lease at every use; `surface.json` remains the
advisory navigation artifact it was designed to be.

## Staged acceptance

1. **Resolver fenced ops**: from a daemon tool process carrying only
   `CODEX_THREAD_ID`, `rt-ack --fenced` and `rt-say --fenced
   --expect-reply` succeed against the exact active lease; superseded lease,
   changed binding revision, or cwd mismatch refuses; two concurrent seats
   in different projects never cross. Proven on a stock release artifact.
2. **Surface capability**: a codex seat drives a Herdr surface through the
   recorded explicit endpoint (broker path included); pane disappearance
   fails closed with a clear diagnostic; no fabricated `HERDR_ENV` anywhere
   in the daemon.
3. **Client equivalence**: Desktop joined to the bound thread via
   `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` operates the seat with identical
   capability and identical fences; a `/btw` child of the bound thread
   resolves to nothing.
4. **Canonical-host soak and upstream round**: unchanged from
   `handoff/archive/codex-canonical-daemon-proposal.md`.
