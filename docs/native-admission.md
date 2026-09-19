# Claude Code and Codex native admission

Phase 1a, 2026-09-18. Scope: [ADR 0005](adr/0005-native-admission-validation.md).
This is a reproducible interface characterization, not a supported adapter.
The production maildir, lease, fencing, wake, and recovery code is unchanged.

## Evidence rules and tested surfaces

- **D**: official documentation; describes a contract, not this host's result.
- **S**: pinned official source or distribution; does not prove UI behavior.
- **L**: isolated real binary, synthetic input and local mock model; no real
  credentials or user sessions. Unit-test doubles are labelled **T**, not L.
- **O**: read-only observation of the installed host; no delivery claim.
- **V**: a message observed in the intended visible root, with its native
  identity established. A new visible test root is distinct from attaching
  to the user's already-open root. Label user-reported terminal evidence
  separately from independently captured UI evidence.
- **Unverified**: no sufficient evidence; not a pass.

The installed standalone Codex is `0.155.1`; the Desktop backend is
`0.155.0-alpha.2.6` (App `26.911.61220`, build `9647`). Claude Code is
`2.1.276`. These are dated observations, not minimum-version support rules.
Use the exact resolved executable and record its version on every rerun.

Live attach retains the existing root **and owner**; reconnect restores a
transport to that owner; cold resume reconstructs stored history after the
owner ends. A session ID, cwd match, transcript, or process list alone cannot
establish that the user is looking at the targeted root. Native owner
generation is unknown where not exposed. Revalidate the exact native root,
current owner evidence, and pneu lease fence separately on each operation.

## Capability matrix

| Question | Claude Code | Codex |
| --- | --- | --- |
| Idle receive in a new visible test root | **V, user-reported terminal evidence:** `idle-1` appeared through the Channel and Claude replied. Its `probe_observed` call was recorded later, during the busy-case continuation; no application ACK is claimed. This is the newly launched test root, not attachment to a pre-existing session. | L: isolated threads receive input and complete synthetic turns. A new native visible-root receive test remains **V: unverified**. |
| Reach an already-open visible root | D: peer inbox addresses existing sessions; full external wire contract was not established. Channels require startup opt-in. O: `agents --json` works, but no confirmed pre-existing Claude visible root in this worktree. **V: unverified.** | O: Desktop owns a stdio app-server with no TCP listener or named Unix listener observed. The current CLI thread's inherited identity resolves to a live writer owner, also with no such listener. A supported endpoint to those owners remains unverified. **V: unverified.** Another backend using the same stored ID is not attach. |
| Ordinary mail during busy human work | V/O: `busy-1` was emitted after the same native session reported busy; the user-reported trace shows its event during the sleep task and two probe calls before the final human-task reply. Local instrumentation confirms the calls. **Strict deferral is not established**; native turn/context boundaries remain unverified. D: peer messages can enter between tools. | S/L: after observing idle, a synthetic human turn starts; `turn/start.toolOutput` then joins that same turn and appears in its next model request. Strict deferral fails in both tested binaries. **V: unverified.** |
| Waiting for approval | O: Claude inventory can omit status on background rows; missing state remains unknown. Channel permission relay is a separate capability and is not enabled by the probe. **V: unverified.** | S: active flags distinguish `waitingOnApproval` / `waitingOnUserInput`; these are busy. A mail client must not answer approval requests. **V: unverified.** |
| Compaction | D: native hooks expose compaction lifecycle. External receipt state must survive context replacement. Strict deferral and identity continuity in the visible root are **unverified**. | S: native compaction preserves the thread, but does not add atomic mail admission. Visible-root compact/receive ordering is **unverified**. |
| Idle check versus admission | No documented atomic per-message idle condition was established for peer messaging, Channels, or monitors. Deferral by external polling alone is insufficient. | S/L: the race is reproduced. Core has `start_turn_if_idle`; public `TurnStartParams` does not expose it. `turnTrigger` classifies a start; it is not an admission fence. |
| Live reconnect versus cold resume | D: SDK resume restores history; it is not injection into the existing interactive process. Native channel restart/re-registration against the same visible root is **unverified**. | L: two clients share one backend/thread and reconnect; another backend is refused while the owner lives, even after all its clients disconnect. Only after owner exit does cold resume succeed with the same turn count. Desktop/CLI continuity is **V: unverified**. |
| Duplicates and response loss | D/T: Channel notification has no processing receipt. The probe distinguishes transport writes, first logical observation, and repeated observation calls. This is not a production application ACK. | L: sender disconnects without reading the admission receipt after the mock model receives input. Replaying the same RPC ID and payload on a new connection creates another turn and a second persisted tool output. Reconcile instead of blindly resubmitting. |
| Plugin / CLI / MCP / receive connector | Plugin packages optional configuration and hooks. CLI/MCP tools call the fenced mail core. A Claude-specific Channel is an optional receive connector with its own admission limitations. | Plugin packages tools/hooks. Generic local MCP supplies tools, not arbitrary idle wake; hosted-app event streaming is a different, restricted surface. App-server access additionally requires the actual owner's endpoint. |

