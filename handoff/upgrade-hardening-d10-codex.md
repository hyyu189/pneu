# D10 upgrade-hardening implementation report

Source directive: `20260810T011007Z-claude-to-codex-74095`; implementation brief:
`handoff/upgrade-hardening-d10.md`.

## Delivered

- **D10.1 registry liveness:** an unknown fenced bind request now triggers one
  immediate registry re-read before rejection. Auto-discovery also refreshes on
  registry stat changes and at an exact five-second maximum interval, so a
  long-running watcher learns newly registered worktrees without a restart.
- **D10.2 migration residue:** doctor now reports tombstoned, missing, symlinked,
  and canonical-hash-drift runtime directories with the recorded root,
  canonical root, registry state, reason, and an exact manual cleanup sequence.
  Host preflight remains fail-closed and preserves that same remedy; no runtime
  directory is deleted automatically.
- **D10.3 primer bypass:** native Codex arguments and
  `RT_CODEX_NO_PRIMER=1` now emit a loud stderr advisory that the seat will not
  arm or bind until its first interaction or resume. The shared pneu skill
  documents the same limitation. Bare primer-enabled launches stay quiet.
- **D10.4 resume shape:** handoff output now constructs
  `rt-codex resume <thread-id>`, never the removed `--resume` flag. The official
  [`rust-v0.144.6` source](https://github.com/openai/codex/blob/rust-v0.144.6/codex-rs/cli/src/main.rs)
  defines the positional `Resume(ResumeCommand)` surface. An isolated
  `@openai/codex@0.144.6` binary and the installed `0.147.0` binary both parsed
  `resume --help`; therefore the `0.144.6` floor does not need to move. The new
  launch-path regression checks the constructed argument vector against both
  boundary versions.
- **D10.5 tombstoned drift:** tombstoned registry rows whose old paths now
  resolve through a reused symlink are report-only warnings. Active-row path
  drift and malformed active structure remain fail-closed. A real worktree
  add/remove/re-add regression proves a new live worktree can be registered
  while the historical tombstone drifts.
- Package and app-server client versions are `1.2.1`; README and package
  artifact expectations match.

## Operator registry recommendation

Keep the current repaired tombstoned registry path. The product fix now permits
either the repaired historical value or later path drift for live operations,
but reverting offers no operational benefit and can re-expose strict behavior
in older installed binaries. This batch did not mutate the host registry or its
backups.

## Verification

- Regression-first D10 slice: 13 expected failures plus one existing pass
  before production changes; `14 passed` after the fixes.
- Affected-module suite: `417 passed`.
- Six explicit mutants (registry refresh, periodic boundary, residue,
  primer advisory, resume shape, tombstone drift): all were killed (`7 failed`
  because the resume test spans two boundary versions); restoration rerun was
  `7 passed`, with identical pre/post diff checksum.
- Full suite: `974 passed, 1 skipped`.
- `python -m compileall -q bin pneu_packaging scripts tests`: passed under the
  shared `general` environment.
- `scripts/check_public_safety.py`: passed over 152 tracked files and full
  reachable history.
- `git diff --check`: passed.

No public branch, tag, release asset, installed Codex binary, host registry, or
runtime residue was changed.
