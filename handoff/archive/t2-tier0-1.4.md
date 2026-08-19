# T2 amendment — architecture review Tier 0

> Status: historical record — 1.4 track T2 Tier-0 result; merged

Track T2, 1.4 cycle. Scope from `handoff/architecture-review-1.4.md` §6 Tier 0,
folded into T2 by the collision analysis in
`handoff/archive/architecture-review-1.4-track-collisions.md` ("Tier 0 is not a
separate track; it is T2's scope").

No production code changed. Everything here is `tests/`.

## 1. The source-text locators

### The live specimen

`tests/test_grok_adapter.py` located the body of `_rtlauncher.launch()` with

```python
launch_source = launcher_source.split("def launch(", 1)[1]
```

which is *everything after* that marker — 138 lines to end of file, including
all of `main()`. Two consequences, both verified:

- **False alarms.** Any definition placed below `launch()` is absorbed. The
  review flagged this precisely because `_rtharness.py` is expected to move
  harness tables around; a table landing after `launch()` would fail a test
  that claims to pin the seat path.
- **No loud failure.** A slice cannot report that it located the wrong region,
  so the assertions silently evaluate over the wrong text.

### What replaced it

`tests/_kit/source.py` locates by syntax:

- `definition_source(path, name)` — the exact source of one function or class
  and nothing below it. Accepts a dotted qualname. Raises `LocatorError`
  naming the candidates when the target is missing or ambiguous.
- `call_source(path, callee, within=...)` — the exact source of one call,
  taken from the file being inspected.
- `called_names` / `defined_names` — AST inventories.

`test_grok_seat_path_is_pinned_away_from_internal_acp_supervisor` now uses
`definition_source` and additionally asserts `"def main(" not in launch_source`,
so an over-capturing regression fails immediately.

### The indentation-pinned needle

`tests/test_grok_mutation.py` hand-copied a six-line `load_validated_lease(...)`
call at 16-space indentation and asserted `source.count(needle) == 1`. That is
the fragility the review records under hidden costs: reindenting the Grok
adapter fails a *safety* test with a confusing count error rather than a real
finding. The needle is now derived with `call_source` from the file under
mutation, so it follows the code. The mutation still turns the private
contract red.

### Mutation checks on the locators themselves

`tests/test_kit_locators.py` mutates the real files the locators are aimed at
and requires the locator to react correctly in both directions. Each case also
shows what the replaced spelling did with the same input:

| Case | Required behaviour |
| --- | --- |
| marker added *inside* `launch()` | located text contains it — caught |
| table added *below* `launch()` | located text does not contain it — no false alarm; the text slice *did* raise one |
| target renamed | `LocatorError` naming the available definitions |
| bare name ambiguous across two classes | `LocatorError`; dotted qualname resolves |
| Grok lease call reindented by 4 spaces | derived needle still matches exactly once; the hand-copied needle matches zero times |
| call absent / duplicated | `LocatorError`, never a silent pick |

## 2. `tests/_kit`

`tests/_kit/projects.py` is now the only place the `agents.yaml` shape is
written down. `Seat` carries the per-seat variation the suite actually uses
(instances, `submit`, `detect`, `session_id: null`, and an explicit empty
`instances` tuple for the no-instances shape the seat-lifecycle tests pin).
`agents_document` renders; `write_project` writes the state directory.

**Call sites were not touched.** Each module keeps its own `write_project`
name and signature and delegates the document to the kit, so the 352 existing
call sites are unchanged and the existing suite is a clean oracle. What is
local stays local and visible — registration, `git init`, `runtime.json`,
`.gitignore`, extra maildir directories.

Before landing any of it, all 15 rendered documents were diffed against the
definitions they replace and confirmed byte-identical.

Consolidated: the 13 `write_project` definitions named in the brief, plus 8
more copies of the same fact under other names that the brief did not count —
`write_registered_project` (×2), `_write_project` (×2), `_write_lab_project`,
`_registered_project`, `_registered_grok_project`, `_grok_launch_fixture`, and
two inline documents in `test_rt_worktree.py` and `test_open_journey.py`.
Leaving those behind would have half-done "one home per fact".

Net effect on the 24 touched modules: −421 lines, +261.

## 3. The derived fitness-function consumer list

`tests/_kit/consumers.py` discovers the universe from the tree — every Python
source under `bin/`, `integrations/`, and `pneu_packaging/`, including
extensionless `#!`-python tools, with symlinked aliases (`bin/roundtable` →
`bin/pneu`) counted once — and classifies each by what it does with the
mailbox: raw resolver, locked resolver, maildir attributes.

The two fitness tests in `tests/test_mailbox_resolver.py` now run over that
derived set. **Both `integrations/` adapters are now covered and both pass**;
they were the exemption the review called out, and they were already correct.

Three ledgers replace the hand-maintained membership list, and each is itself
asserted to be still earned, so a stale entry fails rather than quietly
widening the gap:

| Ledger | Entries | Checked by |
| --- | --- | --- |
| `LAYOUT_DEFINING_SOURCES` | `bin/_rtlib.py`, `bin/_rtmigrate.py` | must still construct layout paths |
| `ADVISORY_UNLOCKED_READERS` | `bin/pneu`, `bin/rt-projects`, `bin/_rtlauncher.py` | must still read the maildir through a raw resolver |
| `ALLOWED_NON_LAYOUT_LITERALS` | one literal in `bin/pneu` | the literal must still be present |

`tests/test_kit_consumers.py` mutation-checks the derivation against synthetic
trees: a brand-new `bin/rt-newcomer` that nobody registered is discovered and
correctly classified as an unlocked maildir consumer; a locked one is
accepted; shell scripts, `scripts/`, and `tests/` are excluded; a symlinked
alias is counted once.

## 4. Findings, not fixes

Two things surfaced that are decisions rather than test work. Neither is
changed here.

**F1 — three production sources read the maildir without the layout lock.**
`bin/pneu` (`_unread_by_seat`, counting unread mail for the launcher card),
`bin/rt-projects` (`resolve`, printing layout metadata), and
`bin/_rtlauncher.py` (`grok_seat_maildir`, deriving a path handed to an
external watcher) each resolve with `resolve_project_mailbox*` and then touch
`inbox_dir`. All three are advisory readers that write no mail, which is why
the hand-maintained list excluded them — but it excluded them *silently*.
They are now named in `ADVISORY_UNLOCKED_READERS` with reasons. Whether any
should move to the locked resolver is a product call, not a test call; the
review's Tier 1 `_rtmail` item is the natural place to decide it.

**F2 — the layout-path detector flags any bare `"inbox"` / `"messages"` /
`"locks"` string literal.** That aggression is deliberate: it is how the
detector catches a path assembled through a variable, and one of its own
parametrized cases depends on it. Widening the check to every production
source surfaced one literal that is not a path at all — the `"inbox"` CLI
subcommand alias in `bin/pneu`'s `ALIASES`. Rather than exempt the file (which
would mask any real violation in it), the single literal is allowed by exact
value. Narrowing the detector was considered and rejected: it would have
broken the indirect-construction case the detector exists for.