This matrix does **not** claim that mail safely waits merely because a
transport says queued, a tool was not interrupted, or the mailbox persisted.
Until a native admission gate is proven, the safe connector result is
`unsupported`/`deferred` with the existing durable mail left pending.

## Recovery contract

| Failure point | Required handling; scope of the spike |
| --- | --- |
| Before a notification is submitted | Maildir publication remains authoritative. Retry the notification for the same logical message/generation after exact binding and fence validation. |
| Submission may have succeeded, response lost | Record uncertain admission. Reconnect to the same owner where supported; inspect native evidence and the existing application acknowledgement. An absent response is not a rejection. |
| Duplicate notification | Coalesce notification intent by logical message/generation. Never infer exactly-once model execution from JSON-RPC IDs or Channel metadata. |
| Connector or native process restart | Re-discover the native root and current ownership; discard stale transport identity. Revalidate lease/session/revision. If ownership cannot be established, leave mail pending. |
| Message processed, receipt missing | Reconcile the core's committed mail/ack facts before repeating work. The quiet receipt may need repair; do not turn a receipt retry into another business task. |
| Compaction or fork | Compaction is context replacement, not receipt erasure. Fork is a different root and requires explicit binding. Never infer binding from the newest transcript. |

The existing core tests cover publication committed despite ledger failure,
downstream ack failure, ack delivery committed before an archive-admission
failure, duplicate messages, quiet receipts, and stale fences. Those tests
protect the existing core, not native admission. The probes use synthetic
records; their local journal is not a second production mailbox.

In particular, an ack receipt can commit before fresh archive admission
fails. The core reports that partial outcome and retains inbound mail;
retry may produce another quiet receipt. Neither receipt deduplication in
every failure window nor exactly-once business execution is promised.

## Reproduce the probes

Use a dedicated CPython 3.11–3.14 environment with `requirements-dev.txt`
installed. From the repository root:

```sh
.venv/bin/python -m pytest -q tests/test_native_admission_claude.py tests/test_native_admission_codex.py
.venv/bin/python scripts/native_admission/claude_probe.py inspect
.venv/bin/python scripts/native_admission/codex_probe.py
```

`inspect` runs the supported `claude agents --json` query and emits only
allowlisted state categories; paths, names, PIDs, and session IDs are omitted.
An optional `--expected-session-id` narrows the inventory but still does not
certify visible-root identity. Missing and unknown states are not idle.

The Codex probe uses the existing `_rtcodex.codex_bin()` resolver once,
pins the resulting executable, and creates a fresh private temporary home,
workspace, two app-server processes and a loopback mock Responses endpoint.
Its subprocess environment is allowlisted and contains no credentials.
It neither connects to existing app-servers nor uses user threads. Set
`RT_CODEX_BIN` to an explicit installed binary to compare versions. Both
tested versions reproduced these 14 observations:

| Case | Observations checked in the local summary |
| --- | --- |
| Same isolated owner | Second client resumes the same thread; both receive completion for the same turn. |
| Deterministic admission race | Idle observed; human turn becomes active; mail returns that human turn ID; mail reaches a later model request before the human turn completes. |
| Reconnect | Reconnected client resumes on the still-running original backend. |
| Lost receipt and duplicate | Submitted turn survives disconnect; exact request replay creates a distinct turn; mail output is persisted twice. |
| Writer lifetime | Other backend refused; refusal persists with zero clients; owner exit allows cold resume; restored turn count matches. |

The two binaries each produced `all_observations_reproduced=true` and
`all_probe_servers_exited=true`. This means the characterized behavior
reproduced, **not** that the behavior satisfies pneu's admission requirement.
Approval and compaction cases remain unverified by this lab. No real model
executed tasks. Raw RPC/model logs stay in the private directory printed on
stderr; stdout contains a path-free summary. Do not commit the raw files.

