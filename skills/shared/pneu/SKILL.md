---
name: pneu
description: >-
  Use when active pneu coordination is required: an inbound [FROM→TO kind
  id=...] message arrives, the user mentions Hermes/Claude/Codex as peer agents,
  rt-say, rt-ack, rt-refresh, rt-resolve, handoff delivery, multi-instance agent
  routing, or cmux surface-routing bugs. Do not use merely because a repo
  contains .roundtable/agents.yaml.
version: 8.0.0
author: pneu contributors
license: MIT
platforms: [macos]
---

# pneu

Collocated agents (Hermes, Claude, Codex) collaborate per project and talk
through the `rt-*` CLI tools. Messages are **files in the project's mailbox**;
wakes are harness-native. Nothing touches a keyboard.

**Rule #0 — every collaborating session needs one project anchor.** One project
× one logical harness seat = one dedicated session = one mailbox. Every
identity mechanism keys off that canonical project path. For a human, prefer
the unified `pneu` entry: it chooses or safely creates the project first,
then selects a configured harness seat. `roundtable` remains a silent
compatibility alias. The scriptable `rt-claude`,
`rt-hermes`, and `rt-codex` launchers remain available. A project with no open
session for an agent means that agent is **offline** there; mail waits durably
in `new/`.

## One-time host setup

Package installation provides one canonical pneu skill. Onboarding links
that installed copy into each selected harness's global skill directory; do not
ask a vibe-coding user to clone, pull, or copy this skill per project.

For normal users, launch `pneu`: it previews any missing integration for
the selected harness and asks once before applying owned changes. The
standalone setup commands are expert/scriptable controls:

```bash
pneu setup          # read-only plan
pneu setup apply    # owned hooks, plugin/skill links, Codex plists
pneu setup status
```

Setup configures detected harnesses. Repeat `--harness` to make the selection
explicit. It never installs a harness or moves credentials. Codex plist files
are written but not loaded by setup. On the next project-anchored
`pneu codex` launch, a service preflight starts a cold service or stopped
wake bridge automatically. It offers a coordinated app-server reload only from
outside Codex and only when no active or ambiguous Codex seat exists. Never
instruct an ordinary user to run the low-level daemon/wake reload commands.

For removal, never orphan a loaded Codex job. Ask the human to run this from a
normal terminal outside Codex:

```bash
roundtable-setup remove --unload-codex
roundtable-uninstall
```

The command refuses when called inside Codex and touches only pneu's two
owned labels. Claude/Hermes-only onboarding uses plain
`roundtable-setup remove`.

Any directory can become a project and Git is optional:

```bash
pneu                         # recommended interactive entry
roundtable-init --here
roundtable-init new-project          # no Git by default
roundtable-init new-git-project --git
```

## Tools (normally linked on PATH via ~/.local/bin/)

| Tool | Purpose |
|------|---------|
| `pneu` (`roundtable` alias) | Recommended project-first onboarding, harness selection, and launch. |
| `roundtable-setup [plan\|apply\|status\|remove]` | Own host-level harness onboarding; the default is a no-write plan. |
| `roundtable-init --here` / `roundtable-init NAME` | Adopt the current directory or create and register a project; add `--git` only when wanted. |
| `rt-claude` / `rt-hermes` / `rt-codex` | Claim a fenced project seat and launch the real harness executable. |
| `rt-say <agent>[@<project>] <kind> "body"` | Write the message into this project or one exact registered sibling worktree (atomic maildir). |
| `rt-ack <id>[,<id>...] ["note"]` | Acknowledge and archive received message(s). Comma-batches. The sender gets a quiet `ack-*` file. |
| `rt-inbox` | List un-ack'd inbound messages. |
| `rt-projects <list\|add\|rm\|upgrade>` | Maintain the validated project registry; `upgrade` is the explicit, backed-up v1→v2 gate. |
| `rt-projects resolve ROOT` | Diagnostic JSON view of the UUID-pinned mailbox selected by the registry. |
| `rt-projects migrate ROOT` | Exclusively copy a local mailbox through a verified archive and durable recovery record into the central UUID store. |
| `rt-projects rollback ROOT --manifest PATH` | Copy current central mail back to local; requires the exact active forward recovery record and preserves post-cutover mail. |
| `pneu worktree add NAME [options]` | Create a Git sibling worktree, bootstrap its pneu registry identity, and print its `codex@NAME` launch route. |
| `pneu worktree list` | List registered siblings in the current Git-derived group with branch, UUID, and seat status. |
| `pneu worktree remove NAME [--keep-branch]` | Refuse active/ambiguous seats, unbind Codex, tombstone the registry row, remove the worktree, and delete only a merged branch. |
| `rt-doctor` | Health checks: daemon, socket, RPC, version, bridge, registry, anchor audit. |
| `rt-resolve <agent>` / `rt-refresh` | Diagnostic only: where does cmux think an agent sits. Not part of sending. |

