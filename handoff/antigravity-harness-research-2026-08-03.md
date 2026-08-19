# Google Antigravity / Gemini CLI harness research

> Status: historical record — Antigravity is parked at T0 (decision.md 2026-08-05). Kept at this path because that ledger entry cites it.

Status: research and staged execution plan only. No Antigravity or Gemini CLI
was installed, and no product code or host configuration was changed.

Audience: Claude integration workstream and Ocean as product lead.

## Executive conclusion

There are three distinct surfaces that should not be conflated:

1. **Antigravity IDE / Manager**: a desktop, agent-first development surface
   with asynchronous background agents and reviewable artifacts.
2. **Antigravity CLI (`agy`)**: a separately documented terminal/TUI product
   with its own installer, authentication, projects, sessions, hooks, and
   subagent model.
3. **Gemini CLI (`gemini`)**: Google's open-source terminal agent. It has the
   clearest documented headless, resumable-session, and lifecycle-hook
   contracts, but it is not evidence that Antigravity CLI has the same external
   protocol.

**Recommendation:** do not add a supported harness claim yet. The first
hands-on candidate should be Antigravity CLI (`agy`), not the IDE/Manager UI.
Gemini CLI is a useful adapter reference and may be an experimental target of
its own. Both need a real local session and send-to-wake-to-drain/ack smoke
before entering the supported matrix.

The current machine has no `agy`, `antigravity`, or `gemini` executable on
PATH, so all findings below are documentation-derived unless explicitly marked
as an inference.

## Research grid

