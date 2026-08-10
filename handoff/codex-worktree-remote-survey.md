# Codex 0.147.0 worktree / remote surface survey

Date: 2026-08-10

This is a read-only upstream-source survey requested by Claude. It does not
change Roundtable product code.

## Scope and provenance

- Installed executable reports `codex-cli 0.147.0`; its standalone package
  manifest is version `0.147.0` for `aarch64-apple-darwin`.
- The source review is the upstream `rust-v0.147.0` tag (peeled commit
  `be6e8eac029b183056b7e4402879f15d2c85f61b`, 2026-08-06). References below
  are paths and line ranges in that exact source snapshot.
- The installed binary help was checked in the same review. It has no
  `worktree` command; it does expose `-C/--cd`, `resume`, `fork`, `app-server`,
  `remote-control`, and the experimental `cloud` command.

## Executive answer

### (a) Native worktree and parallel-session support

Codex has no Claude-style native worktree manager or `--worktree` flag in
0.147.0. The top-level command enum contains the session, app-server, remote
control, and cloud commands, but no worktree command
(`codex-rs/cli/src/main.rs:127-215`). The generic working-root option is
`-C/--cd`; its contract is simply “use the specified directory as the working
root” (`codex-rs/utils/cli/src/shared_options.rs:61-73`). The installed help
matches this source surface.

There are two kinds of built-in parallelism, neither of which creates a Git
worktree:

1. `thread/fork` copies a conversation into a new thread and accepts an
   optional `cwd` override (`codex-rs/app-server/README.md:163-166`,
   `codex-rs/app-server-protocol/src/protocol/v2/thread.rs:306-399`).
2. AgentControl / `spawn_agent` creates Codex subagent threads. The resulting
   parent/child relationship is persisted (`Thread.parentThreadId`,
   `Thread.sessionId`, and `Thread.forkedFromId`), but the source spawn path
   contains no `git worktree add` or worktree lifecycle manager
   (`codex-rs/app-server-protocol/schema/typescript/v2/Thread.ts:12-24`,
   `codex-rs/core/src/thread_manager.rs:1606-1747`).

Therefore Git isolation remains an external concern. Pneu should create and
register a linked worktree, then start Codex in that absolute path using either
`codex --cd <worktree>` or app-server `thread/start {"cwd": ...}`. A thread
spawn/fork can be used for conversation topology, but must not be treated as a
worktree allocator.

### (b) Remote-driving, pairing, and cloud surfaces

There is a native remote-driving surface beyond local app-server thread RPC:

- `codex remote-control [start|stop|pair]` manages an app-server daemon and
  creates a short-lived manual pairing code
  (`codex-rs/cli/src/remote_control_cmd.rs:31-62`).
- The daemon README explicitly describes remote clients such as desktop and
  mobile apps, including machines reached over SSH
  (`codex-rs/app-server-daemon/README.md:3-9`).
- The app-server protocol exposes enable/disable/status, pairing start/status,
  and controller list/revoke methods; the transport owns an authenticated
  remote-control WebSocket and enrollment state
  (`codex-rs/app-server/README.md:255-262`,
  `codex-rs/app-server-transport/src/transport/remote_control/mod.rs:65-123`,
  `codex-rs/app-server-transport/src/transport/remote_control/websocket.rs:451-520`).

`--remote` is a separate TUI client option: it connects the TUI to a supplied
`ws://`, `wss://`, or Unix app-server endpoint and optionally reads a bearer
token from an environment variable (`codex-rs/cli/src/main.rs:910-922,
2450-2482`). It is transport selection, not worktree provisioning.

The experimental `cloud` command is a Codex Cloud task browser/apply surface
(`codex-rs/cli/src/main.rs:196-201`). The reviewed source does not make it a
live Roundtable seat handoff or a Git-worktree allocator. Treat it as a
separate cloud-task workflow unless a future protocol contract says otherwise.

### (c) Extension points for Pneu-owned worktree/session creation

Codex has enough supported surfaces for Pneu to own orchestration without a
new Codex plugin API:

- App-server `thread/start`, `thread/resume`, `thread/fork`, `turn/start`, and
  `thread/settings/update` are explicit JSON-RPC entry points for creating,
  reopening, branching, and changing a thread's working directory
  (`codex-rs/app-server/README.md:161-180`,
  `codex-rs/app-server-protocol/src/protocol/v2/thread.rs:57-115,211-398`,
  `codex-rs/app-server-protocol/schema/typescript/v2/TurnStartParams.ts:13-41`).
