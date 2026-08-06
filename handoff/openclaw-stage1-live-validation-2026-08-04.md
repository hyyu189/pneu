# OpenClaw Stage 1 live validation

Date: 2026-08-04
Workstream: `rt-openclaw` / `wt/openclaw`
Inbound directive: `20260804T225423Z-claude-to-codex-22624`
Status: lab validation complete; no Roundtable product adapter or runtime wiring implemented.

## Executive result

Stage 1 is conditionally verified in an isolated local lab. OpenClaw
`2026.5.4` can run a loopback Gateway, accept an authenticated WebSocket
client, execute the `agent` RPC to a final response, and accept authenticated
`/hooks/agent` and `/hooks/wake` requests. Session routing is deterministic
when the caller supplies an explicitly bounded session key.

The result does not establish a supported Roundtable harness. The lab used a
local OpenAI-compatible fake model and a temporary runtime. A real provider
credential, a Roundtable-owned supervisor, and a release-artifact E2E are still
required before a support claim.

## Lab boundary

- Installed exact package: OpenClaw `2026.5.4` (`325df3e`).
- Temporary root: `/private/tmp/rt-openclaw-stage1.bfXC8i`.
- Isolated config/state/workspace:
  - `state=/private/tmp/rt-openclaw-stage1.bfXC8i/state`
  - `config=/private/tmp/rt-openclaw-stage1.bfXC8i/state/openclaw.json`
  - `workspace=/private/tmp/rt-openclaw-stage1.bfXC8i/workspace`
- Gateway: loopback `127.0.0.1:19321`; browser sidecar was loopback
  `19323`; the fake model endpoint was loopback `19322`.
- The lab used a dedicated gateway token, hooks token, and fake model key;
  their values are intentionally omitted here.
- The existing `~/.openclaw`, `~/clawd`, and LaunchAgent were not used as the
  Gateway runtime and were not modified. No `gateway install`, service start,
  login-item change, Roundtable launcher, wake bridge, or mailbox operation was
  invoked.
- The package was installed with `--ignore-scripts` and was removed twice after
  validation. The temporary root was then removed and verified absent. No lab
  listener remained on ports 19321, 19322, or 19323.

## Live evidence

### Configuration and startup

The isolated config passed `openclaw config validate`. It set:

- `gateway.mode=local`, `gateway.bind=loopback`, token auth, and port 19321;
- hooks enabled at `/hooks-lab` with a dedicated token;
- `models.mode=replace` with `lab/lab-model` pointing only to the local fake
  OpenAI-compatible endpoint;
- `agents.defaults.workspace` to the temporary workspace and a `main` agent;
- cron disabled.

The foreground command was equivalent to:

```text
RT_SESSION_ID=stage1-lab-session RT_LEASE_REVISION=stage1-lab-revision \
OPENCLAW_STATE_DIR=/private/tmp/rt-openclaw-stage1.bfXC8i/state \
OPENCLAW_CONFIG_PATH=/private/tmp/rt-openclaw-stage1.bfXC8i/state/openclaw.json \
<temporary-prefix>/node_modules/.bin/openclaw gateway run \
  --port 19321 --bind loopback --auth token --token <redacted> --verbose
```

The process listened only on loopback, ran with the temporary root as cwd, and
stopped cleanly on SIGINT. The two observed shutdowns completed in about
233ms and 426ms. This demonstrates that a supervisor can own an explicit
Gateway child with a bounded cwd, state/config paths, port, and inherited lease
environment. It does not validate Roundtable lease semantics: no Roundtable
lease validator was called, and PID/lease fencing remains adapter-supervisor
work.

Startup was not instantaneous: the observed ready time was approximately
7.6–12 seconds, and one run reported a 5-second model-warmup timeout before
continuing. Health was still `ok: true`, but its event-loop status reported
`degraded` during plugin/model startup. A readiness probe therefore needs a
startup grace period and must distinguish Gateway readiness from healthy model
execution.

### WebSocket Gateway handshake, auth, and agent finalization

The real CLI Gateway client connected over `ws://127.0.0.1:19321`. The server
logged a `hello-ok` with 155 methods and 25 events. A correct token returned a
successful `health` response whose session-store path was inside the temporary
state directory. An incorrect token was rejected during handshake with close
code 1008 and `unauthorized: gateway token mismatch`.

The `agent` RPC was run with an explicit agent and session key and
`--expect-final`. After correcting the fake provider's SSE framing, run
`stage1-ws-002` returned:

- `status=ok`, summary `completed`;
- final visible text `ROUNDTable-STAGE1-LAB-OK`;
- `stopReason=stop` and `finishReason=stop`;
- `aborted=false`, `fallbackUsed=false`, provider `lab/lab-model`;
- session file under the temporary state directory;
- deterministic key `agent:main:rt-openclaw-stage1-ws-2`.

The first fake-provider attempt deliberately exposed a useful negative case:
literal `\\n` bytes instead of SSE line breaks made OpenClaw surface
`incomplete terminal response`. With standard SSE framing, the same Gateway
path finalized successfully. The adapter must treat a missing terminal event
as a failed turn and must not acknowledge Roundtable mail on that condition.

