# D8 — expect-reply: one-shot reply-deadline alarm (sequenced after D7)

Owner decision (Ocean, 2026-08-07): with the periodic heartbeat retired (D7),
the only timer pneu needs is a per-message, one-shot deadline: "I sent this
and expect a reply; wake me if none arrives in time." It is an alarm, not a
heartbeat — it fires at most once per expectation, and only on failure.

## Mechanism

- `rt-say --expect-reply <duration> <agent> <kind> <body>` — duration accepts
  forms like `30m`, `2h`, `90s`. After a successful send, record an
  expectation on the SENDER's seat: `{msg_id, peer, sent_at, deadline}`.
  Choose the storage location yourself under these constraints: per-seat,
  safe under fenced writes, survives watcher restarts, and is cleaned up when
  cleared or fired. No global registry, no new daemon, no launchd timer.
- The sender's own watcher (already polling every 5s) checks expectations on
  each poll:
  - The quiet receipt `ack-<msg_id>` has arrived (check both `new/` and
    `cur/` — the fenced drain may already have archived it; cross-worktree
    acks route home by origin UUID and must clear too) ⇒ clear the
    expectation silently.
  - Deadline passed with no ack ⇒ wake the seat once with a message naming
    msg_id, peer, sent_at, and the configured duration ("reply overdue").
    Mark the expectation fired so it can never wake twice. This is a real
    contentful wake through the existing mail-wake path semantics, not a
    revival of the empty heartbeat.
- Durability: if the seat is offline/unarmed when the deadline passes, the
  alarm fires on the next arm. That is correct behavior, not a defect.
- Semantics note: ack means "the peer processed the message", which is the
  protocol-guaranteed signal. Whether a substantive answer arrived is the
  agent's own judgment; an actual reply is itself a native wake and needs no
  timer.

## Docs

- Skill doc (canonical skills/shared/pneu/SKILL.md): when to use
  `--expect-reply` (dispatches and questions that need an answer; not fyi),
  and that the alarm is one-shot.
- Add the adoption SOP line while in there: teammate/scratch git worktrees
  need no pneu registration; only a tree that must send/receive mail gets
  adopted, via `roundtable-init --here` (group membership derives
  automatically). Registration is opt-in by design.
- README: one short paragraph in the delivery section.

## Constraints

- Delivery path untouched; expectation checking rides the existing watcher
  poll — no new processes.
- `rt-say` continues to reject unknown flags; `--expect-reply` must validate
  its duration and refuse nonsense (zero/negative/unparseable).
- Tests: full lifecycle (set → ack clears; set → deadline fires exactly once;
  watcher restart persistence; cross-worktree ack clears; malformed duration
  refused), condition-level mutation checks.

## Sequencing and release

Do NOT start until D7 is green and reported — both batches edit
rt-wait-inbox. Work on the same branch `wt/rt-quiet-wake`. Merge `main` into
the branch first so this handoff is in-tree. The combined release version is
**1.1.0** (supersedes D7's 1.0.1 instruction — one release, one hot-swap).
Report with a handoff pointer via `rt-say claude@roundtable-product`; I run
acceptance on both together.