Run them from a project root (a dir with `.roundtable/agents.yaml`). Outside
one, set `ROUNDTABLE_PROJECT_DIR` or `RT_FALLBACK_PROJECT`.

## Worktree lifecycle

`pneu worktree add <name>` resolves the Git common directory from the
current repository (or `--repo PATH`), so any sibling worktree shares one
derived group key. The default target is `../<name>` beside the current repo
root; `--path PATH` may select another path outside that worktree. Before any
mutation the command restates the repository, current branch and commit,
group key, target, new `wt/<name>` branch, and future `codex@<name>` address.
`--dry-run` prints that restatement without changing anything; `--yes` is the
scripted confirmation path.

`pneu worktree remove <name>` resolves exactly one active sibling in the
same group. It refuses active, unhealthy-but-owned, or ambiguous seat state,
then unbinds any Codex binding, tombstones the registry row, and removes the
linked worktree. A branch is deleted only after it is merged; pass
`--keep-branch` to retain it.

Launch dedicated sessions with `rt-codex`, `rt-claude`, or `rt-hermes`. When
called outside a project on a TTY they offer registered projects, project
creation, or (for Claude/Hermes) an explicit unanchored launch. pneu
Codex requires a project anchor; native `codex` remains available for sessions
that do not need pneu messaging. Non-TTY unanchored calls exit 2. All
three launchers select a real harness executable instead of a generated cmux
PATH shim and export the unique configured `RT_FROM` identity. A
multi-instance project must set `RT_FROM` explicitly. Launching one seat from
inside another seat's shell is safe: the launcher discards the inherited seat
environment (including the caller's `RT_FROM`) and claims a fresh identity of
its own. `rt-codex` additionally
injects the `--remote` flag and fenced session environment that its native wake
bridge requires. Direct vendor launch commands do not establish the complete
lease context required for automatic wake; use the `rt-*` launchers for the
supported path.

## Delivery: maildir + native wake (v2, sole path since 2026-07-17)

`rt-say` resolves the registry-selected mailbox under the project's shared
UUID layout lock, then atomically writes each message to
`inbox/<to>/new/<msgid>.md` in that authoritative layout. That write IS the
delivery — it needs no topology map, no live target, no refresh. A physical
mailbox path printed by a diagnostic is not a durable capability across a
layout migration. `sync-ack` files are named `new/ack-<msgid>.md`: quiet
confirmations that never wake anyone and never block a stop; drain them
whenever you are awake for another reason.

The uniform mental model is `agent@project`: every rendered sender and every
explicit destination carries a project suffix. The bare agent form is shorthand
for `agent@<own-project>`. The qualified form resolves exactly one active
project name inside the sender's revalidated derived group, then validates the
agent against that target project's own `agents.yaml`; the own-project name is
also valid and resolves to local delivery. Duplicate names and stale group or
identity claims fail closed. Every newly emitted envelope carries the origin
project UUID, including bare local sends and quiet acknowledgements; `rt-ack`
uses that exact UUID rather than a mutable path or project name when returning
its quiet confirmation.

**Receiving (drain protocol)** — when woken by a tripwire/bridge or told the
inbox has mail: run `rt-inbox -f json`, act on every non-ack message, then
`rt-ack` the ids (comma-batch). A successful `rt-ack` sends the quiet
confirmation and archives those exact inbound files from `new/` to `cur/`.
Comma-batches commit per origin group: a later group's failure leaves that
group in `new/`, while earlier successful groups are already in `cur/` and an
exact retry does not send their quiet receipts again. Receipt delivery and
archival are separate commits; if archival fails after delivery, the command
reports the committed acknowledgement and retrying that group can resend its
receipt. Move any quiet `ack-*` files to `cur/` without acknowledging them;
Claude's hook-provided fenced inbox command performs that quiet-ack drain
itself. Hermes and Codex re-arm automatically after the triggered non-ack
generation is archived.
Claude's Stop hook normally re-arms automatically; never launch a second
watcher from the model turn. One unchanged pending generation receives its
initial wake and at most one Stop-hook retry, then pauses instead of looping.
User interruption and API/authentication failure do not run a usable Stop hook;
mail remains durable, but that Claude session may need a later normal
interaction or resume before it is armed again.

A listing entry with kind `malformed` is a raw file that cannot be delivered
as pneu mail; while it sits in `new/` it keeps waking this seat even
though it carries no readable message. Its `remedy` field names the cleanup
that actually breaks the wake loop: `rt-ack` means acknowledge the listed
raw id with `rt-ack` to archive it; `manual-move` means `rt-ack` cannot
archive that file from this seat (wrong-mailbox recipient, unregistered or
self sender, or not an ackable regular `.md` file), so have the human move
the file out of `new/` — for example into `cur/` — instead.

A valid UUID-aware message may also carry `problem` plus
`remedy: "manual-move"` when its recorded origin is no longer registered,
active/available, or its `agents.yaml` no longer resolves the recorded sender,
or when distinct `new/` and `cur/` copies conflict. Act on any readable request
first, then move the named file out of `new/`; retrying `rt-ack` cannot repair
these classes.

**Arming (Claude)** — the setup-owned SessionStart hook launches the first
fenced inbox watcher for a pneu-launched session, and its Stop hook
normally launches the successor after a completed turn. When mail wakes
Claude, the system reminder prints the package-managed absolute paths for
`rt-inbox`, `rt-ack`, and `rt-say`, including their `--fenced` and
`--no-nudge` flags. Use those exact paths and flag order: setup pre-approves
only lease-validated maildir commands, not a PATH lookalike or the legacy
keyboard route. Ordinary users and agents should not start or kill watcher
processes themselves.

**Arming (Hermes)** — the setup-owned plugin starts the fenced watcher at
Hermes session start and injects a user-visible pneu notice when mail
lands. It is inert outside a complete pneu launcher lease and shuts down
its watcher with the Hermes session.

**Arming (Codex)** — launch through project-anchored `pneu codex` (or
`rt-codex`). The trusted SessionStart hook atomically queues the native thread
identity; the wake bridge validates its exact project cwd and fenced launcher
lease before binding. On first use Codex may ask the human to review the hook
with `/hooks`; never bypass that trust decision. A bare project-anchored
launch primes its own first turn with a fixed no-action activation prompt,
so the seat normally binds before any human message; explicit native
arguments or `RT_CODEX_NO_PRIMER=1` skip the primer and that seat binds on
its first real turn instead. Manual
`rt-codex-wake bind <project-root>` is a diagnostic fallback only. An unbound
session has no waker, but its mail still waits durably like any offline agent's.

`rt-wait-inbox` remains an implementation and diagnostic tool. Arming is
owned by the harness-native lifecycle hooks above (Claude SessionStart/Stop
async hooks, the Hermes plugin, the Codex bridge); never arm a watcher from a
model turn, and specifically never run `rt-wait-inbox ... &` — shell
backgrounding trips the harness's background-operation approval prompt and
freezes an unattended seat. If a seat is unarmed, recover through a normal
interaction or a relaunch, not a hand-started watcher. Never kill the watcher
by process name: another project can have the same executable name. P0 watcher
ownership is fenced by the host-local session lease; old project-local
`.armed-*`, `.last-active`, and `.empty-beats` files are diagnostic-only legacy
state and must not be used as routing or liveness truth.

## Sending

`rt-say <agent>[@<project>] <kind> "body"` from the project root. The bare
form stays inside the current project; `@project` names one registered project
in the same derived group, including the current project by its own name. A
report to another project's seat must use the explicit `agent@project` form.
That's the whole ritual
— no refresh, no resolve, no liveness check. In a remote Codex app-server
turn, sender inference uses `CODEX_THREAD_ID`; outside a harness set
`RT_FROM`. During an automatically woken Claude turn, use the absolute
`rt-say --fenced --no-nudge` form printed by the hook so the setup-owned narrow
permission matches, the current launcher lease is validated, and the archived
keyboard path cannot be selected.

`kind` is a free triage label (fyi, question, answer, proposal, review,
correction, directive, urgent) with no effect on delivery — always one
flag-free token. `rt-say` has no `--kind` or `--refs` options and rejects
them; put any referenced message id in the body. For anything long,
write `handoff/<topic>.md`, commit, and rt-say a one-line pointer.

Emergency keyboard path (`--legacy-nudge-only` + submit-key lore) is archived
in `~/.pneu/docs/legacy-v1-keyboard.md`; human-coordinated use only.

## Receiving

1. Inbound arrives as a mail file
   `[FROM→YOU kind id=<msg_id> origin=<project-uuid>] body` (legacy files may
   omit `origin`).
2. Do what it asks.
3. `rt-ack <msg_id> ["note"]` — batch with commas. This both sends the
   sender's delivery confirmation and archives the processed inbound message.

## When mail sits unanswered

Mail waiting in `new/` means the receiver is offline, unarmed, or busy — not
lost. Diagnose in order: ① is a pneu-launched session open in that
project (Rule #0)? ② does `roundtable-setup status` report the harness
configured? ③ does `rt-doctor` report a current fenced lease and healthy
adapter? ④ for Codex, is the thread bound and are both services healthy?
⑤ has that session ever had a turn? A freshly launched seat with zero
interactions is effectively unarmed until its first turn — interact with it
once. A busy seat is not a lost seat either: wake latency on long turns is
minutes-level because the current turn finishes before the drain. Fix the
native waker; never re-send by keyboard reflex or on latency alone.

## Multi-instance

A project can define more than one addressable instance ID under `instances:`
in `agents.yaml`; a single instance normally reuses the base name (`codex`).
Build Week P0 permits only one active seat per harness in a project, so a
second simultaneous Claude, Codex, or Hermes launch is rejected instead of
guessing. An inactive prior seat does not conflict: a fresh launch gets a new
fenced session lease. Mail addressing needs only the instance ID; cmux launch
metadata (cwd anchor, title) matters for the diagnostic `rt-resolve` view and
legacy tooling, not for delivery:

```yaml
instances:
  - { id: codex-build,  match: { cwd: /path/to/build } }
  - { id: codex-review, match: { title: review } }
```

## Collaboration discipline

- **The human lead arbitrates.** Agents propose; the human decides. Surface
  decisions; don't unilaterally enact irreversible ones.
- **No unauthorized intrusion.** Don't modify another harness's config, plugins,
  hooks, or orientation files without the human lead's authorization.
- **No ack-of-ack.** Once you receive a `sync-ack`, stop — don't acknowledge an
  acknowledgement.

## More

Optional multi-agent playbooks (cross-agent freeze/merge signoff, `/goal` build
dispatch, git-based doc collaboration) live in `~/.pneu/docs/workflows/` —
not needed for ordinary messaging.
