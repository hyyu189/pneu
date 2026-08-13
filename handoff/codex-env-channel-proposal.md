# Codex app-server tool-environment channel proposal

Status: investigation and proposal only. No product code or runtime state was
changed.

## Decision summary

Roundtable has an intended per-seat environment channel, but it is incomplete
for the stock Codex 0.147 explicit-remote path.

`rt-codex` claims the seat, rebuilds the four lease variables, and adds six
`shell_environment_policy.set.*` CLI overrides. Those overrides reach the
local TUI configuration. When the TUI connects with an explicit `--remote`
endpoint, however, Codex 0.147 constructs `thread/start.config` from a small
hard-coded subset of configuration and omits `shell_environment_policy`.
The shared launchd app-server therefore builds shell-tool environments from
its own frozen daemon environment, plus any policy that actually reached the
thread, and injects `CODEX_THREAD_ID`. Restarting the TUI cannot add the pane
shell's environment to that daemon.

Recommendation for D16: add a narrow, launch-time, per-thread allowlist
channel. Keep the Roundtable lease envelope reserved and launcher-generated;
forward ordinary pane context separately; require an explicit capability
profile for control sockets. Do not copy the full pane environment into the
daemon, and do not make this a per-turn wake payload.

There is an upstream hard cap: stock 0.147 does not carry the needed policy
from an explicit-remote TUI into thread start/resume/fork. A complete fix must
either change that client transport or make Roundtable own the corresponding
app-server thread lifecycle. Merely changing `_rtlauncher.py` arguments or
restarting the daemon cannot close the channel.

## Observed incident

The read-only launch materials tell a consistent product story once operator
authorization is separated from ambient process state:

- `~/Code/rt-launch/materials/handoff/2026-08-13-codex-dress-rehearsal.md`
  states that the Codex tools execute under the launchd app-server and do not
  inherit the Herdr pane environment. It explicitly warns that the TUI pane's
  ambient Herdr socket targets the operator's main fleet and requires every
  demo command to name the isolated demo socket explicitly.
- `~/Code/rt-launch/materials/assets/demo/takes/rehearsal-log.md` records a safe
  abort at operator preflight: `HERDR_ENV` and `HERDR_SOCKET_PATH` were absent,
  and no Herdr command, recording, or demo-stage mutation occurred. Its claim
  that this proved the session was outside Herdr is a false-negative guard;
  the dispatch document explicitly says the managed pane exists despite the
  missing tool-process variables.

The safe abort should remain part of the record. The conclusion drawn from the
missing variables should be corrected in future operator guidance. Even after
an environment-channel fix, the isolated-demo socket must stay an explicit
command target; blindly restoring an ambient production socket would increase
risk rather than solve the rehearsal boundary.

## Current Roundtable channel

The intended path is:

```text
pane shell
  -> rt-codex launcher process
       - removes inherited RT lease identity
       - claims a fresh seat lease
       - sets RT_PROJECT_ROOT / RT_FROM / RT_SESSION_ID / RT_LEASE_REVISION
       - normalizes RT_RUNTIME_DIR / RT_CODEX_RUNTIME_DIR
       - emits -c shell_environment_policy.set.<name>=<value>
  -> codex TUI --remote unix://...
       - parses the CLI overrides into local Config
       - sends thread/start, thread/resume, or thread/fork
  -> launchd app-server
       - owns the thread and executes tools
       - derives each tool environment from the thread shell policy
       - always injects CODEX_THREAD_ID
```

The Roundtable side is explicit:

- `bin/_rtlauncher.py:78-83` defines the four lease variables.
- `bin/_rtlauncher.py:189-196` defines the six intended Codex tool variables.
- `bin/_rtlauncher.py:639-646` writes the newly claimed lease into the launcher
  process environment.
- `bin/_rtlauncher.py:706-729` serializes all six values as
  `shell_environment_policy.set.*` CLI overrides.
- `bin/_rtlauncher.py:913-932` removes lease identity inherited from another
  Roundtable seat before a new claim.
- `bin/_rtlauncher.py:1410-1423` appends the overrides and execs Codex.

The daemon is intentionally not per-seat:

- `bin/_rtcodex.py:322-357` gives the launchd app-server a stable environment:
  home, path, Codex home/bin, and the two Roundtable runtime-directory values.
  It does not contain the four seat-fence values or Herdr/Tmux pane context.
