Postal Network, Entirely Unplugged

# pneu

**pneu** (Project-Native Envelope Utility) is a durable, local messaging and
coordination layer for coding agents that share a machine. It uses per-project
maildir delivery as the fact source, fenced seat identities, and
harness-native wake bridges. Delivery works without a terminal multiplexer,
daemon, account, or network.

## The short version

An agent sends a message with `rt-say`. The command atomically writes a file
into the recipient's `new/` mailbox; that write is delivery. A Claude Code,
Codex, Hermes, OpenClaw, or Grok Build adapter may wake the recipient. The
recipient acts and runs `rt-ack`, which sends the quiet receipt and archives
the message to `cur/`.
Offline seats keep their mail until they return.

```text
rt-say  ->  project mailbox: new/  ->  agent acts  ->  rt-ack  ->  cur/
              optional native wake
```

The `rt-*` names and `RT_*` environment variables are pneu's tool prefix and
remain stable in 1.3.0. Project state remains under `.roundtable/`, and
`roundtable.*` wire and registry schema identifiers are unchanged. `roundtable`
is a silent compatibility alias for the primary `pneu` command.

## Install

The release archive is the supported new-user path. It contains the pneu
wheel and locked dependencies but not Python; the host needs CPython 3.11
through 3.14.

```bash
tar -xzf pneu-1.3.0-macos.tar.gz
cd pneu-1.3.0
./install
export PATH="$HOME/.local/bin:$PATH"
pneu
```

The default install prefix is `~/.pneu`. If `~/.roundtable` contains an
existing install, the installer moves its managed and durable state to
`~/.pneu`, writes a recovery manifest, and leaves `~/.roundtable` as a
compatibility symlink. The migration is fail-closed when both prefixes hold
independent state and is safe to repeat.

The installer owns only its recorded wrappers, links, version trees, and
managed onboarding assets. Project registries, central mail, runtime state,
layout locks, migration records, and archival backups are preserved during
uninstall unless an explicit runtime purge is requested.

## Daily commands

```text
pneu                         project-first onboarding and launch
pneu guide                   show the local mailroom model
pneu setup                   preview harness setup (read-only)
pneu setup apply             apply owned harness setup
pneu doctor                  diagnose the current project and seat
pneu worktree add NAME       create a registered tree in ../<repo>-worktree/
pneu rc-host enable          enable project-only Claude phone worktree spawn
pneu rc-host status          inspect this project's phone host
rt-say AGENT KIND "MESSAGE" durable local or sibling delivery
rt-inbox -f json             inspect waiting mail
rt-ack MESSAGE_ID            acknowledge and archive handled mail
```

The explicit tool forms remain available: `roundtable-init`,
`roundtable-setup`, `roundtable-smoke`, and `roundtable-uninstall` retain
their names for script compatibility, as do every `rt-*` command. The
compatibility alias emits no rename warning.

On a full TTY, `pneu` presents one compact seat card with the last-used seat
selected, three status lines, and in-place phone access controls. A single
Enter launches that seat. Line-oriented and non-TTY streams retain the
numbered selector for script compatibility; the full guide appears only with
`?` or `pneu guide`.

## Architecture

### Durable delivery

Mail is written atomically into the registry-selected UUID-addressed mailbox.
The maildir, not a pane, title, topology map, or wake process, is authoritative.
`rt-inbox -f json` lists logical messages; duplicate ledger/maildir entries
with one message id are one message. Quiet `ack-*` and `sync-ack` receipts are
archived directly and never acknowledged again.

### Native wake

Wake-up is an adapter layered over delivery:

- Claude Code uses asynchronous SessionStart/Stop lifecycle hooks.
- Codex uses its app-server and Unix-socket bridge.
- Hermes uses its session-start plugin.
- OpenClaw uses an isolated Gateway adapter.
- Grok Build uses an isolated ACP adapter.

Project-anchored `rt-claude` launches enable Remote Control as
`<agent>@<project-name>` by default; pass `--remote-control` to choose the name
or set `RT_CLAUDE_NO_RC=1` to opt out.

