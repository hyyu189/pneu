# D15(a) — pytest-xdist parallel-safety verdict

Track T2, 1.4 cycle. This supersedes the pre-fix reconnaissance recorded in the
demo clone (`~/Code/pneu-worktree/d15a-xdist/handoff/d15a-xdist-findings.md`),
which could not execute a single parallel test because collection aborted.

## Verdict

**`pytest -n auto` and `pytest -n 4` are safe on this suite after one
one-line change**, which is applied here.

- The only blocker was collection-identity nondeterminism, now fixed.
- No test needed an `xdist_group`, `--dist loadgroup`, or a serial marker.
  Every parallel-safety lead inherited from the earlier reconnaissance was
  checked and killed with evidence (see *Serial classification* below); none
  survived as a real shared-resource collision or a contention flake.
- `pytest-xdist>=3.8,<4` is now declared in `requirements-dev.txt`. It was
  previously used but undeclared.

## The blocker and the fix

`tests/test_mailbox_resolver.py` built a parametrize value at module import:

```python
(str(uuid.uuid4()), "distributed", "layout"),
```

The parameter value becomes part of the collected node id, and each xdist
worker imports the module in its own process, so every worker reported a
different id for `test_malformed_uuid_or_layout_fails_closed`. xdist correctly
refused to run with `Different tests were collected between gw0 and gwN`. No
test body ever executed, so nothing downstream could be measured.

The case needs a *valid* UUID, not a unique one. It is now a frozen literal
(`_VALID_UUID_PARAM`) with a comment explaining why it must stay literal.

`tests/test_collection_determinism.py` makes the fix permanent: it collects the
suite twice in separate processes and requires identical node id lists, which
is the same comparison xdist's controller makes. Reintroducing
`str(uuid.uuid4())` at that call site was confirmed to turn it red, naming the
offending ids.

## Measurements

Runner: `mamba run -n general python -m pytest -q`, CPython 3.12.13,
pytest 9.0.3, pytest-xdist 3.8.0, macOS on 10 logical cores. `-n auto`
resolves to 10 workers.

| Run | Command | Result | Wall clock | 1-min load at start |
| --- | --- | --- | ---: | ---: |
| Pre-change baseline (1081 tests) | `pytest -q` | 1081 passed, 1 skipped | 236.64 s | not recorded |
| Post-change serial | `pytest -q` | 1095 passed, 1 skipped | 301.70 s | 17.2 |
| Post-change parallel 1 | `pytest -q -n auto` | 1095 passed, 1 skipped | 80.23 s | not recorded |
| Post-change parallel 2 | `pytest -q -n auto` | 1095 passed, 1 skipped | 74.94 s | 5.2 |
| Post-change parallel 3 | `pytest -q -n auto` | 1095 passed, 1 skipped | 77.72 s | 16.4 |
| Post-change fixed 1 | `pytest -q -n 4` | 1095 passed, 1 skipped | 88.73 s | 16.4 |
| Post-change fixed 2 | `pytest -q -n 4` | 1095 passed, 1 skipped | 84.70 s | 10.9 |
| **Acceptance, delivered tree** | `pytest -q` | 1096 passed, 1 skipped | 293.78 s | 2.9 |
| **Acceptance, delivered tree** | `pytest -q -n auto` | 1096 passed, 1 skipped | 83.50 s | 4.5 |

The two acceptance rows were run against the exact tree in this commit, after
the last edit; the 1095-test rows above are the audit trials that established
the verdict, taken before `tests/test_collection_determinism.py` was added.

Five consecutive parallel runs, zero failures, zero flakes.

`-n auto` lands at 75–80 s against serial runs of 237–302 s on the same host,
so the speedup is roughly **3–4×** depending on background load. A fixed
`-n 4` lands at 85–89 s and captures most of that win: the suite is dominated
by subprocess spawning and filesystem work rather than CPU-bound test bodies,
which is also why ten workers do not beat four by much.

**Where the extra serial minute went.** The pre-change count (1081) is lower
than the post-change count because track T2 adds 15 tests in the same change,
and they are not cheap serially: `tests/test_journey_mutation.py` alone costs
about 50 s because each case copies `bin/`, `templates/`, and `skills/` and
runs a nested pytest process. 236.64 s + ~58 s of new tests ≈ the 293.78 s
final serial run, so the serial suite did not slow down — it grew. Under
`-n auto` that growth is almost entirely absorbed.

