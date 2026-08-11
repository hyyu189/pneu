# D11 — worktree convention + phone-spawn fusion (1.3.0)

Owner decisions (Ocean, 2026-08-10, converged over four design rounds). Two
PoC findings from 2026-08-10 are load-bearing constraints — encode them:

- A project-level `WorktreeCreate` hook does not fire in a repo that has not
  passed workspace trust (verified headless). Trust is a prerequisite the
  product must surface in plain language.
- A `WorktreeCreate` hook that exits 0 with no output is a HARD ERROR in
  Claude Code ("hook succeeded but returned no worktree path"), not a
  fallback. Therefore the hooks must NEVER be installed globally — only into
  an enabled project's `.claude/settings.local.json`. Also: `.worktreeinclude`
  is not processed when a WorktreeCreate hook is active; document this.

## D11b — container default for pneu worktrees

`pneu worktree add` default target becomes `<repo-parent>/<repo-name>-worktree/<name>`
(container auto-created; `--path` escape unchanged; basename-must-match rule
unchanged; the container itself must stay outside the repository worktree).
Docs state the doctrine: the main checkout never migrates; the container only
ever holds pneu-created trees. Update skill/README accordingly.

## D11c — phone-spawn fusion

### rc-host (per-project Claude server)

- Expert commands: `pneu rc-host enable|disable|status`, project-anchored.
- `enable`: (1) writes `WorktreeCreate`/`WorktreeRemove` hooks into the
  project's `.claude/settings.local.json` (repo-local, untracked); (2)
  installs a setup-owned launchd agent running `claude remote-control` in the
  project root with worktree spawn mode and the default anchor session;
  (3) verifies workspace trust first and, when missing, prints the one-line
  remedy (run `claude` once in this directory and accept the trust dialog)
  instead of half-enabling. `disable` reverses both, fail-closed.
- Enable is a PROJECT TRAIT: after enabling, ANY `claude rc` run in that
  project (including bare, user-typed) routes through the hooks. A project
  never enabled keeps 100% native rc behavior. There is no wrapped
  `pneu claude rc` command.
- Naming: server session names carry the pneu seat identity convention so
  the phone's session list reads as the project map.
- `rt-doctor`: one health line per enabled project (launchd state, process,
  last registration), report-only.

### Hooks

- `WorktreeCreate`: reads the spawn request, runs the pneu worktree-add flow
  (container placement per D11b, registry + group membership, mailbox), and
  prints the absolute worktree path. Failure modes name their remedy.
- `WorktreeRemove`: routes through the fail-closed pneu removal path (never
  removes a tree with a live seat; advisory otherwise).

### SessionStart adoption

The pneu Claude SessionStart hook additionally adopts an unleased Claude
session running in a REGISTERED pneu project: claim a seat lease and arm the
watcher, so phone-spawned sessions become first-class addressable seats.
Fail-closed: never adopt in unregistered directories, never displace a live
lease, and adoption must be idempotent across resume/compact hook re-fires.

### Launcher: single-card redesign

Replace the sequential interactive flow with one card (TTY only; non-TTY
keeps the current numbered prompts for script compatibility):

- Card: seat list with the cursor on the LAST-USED seat for this project
  (Enter-through: `pneu` ⏎ ⏎ launches it); unavailable harnesses keep their
  D9 plain-language remedies.
- Status block, hard budget of three lines: active worktree count, unread
  mail per seat, `phone access: on/off` with `[p]` as an in-place toggle
  (this is also the manual-enable path for old projects).
- Footer hotkeys: Enter launch · p phone access · w worktrees · ? guide ·
  q quit. The full tutorial no longer auto-prints; `?` and `pneu guide` show
  it.
- First-run welcome card, shown once per newly created project: the tutorial
  offer and the phone-access offer together on one card, both defaulting to
  No, a single Enter skips both. The phone-access copy must say it affects
  only Claude's mobile/web remote sessions for this project — desktop seats
  and other harnesses are untouched.

### Misc

- Project template root `.gitignore` gains `.claude/worktrees/`.

## Constraints and release

- Full suite + compileall + public-safety green; condition-level mutation
  checks on new guards (trust gate, adoption idempotency, hook failure
  paths, card fallback); pty-driven tests for the card, no computer-use.
- No `Claude-Session:` trailers.
- Version 1.3.0. Branch `wt/rt-d11` in `~/Code/rt-d11`. Commit your work,
  report per-part root design + evidence with a handoff pointer via
  `rt-say claude@roundtable-product`. Acceptance (me) will include a live
  phone-side smoke with Ocean's device before release.