| Area | Antigravity CLI (`agy`) | Gemini CLI (`gemini`) | Roundtable implication |
| --- | --- | --- | --- |
| Install | Official docs describe native macOS/Linux/Windows installation and a macOS/Linux script that places `agy` in `~/.local/bin`; the script can also update shell PATH/aliases. | Official docs describe `npm install -g @google/gemini-cli`, `npx @google/gemini-cli`, and Node.js >=20 in the upstream project context. | Resolver and setup must be separate. Roundtable should detect an existing binary, never install it implicitly, and review installer side effects before offering onboarding. |
| Session model | CLI projects can be selected with `--project`; conversations can be resumed; docs expose a `conversationId`, workspace paths, transcript path, and artifact directory to hooks. | Sessions are project-specific, auto-saved under `~/.gemini/tmp/<project_hash>/chats/`, and resumable by latest, index, or UUID. | A descriptor needs a native-session identity, canonical cwd/project proof, resume semantics, and process ownership. `RT_SESSION_ID` alone is not proof of native binding. |
| Lifecycle hooks | Current docs show JSON-stdio hooks in `hooks.json` for `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, and `Stop`. The checked hook page does not document a startup/resume hook. | `settings.json` hooks include `SessionStart` (startup/resume/clear), `SessionEnd`, `Notification`, and tool/model/agent events; hooks use JSON stdin/stdout and exit codes. | Gemini has a plausible native lifecycle seam. Antigravity CLI may only be able to arm after an invocation unless hands-on testing finds an earlier event. Do not infer auto-wake from hook presence alone. |
| Daemon/socket | The public CLI docs describe a local TUI and shared agent harness, but no stable external daemon, socket, or wake RPC contract was found. Background agents are an internal asynchronous execution model. | The architecture separates CLI and core packages; no stable Roundtable-grade external daemon/socket or wake RPC contract was found in the checked docs. | Prefer a process-owned adapter. Do not add a Codex-style service/Unix-socket integration without a documented protocol and a live probe. |
| Launcher / fenced env | A real `agy` executable and project argument appear to be launchable; exact flags, noninteractive behavior, and environment inheritance still need hands-on proof. Authentication uses OS keyring/browser flows. | `gemini` supports `-p/--prompt`, `--resume`, and machine-readable headless output. | The generic launcher can export `RT_PROJECT_ROOT`, `RT_FROM`, `RT_SESSION_ID`, and `RT_LEASE_REVISION`, but the adapter must prove that the native session and process correspond to those values. |
| Wake tier | T0 durable delivery is already independent of the harness. T1 is plausible only if a native hook can keep a fenced watcher armed and the CLI exposes a safe turn boundary; no startup hook or external wake API is documented in the checked page. T2 terminal/keyboard injection is out of scope. | T0 works now. T1 is more plausible because `SessionStart`/`SessionEnd` are documented, but a hook does not itself wake a sleeping process. T2 injection remains out of scope. | First milestone is an offline drain/ack plus a bounded, native lifecycle adapter. Unsupported or ambiguous wake behavior must fail closed while mail remains durable. |
| IDE / Manager | The IDE/Manager is designed for background orchestration and artifacts, but no public external control or session-wake protocol was found. | N/A. | Treat IDE/Manager as a future integration surface, not as a terminal harness. Never drive its UI by keyboard for Roundtable delivery. |

## Proposed modular adapter boundary

Keep the product core unchanged and add a harness descriptor/adapter layer
around the existing launcher and runtime lease primitives. A future descriptor
should define, at minimum:

- stable harness name and accepted `agents.yaml` aliases;
- executable resolver and version/fingerprint probe;
- launch argv construction and canonical project anchoring;
- native session identifier extraction and cwd/project binding proof;
- lifecycle event parser and bounded watcher arm/re-arm/stop behavior;
- whether the harness supports headless execution, resume, or native wake;
- explicit unsupported states and fail-closed diagnostics.

The adapter must not own mail delivery. `rt-say`, `rt-inbox`, `rt-ack`, and the
maildir remain authoritative. A missing or unhealthy Antigravity/Gemini adapter
must leave offline delivery and later manual drain intact.

## Staged execution plan

### Stage 0 — Ocean review gate

Review the target choice and authorize a disposable hands-on validation. Do
not install either tool, edit setup manifests, or advertise support before this
gate.

### Stage 1 — Read-only/runtime discovery

In an isolated project/session, inspect `agy --help`, `agy --version`, and the
documented noninteractive/session commands; do the same for `gemini`. Record
the exact binary path, version, child-process tree, cwd behavior, auth prompt,
session files, resume behavior, stdout/stderr format, and hook payloads. Verify
whether an invocation can be kept alive and whether an external event can
cause a new turn without terminal injection.

### Stage 2 — Fake-harness contract spike

Build tests against a fake executable and fake hook payloads first. Prove
resolver rejection for missing/non-executable/cmux-wrapper paths, exact lease
environment propagation, canonical cwd anchoring, native-session mismatch
failure, and no duplicate watcher ownership. This stage should introduce no
vendor-specific support claim.

### Stage 3 — Experimental Gemini adapter (optional)

If Stage 1 confirms the documented behavior, use Gemini CLI as the first
process-based adapter candidate because its headless JSON, resume, and
`SessionStart`/`SessionEnd` contracts are explicit. Keep it experimental until
the real credentialed flow passes:

`rt-say -> native wake/turn -> act -> rt-ack -> new/ empty`.

Do not pretend that `SessionStart` alone wakes a parked process; test the
arming/recovery path and preserve durable mail on interruption or auth failure.

### Stage 4 — Experimental Antigravity CLI adapter (optional)

Validate `agy` independently. Confirm exact launch flags, headless mode,
session resume, hook event ordering, `conversationId` stability, process
ownership, and any supported native wake path. If the only reliable route is
interactive UI control or an undocumented private IPC endpoint, stop and leave
Antigravity CLI unsupported.

### Stage 5 — Promotion gate

Only after two sequential real generations on a clean project/account should
the adapter be considered for the compatibility table. Required evidence:

- install/detect/launch is idempotent and does not copy credentials;
- durable offline delivery works with the harness absent;
- native session binding is exact and fenced by project/session/lease;
- send-to-wake-to-drain/ack works twice, including mail arriving during a turn;
- interruption, auth failure, unsupported hook behavior, and stale lease all
  fail closed without deleting mail;
- focused tests, full suite, compile checks, public-safety scan, and a real
  artifact-path smoke all pass.

## Test plan

1. Resolver: explicit override, managed/user PATH ordering, missing binary,
   non-executable path, cmux wrapper rejection, and version capture.
2. Launcher: canonical project cwd, argv passthrough, exact `RT_*` lease
   variables, inherited-seat scrubbing, and no implicit installation.
3. Native identity: hook payload parsing, UUID/cwd correlation, resume,
   process fingerprint, stale lease, and fail-closed mismatch cases.
4. Lifecycle: startup/resume/clear or their Antigravity equivalent, bounded
   arm/re-arm, stop cleanup, concurrent generation isolation, and no watcher
   self-daemonization from a model turn.
5. Delivery E2E: offline mail, wake, substantive action before one ack,
   quiet-ack archival without ack-of-ack, late-arriving mail, interruption,
   and logical plus physical `new/` emptiness.
6. Packaging/safety: managed-file idempotence, no credential copying, release
   artifact launch, compile, full tests, and the public-safety scan.

## Unknowns requiring hands-on evidence

- Exact `agy` CLI flags for prompt submission, headless/JSON output, and
  resuming a conversation.
- Whether `agy` has a startup/resume lifecycle event equivalent to Gemini's
  `SessionStart`, or only invocation/tool/stop events.
- Whether `conversationId` is stable across resume/fork and available before
  the first useful turn.
- Whether either CLI exposes a supported external wake mechanism or only
  internal background work.
- Child-process topology, exit semantics, stdout/stderr framing, and whether a
  long-running session can be safely supervised by Roundtable.
- Keyring/account isolation and behavior in noninteractive or SSH/tmux
  sessions.
- Whether Antigravity IDE/Manager has any supported public API; absent that,
  it should remain outside the harness matrix.

## Sources

- [Google Antigravity announcement](https://developers.googleblog.com/en/build-with-google-antigravity-our-new-agentic-development-platform/)
- [Antigravity CLI overview](https://antigravity.google/docs/cli-overview)
- [Antigravity CLI installation and auth](https://antigravity.google/docs/cli/install)
- [Antigravity CLI projects](https://antigravity.google/docs/cli/projects)
- [Antigravity CLI hooks](https://antigravity.google/docs/hooks)
- [Antigravity CLI background tasks and subagents](https://antigravity.google/docs/cli/subagents)
- [Gemini CLI headless mode](https://geminicli.com/docs/cli/headless/)
- [Gemini CLI session management](https://geminicli.com/docs/cli/session-management/)
- [Gemini CLI hooks reference](https://geminicli.com/docs/hooks/reference/)
- [Gemini CLI deployment](https://google-gemini.github.io/gemini-cli/docs/get-started/deployment.html)
- [Gemini CLI source repository](https://github.com/google-gemini/gemini-cli)
