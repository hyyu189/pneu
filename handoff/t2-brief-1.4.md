# 1.4 Track T2 — D15-complete (test process)

Seat: claude (Opus 5, max reasoning effort; fall back to xhigh if max is
unavailable). You are authorized to use ultracode or the Workflow tool at
your discretion for this work.

## Scope

Finish D15 properly. The demo shoot ran a real slice and left findings; the
canonical repo has none of it yet.

**Harvest first (read-only sources)**: the demo clone holds uncommitted
artifacts — findings at
`~/Code/pneu-worktree/d15a-xdist/handoff/d15a-xdist-findings.md` and
`d15a-xdist-audit-codex.md`, plus an uncommitted journey test file in the
`~/Code/pneu` working tree. Read them; land equivalent work HERE (this
repo), do not modify those trees.

1. **D15(a) xdist**: fix the collection blocker —
   `tests/test_mailbox_resolver.py:679-684` generates parametrize ids with
   `str(uuid.uuid4())` at import time; freeze to literal ids. Add
   `pytest-xdist` to `requirements-dev.txt` (pin sensibly). Then run the
   REAL audit: full suite under `-n auto`; classify genuinely-serial tests
   and mark them with reasons (shared registry/mailbox/tmp state); deliver
   the parallel-safety verdict + wall-clock before/after (serial baseline
   was 1081 passed / ~982s). "It collected cleanly" is not the deliverable;
   the verdict document is.
2. **D15(b) journey tier**: the existing print-fallback journey test is a
   mutation survivor — with the seat vacant, deleting the
   `if selection.kind != 'print'` guard at `bin/rt-worktree:793` leaves it
   green because `_require_launchable_seat` (bin/rt-worktree:751) only
   raises for active_* states. Fix: claim a seat first, then assert exit 0
   + printed command (pattern exists at
   `tests/test_open_journey.py:272-296`). Add an end-to-end ambient
   fallback journey (no HERDR_ENV, no tmux → print). Then build the minimal
   complete journey set: launcher navigation (real pty keys, including
   arrows), seat open → lease active, and a mail send→wake→ack loop at the
   journey level.
3. **Mutation-check your own tests**: for each guard a test claims to pin,
   delete the guard and confirm the test fails. A test that survives its
   mutation is not done.

## Acceptance

Suite green serially AND under `-n auto` (with documented serial markers);
wall-clock comparison recorded; journey tests fail under their mutations;
public-safety green.

## Constraints

- English-only artifacts; public-safe. MARKER_BLOCKS byte-identical.
- Commit to THIS worktree branch only; do NOT merge — operator merges
  manually.
- Report completion + numbers via `rt-say claude@roundtable-product
  update ...`; mail if blocked.
