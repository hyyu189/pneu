# Harness expansion survey — TUI-first wake paths

> Status: current — the 13-candidate verdict table; no candidate has been implemented

Date: 2026-08-12. Scope: read-only candidate survey requested in
`handoff/archive/harness-expansion-dispatch.md`; no interactive harness session was
started and no credential store was read. In this report, **ready** means
"specific enough to implement and run an isolated adapter probe", not
release-supported. Every candidate still needs the fenced live tests listed
below.

## Verdict table

| Candidate | Native interactive seat | Best same-seat wake path | Verdict | Effort | Decisive next proof |
|---|---|---|---|---|---|
| **Pi** | Yes | Extension watcher -> `sendUserMessage` / `sendMessage(..., {triggerTurn:true})` | **A-ready** | S | Two idle/busy deliveries into one visible resumed TUI session |
| **Kiro CLI** | Yes | No current live-TUI injection or persistent wake contract found | **Parked** | M after unpark | Version-pinned proof of same-TUI injection, or a new supported API |
| **OpenCode** | Yes | TUI server `/tui/*` and session APIs; plugin events | **A-ready** | M | Bind external submit to the exact attached TUI session and lease |
| **Oh My Pi (OMP)** | Yes | Extension watcher/timer -> `sendMessage(..., {triggerTurn:true})` | **A-ready** | S | Two sequential idle/busy wakes plus resume and cleanup |
| **GitHub Copilot CLI** | Yes | Seeded experimental `/every` / `/loop`, restored on resume | **B-ready** | S | Show no lost schedule across resume and two Delivery-v2 generations |
| **Devin CLI** | Yes | Synchronous Stop hook waits, then returns `decision:block` + mail | **A-ready, conditional** | S implementation / M proof | Establish timeout/failure semantics and zero-model idle wait |
| **Factory Droid** | Yes | Synchronous Stop hook waits, then exit 2 or `decision:block` + mail | **A-ready, conditional** | S implementation / M proof | Survive timeout, interrupt, config snapshot, and resume |
| **Kimi Code** | Yes | Cron/Stop can re-enter an existing turn, but no interactive first-turn seed | **Parked** | M after unpark | Add a supported TUI seed or active-TUI enqueue API |
| **Kilo Code CLI** | Yes | Server/attach plus `run --attach --session`; plugin SDK events | **A-ready** | M | Prove external run and attached TUI share the exact session |
| **Qoder CLI** | Yes | `asyncRewake` hook exits 2 -> reminder + model wake | **A-ready** | S | SessionStart-arm, idle wake, re-arm after resume, two generations |
| **Cursor Agent CLI** | Yes | Positional seed exists; no documented async channel into exact CLI TUI | **Parked** | M after unpark | Prove `/loop` or another current channel runs in and resumes the CLI TUI |
| **Mastra Code** | Yes, including exported `MastraTUI` | Official controller/session/signals API with a thin native-TUI wrapper | **A-ready via wrapper** | M | One controller/thread must both render TUI and receive cross-process signal |
| **Google Antigravity CLI (`agy`)** | Yes | `-i` seed + schedule/persistent sidecar; `agentapi send-message` is an A candidate | **B-ready; A-candidate** | M | Same-conversation sidecar injection on current version and resume |

The strongest first labs are **Qoder and Pi/OMP** (small native wake adapters),
then **OpenCode/Kilo** (more conventional server/session identity work). Devin
and Droid are valuable but riskier: their documented mechanism deliberately
holds a Stop hook open, so timeout and loop behavior—not code size—is the
confidence bottleneck.

## Decision rule and evidence discipline

- **Shape A:** a harness-native socket/server/RPC/plugin/hook injects work into
  the same live interactive TUI/session. A headless replacement session does
  not count.
- **Shape B:** the first visible TUI turn can be seeded, and that turn can arm a
  persistent monitor/scheduler/loop that later wakes the model. Re-arming on
  each launch/resume is acceptable.
