# worktree-open fix — Codex implementation handoff

Status: complete in `wt/rt-open-fix`; no commit and no push.

## Outcome

All four dispatched parts are implemented.

1. Spawned launcher commands are environment-self-sufficient.
   - Installed `rt-worktree` resolves the manifest-owned, digest-verified
     wrapper under `<prefix>/bin/rt-<harness>`.
   - Source-checkout launchers retain their sibling script and explicitly
     inject `ROUNDTABLE_INSTALL_PREFIX`, `RT_RUNTIME_DIR`, and
     `RT_CODEX_RUNTIME_DIR` after inherited lease fences are scrubbed.
2. `worktree open` now means the selected seat lease is active.
   - Herdr/tmux launches wait up to 45 seconds by default; the internal
     `RT_WORKTREE_OPEN_TIMEOUT` knob shortens isolated tests.
   - Timeout is nonzero and reports the surface, exact command, and last seat
     state; it writes no surface record.
   - `--no-wait` is explicitly spawn-only and writes no surface record.
   - Surface writes are fenced to the exact observed session/revision, so a
     replaced lease cannot receive a stale pane reference.
3. The journey tier is founded in `tests/test_open_journey.py`.
   - A fake Herdr backend runs a fake launcher that claims a real isolated
     lease; open succeeds and records the surface only after the claim.
   - The mutation twin exits without claiming; open times out and leaves no
     surface record.
   - Resolver pins cover source prefix/runtime injection and installed wrapper
     selection. A live-seat preflight pin prevents an already-active seat from
     being mistaken for this launch.
   - `scripts/herdr_open_lab.py` is an operator-only named-session lab using a
     disposable Git pair, private runtime, real Herdr backend, and fake Codex
     lease owner. `docs/release.md` documents it. Per dispatch, I did not run
     the lab or connect to the live Herdr server; only `--help` and compile
     checks ran.
4. Codex wake-bridge repair waits for heartbeat takeover.
   - Repair polls for a fresh, build-valid, socket-valid, PID-matched heartbeat
     for up to 15 seconds.
   - Timeout retains fail-closed behavior and now includes observed heartbeat
     PID and age.
   - Tests cover lag-then-match, never-match timeout, and no added wait on the
     healthy fast path.

## Files

Core:

- `bin/rt-worktree`
- `bin/_rtsurface.py`
- `bin/_rtruntime.py`
- `bin/_rtcodex.py`

Tests and lab:

- `tests/test_open_journey.py` (new)
- `tests/test_rt_worktree.py`
- `tests/test_rt_runtime.py`
- `tests/test_codex_service_preflight.py`
- `scripts/herdr_open_lab.py` (new)

Docs/protocol:

- `README.md`
- `docs/architecture.md`
- `docs/compatibility.md`
- `docs/release.md`
- `skills/shared/pneu/SKILL.md`

The dispatch source `handoff/open-fix.md` was read and left unchanged.

## Verification

- Focused final suite:
  `mamba run -n general pytest -q tests/test_open_journey.py tests/test_rt_worktree.py tests/test_rt_runtime.py tests/test_codex_service_preflight.py`
  → `99 passed in 41.19s`.
- Final full suite: `mamba run -n general pytest -q`
  → `1066 passed, 1 skipped in 1120.93s`.
- Compile:
  `mamba run -n general python -m compileall -q bin pneu_packaging scripts tests`
  → exit 0.
- Public safety:
  `mamba run -n general python scripts/check_public_safety.py`
  → `public-safety check passed (176 tracked files, full reachable history)`.
- `git diff --check` → exit 0.
- `mamba run -n general python scripts/herdr_open_lab.py --help` → exit 0.

No live harness seat, live Herdr session, LaunchAgent, public branch, tag, or
release asset was touched.
