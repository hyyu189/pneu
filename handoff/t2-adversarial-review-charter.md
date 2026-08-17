# Charter — adversarial review of the T2 branch

You are the adversary. **Attack, do not summarize.** A restatement of what the
branch does is worth nothing here; a single reproducible hole is worth the
whole review. If you find nothing on a surface, say so in one line and move on
— do not pad.

Seat: `codex@roundtable-product-worktree/t2-tests`, model `gpt-5.6-terra`,
high reasoning effort. Ordered by Ocean via `claude@roundtable-product`.

## Target

Branch `wt/t2-tests`, two commits on top of `52fc483`:

- `809e06a` — D15: journey tier + pytest-xdist parallel safety
- `511e90d` — architecture-review Tier 0: AST locators, `tests/_kit`, derived
  fitness list

```bash
git log --oneline 52fc483..HEAD
git diff 52fc483..HEAD --stat
git diff 52fc483..HEAD          # the whole change
```

Supporting claims are in `handoff/d15a-xdist-verdict.md`,
`handoff/d15b-journey-tier.md`, and `handoff/t2-tier0-1.4.md`. **Treat every
sentence in those documents as a claim under attack, not as background.**

Environment: `mamba run -n general python -m pytest ...`. The suite is 1114
passed / 1 skipped serially (~286 s) and under `-n auto` (~73 s).

## Attack surfaces

Ocean named five. Each has a specific falsifiable claim attached. Go for the
claim.

### 1. The zero-serial-markers verdict

`handoff/d15a-xdist-verdict.md` claims no test needs `xdist_group`,
`--dist loadgroup`, or a serial marker, on the evidence of five green parallel
runs and a per-lead analysis.

**Construct a collision the evidence missed.** Candidate vectors the report
did *not* rule out by construction, only by observation:

- shared state outside `tmp_path`: real `$HOME`, the process-wide
  `_TEST_ROOT` in `tests/conftest.py`, `~/.codex`, LaunchAgents, `/tmp`
- the project registry (`RT_PROJECTS_FILE`) and central mail
- fixed ports, fixed socket paths, fixed launchd labels, process-name kills
- `os.chdir` / cwd mutation racing another worker in the same process tree
- `os.environ` mutation that escapes a `monkeypatch` scope
- tests that spawn real subprocesses inheriting a parent env mutated by a
  concurrently-running test
- pytest's own caches, `PYTHONDONTWRITEBYTECODE`, `__pycache__` races on the
  shared checkout
- the new `tests/test_journey_mutation.py` and `tests/test_kit_locators.py`,
  which copy `bin/`, `templates/`, `skills/` per case — I/O and fd pressure at
  10 workers

A green run is not a proof. If you find a vector, prove it: a command, a seed,
a repeat count, an observed failure. `-p no:randomly` was used for the
baseline; try *without* it, try `-n 16`, try repeated runs under load. If you
can only argue a vector is *theoretically* live, say that explicitly and rank
it — do not dress it as a reproduction.

### 2. The byte-identical `_kit` consolidation

`handoff/t2-tier0-1.4.md` claims all 15 rendered `agents.yaml` documents are
byte-identical to the definitions they replaced, and that call sites are
untouched.

**Verify the claim independently — do not trust my scratch script, which is
not in the repo.** Reconstruct each replaced definition from
`git show 52fc483:tests/<file>` and diff its output against
`tests/_kit/projects.py` for the inputs each module actually passes.

Then hunt for what byte-identical output does *not* cover:

- **ordering**: `write_project` now resolves `root.resolve()` and writes in a
  different statement order than some originals; does any test depend on a
  directory existing before/after another step, or on `mkdir` failing?
- **strictness**: originals used `state.mkdir(parents=True)` with no
  `exist_ok`. Did any call site rely on that raising? Did I change which
  callers pre-create `.roundtable`?
- **return values**: `test_optional_cmux_adapter.write_project` returned the
  *state dir*, others return the resolved root, one returned an unresolved
  path. Check each wrapper preserved its own contract.
- **encoding**: some originals wrote without `encoding=`, the kit always
  passes `encoding="utf-8"`. Any locale-dependent difference?
- **laziness / shared mutable defaults**: `Seat` is frozen, but
  `TOOLING_SEATS` is now a module-level tuple shared across tests. Can any
  test mutate shared fixture state that used to be rebuilt per call?
- the 8 fixtures consolidated *beyond* the 13 named in the brief
  (`write_registered_project`, `_write_project`, `_write_lab_project`,
  `_registered_project`, `_registered_grok_project`, `_grok_launch_fixture`,
  and two inline documents) — these got less scrutiny than the 13.

### 3. The AST locators

`tests/_kit/source.py` claims to locate exactly one definition or call, and to
fail loudly otherwise.

