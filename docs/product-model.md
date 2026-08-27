# pneu product model

> Status: current, rebaselined 2026-08-27. The canonical objects, invariants,
> message states, attention policy, and switchboard UX. Governed by
> [`PRINCIPLES.md`](../PRINCIPLES.md); decisions in [`adr/`](adr/).

## Thesis

pneu is not merely a `send / inbox / ack` utility, and it is not a full Agent
Development Environment.

> **pneu is a thin, local-first coordination control plane for native
> coding-agent sessions.**

It adds stable addressing, durable mail, occupancy, native wake, resume, and
navigation to the coding tools people already use. It does not ask the user
to move the conversation, transcript, editor, terminal, approval UI, or agent
loop into pneu.

## Canonical objects

### Project group

A local grouping of workspaces belonging to one logical project. In Git this
derives from the Git common directory; without Git, one registered directory
is one project group. A GitHub/GitLab repository is optional metadata, never
the identity source.

### Workspace

An independent file-writing and execution environment: one checkout/worktree,
or the registered directory itself. A workspace has a stable local UUID; its
path is mutable location data.

### Harness family

A user-visible coding-agent product: Claude Code, Codex, Hermes, Grok Build,
DeepSeek Harness. Different clients or surfaces for one native thread do not
create different harness families.

### Seat

> **A seat is a stable, workspace-scoped address for one harness family.**

Canonical key: `(workspace_id, harness_family)`. For pneu 1.x: one workspace
× one harness family → zero or one top-level seat. A native session
*occupies* the seat; it is not the seat itself (ADR 0002).

- Mail is addressed to the seat.
- Wake is sent to the currently bound native session.
- Focus/jump is sent to one of the surfaces showing that session.
- The mailbox remains while no session is running.
- A resumed or replacement native session may reoccupy the same seat.

### Native session

The harness-owned conversation/process/thread the user can independently
open, resume, and control: a Codex root thread, a Claude Code interactive
session, a Hermes TUI session. Harness-internal subagents are not pneu seats;
they remain inside the harness execution plane.

### Surface

Where the user sees or controls the native session: a terminal, tmux or
Herdr pane, Codex Desktop or IDE, Claude mobile/web. One native session may
have several surfaces; several clients showing one Codex thread are still one
occupied seat. Surface records are advisory navigation metadata, never
ownership or liveness evidence.

### Purpose (not role)

Review, implementation, research, testing, and planning are transient task
purposes. Purpose may appear in message metadata or on the switchboard —
never in the seat key or a persistent roster.

## Parallelism and worktrees

The default parallel unit is a **change/workspace**, not an agent persona:

- one independently mergeable change normally gets one worktree;
- several different harness families may collaborate in that worktree, with
  roles changing at any time;
- a second top-level instance of the same harness family belongs in another
  worktree;
- same-workspace concurrency beyond one top-level seat uses the harness's
  own subagent/team mechanism.

This deliberately aligns code isolation, Git state, test environment, seat
identity, and user navigation, instead of inventing `codex-1`, `codex-2`, or
role-named permanent seats in one directory.

## Addressing

Harness-centered and simple:

```text
codex                 # Codex seat in the current workspace
codex@feature-auth    # Codex seat in a sibling workspace alias
```

A message can carry a task reference and a temporary purpose:

```text
To: codex@feature-auth
Kind: review-request
Ref: change:seat-occupancy
Head: a12bc34
Need: inspect takeover semantics and report blockers
```

Persistent role names are never the default address.

## Data plane, control plane, surfaces

**Durable delivery is the data-plane fact.** Atomic publication into the
recipient mailbox is delivery. It must work with no target session, no
daemon, no terminal multiplexer, no network, and no Git or forge.

**Control state lives in the local runtime**, never in committed documents:
current seat owner and lease revision, native session binding, adapter state
and health, surface capabilities, reply alarm state, takeover evidence.

**Message states are distinct** and never collapse into one "sent":

```text
delivered  → message is durable in the mailbox
notified   → a native UI or surface received a signal
awakened   → the harness admitted new work
seen       → the target session drained the message
acked      → the logical message was acknowledged and archived
replied    → a response/result arrived
```

## Attention policy

Default policy by seat state:

- offline: deliver only;
- idle: native wake may start work;
- busy: notify by default — never silently steer ordinary mail into the
  active human turn;
- stale: do not wake; offer reclaim;
- unsupported wake: show `delivered, not woken`;
- multiple surfaces for one session: one session-level wake, not one per
  client.

Urgent steering, where a harness supports it, must be explicit and
policy-controlled.

## Switchboard UX

The pneu UI is a switchboard, not another agent workspace. It shows a small
set of truthful states:

```text
Claude     active here
Codex      active · Codex Desktop
Hermes     stale · reclaim
DeepSeek   3 unread · not running
```

| State | Default action |
| --- | --- |
| vacant | launch the native harness |
| active here | return to the existing native session |
| active elsewhere | jump/focus the appropriate surface |
| stopped but bound | resume the exact native session |
| stale | guarded reclaim |
| unavailable | show the precise missing capability and remedy |

No transcript rendering, task board, prompt composer, diff viewer, or
workflow canvas on the main surface. The screen-level launcher specification
is [`ux/launcher.md`](ux/launcher.md).

## Git and forge are optional capability levels

```text
Level 0 — Folder     seats, durable mail, wake, resume, local navigation
Level 1 — Local Git  branch/commit references, worktree grouping, local diff
Level 2 — Forge      issue/PR references, CI/review status, web navigation
```

pneu core never requires GitHub. It accepts opaque, typed references:

```text
change:seat-occupancy   branch:change/seat-occupancy   commit:a12bc34
file:changes/seat-occupancy/design.md   github:pr/191   gitlab:mr/83
```

## pneu does not own

Model selection or model calls; the harness agent loop; conversation
transcripts or model-visible history; tool execution, sandbox policy, or
approval decisions; editor/terminal/diff/review rendering; a permanent
organization chart; harness-internal subagents; automatic task decomposition
or a workflow graph; a replacement issue tracker or roadmap database; a
mandatory cloud service or cross-machine transport; unrestricted mutation of
vendor configuration or user repository documents.

## Explicitly deferred

The `.roundtable` → `.pneu` project-path rename; same-workspace multiple
top-level seats of one harness family; cross-machine mail transport;
transcript aggregation and replay; an issue/task database; a workflow canvas
or full ADE UI; automatic role assignment; automatic merge or permission
arbitration; a universal agent-loop protocol.
