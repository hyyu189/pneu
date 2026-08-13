# Codex 0.147.0 `/btw` and thread persistence

Date: 2026-08-12  
Scope: read-only investigation of upstream Codex tag `rust-v0.147.0`, commit
`be6e8eac029b183056b7e4402879f15d2c85f61b`, plus public-safe host artifacts.
No production state was changed.

## Conclusions

1. `/btw` is an alias of `/side`. It creates a **new thread ID** by calling
   app-server `thread/fork`, with `ephemeral=true`. The child inherits the
   parent's model context but has no rollout file or thread-store record.
2. `/btw` has no merge or promote operation in 0.147.0. The TUI can toggle
   between parent and child; returning or navigating elsewhere interrupts and
   unsubscribes the child and drops its TUI-local state. A separate ordinary
   `/fork` is a persistent fork, but it is not a way to merge a `/btw` child.
3. `/btw` cannot be the first action in a genuinely fresh, zero-turn TUI.
   Forking needs a materialized source rollout. If the parent has not yet
   received a user turn, the TUI reports that the side conversation is
   unavailable. The rejected `/btw` attempt does not itself persist the parent.
4. A normal non-ephemeral `thread/start` allocates the thread in memory but
   defers creating its rollout file. The first user turn runs the queued
   `SessionStart` path, requests the hook transcript path, and materializes the
   rollout. An untouched thread can therefore remain rollout-less for its whole
   process lifetime, including clean shutdown, without being `ephemeral=true`.
5. `thread/start` and `thread/fork` are the app-server RPCs here that allocate
   new IDs. The remote TUI sends `threadSource=user`; the app-server process is
   launched with session source `vscode`. `initialize`, `thread/read`, and
   `thread/resume` do not allocate a new ID.
6. The two observed anchors are not two persistence states of one thread. The
   old rollout-less anchor is strongly identified as an ephemeral side child;
   the later anchor is a separate normal thread created by a fresh Roundtable
   launcher activation and materialized by its primer turn.

## Exact `/btw` mechanics

- `tui/src/slash_command.rs:124-126` describes `/side` and `/btw` as a side
  conversation in an ephemeral fork. `tui/src/chatwidget/slash_dispatch.rs:881-898`
  routes the inline command and rejects it before a session ID exists.
- `tui/src/app/side.rs:579-591` clones the current configuration and explicitly
  sets `ephemeral=true`. `:656-768` asks app-server to fork, receives the fresh
  child ID, injects a boundary item, switches to the child, and submits the
  inline question as its first turn.
- `tui/src/app_server_session.rs:723-802` implements the side fork with the
  regular `thread/fork` RPC. `:1783-1818` supplies the source thread ID,
  `ephemeral`, cwd, and `threadSource=user`.
- A zero-turn parent has no source rollout to fork. The TUI maps the upstream
  `no rollout found` failure to the user-facing "current conversation has not
  started" message (`tui/src/app/side.rs:604-613`; regression at `:140-148`).
- The app-server response retains real fork lineage, but the side UI hides
  `forkedFromId` in its local snapshot so the side begins visually at its
  boundary (`tui/src/app/side.rs:626-635`).
- Ctrl-/ toggles parent and side without closing either (`tui/src/app/side.rs:391-412`).
  Returning or switching elsewhere discards the side (`:354-389`); discard
  interrupts, unsubscribes, and removes TUI-local state (`:414-503`). There is
  no side-to-parent merge path in this module or its dispatchers.

### IDs and persistence

| Operation | ID | Rollout | Fork lineage |
|---|---|---|---|
| Parent before `/btw` | unchanged | must already be materialized for the fork to succeed | unchanged |
| `/btw` child | fresh | none; in-memory only | app-server reports parent ID, though TUI hides it locally |
| ordinary `/fork` child | fresh | persistent rollout | reports parent ID |
| return from `/btw` | parent selected again | parent unchanged; child remains non-durable | child is discarded/unsubscribed by TUI |

An ephemeral side child remains addressable only while the owning app-server
still has it loaded. It cannot be cold-resumed after process loss because
there is no rollout to discover. Unsubscribe removes event subscription state;
the durable fact is still that no thread record exists.

## SessionStart hooks

Core queues `SessionStart(startup)` for both new and forked sessions and queues
`resume` or `clear` for those respective origins
(`core/src/session/session.rs:1300-1317`). The hook is turn-gated:
`core/src/session/turn.rs:233-235` calls it when the first turn begins.

For a root `/btw` child, the target is ordinary `SessionStart`, not
`SubagentStart` (`core/src/hook_runtime.rs:103-145`). The inline `/btw` question
starts a child turn, so the child gets a startup hook. Because the side is
ephemeral, `hook_transcript_path()` returns `None`; a normal persistent fork
gets its own transcript path (`core/src/session/mod.rs:4067-4084`). Merely
allocating either child without starting a turn does not run the hook.

## When rollout files appear

- `core/src/session/session.rs:631-710` creates no `LiveThread` persistence at
  all when `config.ephemeral` is true. Otherwise new, cleared, and forked
  sessions create a persistent thread recorder.
- `thread-store/src/local/create_thread.rs:10-51` constructs that recorder.
- `rollout/src/recorder.rs:787-920` precomputes path and metadata for a new
  session but defers file creation/open until an explicit persist operation.
