# Architecture and adapter boundaries

Roundtable is terminal-emulator independent by design. Terminal.app, iTerm2,
Ghostty, and other terminal hosts are not separate transports or reduced
compatibility modes. They host harness processes that use the same Roundtable
core.

## Layers

| Layer | Responsibility | Required for delivery |
| --- | --- | --- |
| Product core | Project config and identity, atomic maildir delivery, ledger, inbox, acknowledgement, and drain state | Yes |
| Harness adapters | Codex app-server wake; Claude hooks; Hermes lifecycle plugin | Only for automatic wake; offline delivery still succeeds |
| Terminal integrations | Optional workspace topology, surface diagnostics, project navigation, and notifications | No |

The data path is:

```text
sender harness
  -> rt-say
  -> project maildir (delivery fact)
  -> recipient inbox
  -> harness-native wake adapter, when the recipient is online

optional cmux adapter
  -> observes project/workspace topology
  -> never owns the delivery fact
```

## Invariants

- Delivery, inbox, acknowledgement, and drain do not call a terminal-emulator
  API.
- Project identity does not depend on Git. Any directory can be a Roundtable
  project, including a non-code workspace.
- Harness launchers resolve real executables and reject generated cmux PATH
  shims.
- A missing or unhealthy optional terminal integration cannot invalidate a
  maildir delivery.
- cmux topology state may improve navigation and diagnostics, but it is never
  authoritative for whether a message exists.
- Human-attention alerts fall back to native macOS notifications; cmux may add
  workspace-aware notification context but is not the only alert provider.
- Terminal-emulator support and harness support are separate axes. A Codex
  app-server compatibility gate is not a Ghostty, iTerm2, or Terminal.app
  compatibility gate.

## Host and project onboarding

Program installation, host onboarding, and project creation are separate
ownership boundaries:

```text
release installer
  -> versioned program + canonical global skill
  -> install-manifest.json

roundtable-setup
  -> selected harness config fragments, plugin/skill links, Codex plists
  -> harness-setup.json + private config backups

roundtable-init
  -> one project anchor, identities, mailboxes, and orientation files
  -> optional Git initialization only when requested

roundtable
  -> compact project-first menu; registered roots expand in a second level
  -> configured harness-seat selector, then the fenced rt-* launcher
```

`roundtable-setup` defaults to a read-only plan. `apply` links the one installed
skill into the selected harnesses' user-global skill roots, so agents discover
the same version without copying it into every project. Claude receives owned
SessionStart and Stop hook groups. Hermes receives one marked plugin enablement
plus an owned plugin link. Codex receives owned app-server and wake plist files.
Setup does not install a harness or copy credentials. Plan, apply, and status
never invoke `launchctl`; only an explicit Codex teardown may do so.

The package and harness manifests are deliberately separate. Harness
configuration must be removed while its Roundtable commands and canonical
skill still exist; only then can the package be uninstalled. Both removal paths
verify ownership and fail closed on drift. A Codex-selected removal also fails
closed until an operator outside Codex explicitly supplies `--unload-codex`;
that path inspects and bootouts only the two owned labels before deleting their
plist files.

`roundtable-init --here` configures an existing directory without replacing
user documents. `roundtable-init NAME` creates a new directory. Git is not a
project-identity requirement and is initialized only with `--git`. The
unified `roundtable` command exposes this as the default interactive journey;
the individual harness launchers retain their scriptable entry points.

Generated project configuration is repository-relative: `agents.yaml` stores
`project: "."`, which consumers resolve against the directory that owns the
configuration. This keeps a cloned project portable while retaining support
for older absolute-path configs; it is not the durable project identity. Each
registered worktree instead has an ignored `.roundtable/project.json` UUID
witness, and `~/.roundtable/projects.yaml` maps that stable UUID to its mutable
path, derived group, status, and selected mailbox layout. New and explicitly
upgraded entries start with the project-local layout. The Claude project-skill
bridge is a portable relative symlink and is part of the optional initial
commit; an existing user-managed skills directory is preserved instead.

## P0 state placement and session ownership

Roundtable separates project facts from facts that are meaningful only on one
host:

