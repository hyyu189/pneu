# Harness expansion survey dispatch (codex@roundtable-product, sol ultra)

Owner: Claude (product lead) on behalf of Ocean. Working tree: main checkout
(read-only research — write ONLY `handoff/harness-expansion-survey.md`; do
NOT commit, do NOT touch code; the product lead folds the file into the next
release integration).

## Question

Which additional coding-agent harnesses can pneu support next, under the
TUI-first ruling (decision.md 2026-08-12): a user-launched seat is the
harness's own interactive TUI; wake channels are background paths INTO that
live TUI session, never a replacement for it. Two acceptable wake shapes,
in descending preference (see `handoff/d14-tui-first-survey.md` for the
Grok/OpenClaw precedents):
  A. Harness-native injection: an external process can start/queue a turn
     in a live TUI session (Codex app-server thread injection, Hermes
     background-completion rail, Claude SessionStart/Stop hooks).
  B. Model-armed: the TUI's first turn can be seeded (positional prompt or
     equivalent) and the session has a persistent background/monitor/
     scheduler primitive that can wake it on a filesystem event (the Grok
     1.0.0 pattern) — accepted tradeoff: re-arm on every launch/resume.

## Candidates

Priority set (Ocean named): **pi**, **kiro**, **opencode**.
Roster sweep (herdr's integration list is a hint of what runs in terminal
panes): omp, copilot (CLI), devin (CLI), droid, kimi, kilo, qodercli,
cursor (cursor-agent CLI), mastracode.
Plus the mandated re-examination: **Antigravity (agy)** — it was parked
2026-08-05 because it had no startup/resume hook and no external wake
surface; that verdict predates the TUI-first + model-armed frame. Re-answer
narrowly: does the agy TUI have (i) a seedable first turn, (ii) ANY
persistent background/monitor/scheduler primitive, (iii) resume semantics —
i.e. does shape B apply even though shape A was ruled out? Cite the
2026-08-05 research (`handoff/antigravity-harness-research-2026-08-03.md`,
`handoff/antigravity-stage1-2026-08-04.md`) and say exactly what changed
or did not change.

## Per candidate, report

1. Terminal TUI: does a real interactive terminal UI exist (not an IDE/GUI;
   IDE-only products fail TUI-first at the terminal — note them as such,
   e.g. if kiro is IDE-only say so and stop there).
2. Install/auth model on macOS (npm/brew/binary; account/credential shape;
   anything that would violate our read-only-credentials discipline).
3. Wake shape A evidence: sockets/servers/RPC/plugin/hook systems that can
   reach a live session (source or docs level).
4. Wake shape B evidence: seedable first turn + persistent monitor/
   scheduler/loop primitive + resume semantics.
5. Lifecycle hooks usable for watcher arming (SessionStart/Stop analogs).
6. Verdict tier: A-ready / B-ready / parked (with the exact unpark
   condition), plus a one-line effort guess (S/M/L) for an adapter.

## Method and discipline

Read-only: installed CLIs on this host may be probed with `--help`/version
and by reading their docs/config layouts; do NOT launch interactive
sessions, do NOT touch credentials or live state. Web research (docs,
repos) is expected — cite sources with URLs. Where evidence is
docs-inferred rather than verified, label it (the compatibility discipline:
claims need live smoke before support promotion — this survey feeds
prioritization, not support claims).

Deliverable: `handoff/harness-expansion-survey.md` (verdict table up top,
per-candidate sections below). Then
`RT_FROM=codex rt-say --fenced --no-nudge claude@roundtable-product status
"<one-line summary + file pointer>"`. No commits, no Claude-Session
trailers anywhere.
