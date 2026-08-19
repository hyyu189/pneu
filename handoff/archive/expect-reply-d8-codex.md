# D8 expect-reply batch — Codex implementation handoff

> Status: historical record — D8 implementation report

Source directive: [`expect-reply-d8.md`](expect-reply-d8.md)

## Delivered

- `rt-say --expect-reply <duration> <agent> <kind> <body...>` accepts positive
  seconds, minutes, and hours, and refuses malformed, zero, or negative
  durations before delivery.
- A successful send records a fenced, per-seat expectation in durable
  `reply-expectations.json`; the expectation survives watcher restarts and is
  removed atomically when acknowledged or fired.
- The existing watcher poll reads valid quiet-ack receipts from both `new/`
  and `cur/`, including cross-worktree receipts routed home by origin UUID.
  An unacknowledged expired expectation emits one contentful `reply overdue`
  wake naming the message, peer, send time, and configured duration.
- Hermes turns the overdue watcher output into one native alarm using the
  existing waiter; no daemon, timer, or delivery-path change was added.
- Version surfaces are combined release `1.1.0`. The canonical pneu skill,
  README, and architecture notes document the alarm semantics and the
  opt-in adoption SOP for sending/receiving trees.

## Evidence

- Combined D7/D8 focused watcher, runtime, worktree, doctor, and Hermes suite:
  **121 passed**.
- Tooling suite, including local and cross-worktree expect-reply lifecycle:
  **137 passed**.
- Full suite: **953 passed, 1 skipped** in **16m32s**.
- `compileall` passed.
- The public-safety scan reports one pre-existing reachable history finding in
  `799d9df`: a private Claude session URL in that commit's metadata. No new
  worktree finding was reported; history was not rewritten.

The D7 companion handoff is [`quiet-wake-d7-codex.md`](quiet-wake-d7-codex.md).
Claude acceptance remains release-artifact validation and installed-runtime
hot-swap/reload review for the combined `1.1.0` batch.