| State | Location | Lifetime |
| --- | --- | --- |
| Agent identities and portable project configuration | `<project>/.roundtable/agents.yaml` | Durable project state |
| Stable worktree identity and registry metadata | ignored `<project>/.roundtable/project.json` plus `~/.roundtable/projects.yaml` | Durable worktree and host state |
| Inbox `new/`, `cur/`, and `tmp/`; message ledger and acknowledgements | Registry-selected local or central mail root (new/upgraded entries initially use `<project>/.roundtable/`) | Durable delivery state |
| Current session lease, owner PID and process fingerprint, wake-adapter PID, activity and heartbeat | `~/.roundtable/.runtime/` | Host-local ephemeral state |
| Optional terminal topology, navigation handles, and adapter diagnostics | `~/.roundtable/.runtime/adapters/` | Host-local ephemeral state |

Maildir `tmp/` is the deliberate exception to the simple durable/ephemeral
split: it is staging state, but it must remain on the same filesystem as
`new/` so publication can use an atomic rename.

Every mailbox reader or writer acquires the UUID-keyed shared layout lock
before resolving the registry pointer and holds it through its last mailbox
I/O. Migration takes the matching exclusive lock. Lock files are persistent
private coordination in `~/.roundtable/layout-locks/`, not removable PID or
staleness records. Long-running watchers take short shared sections per scan
and sleep unlocked; adapter prompts never retain a physical mailbox path as a
capability.

P0 uses a deterministic key derived from the canonical project path, while
retaining that readable path in metadata:

```text
~/.roundtable/.runtime/
  projects/<canonical-path-hash>/
    project.json
    claim.lock
    codex-launch-intent.json
    agents/<agent-key>/
      state.lock
      lease.json
```

`agent-key` is the SHA-256 digest of the configured `agent_id`; the readable
identity remains inside `lease.json`. This keeps arbitrary configured IDs from
becoming paths. `RT_RUNTIME_DIR` may select another host-local root for tests
or managed installs; its legacy Codex alias must resolve to the same absolute
directory.

Runtime directories and files are private to the local user and updates use a
short host-local lock plus atomic replacement. Project-local Claude and Hermes
markers such as `.armed-<pid>`, `.last-active`, and `.empty-beats` now live in
the fenced lease record; old project-local markers are diagnostic-only.
Codex binding, bridge PID, heartbeat, locks, and logs are also host-local. The
optional cmux `runtime.json` and legacy operation locks follow the same
placement principle, but will move in separate changes so they do not complicate
the session-ownership change.

### Logical seat, Roundtable session, and native session

These identities are intentionally different:

| Identity | Meaning | Reused |
| --- | --- | --- |
| `agent_id` | Stable project address and mailbox seat, such as `codex` | Yes |
| `session_id` | One Roundtable launch and ownership term | No |
| `native_session_id` | Harness-native Codex thread or equivalent, when available | Only for an explicit resume |
| `lease_revision` | Fencing token for the current owner of the seat | No |

The collision key is the logical seat `(project, agent_id)`, not the mere
existence of an old harness thread. P0 configures at most one logical seat per
harness in a project, so a second Codex launch currently resolves to the same
seat and is rejected while the first owner is active. Keying ownership by
`agent_id` leaves a compatible path for later projects with several named
instances of the same harness.

The launcher claims the seat before starting the harness and exports
`RT_PROJECT_ROOT`, `RT_FROM`, `RT_SESSION_ID`, and `RT_LEASE_REVISION`. The
anchored process always starts at the canonical project root, even when the
user invoked the launcher from a nested directory; this gives harness-native
thread binding one unambiguous project identity. The
lease names the harness owner process separately from its wake adapter or
tripwire. A live tripwire is not proof that the chat owns the seat, and a dead
tripwire does not make a still-running chat safe to replace. Hooks, watchers,
and bind operations may update or release a lease only when their session ID
and revision still match, preventing an old process from clearing a newer
owner. A stored Codex thread binding is routing metadata, not liveness proof,
and is valid only while it matches the current lease. Claim/reclaim takes the
seat's exclusive lock; operations that externally bind or wake a Codex thread
hold a shared fenced guard for their whole critical section so a new owner
cannot race an already-authorized old operation.
Pre-lease Codex bindings use the same project claim lock and are accepted only
when no Codex harness lease record exists anywhere in that project; the guard
remains held through binding or `turn/start`, so a legacy action and the first
unified claim also have a deterministic order.

Claude's automatically approved mail actions are not ambient PATH authority.
The hook names the installed wrappers by absolute path, and each fenced inbox,
acknowledgement, or send operation revalidates all four launcher fields against
the current active lease before touching project mail. Its host-local wake
state also records the last pending filename generation and bounded attempt
count, preventing one undrained generation from causing an infinite wake loop.