## 5. Verification

| Check | Result |
| --- | --- |
| `pytest -q` | 1114 passed, 1 skipped in 286.49 s |
| `pytest -q -n auto` | 1114 passed, 1 skipped in 72.51 s |
| `python -m compileall -q bin pneu_packaging scripts tests` | clean |
| `python scripts/check_public_safety.py` | passed, 207 tracked files |

The suite grew from 1096 to 1114: 8 locator mutation checks, 6 consumer
derivation checks, and 4 net from splitting the two fitness tests into six
(two invariants plus three exemption-ledger checks and one coverage check).

`tests/test_journey_mutation.py` copies `tests/_kit` into its private tree
alongside `bin/`, `templates/`, and `skills/`, since the journey modules now
import the kit. All eight D15 mutations still turn it red.

## 6. Boundaries observed

- Nothing under `bin/`, `integrations/`, `pneu_packaging/`, or `scripts/`
  changed. `git diff --stat` is `tests/` only.
- `integrations/{grok,openclaw}` are parked per `decision.md` 2026-08-12 and
  were not reworked; they are only now *covered* by an existing invariant they
  already satisfy.
- T3's files (`bin/rt-wait-inbox`, `bin/rt-doctor`) and T1's Codex files were
  not touched. D1 (`rt-doctor` OpenClaw coverage) belongs to T3 and is left
  alone.
- The codex cross-check of the review may refine Tier 0. The two verified
  items — the locator class and the `_kit` consolidation — were done first, and
  the consumer derivation is isolated in one module plus three named ledgers,
  so an amendment lands in a small surface.
