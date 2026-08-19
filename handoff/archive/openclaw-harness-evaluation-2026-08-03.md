# OpenClaw harness evaluation

> Status: superseded by d14-openclaw-source-audit-2026-08-17.md and the 2026-08-17 demotion ruling

Date: 2026-08-03
Workstream: `rt-openclaw` / `wt/openclaw`
Inbound directive: `20260804T013508Z-claude-to-codex-59864`
Status: research and staged execution plan only; no OpenClaw installation, system-service change, or Roundtable product implementation was performed.

## Recommendation

OpenClaw is a plausible supported Roundtable harness, but the adapter should be
Gateway-backed rather than TUI-backed. Its long-lived Gateway owns the WebSocket
control plane, channels, agent runs, and session state. The `openclaw agent`
command is a client of that Gateway by default; it is not itself a durable seat.

The first product increment should therefore be a separate, project-scoped
adapter process with an explicit executable resolver, isolated OpenClaw state,
and a documented Gateway probe. It should use the existing Roundtable maildir
as the fact source, submit substantive mail through the Gateway, and acknowledge
only after the agent turn has been observed as complete. Do not claim support
until a real temporary-state end-to-end test proves this lifecycle.

## Research grid

### Install and run model

Audited source: OpenClaw tag `v2026.5.4`, matching the local configuration and
service metadata.

- The package exposes the `openclaw` bin through `openclaw.mjs`; the package
  declares Node `>=22.14.0` while the launcher performs its own Node floor
  check. The normal foreground path is `openclaw gateway` / `gateway run`.
- The CLI gateway path reads config, resolves port/bind/auth, imports the
  Gateway server, and starts `startGatewayServer(port, options)`.
- The service installer builds explicit runtime arguments such as
  `[node, <dist entrypoint>, "gateway", "--port", <port>]` and supports a
  wrapper. On macOS the managed service is a LaunchAgent; the generated
  environment contains service markers, state/config paths, port, and a
  minimal PATH.
- `openclaw agent --message ...` requires a session selector (`--to`,
  `--session-id`, or `--agent`) and calls the Gateway `agent` method unless
  `--local` is supplied. A Gateway failure or timeout can fall back to an
  embedded run, which is not a safe default for a Roundtable seat because it
  can change state and credential boundaries.

Relevant source:

