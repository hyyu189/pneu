# roundtable-product · Decisions

> Project owner decisions, reverse chronological.

- 2026-07-30: Launcher-primed first turn for bare Codex launches (Ocean).
  Supersedes the earlier refusal to send a synthetic turn — the zero-turn
  upstream draft explicitly wanted binding "without sending a fake
  prompt, starting a model turn, or injecting synthetic conversation
  history" — because Ocean accepts the visible tradeoff to make the
  central-mail v1 usable for ourselves now. A bare project-anchored
  `rt-codex`/`roundtable codex` launch appends `--` and a fixed
  activation prompt; on the verified standalone Codex 0.146 the
  positional prompt auto-submits, so the real first turn materializes
  the rollout and dispatches SessionStart before model sampling,
  completing the fenced native-thread binding with no human message.
  The primer is a no-action instruction (no tools, no file access, no
  workspace changes, single-word reply) pinned by exact-argv tests,
  because it runs as a genuine model turn and an inviting prompt under a
  full-auto approval configuration would mean unattended workspace
  changes. Explicit native prompts, flags, and subcommands pass through
  untouched and disable the primer; `RT_CODEX_NO_PRIMER=1` is the
  emergency opt-out, deliberately not a configuration system. Per the
  version policy there is no `>=` support claim and no new probe: 0.146
  is the documented tested combination and other releases keep the
  existing live launch gates. Boundaries: `/clear` rebinds on the next
  real prompt; a blank parked seat has no wake promise; `/new` and
  in-process resume/fork stay outside the v1 wake-safe contract — work
  right after clear, or exit and relaunch. The model's reply text is
  never a binding health source; the host runtime record is.
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
