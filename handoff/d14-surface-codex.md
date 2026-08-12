# D14 visible surface implementation

Status: complete on `wt/rt-d14-surface`. The implementation commit containing
this handoff is reported in the Roundtable delivery message.

## Delivered

- Added `pneu worktree open NAME [--seat AGENT] [--surface SURFACE]` through
  the existing `rt-worktree` alias. It resolves only one active registered
  project in the caller's revalidated Git-derived group.
- Added explicit seat selection: a sole configured seat is automatic; several
  configured seats require an exact `--seat AGENT`.
- Encapsulated the first-match surface chain in `_rtsurface.py`: `--surface`,
  `RT_SURFACE`, exact `HERDR_ENV=1`, current/attached-client tmux, then an exact
  print-only fallback.
- Herdr uses layout JSON to choose `right` for wide panes and `down` otherwise,
  creates only a new `--no-focus` pane, parses `.result.pane.pane_id`, and runs
  the absolute pneu launcher there.
- tmux splits the current server or opens a window in an attached default
  session and captures the printed `session:window.pane` target.
- Successful Herdr/tmux command submission writes a private advisory
  `surface.json` beside the selected seat's runtime lease. It is explicitly not
  ownership or liveness evidence. Backend failure writes no new surface record.
- Updated installer/release helper manifests plus README, architecture,
  compatibility, packaged skill, and CLI help surfaces.

The launched command removes inherited seat-fence variables and sets the
selected `RT_FROM`, so opening a worktree from an existing pneu seat cannot
claim the new seat under the caller's identity.

## Verification

- Focused and related matrix: `190 passed`.
- Full suite: `1048 passed, 1 skipped`.
- `mamba run -n general python -m compileall -q bin pneu_packaging scripts tests`.
- `mamba run -n general python scripts/check_public_safety.py` passed.
- `git diff --check` passed.
- Source-tree smoke through `pneu worktree open rt-d14-surface --seat codex
  --surface print` exited 0 and printed the exact absolute launcher command
  without launching or recording a surface.

## Support boundary

This Codex session was not inside Herdr (`HERDR_ENV` was not `1`), so no live
Herdr server was inspected or mutated. Herdr and tmux backend behavior is
covered with isolated fake executables; the compatibility document retains a
real configured-harness promotion gate for each terminal backend.