- [`openclaw.mjs` at v2026.5.4](https://github.com/openclaw/openclaw/blob/v2026.5.4/openclaw.mjs)
- [`package.json` at v2026.5.4](https://github.com/openclaw/openclaw/blob/v2026.5.4/package.json)
- [`register.agent.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/cli/program/register.agent.ts)
- [`agent-via-gateway.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/commands/agent-via-gateway.ts)
- [`gateway-cli/run.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/cli/gateway-cli/run.ts)
- [`daemon/program-args.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/daemon/program-args.ts)

### Session model

- Session routing is keyed by a normalized `sessionKey`. The canonical direct
  session shape is `agent:<agentId>:<mainKey>`; groups receive an agent-scoped
  group/channel suffix.
- Session state is a `sessions.json` store under the agent state directory;
  transcripts are append-only JSONL files under the corresponding agent
  sessions directory. Reset and daily/idle policies can mint a new session ID
  for the same key.
- For Roundtable, do not reuse the personal `agent:main:main` session. The
  adapter should use a project-isolated OpenClaw state directory and a
  deterministic project session key, or a dedicated agent/workspace if the
  chosen Gateway ingress requires it. The key and state directory must be
  derived from the validated Roundtable project anchor, not from mutable UI
  focus.

Relevant source:

- [`session-key.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/config/sessions/session-key.ts)
- [`paths.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/config/sessions/paths.ts)
- [`lifecycle.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/config/sessions/lifecycle.ts)
- [`session management docs`](https://github.com/openclaw/openclaw/blob/v2026.5.4/docs/concepts/session.md)

### Lifecycle hooks, plugins, and events

OpenClaw has two complementary extension layers:

- Internal hooks are directory-discovered `HOOK.md` plus `handler.ts` modules.
  The v2026.5.4 event surface includes command, session compaction/patch,
  agent bootstrap, Gateway startup/shutdown/restart, and message received,
  transcribed, preprocessed, and sent events.
- Native plugins load an entry module and receive an `OpenClawPluginApi`.
  The API includes `registerHook`, `registerHttpRoute`,
  `registerGatewayMethod`, `registerService`, and typed lifecycle `on(...)`
  hooks, in addition to tools, channels, CLI, session extensions, and agent
  event subscriptions. Plugin runtime code runs inside the Gateway process and
  is therefore trusted code, not a low-risk drop-in.
- External HTTP hooks provide `POST /hooks/wake`, `POST /hooks/agent`, and
  mapped `POST /hooks/<name>` endpoints. `/hooks/agent` can accept a caller
  session key only when explicitly enabled and prefix-bounded. Hook auth is a
  dedicated bearer token and must not reuse Gateway auth.

This makes a Roundtable-specific OpenClaw plugin technically possible, but an
external adapter/sidecar is the safer first boundary: it keeps maildir
acknowledgement, lease fencing, and OpenClaw process lifecycle outside trusted
Gateway plugin code.

Relevant source/docs:

- [`internal-hooks.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/hooks/internal-hooks.ts)
- [`loader.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/hooks/loader.ts)
- [`plugins/types.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/plugins/types.ts)
- [`plugins/api-builder.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/plugins/api-builder.ts)
- [`hooks docs`](https://github.com/openclaw/openclaw/blob/v2026.5.4/docs/automation/hooks.md)
- [`webhook docs`](https://github.com/openclaw/openclaw/blob/v2026.5.4/docs/automation/webhook.md)

### Daemon, Gateway, and socket surfaces

- The Gateway serves HTTP and WebSocket on one configured port, defaulting to
  `18789`. The runtime creates a Node HTTP server and a `ws` WebSocketServer
  with `noServer: true`, attaches the upgrade handler before listening, and
  then binds the configured host(s).
- The client default is `ws://127.0.0.1:18789`. The first client frame is a
  `connect` request. Connect parameters declare protocol range, client
  identity/version/platform/mode, role/scopes, and optional token/password or
  device auth. The server returns `hello-ok` with methods/events and policy.
- Frames are JSON text `req`, `res`, and `event`. The Gateway client has
  reconnect handling, request timeouts, device identity/token persistence, and
  scope selection. The CLI's `agent` path uses this same client layer and
  invokes Gateway method `agent`.
- Loopback is the safe default. Non-loopback binding requires authentication;
  a Roundtable adapter should keep the Gateway loopback-only and avoid a public
  listener.

Relevant source:

- [`server.impl.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/gateway/server.impl.ts)
- [`server-runtime-state.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/gateway/server-runtime-state.ts)
- [`frames.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/gateway/protocol/schema/frames.ts)
- [`client.ts`](https://github.com/openclaw/openclaw/blob/v2026.5.4/src/gateway/client.ts)
- [`Gateway protocol docs`](https://github.com/openclaw/openclaw/blob/v2026.5.4/docs/gateway/protocol.md)

### Launcher and fenced-environment feasibility

Feasible, with explicit boundaries:

1. Roundtable resolves one real OpenClaw executable/entrypoint and rejects a
   missing or stale path. It must not trust a shell PATH lookalike.
2. The adapter launches a project-scoped Gateway with explicit state/config
   directory, workspace, loopback port, and Roundtable lease environment.
3. The adapter proves the child command line, exact cwd, lease, port, and a
   live read-only Gateway handshake before declaring the seat active.
4. The adapter never mutates the user's existing `~/.openclaw` service,
   credentials, cron, or Telegram configuration.

The local host demonstrates why this needs to be explicit: the current
LaunchAgent is running a v2026.5.4 Gateway on loopback port 18789, but the
recorded package path is no longer present on disk and the shell cannot resolve
`openclaw`. The process has been alive since 2026-07-22 and is using the
existing `~/.openclaw` state. This is service/runtime drift, not proof that a
new adapter install is healthy. The adapter should fail closed on this class of
stale service path rather than attach to the user's personal Gateway.

### Wake tiers

| Tier | Candidate | Assessment |
| --- | --- | --- |
| T0 | Roundtable maildir delivery | Guaranteed base. Mail remains durable while OpenClaw is offline; no Gateway or TUI is required. |
| T1 | Gateway WS `agent` RPC, or authenticated `/hooks/agent` / `/hooks/wake` | Recommended native wake. It is machine-addressable and does not depend on terminal focus. Requires a live handshake/probe, deterministic session routing, auth, timeout, and response-finalization handling. |
| T2 | Adapter-supervised Gateway start/restart, then retry | Acceptable recovery tier. Use explicit child args and isolated state. Do not use keyboard injection or infer a TUI switch. Embedded `--local` is only an explicitly scoped emergency mode and should not be the support baseline. |

The first supported claim should be “OpenClaw Gateway adapter” rather than
“OpenClaw TUI wake.” The upstream source exposes Gateway/session surfaces, not a
Roundtable-aware current-terminal focus contract.

### Modular adapter design

Add a harness adapter boundary in Roundtable rather than importing OpenClaw
internals into core mail code. The minimal contract should cover:

- `resolveExecutable()` and version/capability probe;
- `launch(projectAnchor, lease, isolatedRuntime)`;
- `health()` with a real Gateway handshake and required-method check;
- `submit(session, message, idempotencyKey)`;
- final-result/event translation;
- `stop()` and stale-child recovery;
- explicit capability flags for native wake, streaming, session resume, and
  project isolation.

The OpenClaw implementation should use the documented Gateway protocol/client
or the existing CLI's Gateway RPC path. Keep the maildir drain and `rt-ack`
transaction in the adapter supervisor, not in an OpenClaw plugin, so a model
reply cannot accidentally acknowledge mail before the required result and
archive steps are durable.

### Test plan

#### Unit and contract tests

- Resolve the real executable from an explicit candidate list; reject missing,
  deleted, non-executable, wrong-runtime, and PATH-only candidates.
- Build isolated state/config/workspace/port arguments; assert no global
  `~/.openclaw` paths or credentials are selected.
- Verify deterministic project session keys and idempotency keys.
- Fake Gateway protocol: connect challenge/handshake, auth failure, required
  method/event advertisement, `agent` request, final response, timeout,
  reconnect, and malformed frame handling.
- Verify adapter crash boundaries: duplicate mail, duplicate final event,
  process death before ack, ack failure after response, stale child, and
  Gateway already occupied by another project.

#### Live isolated end-to-end test

- Use a temporary `OPENCLAW_STATE_DIR`, temporary config, temporary workspace,
  and unused loopback port; never use the personal Gateway.
- Start the actual v2026.5.4 Gateway from a release/runtime artifact, perform a
  read-only handshake, deliver one Roundtable message, wake the agent through
  the chosen T1 path, and capture the final result.
- Acknowledge exactly once after processing, archive quiet `ack-*`/`sync-ack`
  files without ack-of-ack, and prove both parsed inbox emptiness and physical
  `new/` emptiness.
- Repeat with Gateway restart, adapter restart, duplicate wake, malformed mail,
  auth failure, and missing executable. Assert all failures leave mail durable
  and do not touch personal service/config state.

#### Release gate

Do not add an OpenClaw support row to README or claim a supported runtime until
the isolated E2E passes on the actual release artifact, the install/uninstall
path is idempotent, and the public-safety scan proves no secrets, local runtime
mailboxes, or personal paths entered the artifact.

## Unknowns requiring a later live probe

1. The exact minimum OpenClaw release should be capability/protocol-probed, not
   inferred from a version string. The Gateway method/event set and auth
   behavior are the compatibility contract.
2. Confirm whether the chosen `agent` RPC or webhook path gives a stable final
   result for long turns, streaming, cancellation, and Gateway restart.
3. Confirm the cleanest project session mapping: isolated state directory with
   `agent:main:main`, a dedicated agent/workspace, or a bounded hook session
   key. This must be decided without mutating the user's personal config.
4. Decide whether a sidecar can safely call `rt-*` commands under the launcher
   lease or whether a small Roundtable-owned supervisor should own the maildir
   transaction. The latter is likely safer than executing coordination logic
   inside a trusted OpenClaw plugin.
5. Validate provider/auth requirements using a dedicated test account or mock
   provider. No credentialed setup was attempted in this research turn.
6. The live host's stale-but-running LaunchAgent needs a separate operator
   decision; this workstream intentionally did not repair, stop, reinstall, or
   attach to it.

## Staged execution plan

### Stage 0 — Ocean review (current)

- Review this design and select Gateway-backed adapter versus a different
  OpenClaw integration boundary.
- Confirm whether a project-isolated OpenClaw runtime is acceptable and what
  support floor/release artifact should be used.
- No system-level installation or personal OpenClaw changes.

### Stage 1 — Read-only adapter contract

- Add only pure resolver/config/probe abstractions and fixtures.
- Implement a live Gateway handshake probe against a temporary/fake endpoint;
  fail closed on missing executable, stale service, auth failure, or method
  mismatch.
- Add unit and protocol contract coverage.

### Stage 2 — Isolated launch and T0/T1 delivery

- Add a project-scoped launcher with fenced lease environment and isolated
  OpenClaw state/config/workspace/port.
- Implement maildir-to-Gateway submission and final-result capture while
  preserving Delivery v2 ack/archive ordering.
- Validate one-generation and duplicate-generation recovery.

### Stage 3 — Recovery and release validation

- Add supervised restart/retry, clean shutdown, stale-child detection, and
  service drift diagnostics.
- Run the real artifact E2E, full tests, compile checks, and public-safety scan.
- Update compatibility docs only if the actual support gate passes.

