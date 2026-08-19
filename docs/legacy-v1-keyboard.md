# Legacy v1 keyboard delivery (retired 2026-07-17)

> Status: historical record of a retired path. The `--legacy-nudge-only`
> escape hatch still exists in `rt-say`, so this note is kept as its only
> operating instructions; nothing here describes normal delivery.

v1 delivered messages by typing into the target's terminal. It is retired:
normal `rt-say` is maildir-only by design, rt-watch and its SessionStart
hook are gone. This note preserves the emergency manual path.

## Emergency manual send (last resort, human-coordinated only)

1. `rt-say --legacy-nudge-only <agent> <kind> "body"` still exists but needs a
   fresh topology map: run `rt-refresh` from a real cmux surface in the bound
   workspace first, verify with `rt-resolve <agent>`.
2. Raw `cmux send` / `send-key`: resolve the surface first; then match the
   submit key to the target's state — wrong key on a busy agent submits into
   the wrong prompt:

   | Agent | Idle | Busy |
   |-------|------|------|
   | Claude | text + Enter | text only, no Enter (interrupt-safe) |
   | Codex | text + Enter | text + **Tab** (Enter submits the wrong prompt) |
   | Hermes | text + Enter | prepend `/steer`, then Enter (injects next turn) |

Known failure modes of this path (why it was retired): keyboard collisions
with the human, one inference turn per message, submit-key state drift, stale
surface maps misrouting text into the wrong window (two live incidents on
2026-07-17 alone), and safety-guardrail triggers on injected agent text.
