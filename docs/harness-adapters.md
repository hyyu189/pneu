# Harness and surface adapters

> Status: current direction, rebaselined 2026-08-27. Technical reference for
> adapter work; sequence in [`roadmap.md`](roadmap.md) Phase 4. Principle:
> preserve native harness session semantics; standardize only the
> coordination boundary pneu actually owns (ADR 0003).

## Native protocol first

When a harness provides a maintained native control protocol, use it. Do not
replace it with a lowest-common-denominator universal agent protocol.

Priority order:

1. vendor-native control protocol;
2. vendor-native plugin/extension;
3. stable external injector with exact session identity;
4. model-armed monitor as a weaker fallback;
5. keyboard injection only as retired legacy compatibility.

## Two orthogonal adapter types

**HarnessAdapter** owns: discover and launch/attach; bind/release the native
session; session status and lifecycle; notify/start/steer/context
capabilities; resume; harness diagnostics.

**SurfaceAdapter** owns: surface discovery and liveness; focus/jump; open
workspace/worktree; badge/notification. It never drains a mailbox and never
wakes a model.

Never create combinatorial adapters such as `codex-in-herdr`: the harness
adapter gets mail into the exact thread; the surface adapter takes the user
to the pane displaying it.

## Capability declaration, not a boolean

A harness is never represented by one `supported` flag. Capabilities are
declared and validated individually:

```yaml
discover: true
launch: true
bind_native_session: true
observe_status: true
notify: true
start_turn: true
steer_turn: false
append_context: true
resume: true
focus_surface: false
live_validated: true
```

Support depth taxonomy (each level is separately evidenced):

```text
1 detected        binary/config found
2 launchable      native UI can be opened
3 bound           exact native session identity established
4 deliverable     durable mail available to its seat
5 wake-capable    idle session notified/admitted reliably
6 resume-capable  exact native session resumes/rebinds
7 live-validated  real end-to-end matrix passed on a recorded version
```

## Codex App Server (native harness-protocol reference)

Upstream treats the App Server as the first-class integration surface, with
durable primitives: **thread** (conversation container), **turn** (one
admitted unit of work), **item** (typed lifecycle events). The adapter
contract for a validated version is the schema generated from the installed
binary (`codex app-server generate-ts` / `generate-json-schema`), never a
copied main-branch schema.

Identity mapping:

```text
pneu seat            = workspace × codex
Codex native session = user-visible root thread ID
surface              = TUI / Desktop / IDE client subscriber
```

A TUI and Desktop showing the same root thread are one seat. A Codex
subagent/child thread is not a pneu seat (parent-owned children may also
reject direct control upstream).

Operation semantics stay distinct — never one generic `wake()`:

| Intent | Native shape | pneu meaning |
| --- | --- | --- |
| notify | client/surface notification | mail exists; no turn change |
| append context | `thread/inject_items` | model-visible history for a later admitted request; **not wake** |
| start work | `turn/start` | admit new work on an idle thread |
| steer work | `turn/steer` | add input to a specific active turn; interruptive |
| resume | `thread/resume` | load the exact durable thread |

**Busy-turn safety.** Ordinary mail during an active human turn is never
silently steered into that turn. Admission can race and `turn/start` can
behave as start-or-steer in current implementations (upstream issue open),
so until an atomic idle-only admission contract is validated:

```text
idle thread     → start a concise pneu activation turn
active thread   → notify only; leave mail durable
urgent explicit → turn/steer with exact active-turn precondition
unloaded thread → deliver; resume on user return unless background explicit
```

**Connection strategy**, in order: the supported shared/local App Server
control endpoint when it reaches the exact visible thread; a pinned stdio
child only when the integration owns the visible client; an official SDK
only after proving same-thread attach; experimental transports stay
lab-only. pneu never creates a parallel headless Codex conversation and
calls that adaptation.

**Migration of the existing bridge** (which already implements out-of-band
binding and the capability chain, see [`architecture.md`](architecture.md)):
map each component to an official primitive; generate the schema from the
installed version; run a lab adapter alongside the existing path; prove
same-thread delivery/wake in TUI and Desktop, busy-turn non-interference,
multi-client deduplication, resume/reconnect, App Server restart, and
version fallback; replace only after the full live matrix passes.

## DeepSeek Harness (second native harness reference)

DeepSeek Harness is "everything is a plugin" (Cordis): plugins contribute
services, typed events, and reversible effects; durable session events are
distinct from live agent events — matching pneu's delivery/context/wake
separation. It is a developer preview: any integration is version-pinned and
experimental until live-validated.

Correct shape: a **HarnessAdapter implemented as an out-of-tree native
plugin** that identifies the real user-visible session, claims/releases the
seat, binds the native session id, observes idle/busy, watches durable
mailbox events, uses the exact native inbox/agent API to notify or wake the
same session, optionally registers structured send/inbox/ack tools, and
unloads cleanly. Client-side UI stays additive (unread badge, binding
health, `delivered, not woken`, a link to the switchboard) — never a
replacement chat UI, never a parallel headless agent.

Integration gate before any support claim: exact version recorded; same
visible session proven; idle message starts work once; busy message does not
corrupt the active turn; empty inbox causes zero model requests;
resume/reconnect rebinds; stale lease/session mismatch rejected; unload
leaves durable mail intact.

## Herdr (surface reference)

Herdr owns persistent terminal panes running existing tools unchanged. For
pneu it is a **SurfaceAdapter**, not a harness: it may register pane/tab
addresses, focus/jump to the pane showing a bound session, open a worktree
in the right pane, show unread/blocked/active badges, report surface
liveness, and broker surface commands. It must not read or ack mailboxes,
decide native session identity, inject prompts by keyboard as a wake path,
start model turns, or compete with the harness adapter for wake ownership.

## The common contract comes last

After the Codex, Herdr, and DeepSeek spikes, derive a small process-boundary
plugin contract — shared operations only:

```text
describe  probe  plan_setup  bind  heartbeat/status
notify  resume  focus  release  diagnose
```

Harness-specific operations (`append_context`, `start_turn`, `steer_turn`)
remain capability names or native calls. Third-party adapters speak JSON
over stdio or a local Unix socket, so Rust/TypeScript/Python integrations do
not depend on pneu's private Python ABI. The contract standardizes
discovery, binding, capability negotiation, health, structured errors, and
opaque native IDs — never thread/turn/tool semantics.
