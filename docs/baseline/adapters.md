# pneu — adapter baseline

Part of the 2026-08-27 rebaseline. The generalized rules for integrating any harness, the
settled Codex App Server findings, and per-harness status.
Sources: handoff/harness-expansion-{survey,blueprints}.md, handoff/archive/codex-*.md,
handoff/upstream-codex-zero-turn-resume.md, handoff/btw-thread-semantics.md,
handoff/grok-leader-socket-wake-2026-08-14.md, handoff/d14-openclaw-source-audit-2026-08-17.md.

## Adapter principles (ranked)

1. **The seat is the human's native interactive session.** No headless replacement counts;
   "submit to the most recent session" is banned. The acceptance test is always: does mail
   reach the session the human is actually in?
2. **Delivery is the maildir write; wake is an optional adapter.** An unwoken seat is
   late, not lost.
3. **Quiet by default.** An empty inbox must never produce a model turn. Model-executed
   polls and timers are disqualified wake designs.
4. **Fenced identity before any wake.** Project root, agent id, session id, lease
   revision, and the harness-side thread/session id the visible TUI renders must all
   validate; wakes that cannot prove the tuple refuse.
5. **Native wake channels only; never keyboard injection.** Implementation families by
   cost/strength: F1 hook-rewake (Claude precedent) → F2 in-process extension (Hermes) →
   F3 external injector (Codex-grade, resume-surviving) → F4 model-armed (near-zero build,
   permanently second-class: dies with the session).
6. **Evidence discipline.** [S]/[D]/[I]/[L] source tags; a readiness verdict may not cite
   a wiki, cheat sheet, or changelog summary; fail closed on drift; pin versions; fixtures
   never establish support.
7. **Lab-vs-shipped tiers.** The common L-gate (L0–L11: isolation, version pin, identity,
   idle+busy generations, re-arm, zero-model quiet interval, resume routes, interrupt
   cleanup, fence rejection, doctor truth, clean-account/terminal matrix). L0–L10 =
   launchable and documented; support additionally needs L11 plus a credentialed E2E, and
   the compatibility row must name what is still missing.
8. **Credential boundary.** Presence-only preflight; never parse, copy, refresh, or log;
   recovery is always the vendor's own login.
9. **Never touch shared harness configuration without an explicit ruling.** (Standing
   precedent: a herdr integration install once tripped the Codex hook-trust gate and
   fail-closed every Codex wake on the machine.) Owned setup is approval-gated and
   manifest-owned, with plan/apply/status/remove.
10. **Restraint.** The communication layer is not a permission gate; model turns never arm
    watchers; the default seat template stays at three seats.

## Codex App Server — settled findings

- **Single writer forces one canonical host.** Codex holds an OS advisory lock per
  thread; pneu's daemon is canonical and Desktop joins via
  `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1`. Never patch the Desktop bundle; never PATH-shim
  `codex`.
- **The daemon is machine-wide trust, not seat isolation.** Any socket client can drive
  any thread; seat isolation is entirely pneu's lease/fence/binding layer.
- **The environment channel is dead.** Remote turn paths drop
  `shell_environment_policy`; daemon-executed tools never inherit `RT_*` from the launch
  shell. Environment transport designs are rejected.
- **Capability binding replaces environment transport.** Launcher scrubs inherited
  `RT_*`, claims a fresh lease, writes a private launch intent; SessionStart binds the
  native thread id by compare-and-swap; every fenced call resolves thread → binding →
  live lease → capability, revalidated per call. The bound thread is the seat's control
  entry: any client driving the exact thread operates the same seat.
- **Zero-turn threads cannot be resumed by another client** (rollouts materialize lazily)
  — which is why binding happens at SessionStart, not at thread creation. Upstream issue
  drafted, unfiled.
- **`/btw` = `thread/fork` with `ephemeral=true`**: unresumable, no rollout, no store
  record. The wake bridge refuses to bind ephemeral threads; forks and new threads get
  nothing by default.
- **Standing upstream asks:** env policy on remote turn paths; phone/cloud thread
  reachability semantics; per-seat ACL.

## Per-harness status

| Harness | Wake | Status | Key constraint |
|---|---|---|---|
| codex | F3: `turn/start` to the exact bound thread on the canonical daemon socket | Shipped | Capability binding required (env channel dead); ephemeral threads refused |
| claude | F1: owned SessionStart/Stop hooks + `rt-wait-inbox --claude-hook` (exit-2 rewake) | Shipped | Owned hook group; duplicate-arm suppression |
| hermes | F2: in-process plugin arming a supervised watcher, injecting via the native rail | Shipped | Inert without a complete `RT_*` lease; idempotent re-arm |
| grok | F4: pinned primer arms a persistent file monitor | Shipped, B-class by design | Re-arm per resume; leader socket ruled out as a wake channel; ~2 turns/message overhead |
| openclaw | Rework shape: F3 via the user's own Gateway (paired device, `operator.write` only) | Demoted from seat surface; rework documented, cheapest F3 candidate | Live probe must use an isolated Gateway, never the operator's real state root |
| antigravity | — | Parked at T0 | No hook/wake surface; no documented conversation-id discovery |
| herdr | Not a harness — a multiplexer *surface* | Surface only | Recorded as an explicit lease-associated endpoint; never fabricate its env |
| survey cohort (Qoder, Pi/OMP, OpenCode, Kilo, Copilot, Devin/Droid, Kiro, Kimi, Cursor, Mastra) | mapped to F1/F2/F3 families | Paper/lab only — nothing implemented or scheduled | Each gated by principle 1 and the L-gate |
