# Explicit native inbox and status

This Phase 1a increment adds `rt-native inbox` and `rt-native status` for an
explicit query from a Claude Code or Codex root. Both return JSON. Inbox
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
only these query forms; flags, shell chains and internal hook/ticket calls
are not model tools. Native approval policy still applies: the hook never
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
then rewrites only the requested query with a random, one-use ticket. The
ticket expires after 60 seconds and is bound to operation, project, root,
owner and lease generation. A permission wait beyond that interval requires
a fresh query. Ticket creation/consumption changes only private invocation
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

## Validation boundary

Contract tests cover both harnesses, root/project/fence conflicts, missing
and legacy bindings, subagents/forks, dead/replaced owners, malformed input,
ticket replay/expiry, guarded reads and unchanged mail/runtime snapshots.
Isolated native-binary experiments and visible native session results are
reported separately in [compatibility](compatibility.md#explicit-native-queries).
Existing-session attach, native approval/compaction continuity, automatic
receive and native busy observation remain unverified. This adds no Phase 2
framework and does not promote general harness compatibility.