The Claude probe is a small stdio MCP Channel server and an explicit
synthetic-event driver. Its T tests cover negotiation, duplicate submission,
ambiguous writes, observation response loss, restart and privacy. They do
not run Claude. The server negotiates `2025-06-18`: the documented Channel
restriction for MCP `2026-07-28` must not be mistaken for a generic MCP rule.
It advertises no permission-relay capability and never answers approvals.

### Optional visible Claude root experiment

This starts a **new disposable visible root**. It does not attach to an
already-open root and does not complete that acceptance gate. Choose a fresh
temporary directory, then initialize the probe in the project environment:

```sh
.venv/bin/python scripts/native_admission/claude_probe.py init --run-dir /tmp/pneu-claude-visible-test
```

In a separate native terminal, run:

```sh
cd /tmp/pneu-claude-visible-test
claude --strict-mcp-config --mcp-config ./mcp.json --dangerously-load-development-channels server:pneu-probe
```

Accept the native workspace/MCP/development-channel prompts yourself; verify
the Channel is registered and use `/status` to identify the visible root.
This uses normal native authentication; the probe never reads credentials.
No plugin or global settings are installed. From the repository terminal:

```sh
.venv/bin/python scripts/native_admission/claude_probe.py emit --run-dir /tmp/pneu-claude-visible-test --message-id idle-1
.venv/bin/python scripts/native_admission/claude_probe.py report --run-dir /tmp/pneu-claude-visible-test
```

The only payload is a fixed synthetic observation request, not a business
task. `probe_observed` records model observation separately from application
acknowledgement; repeated tool calls are recorded separately from the first
logical observation. Report fields deliberately retain unverified native
admission and root identity; record independent UI evidence alongside them.

Repeat with distinct IDs during busy work, a native approval prompt, and
actual `/compact`; race a human prompt against an idle observation. Capture
the native root, turn/context boundary and arrival ordering. A delayed
`probe_observed` call alone **does not prove** that the model was not already
shown the event: it may have chosen to defer the tool call. Without native
ordering evidence, record unverified, not pass. An injection into a current
human task is a strict-deferral failure even if no tool was interrupted.

`emit --repeat` deliberately resubmits the same ID. Without it, duplicate
enqueue is suppressed. `pending` means unattempted; `writing`/`unknown`
means uncertain; `written` means only a successful stdout write. Restarting
the probe never automatically replays uncertain or written attempts. Native
MCP reconnect and whole-Claude cold resume need separate visible tests;
restarting this test process alone cannot establish either. Close the
disposable Claude window when done; its stdio probe exits with it.

### Visible experiment observation on this host

The user reported opening the disposable native Claude window. The supported
inventory found exactly one interactive root at that test directory, idle.
The probe completed MCP initialization and wrote `idle-1` to stdout. The
user subsequently supplied terminal evidence showing the Channel event and
Claude's reply: "Probe idle-1 received. No action taken, since it didn't ask
for any work." This establishes **V, user-reported idle receipt and response
in the newly launched visible test root**. The agent did not independently
capture the UI; the computer-use tool could not read that terminal app.

The initial local probe journal contained no `probe_observed` call. Model
receipt, a conversational response, an instrumented observation and a pneu
application ACK remain separate facts. In this case, zero observation calls
did not mean the model had not seen the event. A later idle inventory result
also cannot establish that no turn ran between observations.

The same terminal excerpt included a warning that no MCP server was
configured with the selected name, despite the visible event and response.
The user then checked `/mcp`: `pneu-probe` was connected, dynamically
configured, with tools capability and one discovered tool. Connection and
tool discovery are therefore confirmed by user-reported UI evidence;
the later busy case below also exercised the tool. The startup warning's
cause is unresolved; no matching native debug log was available. It does
not establish that the Channel was unavailable, nor does this receipt prove
universal Channel eligibility.

For the busy case, the user asked Claude to run `sleep 15`, then separately
run `printf 'HUMAN_DONE\n'`, then reply `HUMAN_DONE`. A local driver matched
the previously recorded native session ID, PID and exact test cwd, waited
for `status=busy`, and emitted `busy-1`. This was an observed-state test,
not an atomic admission operation. To repeat manually, check the known root
with `inspect --expected-session-id`, then emit while that task is busy.

