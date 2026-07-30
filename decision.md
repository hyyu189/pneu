# roundtable-product · Decisions

> Project owner decisions, reverse chronological.

- 2026-07-29: Migration robustness frozen at the accepted state (Ocean).
  Remaining `_rtmigrate.py` defects are parked, not fixed — even known
  ones — unless they break one of the three load-bearing properties:
  the exclusive layout lock against live writers, the verified
  pre-cutover backup, or the atomic registry flip. Defects in those
  three are reported to the product lead, still not unilaterally
  expanded. Rationale: the heavy machinery (hold-projection admission,
  budgets, dual-track recovery records) was sized for fleet scale, while
  the non-empty live-mailbox migration cohort is exactly this team's own
  machines; new users' mailboxes are empty and hit only the degenerate
  path. Standing calibration going forward: a surface whose blast radius
  is only ourselves needs the three properties above, not fleet-grade
  admission machinery; the M2 scope displacement (addressing gave way to
  layout serialization while §4 went unbuilt) is the recorded cost of
  over-applying the bar. Backlog, not current work: post-v1-merge new
  projects should mint the central layout directly at init so migrate
  serves only legacy non-empty mailboxes.
- 2026-07-23: Rename working name "Orca" rejected (Ocean). stablyai/orca —
  a YC-backed Electron ADE with an `orca` CLI and an orchestration
  subsystem adjacent to Roundtable's messaging layer — occupies the name
  in this exact category, and GNOME Orca owns the `orca` command on
  Linux; the search and trademark positions are unrecoverable. The rename
  search stays open under hard constraints (unclaimed command name on
  brew/apt/PyPI/npm, clean "<name> agent"/"<name> CLI" search page, no
  active developer-tool brand collision); BRIEF roadmap #5 updated.
- 2026-07-23: `rt-wait-inbox` self-daemonization rejected (findings #5).
  Claude's wake contract needs the hook's own process to exit 2
  (`asyncRewake`); a self-daemonized watcher detaches from the hook process,
  loses that exit-status channel, and can never deliver a wake — it converts
  wake-on-mail into never-wake and orphans processes outside the
  `expected_watcher_pid` fence. Arming stays owned by harness-native
  lifecycle hooks; model turns must never arm (documented in SKILL.md and
  docs/compatibility.md). Backlog: a product-owned "arm this seat"
  affordance if a future harness exposes a native background-watcher API.
- 2026-07-23: Zero-turn arming (findings #6) documented, not redesigned.
  SessionStart hook output alone does not start a model turn, so a
  zero-interaction seat holds mail until its first turn; workaround is one
  initial interaction. Backlog: zero-turn arming needs a harness-native
  first-turn mechanism (e.g. launcher-primed initial turn); redesign
  deferred.
- 2026-07-21: Post-hackathon transition (Ocean). Same GitHub repo; `main` and
  `v0.1.8` frozen through the review window (conservatively to 2026-08-12).
  Development continues in this worktree on `product/0.2` (local-only for now).
  Phase leadership moves to Claude (architecture, implementation, review,
  release); Codex and Hermes become peer reviewers/specialists on request.
  Build Week attribution stays exactly as documented in PROVENANCE/CREDITS.
- 2026-07-21: 项目从 roundtable-init bootstrap 创建。