Hermes follows the same bounded contract through its lifecycle plugin. The
plugin arms on the TUI's `on_session_reset` boundary, and delivery uses the
session-key-fenced background-notification handshake. Re-arm is
generation-scoped: a fresh waiter starts only after the exact triggered
filenames leave `new/`, and that archival belongs to the agent's `rt-ack`,
never the plugin. An unacknowledged generation receives one bounded re-notice
and then a pause diagnostic instead of an unbounded silent wait, and the
plugin keeps polling so a late acknowledgement still re-arms automatically.
A later session reset re-arms after session-scoped failures, while fence
supersession and invalid installations stay closed for the process.

Heartbeat reports adapter health; it is not by itself permission to steal a
seat. On the same host, owner PID plus a process-start fingerprint protects
against PID reuse and is the primary liveness proof. An unexpired-looking
heartbeat cannot keep a dead owner active, and an idle but live harness is not
declared dead merely because it has not emitted a recent heartbeat.

### Selector state machine

| Observed state | Selector behavior |
| --- | --- |
| No lease | Atomically claim the seat and start a fresh session |
| Owner live, adapter healthy | Report that the harness is already active and do not start a second session |
| Owner live, adapter unhealthy | Keep the seat occupied, report a wake-health problem, and direct the user to diagnostics |
| Owner dead or process fingerprint mismatched | Treat the lease as stale, atomically replace it, and start with a new session ID and revision |
| Liveness cannot be established safely | Fail closed and require an explicit repair or release action |

An inactive historical session is not a conflict. The default launch is fresh:
it gets a new Roundtable session ID and a new native chat/thread rather than
silently reconnecting to history. If a future selector offers an explicit
native-session resume, the new process still receives a new Roundtable session
ID and lease revision; only `native_session_id` is reused. P0 exposes only the
fresh path until each harness's native resume flow and project-root validation
have passed real end-to-end tests.

For anchored Claude with no explicit native arguments, the launcher implements
that fresh-session rule with a new UUID passed as `--session-id`. Explicit
Claude arguments and unanchored native startup remain unchanged.

The mailbox remains addressed by stable `agent_id`, so queued mail survives a
session replacement and is drained by the new owner. Historical native IDs
may be retained as bounded local diagnostics, but routing and collision checks
consult only the current fenced lease.

## First-class terminal baseline

A terminal host is first-class when a clean installation can:

1. launch each configured harness in its project with the correct identity;
2. send and inspect durable mail while the recipient is offline;
3. wake an online recipient through the harness adapter, not injected keys;
4. acknowledge and atomically archive the message with `rt-ack`;
5. diagnose and recover the harness adapter without installing cmux.

`roundtable-smoke` automates the core portion in an isolated environment with
no optional terminal adapter loaded. The remaining release gate is the real
Claude, Hermes, and Codex wake/UX matrix in Terminal.app, iTerm2, and Ghostty.
cmux must pass the same baseline and may additionally expose its optional
workspace features.

tmux is a multiplexer rather than a terminal emulator. Same-host tmux and
cross-host SSH require their own lifecycle and wake acceptance before support
is claimed; neither should fork the core transport.

## Current implementation boundary

The release candidate now implements the host-local fenced lease, unified
launcher selector, no-Git project initialization, dry-run-first harness setup,
owned global skill links, Claude lifecycle hooks, the Hermes lifecycle plugin,
Codex SessionStart auto-bind, and owned Codex service definitions. Automated
tests exercise those config changes and their symmetric removal from an
installed release artifact.

Codex setup writes but does not load service definitions. The unified launcher
then performs a fail-closed preflight: cold services and a stopped wake bridge
can be repaired automatically, while a shared app-server reload is offered
only outside Codex and only when no active or ambiguous Codex lease exists. A
fresh trusted SessionStart hook queues the native thread identity; the bridge
validates it against the exact project cwd and fenced launcher lease. Manual
`rt-codex-wake bind` remains a diagnostic fallback.

The remaining P0 promotion work is:

1. load a clean npm Codex `0.144.6` daemon safely, then pass the real
   send-to-wake-to-drain/ack acceptance;
2. install an official standalone Codex and pass the same protocol and
   end-to-end gates before claiming support;
3. pass real clean-account Claude and Hermes skill discovery, lifecycle, and
   wake acceptance;
4. repeat the same harness acceptance in Terminal.app, iTerm2, Ghostty, and
   cmux;
5. test cmux topology, navigation, and notifications separately as optional
   adapter behavior.

Until the real gates pass, the core and onboarding mechanics are distributable
as a release candidate, but mainstream-terminal support is not yet promoted as
complete.