- Lifecycle hooks are configurable in Codex config. The supported event set
  includes `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `SubagentStart`,
  `SubagentStop`, and tool/compaction/stop events
  (`codex-rs/config/src/hook_config.rs:10-58`). Command handlers are the
  natural bridge for an external binder, subject to Codex's hook trust and
  managed-hook policy.
- `SessionStart` is queued during session construction, then dispatched from
  the first turn's hook path (`codex-rs/core/src/session/session.rs:1300-1317`,
  `codex-rs/core/src/session/turn.rs:221-238`,
  `codex-rs/core/src/hook_runtime.rs:102-153`). This is turn-gated: a newly
  started thread can exist before the hook has run. A short preamble turn is
  the smallest deterministic way for a launcher to force the binding point.
- The legacy `notify` config is only an external command after a completed
  agent turn, not a session/worktree creation hook
  (`codex-rs/core/src/config/mod.rs:723-746`). It is useful for liveness, not
  for initial seat ownership.
- `hooks/list` evaluates hooks for each supplied `cwd`. For linked Git
  worktrees, project hook declarations are read from the matching `.codex/`
  folders in the root checkout rather than divergent declarations kept only in
  the linked worktree (`codex-rs/app-server/README.md:1891-1897`). Pneu should
  keep the project-level binding declaration authoritative in the root checkout
  and pass the worktree path as the evaluation cwd.

Recommended ownership boundary: Pneu creates the linked worktree and durable
seat record, starts/resumes the Codex thread with an absolute `cwd`, and uses a
Codex hook or explicit preamble turn only to publish the thread binding. No
Codex-side worktree plugin is needed for this design.

### (d) Thread-to-cwd anchoring and linked-worktree constraints

The app-server `Thread` object persists both a rollout `path` and a captured
absolute `cwd`, plus session/fork/parent lineage and optional Git metadata
(`codex-rs/app-server-protocol/schema/typescript/v2/Thread.ts:12-78`). The Git
metadata is only `{sha, branch, originUrl}`, implemented at
`codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs:161-168`;
there is no worktree identity field. Pneu therefore needs its own stable
seat-to-worktree mapping.

The anchoring rules are:

- `thread/start`, `thread/resume`, and `thread/fork` accept a `cwd` override.
  On resume, the source rollout's `SessionMeta.cwd` is recovered as
  `history_cwd`; an explicit request override is then applied when loading the
  config (`codex-rs/protocol/src/protocol.rs:2604-2610,2758-2763`,
  `codex-rs/app-server/src/request_processors/thread_processor.rs:3176-3205`).
  Resuming an old thread with no override therefore naturally points at its
  previously recorded worktree path, which may be stale after a move.
- `turn/start` documents `cwd` as an override for the current and subsequent
  turns (`codex-rs/app-server-protocol/schema/typescript/v2/TurnStartParams.ts:13-26`);
  `thread/settings/update` has the same subsequent-turn semantics
  (`codex-rs/app-server-protocol/src/protocol/v2/thread.rs:211-269`). These are
  explicit re-anchoring operations, not automatic Git-worktree detection.
- Do not confuse the resume `path` parameter with cwd. `path` selects the
  persisted rollout file. If the target thread is already running, a non-empty
  path is only accepted when it matches the active rollout path; otherwise
  app-server rejects the request (`codex-rs/app-server-protocol/src/protocol/v2/thread.rs:311-345`,
  `codex-rs/app-server/src/request_processors/thread_processor.rs:3530-3545`).
- Codex recognizes linked worktrees for project trust/root resolution, but that
  is a trust/config behavior, not a seat allocator. The source tests explicitly
  model `.git` worktree pointers and resolve the main Git project root
  (`codex-rs/core/src/git_info_tests.rs:686-762`).

Operational implication: use one external seat record per absolute worktree
path and thread ID; on resume, compare the requested seat path with the
returned `thread.cwd` before sending a turn. If they differ, make the user
choose an explicit handoff/re-anchor rather than silently running an old thread
in a new worktree. A moved linked worktree also requires Pneu to update its
record (and, if intended, issue an explicit cwd settings update).

## Decision-relevant findings for Roundtable

1. No native `--worktree`: Pneu must own `git worktree add`, the seat record,
   and launch/app-server `cwd`; Codex fork/spawn is conversation parallelism,
   not filesystem isolation.
2. Native remote control exists: the daemon plus pairing/WebSocket is designed
   for desktop/mobile clients; `--remote` is a TUI-to-app-server transport,
   while Cloud is a separate task workflow.
3. Thread cwd is persisted and resume defaults to the rollout's old cwd; keep a
   seat↔thread↔absolute-worktree mapping, validate on resume, and use a
   preamble turn when a turn-gated `SessionStart` bind must be deterministic.

## Limitations

This survey is limited to the installed 0.147.0 binary's matching upstream
source tag. It does not infer behavior from later releases, unpublished mobile
clients, or Roundtable's own launcher implementation.
