# D14 small fixes — Codex delivery

Status: complete on `wt/rt-d14-fixes`. The implementation, regression tests,
release documentation, and CI wiring are included in the same local commit;
the final commit hash is supplied in the Roundtable delivery message.

## Delivered

1. `scripts/isolated_install_smoke.py`
   - accepts one `pneu-<version>-macos.tar.gz` release archive;
   - constructs the child environment from an explicit allowlist with a fresh
     `HOME`, minimal system/bootstrap `PATH`, and explicit
     `ROUNDTABLE_BOOTSTRAP_PYTHON`;
   - installs only to the fresh `HOME/.pneu`, checks the sandbox `pneu version`
     result against `BUILD-METADATA.json`, and compares byte-identical
     before/after fingerprints of the caller's resolved real prefix;
   - is now a required documented release step and runs in the
     `release-artifact` workflow.

2. `bin/_rtrchost.py`
   - waits up to 15 seconds after `bootout` for the exact LaunchAgent label to
     disappear before removing owned state or reporting success;
   - preserves owned files on timeout and reports the exact label plus a
     `launchctl print` inspection remedy;
   - gives the short-retry retirement hint only when the label remains loaded
     while its plist is absent.

3. `bin/rt-wait-inbox`
   - recognizes a partial Claude Stop-hook fence before usage parsing;
   - emits one calm stderr line listing exactly the missing and invalid fence
     variables, returns 0, and never requests async re-wake;
   - preserves the complete managed-fence path and the fully absent
     session-id-to-lease fallback.

## Regression coverage

- Hermetic fake-archive install and strict environment allowlist.
- Real-prefix mutation detection.
- Delayed LaunchAgent retirement, retirement timeout, and enable-hint scope.
- Parametrized partial Stop-hook fences plus complete- and empty-fence paths.

## Verification

- Focused new regressions: `13 passed`.
- Complete affected modules: `70 passed`.
- Full suite, same `general` environment via the module entrypoint:
  `1041 passed, 1 skipped`.
  - Local `pytest 9.0.3`'s console entrypoint does not add the repository root
    to imports; `mamba run -n general python -m pytest -q` is the equivalent
    full-suite invocation used here.
- `mamba run -n general python -m compileall -q bin pneu_packaging scripts tests`:
  passed.
- `mamba run -n general python scripts/check_public_safety.py`: passed.
- `git diff --check`: passed.

No live pneu state or harness configuration was touched, and no public
attribution/history was changed.