One CLI probe made without the isolated `OPENCLAW_STATE_DIR` and
`OPENCLAW_CONFIG_PATH` used `<user-home>/.openclaw/openclaw.json` and
hit a device scope-upgrade-pending error. The client-side environment is as
important as the server-side environment; `--url` alone is not an isolation
boundary.

### HTTP hooks

The configured `/hooks-lab` surface behaved as follows:

| Request | Observed result |
| --- | --- |
| `/hooks-lab/agent` without auth | `401 Unauthorized` |
| `/hooks-lab/agent?token=...` | `400`; query tokens are rejected |
| authenticated `/hooks-lab/agent` | `200` with a `runId` |
| duplicate authenticated `/hooks-lab/agent` with same idempotency key and payload | `200` with the same `runId` |
| authenticated `/hooks-lab/wake` with `mode=now` | `200`, `{"ok":true,"mode":"now"}` |

The hook agent request is asynchronous: it returns a run ID, not the final
assistant result. The resulting temporary trajectory/session records showed
`finalStatus=success`, the lab marker, and `stopReason=stop` for both tested
session keys. `sessions.json` contained the exact bounded keys:

```text
agent:main:rt-openclaw-stage1-hook
agent:main:rt-openclaw-stage1-hook-2
```

This makes `/hooks/agent` a viable wake/submit ingress, but the adapter needs
an event subscription or an equivalent durable completion observation before
the Roundtable ack transaction. `/hooks/wake` is a trigger, not a completion
receipt.

## Isolation findings and risks

The state, config, workspace, port, and model endpoint were isolated, but
OpenClaw still discovered its normal bundled plugins and emitted warnings
about global `~/.agents/skills` symlink paths. The Gateway also wrote its
normal log under `/tmp/openclaw/`. Strict adapter isolation must explicitly
contain skill/plugin discovery and logging; isolated `OPENCLAW_STATE_DIR` is
not by itself a complete process-level dependency boundary.

Enabling `hooks.allowRequestSessionKey=true` produced OpenClaw's security
warning. A production adapter should prefer a fixed per-project default key,
or enforce a narrow allowlist prefix and agent allowlist, rather than accept
arbitrary caller-controlled session keys.

The second npm install reported 9 dependency audit findings (5 moderate, 3
high, 1 critical). No audit fix was run and no dependency change was made in
this repository. This must be resolved or explicitly accepted before bundling
OpenClaw into a release artifact.

## Revised adapter design

1. Resolve an exact OpenClaw executable/runtime and reject missing or stale
   entrypoints. Use the resolver for both foreground launch and client probes.
2. Derive a project-isolated state/config/workspace/loopback port from the
   validated Roundtable project anchor. Propagate the isolation environment to
   every client invocation, not only to the Gateway child.
3. Keep Gateway auth and hooks auth separate, loopback-only, and token-backed.
   Do not attach to the user's personal Gateway.
4. Prefer the authenticated WS `agent` RPC with `expect-final`/event capture for
   synchronous delivery. Treat hooks as an asynchronous fallback: persist the
   returned run ID, observe a terminal success/failure, then perform the
   Delivery v2 ack/archive transaction.
5. Use deterministic `agent:<agent-id>:<project-session>` keys with a strict
   prefix policy. Make idempotency keys derive from the Roundtable message ID.
6. Put child PID, cwd, argv, port, state root, and lease revision checks in a
   Roundtable-owned supervisor. Restart/retry only after proving the child is
   still the fenced generation; never use the user's service installer.
7. Add an explicit dependency policy for global skills/plugins and log paths;
   strict project isolation must not silently inherit personal automation.

## Proposed tier claim

- **T0 — maildir:** unchanged and remains the durable Roundtable fact source.
- **T1 — Gateway ingress:** live lab evidence supports a provisional claim for
  authenticated WS `agent` RPC plus authenticated `/hooks/agent` and
  `/hooks/wake`. Finalization and ack ordering still require a Roundtable
  adapter implementation and a real release-artifact E2E.
- **T2 — supervised launch/recovery:** the lab proves explicit foreground
  launch and clean shutdown, but not restart/retry fencing. Keep T2 as a
  design target, not a support claim.

## Exact lab uninstall

For a fresh repeat, the package and lab can be removed with the exact scoped
commands below. The root used for this run has already been removed:

```bash
npm uninstall --prefix /private/tmp/rt-openclaw-stage1.bfXC8i openclaw
rm -rf /private/tmp/rt-openclaw-stage1.bfXC8i
```

Do not run either command against `~/.openclaw`, `~/clawd`, the existing
LaunchAgent, or a Roundtable project runtime.

## Next gate

Ocean/Claude should review the isolation and asynchronous-hook findings before
any Roundtable product code is added. The next implementation gate is a
project-scoped adapter contract plus tests for executable resolution,
session/idempotency mapping, WS terminal events, hook run completion, stale
child detection, and failure-before-ack durability.
