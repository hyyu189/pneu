# Grok Build harness evaluation

> Status: historical record — Stage 0/1 evaluation predating the TUI-first ruling

Date: 2026-08-03 (America/Los_Angeles)

Status: Stage 0 research plus Stage 1 hands-on lab validation. The npm package
was installed in the dedicated user-level prefix
`$HOME/.local/share/rt-grok-stage1` and exercised with an isolated
`GROK_HOME`; no system daemon, LaunchAgent, live Roundtable runtime wiring, or
Roundtable product implementation was performed. The original live
`~/.grok/hooks` files were preserved, and the npm postinstall artifacts it
created in live `~/.grok` were removed after capture. The authenticated Stage 2
ACP lab now completes one isolated mail-to-wake-to-drain-to-ack generation;
product support remains gated on the adapter lifecycle and release criteria.

## Executive conclusion

Grok Build is a real standalone xAI coding CLI named `grok`. The official
documentation describes an interactive TUI, headless operation, persistent
sessions, lifecycle hooks, and an Agent Client Protocol (ACP) process over
JSON-RPC stdio.

The safe current recommendation is:

1. Keep durable Roundtable mail plus manual drain as the T0 fallback. The
   authenticated ACP run below is a bounded T2 lab result, not a public
   production-support claim.
2. Do not claim T1 support yet. Session hooks are documented, but the public
   contract does not document a way for a hook to inject a new user turn into
   an already-running interactive TUI.
3. Productize only the explicit ACP subprocess design documented below. The
   lab proves authentication, prompt delivery, permission negotiation, and one
   mail-to-wake-to-drain/ack generation, but not session resume after death,
   duplicate-seat fencing under contention, or two-generation recovery.

The Stage 1 update below supersedes the initial “not exposed in the public
CLI” conclusions where the installed `0.2.118` binary provides stronger
evidence, while retaining the support and safety gates.

The unauthenticated Stage 2 baseline below records the original blocker. The
authenticated rerun at the end supersedes its claim status: Grok is now
**T2-lab-verified / production-unclaimed**.

## Observed official surface

### Standalone CLI and installation

- The executable is `grok`; invoking it without a subcommand starts the TUI.
- The official release path is `curl -fsSL https://x.ai/cli/install.sh | bash`
  on macOS/Linux/WSL, with a PowerShell installer for Windows. The official
  repository says released binaries are published for macOS, Linux, and
  Windows and that the shipped artifact is named `grok`.
- The CLI reference also lists `npm install -g @xai-official/grok` as an
  enterprise deployment alternative. The exact package/version policy still
  needs verification during a controlled install.
- The first interactive launch normally opens a browser for authentication;
  `grok login --device-auth` and `XAI_API_KEY` are documented for headless or
  non-browser environments.

