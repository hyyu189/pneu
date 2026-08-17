# T2 adversarial review — joint verdict

Operator-ordered final stage for the T2 branch. A Codex seat
(`gpt-5.6-terra`, high reasoning effort) attacked `809e06a` + `511e90d` from a
split pane in the `t2-tests` tab under the charter in
`handoff/t2-adversarial-review-charter.md`. Its raw findings are in
`handoff/t2-adversarial-findings-codex.md`, unedited.

**The review landed.** Six of its ten items changed the branch: one document
claim was too strong and is now corrected, and five code defects are fixed.
Its three refutations held up under my own checking, and I upgraded one of its
PLAUSIBLE items to a fix because I had independently confirmed the same gap.

## Dispositions

| # | Finding | Codex rank | My disposition |
| --- | --- | --- | --- |
| 1 | `-n 16` fails under load; zero-serial-markers claim too broad | CONFIRMED | **Claim corrected, cause reclassified** |
| 2 | `_kit` byte/return/strictness regression | REFUTED | Agreed, no action |
| 3 | test-home / registry collision | REFUTED | Agreed, no action |
| 4 | `call_source` conflates attribute and bare calls | CONFIRMED | **Fixed** |
| 5 | `definition_source` drops decorators | CONFIRMED | **Fixed** |
| 6 | Grok seat-path claim broader than its test | PLAUSIBLE | **Upgraded and fixed** |
| 7 | `.pyw` invisible to discovery | CONFIRMED | **Fixed** |
| 8 | Layout exemptions can hide a new violation | PLAUSIBLE | **Fixed** |
| 9 | Mutation runner proves "nonzero", not the named kill | PLAUSIBLE | **Fixed** |
| 10 | Alias imports evade the lock classifier | REFUTED | Agreed, no action |

## 1 — The concurrency envelope: claim corrected, cause reclassified

Codex ran `pytest -q -n 16 -p no:randomly` and got 20 failures. It was right
that `handoff/d15a-xdist-verdict.md` claimed more than it had measured: the
document said "safe" on the evidence of `-n auto` and `-n 4` only, and an
unqualified safety claim is falsified by any red run.

It was also careful to say its run was not a clean reproduction — the host was
carrying other worktree suites plus three of its own detached `-n 16` runs.
That qualification turned out to be the whole story.

**I re-ran `-n 16` twice on an idle host after stopping those runs:**

| Run | Result | Wall clock | 1-min load at start |
| --- | --- | ---: | ---: |
| `pytest -q -n 16` | 1114 passed, 1 skipped | 69.22 s | 12.9 |
| `pytest -q -n 16` | 1114 passed, 1 skipped | 86.08 s | 26.4 |

The reviewer's red run peaked at **load 67 on 10 cores**. Every failure it
named is a deadline — subprocess timeout, poll deadline, lock-acquisition
wait — not two workers observing each other's state. A collision is
load-independent; starvation is not, and a serial marker does not give the
other workers CPU back.

So the finding is real and the fix is documentary, not structural. The verdict
now states the measured envelope (safe to at least 16 workers on an idle
host), names the sensitivity explicitly (wall-clock deadlines under
saturation), and adds the operational consequence: check `vm.loadavg` before
blaming a diff for a red parallel run, because running several agent worktree
suites at once — this project's own workflow — will produce them.

## 4 — `call_source` callee identity

`_callee_name` reduced both `load_validated_lease(...)` and
`client.load_validated_lease(...)` to the trailing attribute, so the stated
"exactly one call to callee" contract was not met. Codex demonstrated it with
a two-line input returning both forms.

Fixed by matching identity rather than the trailing name: a bare `callee`
matches only a bare-name call, and a dotted `callee` (`client.send`) matches
the attribute form. Pinned by
`test_call_source_does_not_conflate_an_attribute_call_with_a_bare_call`.

Worth noting the current impact was zero — the Grok adapter has exactly one
such call — and Codex said so rather than inflating it. That is the right way
to report a contract defect.

## 5 — Decorators

`ast.get_source_segment` starts at `def`, so a decorated definition lost its
decorators — and a decorator is frequently the policy-bearing part.
`definition_source` now includes them by default (dedented to match the
segment), with `include_decorators=False` available. Pinned by
`test_definition_source_keeps_the_decorators`, covering a module-level
function with two decorators and a decorated method.

## 6 — The Grok seat-path claim (upgraded from PLAUSIBLE)

Codex reasoned that moving the ACP reference into a module-level helper called
by `launch()` would leave the three negative assertions green. I had reached
the same conclusion independently while writing the charter, and confirmed it:
the three markers appear **zero times** anywhere in `_rtlauncher.py`, so the
test is a pure regression guard, and a body-only guard is one call deep.