The user-reported terminal sequence was: first shell command; the Channel
event; a continuation listing two probe calls and one shell command; final
`HUMAN_DONE` plus a narrative about the probe. The local journal confirms
one `probe_observed` call for `busy-1`, then one for the earlier `idle-1`,
on the same probe-process generation. Both return `application_ack=false`.
This establishes busy-event receipt and actual tool invocation, not
exactly-once task processing or a pneu acknowledgement.

Claude's narrative said it waited until the shell task finished before
recording the probes. That self-report is not native scheduling evidence:
the visible trace lists probe handling before the requested final human-task
reply. A shell command completing without interruption does not establish
ordinary-mail isolation from the ongoing human task. This case cannot pass
the strict-deferral gate. Exact native turn IDs, input-context timing and
ordering against a second queued human prompt remain unverified.

Already-open-root attach, approval/compaction deferral, the idle-to-busy
race and native reconnect remain unverified for Claude. These two visible
receipts do not promote the integration or establish application processing.

## Responsibilities and smallest next increment

Keep three boundaries distinct:

1. **Plugin packaging** installs reversible, explicitly enabled configuration,
   lifecycle observation, and tool discovery. It does not own a replacement
   harness runtime or grant execution authority.
2. **CLI / optional MCP tools** expose send, inbox, acknowledge, and status
   through the existing core. Bind each call to a trusted native root and
   current lease fence; do not let a free-form sender or one process-wide
   seat stand in for multiple calling sessions.
3. **Native receive connector** observes the actual owner and requests only
   the admission operation proven safe for that surface. Ordinary peer mail
   is external data; it never becomes human permission or answers an approval.

Recommended next implementation, subject to a separate task: an explicitly
invoked, in-session inbox/status tool using the existing fenced CLI. This
can verify trusted same-root tool binding without pretending to solve idle
wake. Keep automatic ordinary-mail push unavailable until the gaps below
are addressed. Do not use Stop-hook polling to keep a task alive indefinitely.

That separately scoped increment is now implemented as
[explicit native inbox/status and send/ACK](native-inbox.md). These explicit
operations do not change the automatic admission results above.

The narrow upstream needs are:

- Codex: expose Core's atomic idle-only admission with explicit accepted/busy
  outcome, preserving external tool-output authority; provide a supported
  endpoint/discovery contract for the existing visible owner's backend.
- Claude: establish a supported external peer schema or in-session channel
  contract that atomically defers ordinary mail through busy/approval/compact
  and defines ordering against human input. Startup opt-in is distinct from
  attaching to an already-open session.
- Both: define reconciliation evidence for a lost admission response and
  native owner replacement. A connection ID is insufficient.

No full adapter, shared adapter framework, global launch change, or additional
harness integration follows automatically from this recommendation.

## Sources

Codex source below is pinned to standalone `0.155.1`, commit
`be2951ea34f0d295ed0becf97079f92fa5f6950e`. The checked turn schema,
handler, Core admission, status schema and MCP gate are byte-identical to
Desktop alpha commit `bf6f0a4ec97919bf697cdc532e7b8af4ec482fc6`:

- [Public turn schema](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L156),
  [start-or-steer handler](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/app-server/src/request_processors/turn_processor.rs#L645),
  [Core idle-only operation](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/core/src/codex_thread.rs#L330).
- [Tool-output authority](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/app-server/src/request_processors/turn_processor.rs#L591),
  [admission precedes persistence/sampling](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/core/src/session/turn_input.rs#L1),
  [writer lock](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/rollout/src/writer_lock.rs#L43).
- [Thread status](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1645)
  and [status computation](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/app-server/src/thread_status.rs#L437):
  active with empty flags is still active; there is no separate compact flag.
- [MCP event-stream gate](https://github.com/openai/codex/blob/be2951ea34f0d295ed0becf97079f92fa5f6950e/codex-rs/app-server/src/request_processors/mcp_event_stream.rs#L47):
  hosted apps only; forwarding an event to a frontend is not turn admission.

Official documentation rechecked for this spike:

- [Codex App Server](https://learn.chatgpt.com/docs/app-server): wire lifecycle
  and external tool-output input.
- [Claude cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)
  and [agent view](https://code.claude.com/docs/en/agent-view): native peer
  semantics and supported read-only inventory.
- [Claude Channels reference](https://code.claude.com/docs/en/channels-reference)
  and [Channels](https://code.claude.com/docs/en/channels): experimental MCP
  extension, startup selection, provider/policy gates and protocol negotiation.
- [Claude sessions](https://code.claude.com/docs/en/sessions): persistent
  resume versus a live process, including concurrent-resume limitations.
