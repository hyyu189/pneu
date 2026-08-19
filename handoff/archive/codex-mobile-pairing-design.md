# D12.1 — Codex mobile pairing integration design

> Status: historical record — D12.1 research: NO-GO for unscoped mobile pairing

Date: 2026-08-11  
Owner: Codex  
Scope: read-only survey of Codex `0.147.0` (`rust-v0.147.0`, source commit
`be6e8eac029b183056b7e4402879f15d2c85f61b`). No Roundtable product code is
changed by this handoff.

## Executive decision

**NO-GO for unscoped mobile pairing against a shared Roundtable app-server.**

The Codex remote-control transport is attached to an app-server, not to a
Roundtable lease, project, or seat. Its request processor does not receive an
authorization scope from the remote connection, and `thread/list` has no
client, seat, or project fence. A paired controller should therefore be
treated as having the same thread surface as the app-server it is enrolled
against. If several Roundtable seats share one app-server/CODEX_HOME, an
unscoped phone can expose the fleet and can contend for writable threads.

There is a conditional **GO** only after one of these boundaries is real and
tested:

1. one app-server and `CODEX_HOME` per fenced seat (the phone is then scoped by
   process/environment isolation); or
2. a Roundtable ACL/proxy that allowlists methods and thread IDs for one lease
   and rejects everything else.

Until then, pneu must not expose a pairing command as a normal feature. A
future implementation must also retain the existing daemon identity and
launchd lineage gates and must never start, stop, bootstrap, or reload the
app-server from the pairing path.

## What Codex 0.147.0 actually owns

### `remote-control start` is a lifecycle/supervisor path

The CLI has `remote-control start|stop|pair` in
`codex-rs/cli/src/remote_control_cmd.rs` (lines 31–95). `start` calls
`codex_app_server_daemon::ensure_remote_control_ready()`; `pair` calls
`start_remote_control_pairing()`.

`codex-rs/app-server-daemon/src/lib.rs` (lines 253–359, 494–585) derives a
fixed control socket under `CODEX_HOME/app-server-control/app-server-control.sock`
and stores daemon state under `CODEX_HOME/app-server-daemon`. It probes the
socket and checks for the Codex-managed pid backend. If a responsive socket is
not managed by that Codex daemon, the remote-control start path returns the
unowned-app-server error; it does not adopt a launchd-owned process. Starting
or restarting this command from pneu would therefore collide with our
launchd-owned single socket and could change lifecycle ownership. This is a
hard **do not call** for the integration.

### Direct RPC is technically possible, but it is not an authorization layer

`codex-rs/app-server-daemon/src/remote_control_client.rs` (lines 39–132,
154–214) connects to an already-running app-server UDS WebSocket, initializes
with the experimental API, and sends:

* `remoteControl/enable` or `remoteControl/disable`;
* `remoteControl/pairing/start` with `manualCode: true`;
* polling through the corresponding pairing/status request.

The helper does not require the Codex daemon supervisor. Roundtable's existing
`AppServerClient` can issue the same JSON-RPC, but only after
`require_supported_daemon`/`require_daemon_identity` and the launchd peer and
path checks in `bin/_rtcodex.py` (roughly lines 1592–1631, 1932–2170).
Re-check the identity immediately before any mutating RPC to avoid a
foreign-socket/TOCTOU mistake.

### Ephemeral enable is process state, not revocation

Protocol shapes in `codex-rs/app-server-protocol/src/protocol/v2/remote_control.rs`
(lines 6–24, 67–166) define `ephemeral`, pairing code/expiry, client listing,
and client revocation. The app-server README confirms that an ephemeral
enable/disable changes the current process only; it does not persist the
enabled preference and disabling does **not** revoke enrolled controllers.

`codex-rs/app-server-transport/src/transport/remote_control/mod.rs`
(lines 249–376, 387–617) shows the state split:

* durable enable writes the SQLite preference;
* ephemeral enable keeps the preference unset and is disabled after restart;
* pairing can still create/update enrollment metadata keyed by websocket URL,
  account, and `app_server_client_name`;
* controller grants remain backend-side and require
  `remoteControl/client/list` plus `remoteControl/client/revoke` to remove.

Never put a pairing code in a mail, log, registry, or handoff file. Display it
only in the operator's TTY and treat the expiry returned by
`remoteControl/pairing/start` as authoritative.

## Thread scope and fenced-seat impact

The remote client tracker creates a normal app-server connection with origin
`RemoteControl` (`codex-rs/app-server-transport/src/transport/remote_control/client_tracker.rs`,
lines 147–215). The request processor receives the connection/session but no
seat or project ACL. `thread/list` supports ordinary filters such as cwd and
search, but no client/lease filter (`codex-rs/app-server/src/thread_processor.rs`,
lines 2006–2104; `app-server/README.md`, lines 161–173).

This is a source-based security inference: the mobile UI/backend may add
presentation constraints not visible in this checkout, but the app-server
surface itself is unscoped. A paired controller should be assumed able to
list/read/resume threads on the enrolled environment. Resume also has a
single-writer constraint: only one app-server process may hold a thread open
for writing (`app-server/README.md`, lines 350–356). A phone can consequently
both observe other seats and create write contention.

The special mobile client names used by
`codex-rs/app-server/src/request_processors/thread_resume_redaction.rs`
(lines 6–38) only redact MCP/image details; they do not fence thread access.

## Proposed pneu design (only after isolation/ACL exists)

### Preconditions