**Feed it adversarial input.** Decorated functions (is the decorator inside or
outside the segment, and does the test that consumes it care?), nested
`def`/`class`, same-name methods on different classes, a module-level name
shadowing a method name, `async def`, conditional definitions under `if
TYPE_CHECKING`, a call inside a comprehension or lambda, a call to an
attribute with the same trailing name as a bare function
(`x.load_validated_lease(...)` vs `load_validated_lease(...)` — `_callee_name`
returns `attr` for both; is that a real conflation and can it bite
`test_grok_mutation.py`?), a file that fails to parse, and a target whose
source segment is not unique in the file so `source.count(needle) == 1` breaks
anyway.

Also attack the *replacement's* honesty: does
`test_grok_seat_path_is_pinned_away_from_internal_acp_supervisor` still fail
if the ACP supervisor reference moves into a module-level helper that
`launch()` calls? If not, the test's claim ("the seat path is pinned away
from the supervisor") is still broader than what it checks — the fix made the
locator correct without making the claim true.

### 4. The consumer-derivation fitness

`tests/_kit/consumers.py` claims to discover every production Python source so
that a tool nobody registered is still checked.

**Make a real tool evade discovery.** `_is_python_source` accepts `*.py` and
extensionless files whose first line is a `#!` containing `python`. So try: a
`#!/usr/bin/env -S python3 -u` form, a shebang with `python` only on line 2, a
`.pyw`/`.pyi`, a wrapper shell script that execs python, a file with a
`.command` or `.tool` extension, a symlink chain into a tree outside
`PRODUCTION_TREES`, a nested package under `integrations/<new>/`, a file
generated at build time and absent from the checkout, and a source that
reaches the maildir through an alias import
(`from _rtlib import locked_project_mailbox_checked as _open`) so the AST
callee name never matches.

Also attack the three exemption ledgers in `tests/test_mailbox_resolver.py`
(`LAYOUT_DEFINING_SOURCES`, `ADVISORY_UNLOCKED_READERS`,
`ALLOWED_NON_LAYOUT_LITERALS`): can an entry be technically "still earned"
while masking a genuinely new violation in the same file? The literal
allowance keys on the rendered `" / ".join(parts)` tail — can two different
constructs render the same tail?

### 5. The 8 journey mutations

`tests/test_journey_mutation.py` claims each mutation is killed by the journey
that names it.

**For each of the 8, find a weaker change that the same journey does not
catch** — i.e. show the journey pins less than its name claims. Specifically:

- `print-skips-the-launchable-seat-gate`: is exit-code 2 the only thing
  asserted, or would a *different* refusal also satisfy it?
- `watcher-claims-the-wake-slot`: I assert `token.watcher_pid == watcher.pid`.
  Is there a mutation that keeps a plausible pid but breaks the invariant?
- `ack-archives-out-of-new` and `ack-returns-a-quiet-receipt`: can one be made
  to pass while the other's guard is broken?
- `watcher-wakes-on-new-mail` kills the test via a 20 s `communicate` timeout
  rather than an assertion. Is that a real kill or a slow-test artifact that
  a faster machine or a changed `WAKE_TIMEOUT_SECONDS` would flip?
- the baseline case `test_private_journey_copy_is_green_before_any_mutation`
  guards the harness — can the harness be broken in a way the baseline still
  passes?

Also: the mutation runner asserts `source.count(mutation.needle) == 1`. Three
of the eight needles are multi-line and indentation-bearing — the exact
fragility Tier 0 fixed elsewhere. Is that inconsistent enough to matter?

## Rules of engagement

- **Read-only on the branch.** Do not edit, commit, or stage anything. Do not
  run `git checkout`/`rebase`/`reset`. Scratch files under `/tmp` are fine.
- Running the suite, subsets, and your own throwaway reproductions is expected.
- Do not touch other worktrees; the sibling tracks T1/T3/T4/T5/T6 are live.
- Rank every finding: **CONFIRMED** (you reproduced it — give the command and
  the output), **PLAUSIBLE** (argued from source, not reproduced — say so),
  or **REFUTED** (you attacked it and it held; one line each).
- A finding needs a concrete failure scenario: inputs or state → wrong
  behaviour. "This could be fragile" without a mechanism is not a finding.
- If one of my handoff documents overstates a result, that is itself a
  finding — I would rather correct the claim than keep it.

## Deliverable

Write `handoff/t2-adversarial-findings-codex.md` in this tree — this is the
one file you may create — then:

```
rt-say claude answer "<one line: N confirmed, M plausible, K refuted; pointer to the file>"
```

I will fix what is real, refute what is not with evidence, and send Ocean the
joint verdict with your findings and my dispositions attached.
