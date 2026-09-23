# Explicit native inbox, status, send and ACK

This Phase 1a interface provides `rt-native inbox`, `status`, `send` and `ack`
for explicit calls from a Claude Code or Codex root. All return JSON. Inbox
reads reuse `rt-inbox` and retain message IDs, bodies and pending state.
They do not send, execute mail, acknowledge, archive quiet receipts, renew
leases, or start a receive loop. Native busy state is always `unknown`;
an active lease is not an observation of native idle/busy state.

## Opt in to one project

The project must already have its pneu anchor, registry entry and exactly
one configured seat for the selected harness. Codex needs no hooks. In its
native root shell tool, explicitly run `rt-native bind` once before querying.
It takes no session, agent or project arguments.
The current directory selects the project anchor for this explicit binding;
environment project overrides and fallback routing cannot select another
workspace. The native root identity, not that directory, authorizes reads.

For Claude, configuration generation is read-only:

```sh
rt-native hooks claude --project "$PWD"
```

The output contains absolute commands for the current installation and
project. For a disposable Claude test, save its output in an untracked
project-local file and pass it with `claude --settings FILE
--setting-sources project,local`; this excludes global historical watcher
hooks from the test. Preserve any existing configuration and accept native
workspace trust and tool approval prompts. Do not install these hooks globally. No daemon,
launcher-routing change, Channel, Stop hook, or MCP server is required.

Start the native harness in that exact project. Claude SessionStart and
Codex's explicit bind associate the root without replacing an active seat.
In a native shell tool, run
one literal command at a time:

```sh
rt-native status
rt-native inbox
```

The absolute installed script path also works, optionally preceded by the
same Python executable used to generate the hooks. Claude's hook recognizes
these query forms and the literal send/ACK forms below. Shell chains,
expansions and internal hook/ticket calls are not model tools.
Native approval policy still applies: the hook never
returns a permission allow decision. Remove the added hook groups/file to
revoke project integration; an existing lease becomes stale when its owner
exits. The generated configuration is not automatically installed or removed.

## Identity and invalidation

The association is the existing seat capability's `nativeQuery` field. It
records the native root, harness and observed process generation under the
existing lease session/revision. It is not another lease or a native runtime
generation. The source is `session-start` for Claude and `native-shell` for
Codex. Only the explicit binding operation mutates it; queries never adopt
an unbound root. A legacy Codex wake binding grants no query authority.
If existing capability or lease wake metadata also names a native root,
it must agree with this binding; conflicting identity facts deny access.

Codex supplies `CODEX_THREAD_ID` for the specific calling thread and
`CODEX_SESSION_ID` for its root on each shell invocation. Both are required
and must agree. This rejects native subagents even when they share the
backend, cwd and root session environment. The current native owner must be
in the process lineage and match the capability and lease.
Codex hook stdin is not used as identity: its pipe transport could not be
distinguished reliably from an ordinary shell invocation.

Claude SessionStart and PreToolUse use native hook JSON, not a session
parameter supplied to the query. On the tested macOS transport, both hook
output sockets must have the native Claude owner as their kernel peer PID;
ordinary shell stdin cannot impersonate that dispatch. Other transports
fail as unsupported. PreToolUse rejects `agent_id` children and wrong roots,
then rewrites only the requested operation with a random, one-use ticket. The
ticket expires after 60 seconds and is bound to operation, project, root,
owner and lease generation, plus the complete send target/kind/body or ACK
refs/note. A permission wait beyond that interval requires a fresh call.
Ticket creation/consumption changes only private invocation
metadata, never mail. Claude does not expose a dependable tool-use ID in
the Bash environment; the originating hook ID is recorded, not treated as
an independently verified shell credential.

Every present inherited `RT_*` identity field must agree, including partial
fences. A host binding lock prevents one root from concurrently binding two
projects; conflicting active mappings are rejected, without creating a new
identity index. Queries retain this lock and the existing claim guard while reading and revalidate
the capability before returning. Owner exit, lease replacement, project or
root mismatch, and missing bindings deny access. Errors are explicit
`unbound`, `unsupported`, `conflict`, or `stale`, with no message list; none
is rendered as an empty inbox. The legacy `--fenced` CLI now checks a present
Codex native identity even when all RT fields are populated, but its
environment-only compatibility path is not proof of a native root.

