# 1.4 Track T5 — harness expansion research

> Status: historical record — 1.4 track T5 dispatch; merged

Seat: claude (Opus 5, max reasoning effort; fall back to xhigh if max is
unavailable). You are authorized to use ultracode or the Workflow tool at
your discretion for this work.

## Context

`handoff/harness-expansion-survey.md` (read in full) holds the 13-candidate
verdict table: Pi / OMP / Qoder / OpenCode / Kilo are A-ready (native
injection into a live TUI), Copilot B-ready, Devin/Droid A-conditional,
agy B-ready + A-candidate via its `agentapi send-message` sidecar;
recommended lab order Qoder → Pi/OMP → OpenCode/Kilo → agy → Copilot →
Devin/Droid. `handoff/archive/d14-tui-first-survey.md` and `decision.md`
(2026-08-12 "Seats are interactive TUIs") define the doctrine: seats are
TUIs; wake adapters are background channels INTO a TUI; model-armed (B) is
acceptable shipped shape, code-armed (A) preferred.

## Scope — research and design, this cycle produces paper not adapters

1. **Per-harness integration blueprints** for the A-ready cohort + agy:
   for each — launcher shape (`rt-<name>` TUI-first), seat identity/lease
   integration, wake adapter design (exact injection face, citing each
   harness's docs/source), orientation payload (`<NAME>.md` template),
   doctor probes (liveness + drift checks for any undocumented internals),
   and the validation lab plan (what a live E2E must prove before any
   support claim — fixtures alone never count).
2. **Re-verify currency**: the survey is dated; check each candidate's
   latest release for changes to the injection faces before blueprinting.
   Web research as needed.
3. **Prioritized 1.5 proposal**: effort-ranked implementation order with
   promotion gates, consistent with `docs/compatibility.md` conventions.

## Hard constraints

- Do NOT install any third-party harness or modify ANY shared harness
  configuration (hooks files, global settings) without first mailing
  claude@roundtable-product and waiting for approval. Precedent: a herdr
  integration install once tripped the codex hook-trust gate and
  fail-closed every codex wake on the machine. Clearly-reversible,
  fully-sandboxed trials that touch nothing shared may proceed at your
  judgment — when in doubt, escalate.
- English-only, public-safe. Commit to THIS worktree branch only; do NOT
  merge — operator merges manually.
- Deliverable: `handoff/harness-expansion-blueprints.md` (+ per-harness
  files if large). Report via `rt-say claude@roundtable-product update ...`.
