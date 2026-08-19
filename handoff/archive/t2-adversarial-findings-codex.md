# T2 adversarial findings — Codex

> Status: historical record — T2 adversarial raw findings

Scope: read-only attack of `809e06a` and `511e90d` against `52fc483`.
I found **4 CONFIRMED**, **3 PLAUSIBLE**, and **3 REFUTED** items.  “Confirmed”
means the command/input below produced the stated result; it does not imply a
root cause not established by that result.

## 1. Zero-serial-markers verdict

### CONFIRMED — high-worker stress is not safe under observed load

Command:

```bash
mamba run -n general python -m pytest -q -n 16 -p no:randomly
```

Observed result: **20 failed, 1079 passed, 1 skipped** before I interrupted
the remaining long-running subprocesses; pytest reported `KeyboardInterrupt`
after **1160.03 s**.  Failures include lock-inode replacement being accepted
(`tests/test_layout_locks.py:814,864`), cutover/ack subprocess timeouts,
reply-watcher timing, `rt-say` lock acquisition, and the unmutated private
journey baseline.  This directly disproves a broad “no serial markers / no
contention flakes” reading at 16 workers.

Qualification: this was intentionally an under-load run, but the host had
other live worktree suites and three earlier detached `t2-tests -n 16` runs
were simultaneously consuming resources before I stopped only those exact
PIDs.  It is therefore evidence of an unbounded concurrency envelope and
timeout sensitivity, **not** a clean reproduction of one deterministic
cross-worker data collision or a refutation of the narrower documented
`-n auto`/`-n 4` measurements.  The verdict must state that boundary, or a
clean isolated repeated `-n 16` run is needed before making a stronger claim.

### REFUTED — a test-home/registry collision was not established

`tests/conftest.py` creates a distinct `mkdtemp` root per worker before test
imports and assigns `HOME`, runtime, registry, and LaunchAgents beneath it.
The failed stress trace was dominated by wall-clock deadlines and nested
subprocess capacity, not two workers sharing one such path.  I found no
reproduction showing `$HOME`, `RT_PROJECTS_FILE`, or `_TEST_ROOT` collision.

## 2. `_kit` consolidation

### REFUTED — no fixture byte/return/strictness regression found

I compared the replaced YAML blocks in the parent (`git show
52fc483:tests/<file>`) with the corresponding `Seat`/`project` arguments in
the diff, including the less-visible Grok/OpenClaw lab fixtures, the no-
`instances` seat-lifecycle shape, `PROJECT_DOT`, and the optional-cmux wrapper
which still returns `path / ".roundtable"`.  The generated line ordering,
trailing newline, project spelling, and wrapper return contracts match.  The
kit retains `state.mkdir(parents=True)` without `exist_ok`, so pre-existing
state still raises.  `git diff --check` was clean and the focused kit/mutation
run passed: `14 passed` for
`tests/test_kit_locators.py tests/test_kit_consumers.py
tests/test_journey_mutation.py`.

I also found no current mutation of `TOOLING_SEATS` or of a mutable `Seat`
payload.  Its mapping fields are technically mutable despite the frozen
dataclass, but that is not a present failure scenario.

## 3. AST locators

### CONFIRMED — `call_source` conflates an attribute call with a bare call

Adversarial input:

```python
def load_validated_lease(): pass
x.load_validated_lease()
load_validated_lease()
```

`_kit.source.call_sources(path, "load_validated_lease")` returned both
`['x.load_validated_lease()', 'load_validated_lease()']`.  `_callee_name`
reduces both forms to the terminal `attr`; it therefore cannot meet the stated
“exactly one call to callee” contract.  A future mutation test which grows an
unrelated `obj.load_validated_lease()` in the same scope turns into a locator
ambiguity rather than pinning the intended imported function.  The current
Grok file has only the one relevant call, so this is a locator-contract defect,
not evidence that its present mutation guard selects the wrong call.

