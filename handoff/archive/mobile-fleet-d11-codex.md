# D11 mobile-fleet implementation handoff

> Status: historical record — D11 implementation report

Branch: `wt/rt-d11`

This handoff describes the implementation in the commit that contains this
file. It was produced from the clean D11 worktree and has not been pushed.

## D11b — managed worktree container

Root design:

- `pneu worktree add NAME` now defaults to
  `<repo-parent>/<repo-name>-worktree/NAME`.
- The default container is created only after confirmation. Dry-run and a
  declined prompt remain read-only; a failed creation removes a newly created
  empty container.
- The container and target are checked as plain, non-symlink paths outside the
  current repository worktree. Explicit `--path` behavior and the
  basename-must-match rule are unchanged.
- A hidden `--porcelain-path` contract gives the Claude WorktreeCreate hook
  exactly one stdout path without weakening the human-facing restatement.

Evidence:

- Focused worktree coverage exercises default placement, explicit paths,
  dry-run, confirmation, cleanup, and removal.
- The RC-hook integration test creates and registers a real temporary
  `anchor-worktree/phone-task` linked tree through the source command.

## D11c — per-project Claude phone host

Root design:

- `pneu rc-host enable|disable|status` owns one UUID-named per-project
  LaunchAgent and only the enabled project's untracked
  `.claude/settings.local.json` WorktreeCreate/WorktreeRemove groups.
- Enablement validates the exact active registry row, one Claude seat, Git,
  the resolved Claude executable, and the exact Claude workspace trust record
  before its first write. Missing trust reports the native one-line remedy.
- The LaunchAgent runs `claude remote-control --spawn worktree`, creates the
  anchor session in the project root, and names the server/session prefix with
  `<seat>@<project>`.
- State records hook ownership, plist digest, project UUID/group, and the last
  adopted phone registration beside the project registry. Re-enable is
  idempotent; hook/plist drift makes enable and disable fail closed.
- Bootstrap failure restores pre-existing settings bytes and mode and removes
  partial plist/state. Disable unloads the exact job first, then removes only
  recorded fragments, with rollback if file mutation fails.

Hook lifecycle:

- WorktreeCreate reads the official hook payload, requires enabled state,
  routes through `rt-worktree add --yes --porcelain-path`, verifies the
  returned directory is one registered pneu project, and prints one absolute
  path. Zero or multiple stdout paths are hard errors.
- WorktreeRemove resolves one same-group registry row and routes through the
  normal fail-closed removal path. A live seat produces an advisory success
  while leaving the tree in place.
- Project templates ignore `.claude/worktrees/`. Documentation explicitly
  notes that Claude does not process `.worktreeinclude` when the custom create
  hook is active.

Official contract references used during implementation:

- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/worktrees
- https://code.claude.com/docs/en/remote-control

The locally inspected Claude Code 2.1.227 CLI exposed the required
`remote-control --spawn worktree`, naming, and anchor-session flags. This is
contract evidence, not the pending phone-side acceptance.

## SessionStart adoption

Root design:

- The existing global Claude SessionStart command stays inert outside an exact
  active registered project with one Claude seat.
- An unleased startup/resume/clear/compact event acquires the UUID layout lock,
  validates project registration, and claims the seat using the Claude parent
  PID and native session ID.
- A different live lease or ambiguous ownership is never displaced. Repeated
  resume/compact events for the same owner/native session reuse the original
  lease revision.
- The fence is appended to the current-user, regular `CLAUDE_ENV_FILE`; a
  persistence failure releases a newly claimed lease. Phone-host registrations
  are recorded only inside the enabled anchor's repository group.

## Launcher, doctor, and lifecycle ownership

- A full TTY gets one seat card with the last-used seat selected, exactly three
  status lines, and `Enter`, `p`, `w`, `?`, `q` controls. The terminal state is
  changed with non-flushing immediate termios operations, preserving fast
  type-ahead.
- New projects get one ignored `launcher.json` welcome flag. One Enter skips
  the default-No tutorial and phone offers; the copy scopes phone access to
  Claude mobile/web sessions in this project only.
- A line-oriented/non-TTY output stream keeps the numbered seat selector, and
  the full guide no longer prints automatically.
- `rt-doctor` prints one report-only health line per enabled project with
  launchd, process, and last-registration state.
- Version and packaging surfaces are 1.3.0 and include `_rtrchost.py` plus
  `rt-rc-host` in the wheel/release inventory.
- Claude onboarding removal and package uninstall refuse while any rc-host
  state remains, preserving the still-installed reversal command.

## Verification

All commands ran from the D11 repository root with the shared Python
environment:

```text
mamba run -n general pytest -q -x
998 passed, 1 skipped in 949.83s

mamba run -n general python -m compileall -q bin pneu_packaging scripts tests
exit 0

mamba run -n general python scripts/check_public_safety.py
public-safety check passed (160 tracked files, full reachable history)
```

Focused evidence additionally includes:

- project-local enable/idempotence/disable, trust no-write, drift refusal, and
  bootstrap rollback;
- real worktree hook creation plus live-seat removal deferral;
- registered adoption idempotence, unregistered/live-owner refusal, and lease
  rollback on environment persistence failure;
- private-source condition mutations for trust, native-session identity,
  zero-path hook output, and the two-stream TTY fallback;
- PTY single-card Enter-through, in-place phone toggle, first-run single-Enter,
  and numbered fallback tests.

## Remaining acceptance gate

Do not promote the phone-host path from release-candidate status based on these
fixtures or local CLI inspection. Acceptance still requires Ocean's real
phone-side smoke: spawn, registration/addressability, message round trip,
live-seat removal refusal, and clean disable with no project hook or
LaunchAgent residue.