My Tier 0 fix made the locator correct but left the claim broader than the
check — arguably a small *reduction* in coverage versus the broken text slice,
which at least scanned to end of file.

Fixed properly with `kit.reachable_definitions` / `reachable_source`: the
transitive closure of module-level functions reachable from `launch()` — 48
definitions, 45.4k of the module's 50.7k characters, versus 6.3k for the body
alone. Two new mutation checks pin it:
`test_seat_path_locator_catches_the_marker_one_call_deeper` injects the marker
into a helper and asserts the seat-path locator sees it *while the body-only
spelling does not*, and `test_seat_path_locator_stops_at_the_module_boundary`
pins that reachability does not wander into unrelated definitions.

## 7 — `.pyw` discovery

`_is_python_source` accepted `.py` and extensionless shebang files, so a
shipped `bin/tool.pyw` was invisible — against a claim to discover *every*
production Python source. Fixed via `PYTHON_SUFFIXES = {".py", ".pyw"}`,
pinned by `test_discovery_finds_a_pyw_tool`.

The residual limit is now stated rather than papered over: a shell wrapper
that execs python, or a file generated at build time and absent from the
checkout, is beyond the reach of a source scan. That is a boundary of the
technique, not a bug, and the module says so.

## 8 — Layout exemptions

`LAYOUT_DEFINING_SOURCES` removed `bin/_rtlib.py` and `bin/_rtmigrate.py` from
the layout-path check entirely, and the "still earned" test required only that
*some* construction remain. A new one would hide behind the old ones.

Fixed by pinning the count (6 and 12) rather than the path, so an added
construction fails with a message naming the delta and has to be justified by
bumping the number deliberately. This mirrors how
`ALLOWED_NON_LAYOUT_LITERALS` already worked — Codex found the one ledger that
was not value-scoped.

## 9 — The mutation runner's kill criterion

`assert result.returncode != 0` counts a syntax-error mutant, or a collection
failure, as a kill. The handoff said each failure was hand-inspected, but that
evidence lived outside the executable guard — a fair objection.

The runner now requires a real journey failure: no `"no tests ran"`, no
`errors` in the summary line, and at least one `FAILED` node id. All eight
mutations still kill their journeys under the stricter criterion.

On the related point that three needles are indentation-bearing: they differ
from the Tier 0 case in kind, not degree — `source.count(needle) == 1` fails
loudly *before* any mutation is applied, whereas the text slice mis-located
silently. Brittle but not unsound, so it stays. Recorded here rather than
silently dismissed.

## What held

Three refutations, all of which I checked rather than accepted:

- **The `_kit` consolidation.** Codex independently reconstructed the replaced
  definitions from `git show 52fc483:tests/<file>` and confirmed line
  ordering, trailing newline, project spelling, `mkdir` strictness, and each
  wrapper's return contract — including the optional-cmux wrapper that returns
  the state dir rather than the root. This is the verification I could not
  supply myself, since my byte-equality check was a scratch script outside the
  repo.
- **Per-worker isolation.** No `$HOME`, `RT_PROJECTS_FILE`, or `_TEST_ROOT`
  collision was reproducible; import-time `mkdtemp` isolation holds.
- **Alias imports.** `from _rtlib import locked_project_mailbox_checked as
  _open` fails *closed* — the classifier misses the locked call and rejects
  the source rather than admitting it. Conservative, which is the correct
  direction for a safety check.

## Verification

Final tree, after all fixes:

| Check | Result |
| --- | --- |
| `pytest -q` | 1119 passed, 1 skipped in 303.02 s |
| `pytest -q -n auto` | 1119 passed, 1 skipped in 72.52 s |
| `pytest -q -n 16` (×2) | 1119 passed, 1 skipped in 69.85 s / 69.05 s |
| `compileall` | clean |
| `check_public_safety.py` | passed |

One note on method: an intermediate verification run showed two failures
caused by my own editing of the test tree *while* the run was collecting —
including `test_collected_node_ids_are_identical_across_processes`, which is
precisely the guard for "the collected set changed between two processes". It
did its job on a self-inflicted input. The numbers above are from a run taken
after all edits were complete.

## Assessment of the review

Worth recording since the pattern will be reused: the highest-value items were
the two the charter did *not* name — the callee-identity conflation and the
`.pyw` gap — both found by feeding adversarial input to a contract rather than
by reading it. The `-n 16` finding was correct in its claim and honest in its
qualification, and that qualification is what let me classify the cause
correctly instead of adding serial markers that would have fixed nothing.
