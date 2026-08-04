# M5 onboarding and communication batch — analysis and execution plan

Status: Ocean approved the M5 scope; implementation is complete and verified.
Worktree: `rt-m5`, branch `wt/m5`.

## Scope confirmation

I would keep all four proposed M5 groups. M5-1 is the only P0 and should land
first; M5-2 through M5-4 are low-risk communication improvements that preserve
the existing durable-mail and fail-closed behavior.

- **M5-1 — wizard resilience:** bounded retries for every interactive choice,
  including blank numbered-menu input, plus an explicit recovery message when
  project creation already succeeded before a later seat-selection failure.
- **M5-2 — observability:** add `roundtable version`; enumerate valid configured
  names in unknown-target errors; and add a post-publish, stderr-only dead-seat
  advisory without turning send into a liveness gate.
- **M5-3 — migration messaging:** put recovery instructions on the
  registry/layout uncertain-commit path, explain lock-timeout retry/ownership
  guidance, and emit a human-readable migration/rollback summary before the
  existing JSON.
- **M5-4 — error-string sweep:** apply the listed help, parse-shape, fenced
  seat, project-root, moved-project, mailbox-path, self-send, component-shape,
  uninstall-preservation, installer-PATH, Python-discovery, subprocess-next,
  and README alias clarifications.

The P2 backlog should be triaged into this batch only where it overlaps the
same communication surfaces: show enough project/worktree identity in inbox
or target diagnostics, expose the derived sibling set where an addressing
error already lists valid names, identify which project configuration refused
an operation, and add the pre-reconcile/self-heal hint to the relevant failure
messages. The remaining P2 items should stay deferred rather than expanding
M5 piecemeal.

I would explicitly drop/defer the reviewed v2 design items: command renaming,
JSON-shape unification, setup confirmation redesign, statusline/unread-view
work, and the ordinary-unacked-mail wake-loop framing. The explicitly
non-bugs remain unchanged: Claude-hook exit status 2, load-bearing
`--no-nudge`, and durable fire-and-forget send.

## Approved additions

- **M5-5 — terminal tutorial:** show a compact ASCII mailroom diagram during
  interactive onboarding and expose the same text through `roundtable guide`.
  It explains durable file delivery versus wake-up, the Claude SessionStart /
  Stop-hook tripwire, the Hermes session-start plugin tripwire, and the Codex
  app-server plus Unix-socket notification path. Grok Build, OpenClaw, and
  Antigravity are named as parallel research targets only, not as supported
  harnesses until live evidence exists.
- **M5-6 — clean cancellation:** catch `Ctrl-C` in the unified onboarding CLI
  and `rt-*` launcher wrapper, print a short cancellation message, avoid a
  Python traceback, and return the conventional 130 status.

## Expected file-touch list

Runtime and command surfaces:

- `bin/roundtable`
- `bin/_rtlauncher.py`
- `bin/roundtable-init`
- `bin/rt-say`
- `bin/rt-inbox`
- `bin/rt-ack`
- `bin/rt-wait-inbox`
- `bin/_rtlib.py`
- `bin/_rtmigrate.py`
- `bin/rt-projects`

Packaging/documentation surfaces:

- `roundtable_packaging/cli.py`
- `roundtable_packaging/setup.py`
- `scripts/install.sh`
- `README.md`
- `templates/README.md.tmpl` if the README alias/PATH wording is mirrored in
  the generated project template.

Focused regression coverage should be added to the existing suites rather
than creating a new test framework:

- `tests/test_roundtable_cli.py`, `tests/test_roundtable_init.py`, and
  `tests/test_rt_launcher_lease.py` for onboarding and launcher prompts.
- `tests/test_rt_tooling.py` and `tests/test_rt_tripwire_runtime.py` for
  messaging, errors, fenced behavior, and quiet-ack invariants.
- `tests/test_mail_migration.py` and `tests/test_layout_locks.py` for
  migration summaries, uncertain commits, and lock guidance.
- `tests/test_packaging.py` and `tests/test_bin_syntax.py` for install/runtime
  surfaces and script integrity.

The dirty `CLAUDE.md` shown by the initial status check is pre-existing and is
not part of this proposed change.

## Sequencing

1. Add prompt helpers/tests and the post-init recovery path (M5-1).
2. Add version and target/seat observability while preserving unconditional
   maildir publication (M5-2).
3. Update migration/lock/user-facing success paths and their JSON-preserving
   tests (M5-3).
4. Perform the mechanical error/help sweep, then update README/template text
   only where generated-user behavior requires it (M5-4).
5. Add the ASCII tutorial and clean cancellation path (M5-5/M5-6).
6. Run focused tests after each group; then run the full pytest suite, compile
   checks, public-safety scan, and packaging/install smoke required by the
   repository instructions.

## Test plan

- Feed invalid, blank, and EOF input to every wizard/launcher prompt; assert
  bounded retry behavior, clean failure, and the project-survived message after
  initialization.
- Assert `roundtable version` reports version, install prefix, and resolved
  `current` target, including a symlink-target case.
- Assert unknown `rt-say`/`rt-inbox` targets list configured names and that a
  successful send still publishes before a dead-seat advisory is emitted.
- Exercise uncertain migration commit and layout/registry lock timeout paths;
  assert recovery/retry guidance, human summary on stderr, and unchanged JSON
  machine output.
- Assert `rt-ack -h/--help`, malformed-id guidance, fenced-seat guidance,
  moved-project remediation, mailbox-path context, allowed component shape,
  self-send policy, installer PATH caveat, and Python-not-found guidance.
- Preserve regression coverage for the intentional behaviors listed as
  non-bugs, especially quiet acknowledgements and no second ack.
- Assert `roundtable guide` renders the ASCII tutorial, interactive onboarding
  prints it, and both unified and harness-specific launchers turn `Ctrl-C`
  into a clean exit 130 without traceback text.

## Effort and risks

Estimate: roughly 2–3 focused engineering days, including regression tests and
the required verification pass; M5-1 and M5-2 are each about half a day, M5-3
about half to one day, and M5-4 plus verification about one day.

Main risks are accidental prompt behavior changes for scripted/EOF callers,
turning the seat advisory into a delivery gate, changing migration JSON relied
on by automation, and broad error-string edits that miss installed-artifact
parity. Keep all new liveness checks read-only and post-publish, preserve JSON
schemas, and test both source-tree and packaged command paths.

## Decision gate

Ocean approved the original M5 scope and these two additions; implementation
may proceed while external harness research remains fact-check-only.

## Implementation result — 2026-08-04

Implemented M5-1 through M5-6 in this worktree. The launcher menus now give
invalid input bounded retries and surface a project-ready recovery path after
init. `roundtable version`, configured-target hints, post-publish dead-seat
advice, migration recovery/summary output, and the requested error/help and
packaging guidance are in place. The ASCII tutorial and clean Ctrl-C handling
are included in the unified and harness-specific launchers.

Verification completed before local commit:

- focused onboarding/launcher/init regression set: 82 passed;
- final full suite: 872 passed in 798.78s (13:18);
- `mamba run -n general python -m compileall -q bin roundtable_packaging scripts`;
- `mamba run -n general python scripts/check_public_safety.py` — passed,
  116 tracked files, full reachable history;
- `git diff --check` — passed.

Scope notes: Grok Build, OpenClaw, and Antigravity remain research-only names
in the tutorial; no support claim or seat configuration was added. The
pre-existing `CLAUDE.md` routing edit remains outside this change.