**Load caveat.** This host was simultaneously running other 1.4-cycle worktree
suites; the 1-minute load average at each run's start is recorded above and
ranged from 2.9 to 17 on 10 logical cores. The parallel figures are stable
across that whole range, which strengthens the safety verdict: a
contention-sensitive suite would have flaked at load 17. The 301.70 s serial
row was taken at the highest observed load; the 293.78 s final row was taken
at the lowest, and they agree within 3%.

The `982 s` serial figure carried in the earlier reconnaissance is not
reproducible here and is not used as this track's baseline: it was measured
under different host conditions, and a same-checkout serial run here completes
in a quarter to a third of that time.

## Serial classification

The brief asked for genuinely-serial tests to be marked with reasons. After
executing the full suite in parallel repeatedly, **the correct answer is that
there are none**, and adding markers would be dead scaffolding that claims a
hazard the evidence does not support. Each inherited lead was resolved:

### Per-worker runtime isolation holds — this is why the suite is safe

`tests/conftest.py` does its isolation at *import* time, not in a fixture:
`tempfile.mkdtemp()` becomes `HOME`, `CODEX_HOME`, `RT_RUNTIME_DIR`,
`RT_CODEX_RUNTIME_DIR`, `RT_PROJECTS_FILE`, and `RT_LAUNCH_AGENTS_DIR`, with
`RT_LAUNCHCTL=/usr/bin/false` as a fail-closed guard. Every xdist worker is a
separate process that imports `conftest.py` itself, so each worker gets a
private root before any test module is imported, and each worker's
`pytest_sessionfinish` removes its own root. Import-time isolation is exactly
the property that makes process-level parallelism safe here; a per-test
fixture would have been too late, because modules resolve their runtime
directory at import.

### Fixed `/tmp` strings are inert inputs, not shared resources

- `tests/test_hermes_adapter.py:350-356` derives
  `/tmp/roundtable-hermes-<sha256>.sentinel` and only regex-matches it inside a
  generated command string. The test never creates, reads, or deletes that
  path.
- `tests/test_codex_service_preflight.py` passes fixed `/tmp/...` socket and
  binary paths into daemon status dictionaries, with
  `require_default_socket`, `_validate_service_paths`, `_setup_manifest`, and
  `probe_handshake_detailed` all monkeypatched. Nothing touches the
  filesystem or binds a socket.
- The one test that binds a real `AF_UNIX` socket
  (`tests/test_rt_codex.py:1433-1441`) `chdir`s into its own `tmp_path` first
  and binds the relative name `rtws.sock`, so the path is per-test unique.

### Timing-sensitive tests did not become contention flakes

Roughly twenty modules use `time.sleep`, TTL arithmetic, monotonic deadlines,
lease heartbeats (`DEFAULT_HEARTBEAT_TTL` is 30 s), seat-activation waits, and
`tests/test_grok_soak.py`. These were the plausible contention surface and the
reason repeated trials were run rather than a single green run. They passed in
every parallel trial, including runs taken while the host was carrying other
concurrent work. The 30-second health TTL is generous relative to the
scheduling jitter observed at 10 workers.

### No test writes into the checkout

Nothing under `tests/` writes to the repository root. `ROOT`-relative paths in
the suite are read-only sources, label-only arguments (for example
`ROOT / "fixture.txt"` in `tests/test_public_safety.py`, which is never
created), or copy sources for private mutation trees.

## Cost of the new journey/mutation tests under parallelism

Track T2's `tests/test_journey_mutation.py` spawns a nested pytest process per
mutation against a private copy of `bin/`, `templates/`, and `skills/`. That is
the most I/O- and process-heavy module in the suite. It was included in every
parallel trial above and did not fail or destabilize its neighbours; xdist
distributes its parametrized cases across workers, which is why the suite's
wall clock still improves.

## Recommendation

1. Keep `pytest-xdist>=3.8,<4` declared. Done.
2. Keep the frozen parametrize literal and its guard test. A future
   collection-time `uuid.uuid4()`, `random`, or timestamp in a parametrize
   value reintroduces exactly this blocker;
   `tests/test_collection_determinism.py` now catches it in the serial suite,
   before anyone reaches for `-n auto`.
3. Do **not** add `addopts = -n auto` to `pyproject.toml`. Parallel runs
   interleave output and make a single failure harder to read, and the local
   speedup is not large enough to pay for that by default. Run `-n auto`
   explicitly when you want it.
4. Do not add serial markers or `--dist loadgroup` until a real collision is
   demonstrated. There is no evidence for one today.
