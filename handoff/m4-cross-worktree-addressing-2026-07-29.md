# M4 cross-worktree addressing handoff

Date: 2026-07-29

## Scope

This branch now implements design section 4 without changing the live
registry, migrating a real mailbox, merging, or pushing:

- `rt-say agent@project` resolves one exact active project name inside the
  sender's revalidated derived group.
- Sender and target identities come from one registry snapshot. A moved sender
  is reconciled under layout → registry lock order before that final snapshot.
- The target agent is authorized only by the target project's strict
  `agents.yaml`; base aliases canonicalize to their one concrete instance and
  duplicate or malformed identities fail closed.
- Durable envelopes carry `origin=<project-uuid>`.
- `rt-ack` reads each exact inbound envelope, groups receipts by origin UUID,
  returns quiet acknowledgements to those UUIDs, and archives each successful
  origin group before attempting the next. If a later group fails, the earlier
  groups' `cur/` files suppress duplicate receipts on an exact retry. Receipt
  delivery and archival are separate commits, so an archival failure after
  delivery can still make retry resend the current group's receipt.
- UUID-aware mail cannot downgrade to the legacy local route.
  Missing, conflicting, unreadable, or identity-inconsistent evidence remains
  in `new/` with an executable fail-closed remedy.
- Normal and central mailbox layouts retain non-nested lock ordering. Opposite
  direction sends, target cutover, origin cutover, moved-origin reconciliation,
  and receiver identity changes have focused coverage.

## Compatibility decisions

- Bare `rt-say agent kind body` keeps its syntax and current-project target
  selection, but its wire bytes change: all newly emitted envelopes, including
  local sends and quiet acknowledgements, carry `origin=<project-uuid>`.
- Legacy envelopes without `origin` remain locally acknowledgeable when the
  exact inbound file is present.
- An unproven missing ref is no longer treated as an idempotent success because
  its origin UUID cannot be recovered safely.
- New `origin=` envelopes require the matching M4 reader. The pre-M4 reader at
  `b45307d6f107e1e37aa969386d59ad63b1bac30d` classifies even a local
  origin-bearing envelope as malformed with `problem=invalid mail header`;
  its filename-only `rt-ack` remains local and cannot return a cross-worktree
  receipt correctly.

## Verification

- `mamba run -n general pytest -q` — **835 passed**
- explicit `py_compile.compile(..., doraise=True)` sweep — passed for all
  **23** regular `bin/` files, including **18** extensionless CLIs
- `mamba run -n general python -m compileall -q roundtable_packaging scripts`
  — passed
- `mamba run -n general python scripts/check_public_safety.py` — passed,
  **115 tracked files** and full reachable history
- `git diff --check` — passed
- pre-M4 reader lab — executed the exact `_rtlib.py` and `rt-inbox` sources
  from `b45307d6f107e1e37aa969386d59ad63b1bac30d` against a synthetic
  origin-bearing envelope; it returned
  `schema=roundtable.maildir_malformed.v1`, `lifecycle=new`, and
  `problem=invalid mail header`
- Acceptance-directed F1–F5 regression coverage is included in the full
  suite.

## Closure boundary

The committed third-round record at `handoff/acceptance-m4-2026-07-29.md`
issued conditional acceptance with five P1 findings. This F-pass closes those
findings and their named coverage gaps. Claude should rerun every recorded
reproduction against the resulting commit before declaring M4 closed. Preserve
the branch-only, no-live-migration, no-merge, and no-push boundary.