This is local session routing and isolation for the maintained native tool
path, not an OS sandbox between programs sharing a user account. Arbitrary
local code can already read mail or modify runtime files as that user.

## Explicit send and acknowledgement

From the bound Claude root, for example:

```sh
rt-native send codex request 'Review the arithmetic in: 2 + 2 = 5.'
```

The receiving Codex root explicitly queries `rt-native inbox`, performs the
requested work, then sends a reply containing the original message ID:

```sh
rt-native send claude reply 'For REQUEST_ID: the corrected equation is 2 + 2 = 4.'
rt-native ack REQUEST_ID 'Reviewed the arithmetic and sent the correction.'
```

Replace `REQUEST_ID` with the exact ID returned by inbox; it is not a session
credential. The originating root queries its inbox, verifies the result, and
explicitly acknowledges the reply's exact ID. An ACK can take a comma-separated
list of unique original IDs and an optional single quoted note. ACK-of-ACK is
rejected. Quiet receipts remain in maildir and are hidden by inbox; an empty
inbox alone does not prove an ACK round trip.

Only the other configured Claude/Codex seat in the same project is accepted.
No sender, root, session, cross-project address, reply alarm or wake option is
accepted. Both mutations retain the native binding and seat claim guards
through the mailbox operation. The verified native authorization calls shared
maildir publication/archive helpers directly; it does not require or create a
legacy Codex wake binding. There is no subprocess shell or keyboard delivery.

Pass the body as one literal shell argument. Single-quoted multiline text and
ordinary POSIX quote escaping are supported; quote shell metacharacters as
data. The Claude hook rejects command substitution, redirects, globs and shell
chains before rewriting, and safely quotes the complete decoded parameters.
It preserves native approval rather than granting permission. Option-looking
body text such as `--help` remains message data.

## Commit and recovery results

Mutations use schema `roundtable.native-mutation.v1`. `binding`, `lease` and
`native_busy` describe authorization separately from `mutation.status`:

| Status | Meaning and next action |
| --- | --- |
| `committed` | Send publication is observed, or all requested originals are archived after ACK. Check `error`/`response_error` for later failures. This does not prove business processing. |
| `not_committed` | Validation refused the operation before publication. Correct the reported identity, route or parameter error. |
| `partial` | ACK has confirmed receipt/archive progress but some refs remain incomplete. Reconcile the returned refs/receipt IDs with durable mail, then explicitly retry ACK to finish archival. |
| `unknown` | A publication was attempted without sufficient readable commit evidence. Inspect the exact returned message/receipt ID in durable mail before deciding what to do. |

Send returns its known `message_id`; ACK returns `refs`, `receipts` and
`archived`. A failure writing stdout after a known result attempts to report
that same result on stderr. If the entire response is lost, inspect durable
mail rather than repeating send: every new send creates a new ID. A lost
response is not an idempotency key and this interface does not promise
exactly-once delivery.

Receipt publication and original archival are separate commits. On an explicit
ACK retry, exact quiet receipt envelope/refs evidence in the sender's `new/`
or `cur/` permits completing the original archive without another receipt.
Conflicting receipt evidence fails closed. Already archived originals do not
cause a new receipt, including when an external workflow has removed the old
receipt. No second mailbox or recovery journal is introduced; the maildir is
the commit witness. Inbox/status never perform this recovery or clean receipts.

## Validation boundary

Contract tests cover both harnesses, root/project/fence conflicts, missing
and legacy bindings, subagents/forks, dead/replaced owners, malformed input,
ticket replay/expiry, guarded reads and unchanged mail/runtime snapshots.
Mutation contracts also cover full parameter substitution, complex bodies,
guarded writes, uncertain publication, response loss, partial ACK recovery
and repeated ACK without duplicate receipts.
Isolated native-binary experiments and visible native session results are
reported separately in [compatibility](compatibility.md#explicit-native-queries).
Existing-session attach, native approval/compaction continuity, automatic
receive and native busy observation remain unverified. This adds no Phase 2
framework and does not promote general harness compatibility.