cmux is optional. The core send, receive, acknowledge, recovery, and doctor
paths work in ordinary terminals and do not inject keyboard input.

An armed Claude or Hermes inbox watcher is long-lived: while `new/` is empty it
renews its fenced lease silently and does not emit a heartbeat wake or create a
model turn. The watcher wakes only when mail appears; its 30-second health TTL
is renewed on a tighter cadence so a dead watcher becomes stale promptly.
For dispatches or questions that need an answer, `rt-say --expect-reply 30m ...`
adds a durable one-shot sender alarm: a quiet acknowledgement clears it, while
an unanswered deadline wakes the sender once through the existing watcher.

### Managed worktrees and project phone access

`pneu worktree add NAME` creates
`<repo-parent>/<repo-name>-worktree/NAME` by default. The container is created
on demand and holds only pneu-created linked trees; the main checkout never
migrates into it. `--path` remains the explicit escape hatch.

`pneu rc-host enable` is an expert, project-anchored opt-in for Claude mobile
or web worktree spawn. It first requires an already accepted Claude workspace
trust decision, then owns one per-project LaunchAgent and only that project's
untracked `.claude/settings.local.json` WorktreeCreate/WorktreeRemove hooks.
It never installs those hooks globally. Once enabled, phone access is a
project trait, so a native `claude rc` started in that project sees the same
hooks; projects that were never enabled keep native behavior. Disable it with
`pneu rc-host disable` before removing Claude onboarding or uninstalling pneu.

The create hook must return exactly one absolute registered worktree path;
empty output is an error. Claude does not process `.worktreeinclude` while a
custom WorktreeCreate hook is active, so copy or bootstrap any extra files in
another explicit workflow. Phone-spawned SessionStart events are adopted only
inside exact registered projects and never replace another live lease.

### Installation and migration

Pneu installs versioned command trees under `~/.pneu`, with `current` selecting
the active version and stable links under `~/.local/bin`. On a legacy install,
the installer moves `versions`, the project registry, central `mail`, runtime,
`backups`, `migration-records`, and related layout state as one prefix move.
The old prefix symlink keeps already-deployed hook, plist, and permission
paths valid. Re-running harness setup rewrites pneu-owned paths to `~/.pneu`.

## Compatibility and limitations

The repository records validation evidence and open promotion gates in
[`docs/compatibility.md`](docs/compatibility.md). A supported platform/runtime
claim requires a real end-to-end smoke test; version-number comparisons and
fixtures alone do not establish support. Cross-host SSH, Linux service
management, and multi-auth switching remain out of scope for 1.3.0. The
project phone-host path remains a release candidate until its required live
phone-side smoke passes; fixtures and CLI inspection alone are not a support
claim.

## History

The working name was **roundtable** through `0.3.0-dev`. After a four-round,
collision-checked search across western creative, CJK imagery, deep Chinese,
and Wade–Giles military/pastoral/commercial directions, Ocean selected
**pneu**: Paris pneumatique slang where *un pneu* is the message itself. The
technical backronym is Project-Native Envelope Utility; the public tagline is
Postal Network, Entirely Unplugged. The 0.3.0 content rolls into this 1.0.0
release rather than shipping as a public 0.3.0.

The Build Week phase and its attribution remain documented exactly in
[`PROVENANCE.md`](PROVENANCE.md) and [`CREDITS.md`](CREDITS.md). The product
phase is developed on the product worktree with the human product lead as
final decision-maker.

## Development

Use the shared environment for Python commands:

```bash
mamba run -n general pytest -q
mamba run -n general python -m compileall -q bin pneu_packaging scripts tests
mamba run -n general python scripts/check_public_safety.py
```

For the source-install path:

```bash
mamba run -n general ./scripts/install.sh
```

See [`docs/release.md`](docs/release.md) for the deterministic artifact
workflow and [`docs/install.md`](docs/install.md) for ownership and migration
details.
