# Grok leader socket as a code-armed wake channel — feasibility, 2026-08-14

Track T4, phase 3. Research and documentation only; no adapter was changed.

## Question

pneu's Grok seat is a **B-class** wake: the wake loop exists only because a
model turn created it. A bare `rt-grok` launch submits one pinned activation
prompt, the model calls Grok's `monitor` tool, and that background task watches
the seat maildir. Every resume needs the turn again.

The **A-class** target is a wake loop that pneu arms in code, like the Claude
SessionStart/Stop watchers or the Codex app-server bridge. The candidate raised
for Grok was its *leader socket*. This note answers whether pneu can inject a
wake turn into a live Grok TUI through that socket.

**Answer: no, not on Grok Build 1.0.3, and not as anything pneu should ship.**

## What the leader actually is

The leader is off by default (`[cli] use_leader`, or `--leader` / `--no-leader`,
both hidden from `--help`). When it is on, the first client acquires a lock and
spawns a subprocess:

```text
grok agent leader --no-exit-on-disconnect --relay-on-demand \
  --grok-ws-url wss://code.grok.com/ws/code-agent --grok-ws-origin https://grok.com
```

So the leader is the **agent host and upstream websocket relay**, keyed by that
websocket target. The interactive TUI is not the leader; it is a *client* of it.
In the live lab the TUI registered as:

```text
Client registered client_id=1 client_type=grok-shell mode=Stdio client_version=1.0.3
Client identifier set to: grok-shell
Client type set to: GrokPager
```

Traffic on the socket is a JSON-RPC/ACP-shaped stream: an `initialize` request,
`x.ai/mcp/servers_updated` and announcement notifications pushed leader to
client, `x.ai/bundle/status`, `x.ai/billing`, `x.ai/internal/reload_models_cache`,
and a lazily initialised `x.ai/code/*` code-navigation capability.

The framing is **length-prefixed**, not newline-delimited. Newline-delimited
JSON probes were read as a length header and rejected, which is itself the
cleanest proof of the framing:

```text
Registration failed client_id=3 error=Message too large: 2065855609 bytes (max: 67108864)
```

## Why it cannot carry a wake

The leader's control surface, read out of the 1.0.3 binary, is:

- client message kinds: `stdio`, `control`, `disconnect`
- control commands: `get_leader_info`, `cpu_profile_status`, `start_cpu_profile`,
  `workspace_start`, `workspace_pause`, `workspace_stop`, `relaunch_for_update`
- advertised capabilities: `control_v1`, `runtime_cpu_profile`, `profile_formats`,
  `workspace_exposure`, `relaunch_v1`, `fs_read`

Every verb is process lifecycle, workspace exposure, CPU profiling, or update
relaunch. **There is no verb that delivers a user turn, prompt, or input event
to an already-attached client.** Notifications flow leader to client for MCP and
announcement state, never for prompts.

That is the structural point: the input box belongs to the TUI client, and the
leader has no authority over it. Reaching the seat means reaching the client,
and the leader is one layer below the client, not above it.

A second client *can* register against the same leader, but that yields a new
session of its own — which is exactly what pneu's ACP supervisor already does,
and exactly why `docs/compatibility.md` keeps that supervisor as lab machinery
rather than the seat. A new session is not a wake of the operator's seat.

## The one avenue that is not fully closed

Because the leader hosts sessions, a second registered client might in principle
address the *existing* session id rather than opening its own. That would need
all of: the live session id, correct length-prefixed ACP framing, a leader that
routes a prompt into a session another client owns, and a TUI that renders the
resulting turn. Each step is undocumented and unverified. It was not pursued.

## Risks if it were pursued anyway

- **Undocumented internal.** `--leader`, `--no-leader`, and the control protocol
  are absent from `grok --help`. The vendor README mentions leader mode only to
  explain a credential-minting edge case. There is no compatibility promise.
- **Version-gated wire.** The binary carries `leader_protocol_version`,
  `leader_binary_version`, a `version_floor`, and an `x.ai/leader/version_mismatch`
  error, plus eviction and adoption paths. A CLI update can move the wire, and
  pneu would silently lose its wake.
- **Off by default.** pneu would have to turn on a global vendor mode the
  operator did not choose, changing how every Grok session on the host connects
  upstream.
- **Shared blast radius.** One leader fronts all sessions for a websocket
  target. A pneu bug there is not scoped to one seat.
- **Unclean shutdown observed.** After the lab leader was terminated, its socket
  and lock file stayed behind; the binary carries explicit "reclaiming socket
  anyway" recovery for this. Anything pneu builds here inherits that mess.
- **Socket path limit.** A first lab attempt failed with
  `path must be shorter than SUN_LEN`, so any pneu-chosen socket path would need
  its own length budget.

## Recommendation

1. **Do not build a leader-socket wake.** Keep the Grok seat model-armed. The
   channel is architecturally wrong for the job before it is risky, and it is
   risky too.
2. **Do not inject keyboard input** into the TUI as the alternative. pneu's core
   promise is that delivery works without injecting keystrokes, and the project
   already retired that class of mechanism.
3. **Track `auto_wake_enabled` instead.** The binary carries a server-side
   feature flag of that name with a `GROK_AUTO_WAKE` environment override. If
   Grok ships a native wake, that is the A-class path, and it arrives without
   pneu reverse-engineering anything. Re-check it on each CLI upgrade.
4. **Ask the vendor for a documented local control channel.** A supported
   "deliver this turn to this session" verb is the only thing that makes Grok a
   real A-class harness. Until then Grok stays B-class by design, not by
   omission, and `docs/compatibility.md` should keep saying so.
5. **Cheap win, unrelated to the leader:** the seat currently costs about two
   model turns per handled message, because the ack's own `new/` to `cur/` move
   raises a second monitor event and the follow-up turn re-drains an empty
   inbox. The monitor's watch is created by the model from pneu's pinned primer
   text, so the primer is the place to narrow the watched event set. Worth a
   scoped follow-up; it lowers quota cost without touching any wake contract.

## Lab method and boundaries

The leader was exercised live on the development host with the operator's own
credentials: a TUI launched with `--leader --leader-socket <short path>`, the
spawned leader inspected through its process arguments, lock file, `lsof`, and
`~/.grok/leader.log`, and the socket probed read-only. The only control command
attempted was the informational `get_leader_info`. Credentials were never
refreshed, copied, or logged, and no isolated-home credential replica was made,
so leader behaviour under a clean account is untested. The lab leader was
terminated and its tab removed.

`grok leader list` and `grok leader info` did not discover the lab leader even
when pointed at its socket, reporting `no reachable leader found for target
wss://code.grok.com/ws/code-agent`. Vendor discovery appears to key on the
default socket rather than the flag, which is one more sign that the custom
socket path is not a supported integration seam.