1. Pass the existing Roundtable daemon preflight and peer-lineage checks.
2. Capture a socket identity snapshot (canonical socket path, launchd service
   path, peer pid, daemon/app-server versions, and project/lease UUID). Refuse
   if any value changes during the operation.
3. Use a stable client name such as `roundtable_rt_codex_mobile`; it is part of
   Codex's enrollment persistence key.
4. Use a per-seat app-server/CODEX_HOME or an enforcing ACL proxy. Do not rely
   on a phone-side cwd selector as a security boundary.

### Pair

1. Directly call `remoteControl/enable {"ephemeral": true}`.
2. Wait for `remoteControl/status` to become connected/ready.
3. Call `remoteControl/pairing/start {"manualCode": true}`.
4. Show the short-lived code in the local TTY only; poll pairing status until
   claimed or expired.
5. Record only non-secret state in pneu (environment ID, client ID after
   claim, expiry, socket snapshot, and lease revision). Never persist the
   code or relay it through Roundtable mail.

### Stop/revoke

1. List controllers for the exact environment and revoke the known client ID.
2. Call `remoteControl/disable {"ephemeral": true}`.
3. Report clearly that disable without a successful revoke is not a security
   stop. If no client ID is known, fail closed and require an operator to
   revoke from the controller list; do not claim success.

### Fail-closed rollback

* Any preflight, policy, account, or socket-identity failure: issue no RPC and
  make no service change.
* Enable succeeds but pairing start fails: best-effort ephemeral disable;
  preserve the failure and any cleanup error for the operator.
* Identity drift during pairing: ephemeral disable, then refuse. Never
  `kickstart`, bootout, restart, bootstrap, or adopt the socket.
* Pairing expiry is a normal failed attempt; it must not silently retry or
  leave an enabled durable preference.
* Revoke and cleanup operations must be idempotent and must not touch other
  leases.

## Risk register

| ID | Risk | Evidence/impact | Required mitigation |
| --- | --- | --- | --- |
| R1 | Lifecycle collision | `remote-control start` manages Codex-owned daemon state and rejects an unowned responsive socket. | Direct RPC only; never invoke supervisor lifecycle commands. |
| R2 | Cross-seat exposure | Remote connections enter the normal request processor; no seat/client ACL on thread list. | Per-seat app-server/CODEX_HOME or an enforcing ACL proxy; otherwise NO-GO. |
| R3 | False stop | Ephemeral disable does not revoke enrolled controllers. | Revoke by environment/client ID, then disable; report partial cleanup. |
| R4 | Enrollment side effects | Pairing may persist enrollment metadata even with an ephemeral preference. | Dedicated client name; inspect/list state; no pairing code persistence. |
| R5 | Roundtable gate rejection | Identity checks require default socket, managed path, launchd lineage, and protocol probe. | Reuse and re-run all existing gates immediately before mutation. |
| R6 | Auth/account drift | Enrollment is account and websocket scoped; refresh can fail or change account. | Verify account/environment before and after pairing; fail closed on change. |
| R7 | Restart surprise | Ephemeral enabled state returns disabled after app-server restart. | Treat pairing as session-scoped; expose status and require explicit re-enable. |
| R8 | Foreign socket/TOCTOU | A socket can change between preflight and RPC. | Snapshot and revalidate peer PID, launchd plist, path, and versions. |
| R9 | Thread write contention | A remote process can hold a thread open for writing. | Require resume-time seat↔thread↔cwd validation and surface single-writer errors. |
| R10 | Code expiry | Pairing start returns an expiry and invalid/expired status. | TTY-only display, authoritative deadline, no automatic replay. |
| R11 | UI scope unknown | This source does not prove how every phone UI filters threads. | Assume app-server-wide scope until an isolated end-to-end test proves otherwise. |

## Acceptance tests before a conditional GO

Run these in an isolated lab, never against the production shared socket:

1. With a launchd-owned responsive socket and no Codex daemon pid backend,
   `codex remote-control start` returns the unowned error and leaves the
   Roundtable service unchanged.
2. Direct enable/pair succeeds on an isolated app-server; the enabled
   preference remains unset while any enrollment metadata is observable; a
   restart returns to disabled.
3. Pair a test controller and issue unfiltered `thread/list` with threads from
   two project roots. The test must prove that the chosen isolation/ACL fence
   hides the other root.
4. Change the socket peer or launchd plist between preflight and mutation; the
   operation must refuse without lifecycle changes.
5. Exercise pairing failure, expiry, revoke, and repeated cleanup; every path
   is idempotent and reports incomplete revocation honestly.

## Source index

* `codex-rs/cli/src/remote_control_cmd.rs:31-95`
* `codex-rs/app-server-daemon/src/lib.rs:253-359,494-585`
* `codex-rs/app-server-daemon/src/remote_control_client.rs:39-132,154-214`
* `codex-rs/app-server-protocol/src/protocol/v2/remote_control.rs:6-24,67-166`
* `codex-rs/app-server-transport/src/transport/remote_control/mod.rs:249-376,387-617,932-1084`
* `codex-rs/app-server-transport/src/transport/remote_control/client_tracker.rs:147-215`
* `codex-rs/app-server/src/thread_processor.rs:2006-2104`
* `codex-rs/app-server/src/request_processors/thread_resume_redaction.rs:6-38`
* `codex-rs/app-server/README.md:161-173,255-262,350-356`
* `bin/_rtcodex.py:1592-1631,1932-2170`

