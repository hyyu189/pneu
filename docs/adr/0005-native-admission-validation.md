# ADR 0005 — validate Claude Code and Codex native admission first

- Status: accepted scope, 2026-09-18; native capability acceptance pending.
- Supersedes ADR 0003's integration sequence and fixed interface ranking,
  not its native-session or thin-control-plane constraints.

## Context

After the occupancy change, the next bounded increment is a Claude Code
and Codex native-interface spike. A plugin, SDK, or socket can be present
without reaching the root session the human is using. Checking idle before
sending also does not prove that mail cannot enter a newly started human
turn. These questions precede adapter implementation and Phase 2 changes.

## Decision

Prefer the maintained native interface whose actual session and admission
semantics satisfy the requirement. A native plugin can package tools and a
connector; MCP tools alone do not establish an unsolicited receive path.
There is no blanket socket-over-plugin ranking.

For this spike, ordinary mail must wait while the human turn is busy,
waiting for approval, or compacting. A native tool boundary is not proof
that the human task has finished. Status observation followed by input is
insufficient unless the native runtime serializes the condition and input.
Absent that contract, retain pending mail and report automatic admission
as unavailable. Any weaker policy needs a separate product decision.

Distinguish three operations:

- **Live attach:** reach the existing root and its existing runtime owner.
- **Reconnect:** restore a connection to that owner and reconcile what
  happened while disconnected; a new connection is not a new session.
- **Cold resume:** the native runtime restores stored history after the
  previous owner has ended. Equal session IDs alone do not prove live attach.

Harnesses own their runtime, tools, approval flow, and conversations. This
work does not restore a historical daemon, change global launch routing,
bypass writer locks, or use a second headless session as a visible-seat
substitute. Existing shipped service machinery is not authorization to
activate it. Native use must survive connector failure.

Keep the maildir, lease, fencing, and recovery contracts. Publication,
notification, admission, model visibility, and application acknowledgement
are separate facts. A lost admission response is an uncertain outcome,
not permission to repeat a task. Native owner generation remains unknown
when the interface does not supply it; a PID, socket connection, or pneu
lease revision does not manufacture that identity.

## Acceptance and follow-up

The spike delivers reproducible minimal probes, regression coverage,
an evidence-graded capability matrix, interface gaps, and the next smallest
implementation recommendation. Raw logs and session identifiers stay local.
Source inspection, isolated real-binary experiments, and real visible-root
tests are separate evidence classes. Unsupported or untested paths remain
explicitly unverified; this ADR does not certify a harness.

The evidence lives in [native admission](../native-admission.md), linked
from the architecture and compatibility documents. The follow-up adapter
and daily two-harness workflow gate require separate implementation and
real-session acceptance. Broader Phase 2 decomposition, a common adapter
framework, and additional harnesses are outside this increment.