- `bin/_rtcodex.py:2053-2093` verifies the loaded daemon definition and rejects
  unexpected explicit environment keys outside a very small system allowlist.
  Adding arbitrary pane variables to the shared plist would both violate this
  identity check and leak one seat's context to every thread.
- `bin/rt-codex-wake:2011-2098` sends only a thread id, a message id, and text in
  `turn/start`. It does not attach configuration or environment variables. A
  wake turn therefore reuses the thread's already stored configuration.

### Why successful mail tools are not sufficient proof

Roundtable's ordinary and fenced mail modes have different evidentiary value:

- `rt-inbox codex` needs no environment identity. With no recipient argument,
  `bin/rt-inbox:50-85` can infer a unique Codex seat from `CODEX_THREAD_ID` and
  the project registry.
- Ordinary `rt-ack <ref>` can discover the recipient from the durable inbound
  envelope; `bin/rt-ack:311-381` does not require a lease.
- Only `rt-inbox --fenced` or `rt-ack --fenced` invokes
  `bin/_rtlib.py:680-746`, which requires and validates all four seat variables
  against the active lease.

Consequently, an inbox drain or receipt proves the seat environment only when
the captured command used `--fenced`. Bare successful calls do not establish a
hidden RT environment channel.

## Codex 0.147 source finding