- **[H] Host observation:** executable/version/help inspected without entering
  a TUI. Only `opencode 1.17.3` and Cursor Agent
  `2026.05.24-dda726e` were present. `pi`, `kiro-cli`, `omp`, `copilot`,
  `devin`, `droid`, `kimi`, `kilo`, `qodercli`, `mastracode`, and `agy` were
  absent. OpenCode help exposed TUI/serve/attach/session options; Cursor help
  exposed initial prompt/resume/plugin/worker options.
- **[D] Official-document/source inference:** all capability, install, and auth
  statements below unless explicitly tagged [H]. No readiness verdict is a
  host support claim.
- **[L] Required live smoke:** an isolated, version-pinned run with a visible
  native TUI, a unique project/cwd and lease, two sequential fenced messages,
  busy and idle arrival, resume/reconnect, interrupt/cleanup, and Delivery-v2
  acknowledgement evidence. The probe must show that no parallel headless
  conversation consumed the mail.

## Priority candidates

### Pi — A-ready, S

- **TUI / macOS / auth [D]:** `pi` is the native interactive terminal agent;
  it accepts an initial prompt and supports `-c`, `-r`, and `--session`.
  Install is available through npm or the official installer. `/login` and
  provider/API-key configuration are documented; an adapter must never copy
  or log those credentials.
- **Shape A [D]:** an auto-loaded extension can start a file watcher during
  `session_start`, call `sendUserMessage()` or
  `sendMessage(..., {triggerTurn: true})`, and thereby start a turn in the
  same TUI session. `session_shutdown` supplies the cleanup boundary. RPC mode
  is a separate headless surface and is not needed.
- **Shape B / resume [D]:** an initial prompt can arm the extension and session
  resume is first-class, but A is both simpler and less model-mediated.
- **Lifecycle [D]:** session start/shutdown, turn/tool events, and extension
  commands are sufficient to install, re-arm, and remove the watcher.
- **[L]:** prove exact TUI/session identity, two sequential wakes (one while
  idle and one while streaming), queued delivery, resume re-arm, watcher crash
  visibility, and fenced ACK ownership.
