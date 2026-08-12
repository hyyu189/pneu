# roundtable-product · Decisions

> Project owner decisions, reverse chronological.

- 2026-08-12: **Seats are interactive TUIs** (Ocean). A pneu seat launched
  by a user — from the launcher card or an explicit seat command such as
  `rt-grok` — is an interactive TUI session. pneu exists to build a network
  between those TUIs; wake adapters are background channels into a TUI
  session, never a replacement for one. Headless or oneshot agent processes
  remain legitimate inside a harness (subagents, teammates, internal
  automation) but are not user-facing seats. Claude/Codex/Hermes seats
  already follow this; the Grok ACP supervisor and the OpenClaw Gateway
  adapter predate the ruling and are recorded deviations — rework them
  TUI-first or park them honestly (tracked in D14). Every future harness
  integration arrives TUI-first.
- 2026-08-11: **The communication layer is not a permission gate** (Ocean).
  Harness-side permission models own execution policy; pneu adapters default
  to full permission on wake turns. The Grok ACP supervisor — the one adapter
  structurally forced into the approver role because the ACP client answers
  `session/request_permission` — now approves by default with an audit trail;
  `RT_GROK_WAKE_MAILROOM_ONLY=1` restores the fenced mailroom-only policy.
  Claude/Codex/Hermes/OpenClaw keep approval inside the harness and need no
  change. Support-claim discipline unchanged: Grok work-seat promotion still
  requires a live write-action wake E2E.
- 2026-08-07: **Quiet wake** (Ocean). The periodic empty-inbox heartbeat
  wake is retired in 1.1.0: an armed watcher is long-lived, renews its lease
  silently, and wakes the model only for mail or a configured reply alarm.
  The one-shot `rt-say --expect-reply <duration>` deadline alarm (cleared by
  the quiet `ack-` receipt, fired at most once) is the only timer edge.
  Runtime residue of retired projects is reclaimed fail-closed at
  `worktree remove`; `rt-doctor` reports orphans without deleting.
- 2026-08-07: **No session URLs in this public repository** (Ocean). The
  public-safety gate forbids private Claude session URLs anywhere in
  reachable history. Commit messages here must not carry `Claude-Session:`
  trailers (`Co-Authored-By` remains fine). The two pre-1.1.0 handoff
  commits that violated this were rewritten before any external consumption;
  v1.0.0 and earlier history was not touched.
- 2026-08-07: **Onboarding never dirties a linked worktree** (Ocean).
  `roundtable-init` skips orientation marker-block appends when the target
  is a linked Git worktree — those files belong to the repository's main
  checkout. Missing files are still created; standalone projects are
  unchanged.
- 2026-08-06: Product name is **pneu** (Ocean). From Paris pneumatique
  slang where "un pneu" is the message itself; dual backronyms adopted:
  "Project-Native Envelope Utility" for technical contexts and "Postal
  Network, Entirely Unplugged" as the tagline. Chosen from a four-round
  naming search (western creative, CJK imagery, deep Chinese, Wade-Giles
  military/pastoral/commercial) totaling ~140 candidates and 36 real
  collision checks; 18 finalists cleared PyPI+npm+brew and both search
  gates. Runners-up recorded for history: chienmo 阡陌, chidu 尺牍,
  ekirei 駅鈴, restante. The rename ships as release 1.0.0 in a
  dedicated worktree after the in-flight grok and worktree-cmd branches
  merge; rt-* tool names and RT_* env vars are retained in 1.0.0 as
  pneu's tool prefix (deployed hook paths and permission allowlists
  reference them; migration to a pn- prefix is a later major at most).
  The 0.3.0 version number is skipped as a public release: its content
  rolls into 1.0.0.
- 2026-08-05: Antigravity harness parked at T0, workstream closed (Ocean).
  Hands-on validation of Antigravity CLI (`agy` 1.1.10) confirmed no
  startup/resume hook and no external wake surface, so automatic wake is
  not currently buildable and no support claim is made; T0
  (durable mail + manual drain) is architecturally free but will not be
  claimed publicly on its own. Gemini CLI was dropped entirely (no longer
  actively maintained). Research and stage-1 evidence are archived in
  handoff/antigravity-harness-research-2026-08-03.md and
  handoff/antigravity-stage1-2026-08-04.md; revisit only if upstream
  ships an early lifecycle hook or wake API.
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
