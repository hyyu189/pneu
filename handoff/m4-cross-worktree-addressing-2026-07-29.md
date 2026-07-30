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
  returns quiet acknowledgements to those UUIDs, and archives only after every
  receipt delivery succeeds.
- UUID-aware malformed mail cannot downgrade to the legacy local route.
  Missing, conflicting, unreadable, or identity-inconsistent evidence remains
  in `new/` with an executable fail-closed remedy.
- Normal and central mailbox layouts retain non-nested lock ordering. Opposite
  direction sends, target cutover, origin cutover, moved-origin reconciliation,
  and receiver identity changes have focused coverage.

## Compatibility decisions

- Bare `rt-say agent kind body` is unchanged.
- Legacy envelopes without `origin` remain locally acknowledgeable when the
  exact inbound file is present.
- An unproven missing ref is no longer treated as an idempotent success because
  its origin UUID cannot be recovered safely.
- New `origin=` envelopes require the matching M4 reader for cross-worktree
  routing; this branch does not claim rolling compatibility with an older
  reader that does not understand the field.

## Verification

- `mamba run -n general pytest -q` — **816 passed**
- `mamba run -n general python -m compileall -q bin roundtable_packaging scripts`
- `mamba run -n general python scripts/check_public_safety.py` — passed,
  113 tracked files and full reachable history
- `git diff --check` — passed
- Three independent Codex review passes converged with no remaining P0/P1.

## Acceptance boundary

Run the third and final M4 acceptance workflow against this commit. The
handoff message explicitly injects `\ultracode` as requested. Any rejection
should name the exact failing invariant and preserve the branch-only,
no-live-migration boundary.