The inspected source was the exact `rust-v0.147.0` tag at commit
[`be6e8eac029b183056b7e4402879f15d2c85f61b`](https://github.com/openai/codex/tree/be6e8eac029b183056b7e4402879f15d2c85f61b).

### Tool execution inherits the app-server, not the TUI

Codex's shell policy defaults to inheriting all variables from the process
that executes the tools, filters as configured, and then applies the explicit
`set` map. See
[`config_types.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/protocol/src/config_types.rs#L187-L253).
`create_env` also injects `CODEX_THREAD_ID`; see
[`exec_env.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/exec_env.rs#L15-L30).

Both shell implementations obtain the policy from the turn configuration:

- the shell-command handler at
  [`shell_command.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/tools/handlers/shell/shell_command.rs#L88-L121);
- unified exec at
  [`process_manager.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/core/src/unified_exec/process_manager.rs#L1124-L1183).

This explains the observed split: daemon-wide `RT_RUNTIME_DIR` and
`RT_CODEX_RUNTIME_DIR` can be inherited, and `CODEX_THREAD_ID` is injected, but
pane-only Herdr variables and per-seat lease values require an explicit thread
policy overlay.

### The protocol has a thread config field, but no turn env-var map

`ThreadStartParams` accepts a generic `config` map. It also has an experimental
`environments` field; that field selects registered execution environments by
environment id and cwd, not process environment variables. See
[`thread.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L57-L149)
and
[`turn.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L37-L47).

`TurnStartParams` has no generic config or process-variable overlay. Its
`environments` field is the same registered-environment selector. See
[`turn.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L66-L161).

### The explicit-remote TUI drops this policy

An explicit remote endpoint is classified as a remote app-server target; see
[`lib.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/lib.rs#L833-L847).
For thread start, resume, and fork, the TUI creates the protocol `config` map
through `config_request_overrides_from_config`. That function forwards only
reasoning effort, reasoning summary, verbosity, personality, web search, and
one trust flag. It does not forward `shell_environment_policy`; see
[`app_server_session.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app_server_session.rs#L1570-L1612)
and its thread-start use at
[`app_server_session.rs`](https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/tui/src/app_server_session.rs#L1693-L1728).
Resume and fork call the same selective function.

That omission is the broken edge. Roundtable's launcher tests validate CLI
composition, but they cannot prove that an upstream explicit-remote TUI carries
the parsed policy across the app-server boundary.

## Options

| Option | Benefit | Risk or limitation | Disposition |
|---|---|---|---|
| Copy the full pane environment | Closest shell fidelity and least policy design | Leaks credentials, tokens, control sockets, unrelated seat identity, and unstable shell internals into a long-lived thread | Reject |
| Add pane variables to the launchd app-server plist | Simple inheritance model | Host-wide and frozen; cross-seat leakage; daemon reload required; violates the managed identity allowlist | Reject |
| Send an allowlisted overlay at each wake turn | Could refresh changing context | 0.147 `turn/start` has no such field; wake and interactive turns would diverge; expands a security-sensitive protocol surface | Reject for D16 |
| Send an allowlisted overlay at thread start/resume/fork | Matches sticky thread configuration, supports interactive and wake turns, keeps seats isolated | Requires an upstream TUI transport change or Roundtable ownership of thread lifecycle | Recommend |
| Have Roundtable pre-create threads and make the TUI resume them | Could use the existing generic `thread/start.config` field without changing the TUI start request | Roundtable would take over thread creation, persistence, resume, fork, primer, and failure recovery; cold-resume semantics need proof | Fallback experiment, not first choice |

## Recommended D16 contract

### 1. Two separately governed overlays

Treat environment propagation as two data classes:

1. **Reserved seat envelope.** Always overwrite, never inherit:
   `RT_PROJECT_ROOT`, `RT_FROM`, `RT_SESSION_ID`, `RT_LEASE_REVISION`,
   `RT_RUNTIME_DIR`, and `RT_CODEX_RUNTIME_DIR`. The first four come only from
   the fresh validated launcher claim. Preserve the existing inherited-seat
   scrub before selection and claim.
2. **Pane-context allowlist.** Snapshot at `rt-codex` launch and filter by exact
   name. A reasonable base set is `PATH`, `LANG`, `LC_*`, `TERM`, `COLORTERM`,
   `HERDR_ENV`, and non-capability Herdr surface identifiers such as pane,
   workspace, and tab ids. Missing values stay missing; do not synthesize proof
   that a TUI belongs to Herdr.

Never forward wildcard `RT_*`, `HERDR_*`, or the complete environment. Explicit
entries beat inherited daemon values, and diagnostics should report variable
presence and provenance without logging values.

### 2. Control endpoints require an explicit profile

`HERDR_SOCKET_PATH`, `TMUX`, `SSH_AUTH_SOCK`, and similar variables are control
capabilities, not harmless context. Exclude them from the base allowlist.

If an operator workflow needs them, require a named, visible launch profile
that selects and validates the intended endpoint before adding it to the
thread overlay. The profile must not infer authorization from `HERDR_ENV` alone
and must not silently substitute an ambient production endpoint for an
isolated-demo endpoint. D15's exact per-command demo-socket prefix remains the
right control even if base pane context becomes visible.

### 3. Thread-lifecycle timing, not turn timing

Capture and transmit the overlay on every user-driven `rt-codex` start or
resume, and on any fork that is intentionally the same seat. Store it in the
thread config so interactive turns and daemon wake turns see one environment.
Do not attach environment data to `rt-codex-wake` messages.

Moving a TUI to another pane does not refresh its environment. The operator
must relaunch/resume through `rt-codex` to take a new snapshot. This limitation
should be explicit in diagnostics and documentation.

Before forwarding to forks or side threads, define their authority. Copying a
live seat fence into an independently active thread increases the number of
processes able to invoke fenced tools. Default to one authoritative interactive
thread per claimed seat; add propagation only where lifecycle and revocation
are proven.

### 4. Preferred implementation boundary

Prefer a Codex-side change that serializes the parsed
`shell_environment_policy` into the generic config map used for remote thread
start, resume, and fork, with round-trip tests. Roundtable can then keep its
existing launcher-generated `set` entries and extend them with the approved
pane allowlist.

If upstream cannot carry this safely, fail closed and keep the limitation
visible. Do not work around it by writing shared user configuration, mutating
the long-lived daemon environment per seat, or claiming support from launcher
fixtures alone.

## Acceptance gates

D16 should not claim the channel complete until a stock release-artifact E2E
proves all of the following:

1. Two concurrent explicit-remote seats launched from different pane contexts
   see their own harmless marker and never the other's marker.
2. `rt-inbox --fenced` and `rt-ack --fenced` succeed with the exact active lease;
   stale values fail after lease revision or seat replacement.
3. Interactive turns and later daemon wake turns see the same stored overlay.
4. Start, resume, and the supported fork/side-thread behavior are each tested;
   a TUI-only restart does not create an unexplained environment change.
5. Secret canaries and excluded control sockets are absent under the base
   profile. An opted-in control socket is the validated requested endpoint.
6. A daemon restart and cold thread recovery preserve or deliberately rebuild
   the documented contract without borrowing another seat's values.
7. Diagnostics disclose names, source class, and freshness only—not variable
   contents—and public-safety checks find no personal paths or secrets.

Until those gates pass, the honest support statement is: Roundtable constructs
per-seat Codex shell-policy overrides, but Codex 0.147 explicit-remote transport
does not deliver them to the shared app-server thread.