### CONFIRMED — `definition_source` excludes decorators

For `@decorator\ndef target(): ...`, `definition_source(path, "target")`
returned only `def target(): ...`.  `ast.get_source_segment` on
`FunctionDef` starts at `def`, not the decorator list.  Thus a test claiming
to inspect the exact definition misses a policy-bearing decorator.  No current
consumer depends on a decorated definition, so this is a demonstrated generic
locator gap rather than a current production escape.

### PLAUSIBLE — the Grok “seat path” claim remains broader than its test

`test_grok_seat_path_is_pinned_away_from_internal_acp_supervisor` scans only
the AST segment for `launch`.  Moving ACP construction into a module-level
helper (whose name does not contain `grok_adapter_bin`, `--grok-bin`, or
`rt-grok-wake`) and calling that helper from `launch()` would leave the three
negative string assertions green while restoring the supervisor path.  The
separate help test checks wording, not launch reachability.  This is source
reasoning, not a branch mutation, so it is PLAUSIBLE.

## 4. Derived consumer inventory and ledgers

### CONFIRMED — a shipped `.pyw` Python tool is invisible

Synthetic production root input: `bin/tool.pyw` containing `print(1)`.  The
observed `production_sources(root)` result was `[]`.  `_is_python_source`
accepts only `.py` or extensionless first-line shebangs, so the claim to
discover *every production Python source* is false for Python’s `.pyw`
extension.  This does not identify a current untested `.pyw` in this checkout;
it proves the discovery rule is narrower than the claim.

### PLAUSIBLE — path-level definition exemptions can hide a new violation

`LAYOUT_DEFINING_SOURCES` completely removes `bin/_rtlib.py` and
`bin/_rtmigrate.py` from `test_production_sources_do_not_construct_layout_paths`.
Its “still earned” test requires only one detected construction in each file.
A new non-layout consumer/path construction in either file therefore passes as
long as one old allowed construction remains.  The mechanism is concrete but
requires a future source change, so it is PLAUSIBLE.

### REFUTED — alias imports do not silently pass the current lock invariant

For `from _rtlib import locked_project_mailbox_checked as _open; mailbox =
_open(...); mailbox.inbox_dir`, `source_facts` correctly sees
`touches_maildir=True` and misses the locked-call name.  That is conservative:
`test_maildir_consumers_use_the_locked_resolver_only` would reject the source
because `not facts.calls_locked_resolver`, rather than allowing it through.
This is a false-positive/ergonomics limitation, not the requested evasion.

## 5. Journey mutations

### PLAUSIBLE — the mutation runner proves only “nonzero”, not the named kill

`test_journey_guard_mutations_turn_the_private_copy_red` ends with
`assert result.returncode != 0`; it does not assert the selector’s named test
failed or that the failure is semantic rather than import/timeout.  A mutant
that turns the selected source segment into a syntax error would be counted as
killed.  The handoff says each failure was manually inspected, which helps for
the eight present mutants, but that evidence is outside the executable guard.
The three indentation-bearing string needles have the same `source.count() ==
1` reindent fragility that Tier 0 removed elsewhere: a harmless reindent makes
the mutation test fail before running its journey.

### REFUTED — no present survivor among the eight specified mutations

The focused run of `tests/test_journey_mutation.py` passed in the normal
focused environment.  The high-worker failure of its unmutated baseline
occurred only amid the overloaded run above; it is capacity evidence, not a
surviving mutation or proof that the baseline is intrinsically broken.  The
print, watcher-pid, archive, and receipt journeys all contain the direct happy
path assertions described by the handoff; I found no weaker current production
change that leaves its named journey green.

## Required correction

Keep the core implementation claim, but narrow the xdist handoff: it has
evidence for the measured 4/auto-worker envelope, not a universal
no-contention/no-serial-marker verdict.  Separately, tighten AST callee
identity and either support `.pyw` or constrain the consumer-derivation claim
to `.py` plus first-line-shebang executables.