- **Sources:** [Pi coding-agent documentation](https://pi.dev/docs/latest),
  [extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md),
  [sessions](https://pi.dev/docs/latest/sessions).

### Kiro CLI — parked; M after unpark

- **TUI / macOS / auth [D]:** Kiro has a rich interactive terminal UI, accepts
  a positional first prompt, and supports `--resume` / `--resume-id`. Official
  macOS installation and browser/device authentication are documented.
- **Shape A [D]:** ACP is a separate stdio agent, not a documented second
  client for an already-running TUI. Current v3 Stop hooks cannot block, and
  filesystem hooks cover agent-originated changes rather than an arbitrary
  external Maildir arrival.
- **Shape B [D]:** seed and resume exist, but no documented persistent monitor,
  scheduler, or model-rewake contract was found. Old v2 Stop-hook descriptions
  conflict with the current v3 migration material and therefore cannot support
  a present claim.
- **Lifecycle [D]:** hooks cover commands, tools, prompts, and files, but their
  current semantics do not form an idle wake channel.
- **Exact unpark:** either (1) a supported socket/server/ACP multi-client API
  that targets the exact live TUI session, or (2) a version-pinned live proof
  that a current Stop/file hook waits for arbitrary external mail, injects the
  dynamic message, re-arms without empty model turns, and survives
  `--resume-id`.
- **Sources:** [interactive chat](https://kiro.dev/docs/cli/chat/),
  [CLI reference](https://kiro.dev/docs/cli/reference/cli-commands/),
  [ACP](https://kiro.dev/docs/cli/acp/), [hooks](https://kiro.dev/docs/hooks/),
  [v3 hooks migration](https://kiro.dev/docs/cli/v3/hooks-migration/),
  [installation](https://kiro.dev/docs/getting-started/installation/),
  [authentication](https://kiro.dev/docs/getting-started/authentication/).

### OpenCode — A-ready, M

- **TUI / macOS / auth [D, H]:** bare `opencode` is the native TUI. Official
  curl, npm, and Homebrew installs are documented; `/connect` or
  `opencode auth login` configures a provider. [H] Version 1.17.3 is installed
  and only its help/version surfaces were inspected.
- **Shape A [D]:** the running TUI is a client of OpenCode's HTTP server. The
  official server exposes `/tui/append-prompt`, `/tui/submit-prompt`, and
  session message / asynchronous prompt endpoints; `opencode attach` connects
  a TUI to an existing server. This is a conventional same-session injection
  path if the adapter binds the correct server and session.
- **Shape B / resume [D]:** `--prompt`, `--continue`, and `--session` can seed
  or resume, while plugins receive session/status/message events. A is
  preferable because it need not poll.
- **Lifecycle [D]:** plugins expose session created/idle/status and other
  server events; server startup, reconnect, and teardown must be explicit.
- **[L]:** use a fixed loopback address with server password, resolve the
  session rendered by the seat TUI, submit twice, reconnect/resume, and prove
  cwd/project/lease fencing. Also reconcile installed 1.17.3 behavior with
  current docs before implementation.
- **Sources:** [OpenCode overview/install](https://dev.opencode.ai/docs),
  [CLI](https://dev.opencode.ai/docs/cli/),
  [server API](https://dev.opencode.ai/docs/server/),
  [plugins](https://dev.opencode.ai/docs/plugins/),
  [providers/auth](https://dev.opencode.ai/docs/providers).

## Extended roster

### Oh My Pi (OMP) — A-ready, S

- **TUI / macOS / auth [D]:** this is `can1357/oh-my-pi`, not upstream Pi.
  `omp` is its interactive TUI; official installer, Homebrew tap, and Bun
  paths are documented. `/login` / `omp auth-broker login` and provider
  configuration use OMP's own credential store.
- **Shape A [D]:** auto-discovered JS/TS extensions can own managed
  watchers/timers and call `sendMessage(..., {triggerTurn:true})` while idle;
  `sendUserMessage` follows the normal prompt path. The same API supports
  steer/follow-up behavior while a turn is active.
- **Shape B / resume [D]:** resume/continue and extension start are first-class,
  but direct A injection is stronger.
- **Lifecycle [D]:** `session_start`, managed intervals, session shutdown, and
  turn/tool/MCP events support arming and cleanup.
- **[L]:** two idle/busy deliveries into one visible TUI/session, queue
  ordering, resume re-arm, extension exception handling, and fenced ACKs.
- **Sources:** [repository/install](https://github.com/can1357/oh-my-pi),
  [extension injection](https://github.com/can1357/oh-my-pi/blob/main/docs/extensions.md),
  [extension loading](https://github.com/can1357/oh-my-pi/blob/main/docs/extension-loading.md),
  [resume](https://github.com/can1357/oh-my-pi/blob/main/docs/session-operations-export-share-fork-resume.md),
  [providers/auth](https://github.com/can1357/oh-my-pi/blob/main/docs/providers.md).

### GitHub Copilot CLI — B-ready, S

- **TUI / macOS / auth [D]:** `copilot` is interactive; `-i/--interactive
  PROMPT` seeds a visible TUI. Official Homebrew, npm (Node 22+), and installer
  paths exist. `copilot login` uses browser/device auth and the product's
  secure credential store.
- **Shape A [D]:** `agentStop` may return `decision:block` and a reason, but
  commands default to a bounded timeout and Copilot stops after eight
  consecutive block continuations. Remote Control can inject web/mobile input
  into a local TUI, but no supported local enqueue API for pneu is documented.
  This is not a durable A path.
- **Shape B [D]:** experimental `/every` (also documented as `/loop`) and
  `/after` automatically submit prompts while the interactive session remains
  open. Schedules restart with `--continue` / `--resume`; `-i` supplies the
  first visible turn. Minimum cadence and experimental status make this a
  polling adapter, not an event-driven one.
- **Lifecycle [D]:** session, prompt, tool, permission, agent/subagent stop,
  and notification hooks exist; prompt hooks do not replace resume re-arming.
- **[L]:** seed the recurring mail check, prove two generations and schedule
  restoration after resume, measure empty-poll model cost, and exercise the
  eight-block guard so the implementation never silently falls back to it.
- **Sources:** [CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference),
  [schedule prompts](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/schedule-prompts),
  [hooks](https://docs.github.com/en/copilot/reference/hooks-reference),
  [Remote Control](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-remote-control),
  [installation](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli).

### Devin CLI — conditional A-ready, S implementation / M proof

- **TUI / macOS / auth [D]:** local Devin CLI is a native interactive terminal
  application, distinct from cloud Devin. It accepts an initial prompt and
  supports `--continue` / `--resume`. Official curl/Homebrew installation and
  `devin auth login` browser/manual-token flows are documented.
- **Shape A [D, conditional]:** Stop fires as the agent decides to finish. A
  command hook can synchronously wait on Maildir, then return
  `{"decision":"block","reason":"<mail>"}`; the reason becomes a follow-up
  in the same CLI session. `stop_hook_active` helps avoid recursion. This meets
  the dispatch's hook-inclusive A definition only if the hook can remain
  dormant without timing out or spending model turns.
- **Shape B / resume [D]:** seeded start and resume exist; background subagents
  survive reload and notify the parent, but their ability to wake an otherwise
  idle parent is not needed—or proven—if Stop succeeds.
- **Lifecycle [D]:** tool, permission, prompt, Stop, compaction, SessionStart,
  and SessionEnd hooks exist. Docs warn that blocking Stop hooks can loop and
  do not state a default/max timeout or timeout failure policy.
- **[L] hard gate:** wait beyond likely timeout with zero model calls, deliver
  two mails sequentially into the same visible session, and test timeout,
  error/exit-0, interrupt, recursion guard, resume re-arm, and cleanup. Any
  fail-open timeout that lets the turn finish demotes this candidate to parked.
- **Sources:** [CLI](https://docs.devin.ai/cli),
  [commands](https://docs.devin.ai/cli/reference/commands),
  [hook overview](https://docs.devin.ai/cli/extensibility/hooks/overview),
  [lifecycle hooks](https://docs.devin.ai/cli/extensibility/hooks/lifecycle-hooks),
  [authentication](https://docs.devin.ai/cli/enterprise/devin-auth).

### Factory Droid — conditional A-ready, S implementation / M proof

- **TUI / macOS / auth [D]:** `droid` is a full-screen terminal UI;
  `droid "query"` seeds and `droid --resume [id]` resumes. Official curl/npm
  installs and first-run browser sign-in are documented; `FACTORY_API_KEY` is
  an automation option, not something the adapter should harvest.
- **Shape A [D, conditional]:** Stop fires before the main Droid finishes. A
  synchronous watcher can exit 2 with the mail in stderr, or return
  `decision:block/reason`, preventing stop and continuing the same TUI.
- **Shape B / resume [D]:** seed/resume and background process tracking exist,
  but docs do not promise that a background completion begins a fresh idle
  parent turn. The Stop path is the only current same-TUI claim.
- **Lifecycle [D]:** prompt, notification, Stop/SubagentStop, compaction,
  SessionStart/End, and tool hooks exist. Hooks are snapshotted at startup;
  SessionStart distinguishes resume. Command timeout defaults to 60 seconds
  and is configurable, with no documented maximum.
- **[L] hard gate:** hold the seat with no model calls, inject two sequential
  mails, exercise 60-second timeout/error/interrupt and `stop_hook_active`,
  restart after a config change, resume, and verify cleanup. A timeout that
  disarms the seat or an empty-block loop demotes it to parked.
- **Sources:** [quickstart](https://docs.factory.ai/droid-cli/quickstart),
  [CLI reference](https://docs.factory.ai/droid-cli/cli-reference),
  [hooks](https://docs.factory.ai/harness/hooks),
  [official repository](https://github.com/Factory-AI/factory).

### Kimi Code — parked; M after unpark

- **TUI / macOS / auth [D]:** this is the current `MoonshotAI/kimi-code`, not
  the winding-down `MoonshotAI/kimi-cli`. Bare `kimi` is a native TUI;
  official curl/npm installation, OAuth device login, API keys, `--session`,
  and `--continue` are documented. `-p/--prompt` is explicitly noninteractive.
- **Shape A [D]:** a Stop hook can block and append a message after a turn
  exists, but timeouts are bounded to 1–600 seconds and fail open. Web/ACP
  surfaces are not documented as clients of the already-running native TUI.
- **Shape B [D]:** current-session Cron tools can reinject prompts and survive
  `kimi --session`; however, no supported argv option seeds the first visible
  TUI turn or installs that Cron unattended.
- **Lifecycle [D]:** prompt/queue, Stop, turn/session, tool, notification, and
  heartbeat hooks exist. Only prompt, pre-tool, and Stop hooks block;
  SessionStart is observational rather than an auto-submit surface.
- **Exact unpark:** a supported interactive initial-prompt option,
  SessionStart auto-submit, or active-TUI enqueue API. Then test either the
  bounded/fail-open Stop loop or session Cron over resume with two generations.
- **Sources:** [current repository](https://github.com/MoonshotAI/kimi-code),
  [legacy disambiguation](https://github.com/MoonshotAI/kimi-cli),
  [getting started](https://moonshotai.github.io/kimi-code/en/guides/getting-started),
  [command reference](https://moonshotai.github.io/kimi-code/en/reference/kimi-command),
  [tools/scheduler](https://moonshotai.github.io/kimi-code/en/reference/tools.html),
  [hooks](https://moonshotai.github.io/kimi-code/en/customization/hooks).

### Kilo Code CLI — A-ready, M

- **TUI / macOS / auth [D]:** `kilo` is a native TUI; npm installation and
  `/connect` / `kilo auth` for user-managed providers are documented.
- **Shape A [D]:** Kilo documents a local server/daemon, `kilo attach`, and
  `kilo run ... --attach <server-url> --session <id>`. This should submit to
  the session rendered by the attached TUI rather than create a replacement
  session, but that identity is the central live-test gate.
- **Shape B / resume [D]:** initial prompt, `--continue`, and `--session` are
  supported. Startup plugins receive the SDK client/server URL and session,
  message, file-watcher, and server events.
- **Lifecycle [D]:** plugin events are rich enough for registration and
  teardown; the adapter still needs explicit server authentication, session
  selection, reconnect, and lease checks.
- **[L]:** externally `run --attach --session` twice while the exact TUI is
  attached; observe both prompts and outputs there, then restart/resume and
  repeat with unique cwd/project/lease fencing.
- **Sources:** [CLI overview](https://kilo.ai/docs/code-with-ai/platforms/cli),
  [CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference),
  [plugins](https://kilo.ai/docs/automate/extending/plugins).

### Qoder CLI — A-ready, S

- **TUI / macOS / auth [D]:** Qoder CLI has a native interactive UI,
  installer/npm/Homebrew paths, browser login, and PAT authentication.
  `--prompt-interactive/-i`, `--continue`, and `--resume` are documented.
- **Shape A [D]:** a command hook with `asyncRewake:true` runs in the
  background; exit code 2 makes the CLI construct a system reminder from its
  output and explicitly wake the model. This is the closest documented analog
  to Claude's existing async-rewake design.
- **Shape B [D]:** persistent scheduled tasks and `/loop` also exist, but are
  unnecessary for an event-driven adapter. Scheduler constraints (single
  process, missed-run handling, minimum cadence, expiry) make it a fallback.
- **Lifecycle [D]:** `SessionStart` distinguishes startup, resume, clear,
  compact, and new sessions; Stop and other hooks carry session/transcript/cwd
  identity. Hook config is hot-loaded and supports explicit timeout/status.
- **[L]:** arm from SessionStart, block in a background mail watcher, exit 2
  only on mail, prove same idle TUI wake and two re-arms, then resume/compact
  and repeat. Validate duplicate suppression and exact fence propagation.
- **Sources:** [installation](https://docs.qoder.com/cli/installation),
  [authentication](https://docs.qoder.com/cli/authentication),
  [CLI reference](https://docs.qoder.com/cli/cli-reference),
  [hooks](https://docs.qoder.com/cli/hooks),
  [scheduled tasks](https://docs.qoder.com/cli/scheduled-reference),
  [loop](https://docs.qoder.com/cli/loop-reference).

### Cursor Agent CLI — parked; M after unpark

- **TUI / macOS / auth [D, H]:** `agent` / `cursor-agent` is a native TUI with
  positional initial prompt and resume/continue. Official installer,
  `cursor-agent login`, and API-key paths are documented. [H] Installed build
  is `2026.05.24-dda726e`; only version/help was inspected.
- **Shape A [D]:** Stop can return `followup_message`, but this immediately
  chains a bounded number of turns rather than waking a TUI after it becomes
  idle. SessionStart/End are fire-and-forget. ACP is a separate stdio mode and
  is not documented as attaching to the live TUI conversation.
- **Shape B [D, unresolved]:** Cursor's 2026-05-20 changelog advertises a
  `/loop` skill for local long-running agents, including event-chosen wakeups.
  Current CLI documentation inspected for this survey does not bind that
  feature to the terminal TUI or document its resume persistence. The
  installed help also trails current docs on ACP, demonstrating version drift.
- **Lifecycle [D]:** session/tool/subagent/Stop hooks exist, but none has a
  documented asynchronous re-wake contract for the CLI seat.
- **Exact unpark:** vendor documentation or a current-version live proof that
  `/loop`/scheduler/event delivery runs inside the exact CLI TUI conversation,
  wakes it from idle without empty model turns, and persists or re-arms after
  resume; alternatively, an API that attaches to that exact live conversation.
- **Sources:** [CLI overview](https://cursor.com/docs/cli/overview),
  [CLI usage](https://cursor.com/docs/cli/using),
  [installation/auth](https://cursor.com/docs/cli/installation),
  [hooks](https://cursor.com/docs/hooks), [ACP](https://cursor.com/docs/cli/acp),
  [2026-05-20 `/loop` announcement](https://cursor.com/changelog/page/4).

### Mastra Code — A-ready via official wrapper, M

- **TUI / macOS / auth [D]:** this is Mastra.ai's `mastracode`, not Mistral.
  The package provides a native `pi-tui`; npm/pnpm/yarn/Bun installation on
  Node 22.13+ and OAuth/provider-key authentication are documented.
- **Shape A [D]:** the official programmatic surface exports
  `createMastraCode()`, a ready session `sendMessage()`, `MastraTUI`,
  controller interval handlers, cross-process Unix-socket PubSub, and Agent
  Signals that address and wake idle threads. A thin official-API wrapper can
  render the TUI over the same controller/thread that receives Roundtable
  signals. The stock binary alone exposes no equivalent external enqueue API.
- **Shape B / resume [D]:** stock `-p` is headless and therefore not a seat;
  thread resume exists, but A through the compositional API is the credible
  route.
- **Lifecycle [D]:** stock SessionStart/End, AgentStart/End, Stop, and tool
  hooks lack async re-wake. The wrapper must own controller init/destroy,
  PubSub lifecycle, interval cleanup, and durable thread identity.
- **[L]:** render official `MastraTUI` and inject a cross-process signal into
  that exact controller/thread; test idle and busy queueing, restart/resume,
  and two fenced generations. If Roundtable requires the unmodified CLI
  binary rather than an official compositional TUI, this candidate is parked
  until the stock binary gains equivalent IPC.
- **Sources:** [Mastra Code overview](https://code.mastra.ai/),
  [configuration](https://code.mastra.ai/configuration),
  [customization](https://code.mastra.ai/customization),
  [API reference](https://code.mastra.ai/reference),
  [AgentController](https://mastra.ai/reference/agent-controller/agent-controller-class),
  [Agent Signals](https://mastra.ai/blog/announcing-agent-signals).

## Mandatory Antigravity re-examination

### Google Antigravity CLI (`agy`) — B-ready, A-candidate, M

- **TUI / macOS / auth [D, H]:** `agy` has a native TUI and official installer;
  OS keyring, browser OAuth, and SSH URL/code login are documented. [H] It is
  not currently installed. The previous isolated lab used arm64 1.1.10; this
  survey did not launch it or touch auth.
- **Shape A candidate [D]:** current sidecar docs expose persistent managed
  processes and `agentapi send-message <conversation_id> <prompt>`. That is a
  plausible external injection path, but docs do not yet prove that the target
  conversation is simultaneously rendered by the native TUI.
- **Shape B [D]:** `--prompt-interactive/-i` seeds the native TUI. The model can
  install a schedule and a persistent/crash-restarted sidecar that watches
  Maildir; resume uses `--conversation`, `-c/--continue`, or `/resume`. This
  satisfies the accepted model-armed B shape if re-armed per launch/resume.
- **Lifecycle [D]:** hooks remain Pre/PostToolUse, Pre/PostInvocation, and
  Stop. There is still no SessionStart/resume hook, and Stop continuation is
  not an asynchronous idle-wake substitute.
- **What changed since the 2026-08-05 ruling:** releases 1.1.11 and 1.1.12 did
  **not** add a startup/resume hook. The material correction is discovery of
  the official sidecar/`agentapi send-message` surface and application of the
  newly accepted Shape B rule. This capability may not itself be new: release
  1.1.6 (2026-07-24) already mentioned CLI `sidecar.json`, and the tested
  1.1.10 already exposed scheduling. Therefore the old host observation—no
  wake surface was observed in that lab—remains true, but the broader parked
  conclusion was incomplete rather than cleanly obsoleted by a new release.
- **[L] exact promotion test:** on current 1.1.12, launch an isolated visible
  TUI with `-i`; have a persistent sidecar detect a file event; prove
  `agentapi send-message` wakes and renders in that exact conversation UUID;
  repeat via both `--conversation` and `--continue`, with cwd/project/lease
  checks and two fenced Delivery-v2 generations. Until that passes,
  operational support remains T0/parked even though the research verdict is
  B-ready.
- **Sources:** [installation/auth](https://antigravity.google/docs/cli/install),
  [CLI reference](https://antigravity.google/docs/cli/reference),
  [conversations](https://antigravity.google/docs/cli/conversations),
  [hooks](https://antigravity.google/docs/hooks),
  [sidecars](https://antigravity.google/docs/sidecars),
  [releases](https://github.com/google-antigravity/antigravity-cli/releases),
  [changelog](https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md).

## Cross-candidate implementation gates

1. **Same-seat identity is the first assertion.** Record the native TUI's
   conversation/session/thread ID, cwd, project UUID, agent address, and lease
   revision. Reject any wake that cannot be proven to target that tuple.
2. **No headless substitution.** A convenient `-p`, ACP, SDK, or server mode
   counts only if it is demonstrably a second client/injector for the session
   rendered by the user's TUI.
3. **Two generations, not one demo.** Deliver mail A while idle, let the seat
   act and re-arm, deliver mail B while busy or after resume, and require one
   ACK per logical `msg_id` with duplicate maildir/ledger records collapsed.
4. **No empty-turn burn.** For Stop-hook and scheduled candidates, measure
   model requests during a quiet interval. A timeout that ends the turn
   disarms the seat; a timeout that fabricates another model turn is not a
   quiet watcher.
5. **Fail closed on drift.** Pin and report binary version, probe the actual
   command/API/hook contract, and make doctor name the missing executable or
   unsupported capability. Version comparisons and docs alone never claim
   support.
6. **Credential boundary.** Use the harness's normal login/keychain/provider
   path. Roundtable stores neither copied tokens nor credential output.

Recommended lab order: **Qoder -> Pi/OMP -> OpenCode/Kilo -> Antigravity ->
Copilot -> Devin/Droid -> Mastra wrapper**. Kiro, Kimi, and Cursor stay parked
until their exact unpark condition is met; they should not consume adapter
implementation time before that evidence exists.