- `rollout/src/recorder.rs:1624-1649` makes flush and shutdown no-ops while the
  recorder is still deferred and has no pending items. Thus a normal untouched
  mainline may remain rollout-less indefinitely, but it is still a loaded
  `ephemeral=false` thread until the process exits.
- At the first turn, `run_pending_session_start_hooks` asks for
  `hook_transcript_path` (`core/src/hook_runtime.rs:103-145`), which ensures the
  persistent rollout is materialized (`core/src/session/mod.rs:4074-4084`).

The standard TUI sends `ephemeral=config.ephemeral`, paginated history for a
non-ephemeral thread, and `threadSource=user`
(`tui/src/app_server_session.rs:1693-1727`). The normal configuration default is
`ephemeral=false` (`core/src/config/mod.rs:925-929`). A client can explicitly
call `thread/start` with `ephemeral=true`, but that is not the standard TUI
mainline path. `/btw` is the standard interactive path that deliberately does
so.

## App-server creation and read behavior

The 0.147.0 CLI starts app-server with `SessionSource::VSCode`
(`cli/src/main.rs:1173-1183`). Within that process:

- `thread/start` loads configuration and calls the thread manager to create a
  fresh thread (`app-server/src/request_processors/thread_processor.rs:982-1095,
  1144-1275`). The TUI request sets `threadSource=user`.
- `thread/fork` reads the source history, creates a new child ID, and chooses
  persistent or ephemeral storage from the request
  (`app-server/src/request_processors/thread_processor.rs:4010-4213,
  4354-4396`). The TUI also sets `threadSource=user` for forks.
- `thread/resume` reopens the supplied ID. It does not allocate a replacement.
- `initialize` records connection capabilities and client identity/originator,
  then returns; it does not touch the thread manager
  (`app-server/src/request_processors/initialize_processor.rs:44-158` and
  `app-server/src/message_processor.rs:763-805`).
- `thread/read` first looks for an already-loaded thread or stored rollout. If
  neither exists it returns `thread not loaded` without creating anything
  (`app-server/src/request_processors/thread_processor.rs:2275-2351`). A
  successful `includeTurns=true` read of an already-loaded, non-ephemeral,
  paginated thread can explicitly persist that existing lazy thread at
  `:2476-2504`; the observed failed read never reaches that side-effect path.

### cwd and originator

For `thread/start`, an explicit request cwd wins; otherwise configuration falls
back to the app-server process cwd (`core/src/config/mod.rs:1454-1458`). The TUI
sends its configured cwd in embedded mode and a supplied remote cwd override in
remote mode (`tui/src/app_server_session.rs:1822-1831`). For `thread/fork`, an
explicit request cwd wins and otherwise the source thread cwd is the fallback
(`app-server/src/request_processors/thread_processor.rs:4128-4207`).

`initialize.clientInfo.name` establishes the originator metadata for later
threads (`app-server/src/request_processors/initialize_processor.rs:81-139`).
Therefore `originator=roundtable_rt_codex_wake` identifies the initialized
Roundtable client path, not a hidden thread-creation RPC.

## Reconstruction of the two anchors

### Old anchor: `019ff83e-5cc2...`

Public-safe read-only evidence:

- no rollout exists for this ID under the Codex session store;
- three host history entries are attributed to this ID, so it was not merely an
  untouched lazy normal thread;
- the first turn emitted a Roundtable SessionStart bind request;
- the wake bridge immediately rejected it with `refusing to bind an ephemeral
  thread`;
- the operator reports entering `/btw` in this session.

Together these facts identify the anchor as the `/btw` child ID, not the
pre-side parent. Its first child turn ran SessionStart, but it could not create
a rollout because the child configuration was truly ephemeral. After the
owning app-server lost that in-memory thread, `thread/read` correctly returned
`thread not loaded`. The elapsed 30 hours is not itself causal.

Because `/btw` cannot fork a zero-turn parent, some different parent thread had
already been materialized before this child was created. The available
artifacts do not identify that parent's ID, so no parent ID is asserted here.

### New anchor: `019ff97a-0d64...`

The rollout begins at `2026-08-13T04:55:53Z` with cwd `rt-launch`, source
`vscode`, `threadSource=user`, originator `roundtable_rt_codex_wake`, and no
`forkedFromId`. The corresponding launch-intent record armed that same ID at
`04:55:53Z`. Its first user turn is the exact Roundtable seat-activation primer,
followed by the expected `ready`; the SessionStart request and successful
auto-bind follow.

This is positive creator evidence for a fresh `thread/start` issued by the
Roundtable launcher/TUI. The primer was the first turn that materialized the
rollout. There is no evidence of a fork or merge from `019ff83e-5cc2...`, and
the missing `forkedFromId` plus independent activation sequence rule that out.

## Upstream source

All source references above are against OpenAI Codex commit
[`be6e8eac`](https://github.com/openai/codex/tree/be6e8eac029b183056b7e4402879f15d2c85f61b).
The app-server's own protocol summary independently describes `thread/start`
as creating a new thread and `thread/fork` as creating a new child ID in
[`codex-rs/app-server/README.md:163-166`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server/README.md#L163-L166).
