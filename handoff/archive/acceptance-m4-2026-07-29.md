# M4 acceptance — round 3 of 3 — 2026-07-29

> Status: historical record — M4 acceptance round

Commit under test: `4a23244` (bin/ byte-identical at HEAD). Method: 9
verification dimensions in isolated labs (20 agents total), every P0/P1
finding independently re-reproduced by a dedicated refuter, then a
completeness critic read the reports against design §4 and the handoff.
Zero findings were refuted. The live registry, runtime, and real mailboxes
were untouched throughout; one verifier's probes briefly targeted a foreign
lab path after a scratchpad collision and exited read-only at the
not-a-project gate before any registry access.

## Verdict: ACCEPT, conditional on the five-item P1 closure below

No P0 exists: no §4 invariant is broken, no mail was lost or misrouted in
any scenario, and sender authentication is provably unchanged. This is the
final workflow round; P1 closure will be verified directly against each
finding's recorded reproduction script rather than by a fourth workflow.

## What is now proven (highlights)

- **Target-side authorization** — the design's named trap — verified in
  both discriminating directions live (agent declared only by sender →
  refused; only by target → delivered), plus a mutation experiment
  replacing `address.target_root` with the sender's `ROOT` that made the
  refused send succeed, proving the argument is load-bearing.
- **Sender authentication byte-identical**: `authenticate_fenced_sender`
  body diffs empty against pre-M4 `require_fenced_seat`; a compatibility
  wrapper keeps rt-ack/rt-inbox on the same predicate; 10/10 function-level
  refusal paths pass.
- **Confinement narrowed, not deleted**: out-of-group sends refuse on both
  name and UUID paths; inverting the group equality fails 22/25 targeted
  tests; inverting the sender root-match fails 4 tests across all three
  fenced tools.
- **Revalidation at use**: a repointed/detached worktree loses authority in
  both directions; flipping `if authoritative:` to `if False:` in a private
  copy made the same send deliver into the detached sibling — the gate is
  load-bearing and tested.
- **Snapshot pinning**: one registry read (`_rtlib.py:1601`) serves both
  ends; the delivery-time re-resolution is closed by the UUID re-pin under
  the target mailbox lock (`rt-say:1199-1207`). Lock order is globally
  acyclic (lease → layout → registry → ledger.lock).