Sources: [Grok Build overview](https://docs.x.ai/build/overview), [CLI
reference](https://docs.x.ai/build/cli/reference), [official grok-build
repository](https://github.com/xai-org/grok-build), [enterprise
deployments](https://docs.x.ai/build/enterprise).

### Session model

- Interactive, headless, and ACP use the same session model.
- Local session history is stored under `~/.grok/sessions/`, keyed by working
  directory.
- `-s/--session-id` creates a new named session; `-r/--resume` resumes one;
  `-c/--continue` resumes the most recent session for the current directory;
  `--fork-session` branches a resumed session.
- Headless mode supports `-p/--single`, `--cwd`, `plain`, `json`, and
  `streaming-json` output. `--no-auto-update` is recommended for automation.

Source: [headless and scripting](https://docs.x.ai/build/cli/headless-scripting),
[sessions](https://docs.x.ai/build/features/sessions).

### Lifecycle hooks

- Hooks are JSON files in `~/.grok/hooks/`, project `.grok/hooks/`, or enabled
  plugins. Project hooks require trust via `/hooks-trust` or `--trust`.
- Documented events include `SessionStart`, `SessionEnd`, `Stop`,
  `StopFailure`, tool events, and notification events.
- A hook receives JSON on stdin containing `hookEventName`, `sessionId`, `cwd`,
  and `workspaceRoot`; environment variables include `GROK_HOOK_EVENT`,
  `GROK_HOOK_NAME`, `GROK_SESSION_ID`, and `GROK_WORKSPACE_ROOT`.
- Hooks may be shell commands or HTTP endpoints. `PreToolUse` is the only
  blocking event; passive hook failures are recorded but do not themselves
  stop the tool/session.

Source: [hooks](https://docs.x.ai/build/features/hooks), [skills/plugins/hooks
overview](https://docs.x.ai/build/features/skills-plugins-marketplaces).

### Daemon, socket, and ACP surfaces

- The official non-TUI integration documented for a long-lived process is
  `grok agent stdio`.
- It speaks JSON-RPC over stdin/stdout. The official example covers
  `initialize`, `authenticate`, `session/new`, `session/prompt`, and
  `session/update` notifications containing assistant chunks.
- The CLI reference and official repository do not document a Grok-managed
  daemon, Unix socket, LaunchAgent, or equivalent local service endpoint.
  This is an absence in the reviewed public documentation, not proof that no
  internal service exists.

Source: [ACP/headless integration](https://docs.x.ai/build/cli/headless-scripting),
[CLI reference](https://docs.x.ai/build/cli/reference).

## Roundtable feasibility

| Tier | Assessment | Evidence and gate |
| --- | --- | --- |
| T0: mail + manual drain | Candidate now | Delivery is independent of the harness; no Grok-specific runtime is needed. |
| T1: hook wake | Plausible, unverified | SessionStart/Stop hooks exist, but prompt injection or a native “wake this TUI” API is not documented. A hook may be able to notify or arm a helper without being able to start a new turn. |
| T2: service bridge | ACP-oriented candidate, unverified | `grok agent stdio` supplies a persistent JSON-RPC process and `session/prompt`; it is not a documented daemon/socket backend and requires lifecycle, buffering, auth, and failure testing. |

The first automatic-wake experiment should therefore be an explicitly named
“ACP subprocess adapter” rather than pretending Grok has a Codex-like daemon.
If the process can remain healthy between prompts and be safely owned by a
fenced Roundtable lease, it may earn a T2-like capability classification.

## Proposed modular adapter

Keep the existing maildir fact source and lease model unchanged. Add a
harness-neutral capability boundary with Grok-specific code behind it:

- executable discovery and version reporting;
- interactive/headless/ACP launch modes;
- project-root and fenced-seat environment construction;
- hook discovery/trust diagnostics, without silently editing user config;
- ACP `initialize`/`authenticate`/`session/new`/`session/prompt` handling;
- streamed update collection, timeout, process-exit, and rebind behavior;
- health and stop operations fenced by `RT_PROJECT_ROOT`, `RT_FROM`,
  `RT_SESSION_ID`, and `RT_LEASE_REVISION`.

The launcher would claim the existing logical seat first, set an explicit
Grok identity such as `RT_FROM=grok`, and start from the canonical project
root. The adapter must never make cmux topology or a vendor session file the
delivery fact. A T0/manual path remains valid if the automatic adapter is
missing or unhealthy.

Likely future surfaces are an isolated `integrations/grok/` adapter, a
launcher entry point, and focused tests. No such surfaces should be added
until Ocean approves the next stage.

## Staged execution plan

### Stage 0 — completed research

- Confirm the official CLI, installation routes, session storage/resume flags,
  lifecycle events, hook contract, ACP transport, and authentication paths.
- Record unverified claims explicitly; do not install or modify system state.

### Stage 1 — contract and fake-process tests (approval required)

- Define the adapter capability contract without changing the delivery core.
- Test executable discovery, explicit path selection, version parsing, missing
  and incompatible binaries, canonical cwd, identity injection, seat collision,
  and fail-closed behavior.
- Use a fake `grok` process to test ACP JSON-RPC framing, streamed
  `session/update`, prompt completion, malformed output, timeout, crash, and
  restart/rebind behavior.
- Add hook-fixture tests for SessionStart/Stop event parsing, trust-required
  project hooks, and the documented passive-hook failure semantics.

### Historical staged plan — controlled hands-on install

In a disposable user/project context, install the official artifact and record
the exact version and binary path. Run only read-only checks first:

```text
grok version
grok inspect --json
grok sessions list
grok login --device-auth   # only if the operator explicitly chooses auth
grok agent stdio
```

Then verify session creation/resume, hook event payloads, project trust, ACP
authentication, process lifetime between prompts, stderr/stdout separation,
and behavior with `--no-auto-update`. Do not add Roundtable hooks or modify
managed/user configuration until the contract is understood.

### Historical staged plan — isolated wake spike

- Run an ACP subprocess under a fenced test lease.
- Send a test mail, convert it to one ACP `session/prompt`, and verify that
  streamed output can cause a normal drain/ack turn.
- Exercise stop, crash, stale lease, duplicate seat, mail backlog, and
  re-arm behavior. Keep T0 manual drain available for every failure.

### Historical staged plan — support gate

Only after a real send-to-wake-to-drain/ack test on a supported host should
Roundtable document a Grok support claim. The gate must include a fresh install,
credentialed and headless auth paths as applicable, recovery after process
failure, and public-safety checks. Until then the README should describe Grok
as research-only/T0 candidate, not as a supported harness.

## Open questions requiring hands-on evidence

- Which exact release/version floor is stable, and does the npm distribution
  match the release-script binary and `grok` executable behavior?
- Can `grok agent stdio` stay resident across multiple prompts, and what are
  the exact JSON-RPC errors, stop reasons, buffering rules, and exit semantics?
- Does ACP authentication work with a pre-existing OAuth/device-code session,
  an API key, or both in the intended headless environment?
- Can a `Stop` or `SessionStart` hook initiate a new user turn, or can it only
  run side effects/notifications? This determines T1 viability.
- What exact event payload and trust behavior occur for resumed, cleared,
  forked, and failed sessions?
- Does automatic update activity interfere with an owned adapter process?
- Is there any supported local daemon/socket surface not exposed in the public
  CLI docs? Do not infer one from process inspection alone.
- What sandbox and permission defaults apply when the adapter is launched
  non-interactively?

## Stage 1 hands-on validation — 2026-08-04

### Install record and cleanup

The official shell installer was downloaded for inspection but not executed.
Its SHA-256 was
`0465d810453bbf18608ccae310fa79f4c59ae4a0538bd8a3a374ebce749be952`.
The script writes to the live `$HOME/.grok` tree even when `GROK_BIN_DIR` is
customized, so it was not suitable for this bounded test.

The npm alternative was installed as a user-level package, without a global
prefix or system files:

```text
GROK_STAGE1_PREFIX="$HOME/.local/share/rt-grok-stage1"
npm install --prefix "$GROK_STAGE1_PREFIX" \
  --no-package-lock --no-fund --no-audit @xai-official/grok@0.2.118
```

Recorded package metadata:

- package: `@xai-official/grok@0.2.118`
- package tarball: `https://registry.npmjs.org/@xai-official/grok/-/grok-0.2.118.tgz`
- package integrity: `sha512-51BumA66Y9Xp1Qv2HCphEE/lTmMF4DPPueX945b3nH30/VN0T3QsbxBQLVrRtv0Q6FmDAR3bns4T9fRebpCBbg==`
- npm launcher: `$GROK_STAGE1_PREFIX/node_modules/.bin/grok`
- isolated runtime binary:
  `$GROK_STAGE1_PREFIX/grok-home/bin/grok-0.2.118`
- runtime binary SHA-256:
  `2de5b9609a03492dd6b9e4cca9637d651fe998bb8371bf9f852e7b28b38c034e`
- observed version: `grok 0.2.118 (1e1687c1cf6a)`
- scratch lab: `/private/tmp/rt-grok-lab.DgmW6H`

The npm `postinstall` unexpectedly created these new live artifacts under
`$HOME/.grok`:

```text
bin/grok -> grok-0.2.118
bin/grok-0.2.118
config.toml   ([cli] installer = "npm")
```

The initial inventory showed only the pre-existing
`hooks/cmux-session.json` and `hooks/orca-status.json`. The three new files
were removed explicitly; the original hooks remain. No `grok`/`xai` LaunchAgent
or system daemon was installed, and no matching process remained after the
lab was stopped.

Exact uninstall for the bounded install is:

```text
GROK_STAGE1_PREFIX="$HOME/.local/share/rt-grok-stage1"
npm uninstall --prefix "$GROK_STAGE1_PREFIX" \
  @xai-official/grok
rm -rf "$GROK_STAGE1_PREFIX"
rm -rf /private/tmp/rt-grok-lab.DgmW6H
```

Those commands target only the dedicated prefix and scratch lab. They must
not be generalized to deleting the live `$HOME/.grok` directory;
its two pre-existing hook files are unrelated user state.

### Prior-claim verification matrix

| Prior claim | Stage 1 result | Evidence and limit |
| --- | --- | --- |
| A standalone `grok` CLI exists | Verified | The package launcher and isolated binary returned `0.2.118`; the cmux `grok` wrapper alone was not a usable standalone binary because it could not find a real Grok binary in PATH. |
| `GROK_HOME` is sufficient to isolate a lab | Refuted | `grok inspect --json` enumerated live Claude instructions/hooks/MCP config, and an actual run attempted to start `uvx sec-edgar-mcp` from live `$HOME/.claude.json`; the MCP process later exited. A future adapter needs a stronger isolation gate. |
| Session lifecycle is persistent and inspectable | Partially verified | `grok sessions list` initially reported no sessions; the hook run created session `019fcf03-c415-7943-9ac9-5166f2711737` with local summary/updates data. Resume/fork/clear behavior was not tested. |
| Hooks fire at useful lifecycle points | Verified, bounded | A lab hook observed `SessionStart` at startup, `UserPromptSubmit` when the prompt was submitted, then `SessionEnd` and `Stop` on shutdown. This was an invalid-API-key run, so successful-turn ordering remains untested. |
| A hook can inject a new user turn | Unverified / no evidence | Hooks executed shell side effects and received JSON payloads, but no native prompt-injection API was observed. T1 cannot be claimed. |
| ACP stdio is a resident subprocess surface | Verified, auth-gated | `grok agent stdio` answered ACP `initialize`, stayed alive with stdin held, returned `Authentication required` for `session/new`, and exited cleanly on EOF. No authenticated session or real prompt was run. |
| No local service/socket surface exists | Refuted for the current binary | `agent serve` exposed a local WebSocket at `127.0.0.1:2420` with a server key, and `agent leader` opened a custom Unix socket. The ACP handshake worked over WebSocket; `leader list` did not find a candidate, so leader discovery remains unresolved. |
| The process can be lease-owned | Process-level candidate verified | The lab observed a dedicated leader PID and socket, and the ACP stdio process remained resident until stopped. Lease fencing, duplicate ownership, crash recovery, and reconnect are still untested. |
| Authentication works headlessly | Verified in bounded lab | A read-only key extracted from the existing live login state authenticated ACP `session/new` and two `session/prompt` calls in the scratch environment; the intentional fake-key probe still returned HTTP 400. The key value was never logged or persisted by the lab. |

### Adapter design revisions

- Prefer a per-seat, user-owned ACP stdio child or `agent serve` child under an
  explicit PID/lease fence. Use a per-seat Unix socket or loopback bind and
  secret; do not install a LaunchAgent or system daemon.
- Run a preflight equivalent to `grok inspect --json` and fail closed when
  live external Claude config, MCP servers, hooks, or plugins are visible.
  `GROK_HOME` plus compatibility flags did not provide that boundary here.
- Make authentication an explicit preflight. Parse ACP `authMethods`, do not
  open a browser implicitly, and do not send Roundtable work until a valid
  authenticated `session/new` succeeds.
- Own and observe stdout/stderr, ACP `session/update`, turn completion and
  stop reasons; fence duplicate seats; clean sockets/ports; and recover from
  EOF, crash, and reconnect. Keep manual T0 drain as the fallback.
- Do not implement this adapter from the Stage 1 evidence alone. The next
  lab must first prove safe config isolation, valid authentication, a real
  prompt, reconnect/recovery, and a complete mail-to-wake-to-drain/ack path.

### Tier claim after Stage 1

- **T0 — claimable:** durable Roundtable mail plus manual drain.
- **T1 — unverified:** lifecycle hooks are real, but hook-to-new-turn
  injection is not established.
- **T2 — research candidate, not supported:** ACP stdio and the current local
  `agent serve`/`leader` surfaces are viable integration candidates, but auth,
  isolation, prompt delivery, reconnect, and end-to-end delivery are open.

## Stage 2 ACP subprocess experiment — 2026-08-05 (unauthenticated baseline)

The committed lab runner is
[`scripts/grok_acp_stage2_lab.py`](../scripts/grok_acp_stage2_lab.py). It is
not a product adapter: it refuses the checkout as a lab root, uses a temporary
Roundtable registry/project, sets `HOME`, `GROK_HOME`, and XDG directories to
the scratch root, and starts `grok agent --no-leader stdio` under an atomic
lease marker. The default registry and current project mailbox are never used.

Run used the previously recorded user-level `0.2.118` npm launcher with:

```text
mamba run -n general python scripts/grok_acp_stage2_lab.py \
  --lab-root /private/tmp/rt-grok-stage2c.0cFMa8
```

Raw evidence is retained in the scratch lab's `result.json` and
`acp-events.jsonl`. The lab created one physical test mail,
`20260806T013139Z-codex-to-grok-88545`; `rt-inbox -f json` exposed the same
message once from the maildir and once from the local pending ledger source.
No second physical delivery was present.

### Evidence

- **Isolation:** verified for this runner. ACP `initialize` reported the
  scratch project as `currentWorkingDirectory` and `mcpServers: []`; the
  prior live-Claude-config leakage was not observed when `HOME` and XDG roots
  were fenced as well as `GROK_HOME`. After removing two older, identified
  live npm binary remnants, a before/after check around the fresh run showed
  only the two pre-existing `$HOME/.grok/hooks` files and no new live Grok
  artifacts.
- **ACP framing and buffering:** verified. The runner sent newline-delimited
  JSON-RPC and received `initialize` while the child was still alive. The
  response advertised protocol version 1, Grok `0.2.118`, model `grok-4.5`,
  and the `grok.com` auth method.
- **Unauthenticated session:** blocked deterministically. `session/new`
  returned JSON-RPC `-32000`, `Authentication required`, with
  `no auth method id provided`; the process stayed alive until the lab closed
  it with SIGTERM.
- **Invalid-key failure:** reproduced. With the explicit fake
  `XAI_API_KEY=xai-stage2-invalid` override, `session/new` succeeded and
  emitted `_x.ai/mcp_initialized` plus `session/update`; the first
  `session/prompt` stayed process-live but returned `-32603` with HTTP 400
  `Incorrect API key provided`. The event stream ended the turn with
  `stop_reason: error` and returned the session to idle.
- **Mail injection:** not completed. The lab mail was present before and after
  the attempted prompt, with two logical source records and one physical
  `new/` file. Because no valid primary session was available, no Grok turn
  ran `rt-inbox` or `rt-ack`; the lab did not manufacture an ack.
- **Process death/restart:** reproduced. A child was killed with SIGKILL
  (`returncode -9`); a fresh child then completed `initialize` with a new PID
  and new `agentInstanceId`. ACP stdio has no reconnect operation in this
  experiment; recovery requires a new child and an independently authenticated
  session. Lease cleanup completed.

### Conclusion and upstream ask

The ACP path was **protocol-viable but delivery-unproven in this baseline**.
This run did not disprove that a valid authenticated Grok account could
complete the loop; it proved that an adapter cannot treat `initialize` or
`session/new` as a successful wake. The authenticated rerun below resolves the
credential blocker while retaining the remaining lifecycle gates.

The remaining upstream ask is a documented auth-expiry contract and a
session-resume/reconnect contract. The authenticated rerun demonstrates the
fresh-child path and one real mail-to-wake-to-drain/ack run, but not resume of
an existing session after process death.

### Stage 2 adapter position

- Keep the maildir fact source and manual drain as an internal fallback only.
- If auth becomes available, use one user-owned ACP child per fenced seat,
  preserve the scratch isolation preflight, and require `session/new` before
  sending work. Treat `session/update`, `stop_reason`, EOF, PID, and lease
  revision as authoritative health evidence.
- Do not add Grok launcher/runtime wiring, hooks, daemon jobs, or LaunchAgents
  from this experiment.

## Authenticated Stage 2 ACP rerun — 2026-08-05

This rerun supersedes the unauthenticated baseline above. It used the
previously recorded user-level `0.2.118` launcher and a fresh lab root:

```text
mamba run -n general python scripts/grok_acp_stage2_lab.py \
  --lab-root /private/tmp/rt-grok-stage2-auth3.j6RZdj
```

The existing live login state was read only to obtain the credential for the
child environment. Its value was never printed, stored in the lab, committed,
or included in this handoff. A pre/post metadata check showed the live auth
file unchanged (`1711` bytes and the same modification time). The lab kept
the project, registry, `HOME`, `GROK_HOME`, XDG roots, and Roundtable mailbox
under the scratch root; the existing general environment's PyYAML path was
passed through explicitly so `rt-inbox` could run without an install.

### Verified evidence

- **Authenticated ACP session:** `initialize`, `session/new`, and two
  `session/prompt` requests succeeded. The primary session used model
  `grok-4.20-0309-non-reasoning`; both prompts returned `stopReason: end_turn`
  while the child remained alive.
- **Process/session reuse:** both prompts used session
  `019fd4d1-bfe4-7342-9372-a72776c98a14` in one resident primary ACP child;
  the child remained alive before the lab closed it.
- **Permission protocol:** the runner handled ACP
  `session/request_permission` with a fail-closed lab policy. It allowed
  exactly `rt-inbox -f json`, the exact `RT_FROM=grok rt-ack <message-id>`,
  and the final `rt-inbox -f json`; an attempted `pip install PyYAML` in the
  earlier rerun was rejected. No product runtime or live environment was
  granted these lab decisions.
- **Mail-to-wake-to-drain-to-ack:** the lab delivered
  `20260806T020547Z-codex-to-grok-88428`. ACP Grok ran `rt-inbox`, then
  acknowledged that exact id once; the final `rt-inbox -f json` returned `[]`.
  The lab ended with zero physical files in `grok/new/` and one archived file
  in `grok/cur/`. The two initial records were the expected maildir and
  pending-ledger views of one logical message.
- **Invalid-auth guard:** the intentional fake-key probe returned the
  machine-visible ACP error `-32603` with HTTP 400 `Incorrect API key
  provided`, without weakening the authenticated path.
- **Death/restart:** a child died with SIGKILL (`-9`), and a fresh child
  initialized successfully with the same stable agent id and a new process/
  instance identity. Existing-session resume was not attempted; ACP recovery
  currently means a new child and a new authenticated session.

### Productization adapter design

1. **Seat supervisor and fencing.** Start one user-owned `grok agent
   --no-leader stdio` child per fenced Roundtable seat. Persist only a
   non-secret PID/lease record, require the current lease revision for every
   send/drain/ack action, and refuse duplicate or ambiguous ownership. Do not
   add a daemon, LaunchAgent, or live Roundtable wiring in this phase.
2. **Hermetic preflight.** Construct a declared child environment with the
   project cwd, `HOME`, `GROK_HOME`, XDG roots, Roundtable registry, `RT_FROM`,
   and the installed Roundtable/PyYAML runtime. Run an inspection gate and
   fail closed if live Claude config, MCP servers, hooks, or plugins leak
   into the child. The lab's explicit PyYAML path is a packaging requirement,
   not permission to install dependencies at runtime.
3. **Auth gate.** Read existing auth material only through an approved
   credential boundary; never echo, persist, or put it in logs or handoffs.
   Require successful ACP `initialize` plus authenticated `session/new`
   before sending a wake prompt. Map expiry/invalid credentials to a stable
   blocked state and retain the manual T0 drain.
4. **Wake and permission bridge.** Convert one inbound Roundtable wake into
   one `session/prompt`, keep the prompt tiny and explicit, and service ACP
   `session/request_permission` requests through an operator/policy callback.
   Default deny; never use the lab's command allowlist as a production
   blanket approval policy. Only acknowledge mail after the agent reports a
   successful drain/ack and the supervisor verifies the mailbox state.
5. **Lifecycle and recovery.** Record structured `session/update`, stop
   reason, stdout/stderr, PID, agent id/instance id, duration, and lease
   revision evidence. Keep the child for subsequent prompts, detect EOF,
   timeout, SIGTERM/SIGKILL, and stale leases, then start a new child and
   explicitly create or load a session. Resume behavior remains a release
   gate because this lab only verified new-child initialization.
6. **Release gates.** Before a public support claim, run two independent
   send-to-wake-to-drain-to-ack generations, a permission-denial path, lease
   contention, process death, recovery, auth expiry, and a release-artifact
   install smoke test. Preserve the maildir as fact source and prove both
   logical inbox emptiness and physical `new/` emptiness after each drain.

### Current tier claim

- **T0 — claimable:** durable Roundtable mail plus manual drain.
- **T1 — unverified:** lifecycle hooks are real, but hook-to-new-turn
  injection is not established.
- **T2 — lab-verified / production-unclaimed:** authenticated ACP wake and
  one complete mail-to-wake-to-drain-to-ack generation are verified in a
  scratch lab. Public support still requires the lifecycle, recovery,
  packaging, and two-generation gates above.

## Boundary and safety notes

- This handoff responds to the Stage 2 directive
  `20260806T005834Z-claude-to-codex-95618` from `claude`, building on the
  earlier Stage 1 messages; it is a lab deliverable, not live runtime wiring.
- Existing unrelated `CLAUDE.md` worktree changes were preserved untouched.
- No system-level installation, daemon/LaunchAgent, live Roundtable runtime
  wiring, public `main` push, release asset, or tag change was performed. The
  only committed code is the explicitly scratch-only lab runner.
