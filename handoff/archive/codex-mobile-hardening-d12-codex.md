# D12.2/D12.3 mobile hardening implementation handoff

> Status: historical record — D12 implementation report

Branch: `wt/rt-d11`

This handoff describes the D12.2 and D12.3 implementation in the commit that
contains this file. D12.1 remains research-only and was not implemented here.
The branch includes the local merge of `main` requested after D11, has not been
pushed, and keeps the combined release version at 1.3.0.

## Root causes

### Resume path identity

Codex persists each thread's absolute cwd in app-server state, while
Roundtable's seat lease is keyed independently by the selected project root.
The wake bridge already compared cwd during ordinary thread validation, but an
explicit launcher resume could claim and arm a seat before reading its target
thread. A moved worktree also passed through non-strict path resolution as a
path-shaped value instead of being classified as missing. The bind-side guard
prevented the live near-miss from becoming a wrong binding, but the refusal was
late and did not give an explicit recovery operation.

### Hermes recovery

An anchored `rt-hermes` launch claimed its Roundtable seat before Hermes
reported that browser authentication was unavailable inside the TUI. Recovery
therefore required leaving a confusing claimed-seat state and relaunching the
native client. Installed Hermes source confirms two presence-level stores:

- active profile `auth.json`, the provider state source of truth;
- `${HERMES_SHARED_AUTH_DIR:-<hermes-root>/shared}/nous_auth.json`, the shared
  cross-profile Nous OAuth store (default
  `~/.hermes/shared/nous_auth.json`).

No credential contents were read during implementation; only the installed
source and filesystem presence were inspected.

## D12.2 — resume-time seat/thread/path validation

- `_rtcodex.require_thread_project_cwd` is the single cwd proof shared by the
  launcher and wake bridge. It resolves both paths strictly, accepts symlink
  aliases that resolve to one directory, and refuses missing, non-directory,
  invalid, or different recorded paths.
- Every refusal names the recorded cwd and selected seat project, then gives
  both required remedies: resume the thread in its own project, or run the
  named explicit `rt-codex-wake reanchor PROJECT --thread-id THREAD_ID`
  operation.
- `rt-codex resume THREAD_ID` performs `thread/read` inside the Codex service
  preflight lock before the launcher claims or arms the seat. Picker and
  `--last` shapes fail closed for Roundtable because their thread identity is
  unknown before native launch; native `codex resume` remains available.
- Manual bind, SessionStart request binding, stale-fence adoption, handoff,
  bridge resume, and refresh all retain the same strict `validate_thread`
  gate. Focused tests prove bind/adopt/handoff mismatches leave bindings and
  fences unchanged.
- `reanchor` is deliberately explicit. It validates project configuration,
  thread ID, interactive/root-thread identity, sends
  `thread/resume {cwd: PROJECT, excludeTurns: true}`, and validates the
  returned cwd before printing the exact relaunch step.

## D12.3 — Hermes credential preflight

- Anchored Hermes launches resolve the active profile store plus the installed
  shared Nous store before claiming a seat. Either regular file satisfies the
  presence-only gate.
- `HERMES_HOME`, `HERMES_SHARED_AUTH_DIR`, `--profile`, and `-p` are reflected
  in path selection. Profile IDs are validated with the installed Hermes
  on-disk form before any seat claim.
- When both stores are missing, the refusal says to run native `hermes` once
  outside pneu to complete browser login and then relaunch. It also states that
  a present-but-stale credential can still fail inside Hermes.
- `RT_HERMES_SKIP_AUTH_CHECK=1` is the exact intentional bypass. Unanchored
  Hermes does not claim a seat and remains available as the recovery path.

## Verification

All commands ran from the repository root with the shared Python environment:

```text
mamba run -n general pytest -q -x \
  tests/test_mobile_hardening.py \
  tests/test_mobile_hardening_mutation.py \
  tests/test_rt_launcher_lease.py \
  tests/test_rt_codex.py \
  tests/test_rt_codex_lease.py \
  tests/test_seat_lifecycle.py
217 passed in 41.75s

mamba run -n general pytest -q -x
1017 passed, 1 skipped in 987.42s

mamba run -n general python -m compileall -q bin pneu_packaging scripts tests
exit 0

mamba run -n general python scripts/check_public_safety.py
exit 0
```

Condition-level coverage includes private source-copy mutations that disable
the cwd inequality or Hermes missing-file condition. The baseline contracts
pass and each mutant turns the contract red.

## Remaining live gates

This change does not claim a live cross-worktree re-anchor acceptance or a
fresh missing-Hermes-credential login cycle. Those operations mutate real
native session/auth state and remain explicit operator acceptance gates. The
implementation and tests prove fail-closed ordering, state preservation, RPC
shape, and recovery copy without reading or refreshing credentials.
