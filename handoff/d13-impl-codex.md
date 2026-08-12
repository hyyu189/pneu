# D13 implementation report

Branch: `wt/rt-d13-impl`

Source dispatch: `20260812T005542Z-claude-to-codex-8417` and
`handoff/d13-impl.md`. D13.9 was held until the research handoff
`20260812T014605Z-claude-to-codex-73830` arrived from
`claude@rt-d13-research`; its source commit is `f11d8ab`.

This report describes the implementation commit that contains it. The branch
has not been pushed.

## Delivered

### D13.1 — Codex thread names

- Every successful explicit bind, SessionStart bind-request consumption,
  auto-discovery bind, and stale-binding adoption now calls
  `thread/name/set` with `{agent}@{project-basename}`.
- The rename runs only after the durable binding update. RPC or logging
  failure is cosmetic and cannot roll back or fail a valid binding.
- Before implementation, an isolated scratch thread was created against the
  installed Codex `0.147.0` app server. The exact request
  `{"threadId": <id>, "name": "pneu-d13-name-probe"}` returned `{}`;
  `thread/read` showed the new name. The scratch thread was then deleted. No
  existing fleet thread was read, renamed, resumed, or deleted.

### D13.3 — occupied-seat process location

- Shared launcher refusals now probe the owner PID's TTY and, when tmux is
  installed, match the PID's ancestor chain to `tmux list-panes` output.
- Successful evidence is appended as `tty=<tty>` and
  `tmux=<session>:<window>`. A headless successful TTY probe renders
  `tty=none`.
- Every subprocess has a short timeout. Missing tools, command failures,
  malformed output, and timeouts omit their fragment without delaying or
  weakening the original seat refusal.

### D13.4 — bound Codex thread resume

- The full-TTY seat card reads the private Codex wake-state file read-only.
  A vacant or stale Codex seat with an exact project/agent binding renders
  `(bound thread)`.
- Enter first runs the guarded `rt-codex-wake handoff <project> --thread-id
  <id>` transition, then executes `rt-codex resume <id>`.
- A handoff refusal returns to the card with the final refusal line as its
  notice. Missing, malformed, unsafe, or identity-mismatched wake state is a
  silent fresh-launch fallback.

### D13.5 — in-card seat creation

- When at least one installed harness lacks a project seat, `a` opens a
  one-line chooser. The selected conventional seat block is appended and the
  card immediately re-renders with it selected.
- Missing executables retain their install guidance and are never addable;
  the footer shows `a add seat` only when an addable harness exists.
- The editor requires a parseable `roundtable.agents.v1` mapping and a safe
  append-only layout, preserves every existing byte, writes and fsyncs an
  exclusive temporary file, compares the source identity, atomically
  replaces the file, and fsyncs its directory. Drift is refused without a
  rewrite or temporary-file residue.

### D13.9-fix — phone-spawn adoption

- A valid SessionStart claims or validates its Claude lease before touching
  `CLAUDE_ENV_FILE`.
- The persistence path creates absent parent directories and the env file,
  enforces user ownership and real directory/regular-file types, uses
  `O_APPEND|O_CREAT|O_NOFOLLOW`, and fixes directory/file modes to `0700` and
  `0600` before appending shell-quoted fence exports.
- Env-file failure is degraded: the active lease and current hook-process
  environment remain usable, and stderr records that persistence failed.
- An unmanaged Stop hook can recover only when its payload project and native
  `session_id` match the exact active Claude lease. It reconstructs the four
  fenced `RT_*` values and arms the existing watcher; mismatches remain silent
  no-ops. Partial pre-existing `RT_*` context remains fail-closed as before.

## Documentation and compatibility

- README now documents in-card seat creation and bound-thread resume.
- The package was already versioned `1.3.1`; stale packaging regression
  expectations were aligned with that committed version bump.
- No `Claude-Session:` trailer is used. No public branch, tag, release asset,
  installed binary, host registry, or live seat runtime was modified.

## Verification

- Combined affected-module regression set: `359 passed`.
- D13.9 adoption/Stop-hook set: `51 passed`.
- Seat-card set after refusal and missing-executable coverage: `60 passed`.
- Final full suite: `1032 passed, 1 skipped in 928.66s`.
- `python -m compileall -q bin pneu_packaging scripts tests`: passed under the
  shared `general` environment.
- `scripts/check_public_safety.py`: passed over 167 tracked files and full
  reachable history.
- `git diff --check`: passed.