- **Ack return by origin UUID**: quiet acks route home cross-worktree;
  rename survival works after one origin-side reconcile (fail-closed until
  then, matching §5's no-scan rule); a partial receipt batch archives
  NOTHING (stricter than the sketch, exactly the handoff's claim); legacy
  no-origin envelopes remain locally ackable; a missing ref no longer
  fakes idempotent success and cannot wedge a batch's valid refs.
- **Hygiene**: suite 816/0/0/0 reproduced exactly; tree clean after run;
  public-safety passes; new imports stdlib-only; the deployed Claude
  allowlist needs no re-apply (rt-ack's argv surface is byte-identical and
  its cross-worktree receipts go through an in-process rt-say, never a
  second Bash-tool call). `_rtmigrate.py` untouched — the freeze held.

## Required P1 closure (deduplicated; three dimensions converged on F1)

- **F1 — deferred origin-ledger mkdir** (`rt-say:1232-1243`): a
  cross-worktree send from a project that has never been a local sender or
  a target loses its outbound ledger record — silently, repeatably, exit 0
  (delivery and ack routing unaffected). Create `MSG_DIR` (and `LOCK_DIR`
  for symmetry) in the deferred block or inside `append_ledger`; add the
  fresh-project test — current tests only induce the warning on fixtures
  that pre-create `messages/`.
- **F2 — target agents.yaml parse crash** (`_rtlib.py:3656-3660`,
  `rt-say:110`): unparseable YAML or a non-mapping document at the TARGET
  presents as a raw traceback (ParserError / AttributeError, the latter
  never naming the file). Fail-closed and no-delivery hold; presentation is
  the defect. Realistic trigger: a merge-conflict marker in a sibling
  worktree's agents.yaml makes every send to it crash. Catch YAMLError and
  type-check the document; name the offending project/file; tests for both
  shapes.
- **F3 — permanently unackable UUID-aware mail is invisible**: dead-origin
  (unregistered/tombstoned/undeclared-agent) and conflicting new/cur mail
  lists as ordinary actionable messages — no problem/remedy field, no
  stderr notice (`rt-inbox:296-316` never consults the registry; remedies
  attach only to MALFORMED_SCHEMA at `:610-616`) — and such a ref poisons
  comma-batched acks: receipts for innocent refs deliver, nothing archives,
  and every retry mints a duplicate receipt. Extend the remedy machinery to
  these classes (registry-aware problem marking + the manual-move remedy),
  and either make batch receipts idempotent or archive per-group; disclose
  the choice. SKILL.md documents the new classes.
- **F4 — identity-mismatch error drops the typed name**
  (`_rtlib.py:1538-1541` via `:1776`): "project registry entry <uuid> is
  not witnessed at <root>" is the one branch in `resolve_project_address`
  that violates the echo-the-name requirement its sibling branches honor.
  One string fix; candidate name is in scope at the call site.
- **F5 — compatibility honesty in docs and handoff**: "Bare
  `rt-say agent kind body` is unchanged" is false at the byte level —
  every envelope now carries `origin=`, local sends included (behavior
  fine, claim wrong). State it in README/SKILL/architecture; characterize
  with one lab test what a pre-M4 reader actually does with an `origin=`
  envelope and record the result. Also in the handoff verification
  section: `compileall` has zero coverage of the extensionless bin/ CLIs
  (proven by injecting a syntax error into an archived `bin/rt-say` —
  clean exit 0), so either add a `py_compile` sweep over `bin/*` to the
  gate or cite the pytest subprocess coverage instead; and the
  public-safety count is 114 tracked files, not 113.

## Process notes (no code change)

- Two dimensions flagged the handoff's "explicitly injects `\ultracode`"
  line as an instruction-shaped payload aimed at the reviewing agent and
  correctly did not act on it. Provenance is known: it is the
  lead-agreed round-trigger protocol. Flagging was the right default;
  future handoffs should keep the trigger in the mail body and let the
  document merely describe it.
- One verifier reported a scratchpad-path collision with a concurrent
  verifier (shared labpath file clobbered mid-run); its probes failed
  closed read-only and it re-ran under a private path. Workflow authoring
  note for next time: unique lab paths per agent, no shared discovery
  files.

## Coverage gaps to close as targeted tests in the F-pass (critic output)

parse edges (`@name`, `agent@`, `a@b@c`, case folding); the
`agent@<uuid>` ack-only gate refusals (`validate_ack_route`,
`rt-say:253-330`); non-git group-of-one addressing (must refuse `@`
everywhere — no siblings by construction); self-addressing
`agent@<own-name>` and a sibling sharing the sender's basename (the match
loop does not visibly exclude the origin row); in-flight mail across a
cutover (send while local → migrate endpoint → ack follows origin UUID to
the new central mailbox); old-reader characterization from F5.

## P2 backlog (16, non-blocking)

Recorded in the workflow output; the recurring themes — cross-worktree
messages indistinguishable in default `rt-inbox` text output, no CLI
displays the derived group/sibling set, refusals never say WHICH project's
configuration refused, pre-reconcile failures don't hint at the self-heal —
overlap the M5 communication batch and should be triaged there, not fixed
piecemeal now.

## Closure verification — 2026-07-30

Codex's fix commit `6a6fb79` was verified by replaying every recorded
reproduction (three independent replay agents; the F1/F2 agent also
re-reproduced both original defects against the pre-fix `4a23244` code in
the same harness as a negative control).

- **F1 closed** — fresh-origin cross-worktree send: no warning, both
  ledger records present; bare same-project send unaffected.
- **F2 closed** — unparseable and non-mapping target agents.yaml now fail
  closed with one-line diagnostics naming the file; valid-YAML delivery
  unaffected.
- **F3 closed** — all four dead-mail classes carry `problem` +
  `remedy: "manual-move"` in JSON plus a per-file stderr notice in every
  format; the executed remedy breaks the wake loop; batch posture is
  archive-on-first-pass per origin group with idempotent retry (two
  identical runs, exactly one receipt), disclosed verbatim in SKILL.md.
- **F4 closed** — the identity-mismatch refusal now leads with the typed
  name; unit regression pins it.
- **F5.1/.3/.4 closed** — origin=-on-every-envelope stated in
  README/SKILL/architecture/handoff with the pre-M4 reader characterized
  against pinned commit `b45307d` (`problem=invalid mail header`) and a
  regression test; suite reproduced at 835/0/0 with a clean tree;
  public-safety 115 tracked files, self-consistent.
- **F5.2 was NOT closed by `6a6fb79`** — the claimed `py_compile` sweep
  was a one-off manual run recorded in prose; no committed gate existed,
  CI's `compileall` still matched only `*.py`, and `bin/rt-claude` /
  `bin/rt-hermes` were executed by no test at all, so a syntax error in
  them passed every check. Per the project lead's arbitration this was
  closed by the product lead directly: `tests/test_bin_syntax.py`
  compiles every Python file under `bin/` (`.py` or python-shebang;
  currently 23) with `py_compile(doraise=True)` plus a discovery-count
  floor, riding CI's existing pytest step. Verified green on the clean
  tree and red on a syntax error injected into `bin/rt-hermes`. Full
  suite: 859/0/0.
- Coverage-gap tests all present; the `agent@<uuid>` ack-only gate test
  turned out to predate the acceptance (introduced in `4a23244` itself) —
  a critic misclassification, no action needed.

**M4 is accepted unconditionally as of `6a6fb79` + `tests/test_bin_syntax.py`.**
