# pneu — implementation truth (v1.3.5)

Part of the 2026-08-27 rebaseline. What actually exists and works, verified against source
at 23d630e; support promotion status is tracked separately in docs/compatibility.md.
Sources: docs/architecture.md, docs/compatibility.md, the 1.4 architecture reviews and
crosscheck, and direct code inspection.

## Verified working

- **Delivery core**: per-project maildir is the delivery fact. `rt-say` commits with a
  tmp→`new/` atomic rename (ledger failure after commit is a warning, not a rollback);
  `rt-inbox --fenced --archive-quiet-acks` drains; `rt-ack` hard-links `new/`→`cur/` and
  sends the quiet receipt via a subprocess `rt-say` deliberately outside its lock windows
  (the process boundary IS the lock discipline). No daemon, terminal emulator, or network
  required for delivery.
- **Registry**: `roundtable.projects.v2` schema with v1 legacy detection and per-worktree
  UUID witness (`.roundtable/project.json`). State root prefers `~/.pneu` but silently
  serves a legacy `~/.roundtable` root when only it exists (undocumented compat behavior —
  live installs still run on `~/.roundtable`). Local vs central mail layout guarded by
  two-lock UUID-keyed layout locks; migration is a one-way copy transaction in frozen
  `_rtmigrate.py`.
- **Seat lease/fencing**: host-local fenced leases (PID + start-fingerprint liveness,
  `lease_revision` fencing) under the runtime root; `RT_*` identity exported by the
  launcher.
- **Wake paths**: Claude (SessionStart/Stop hooks arm `rt-wait-inbox`, bounded wake
  attempts, `--expect-reply` alarm), Codex (bind-request queue → wake bridge validates
  thread/cwd/lease/launch-window → capability chain in `_rtcapability.py`, revalidated per
  use), Hermes (packaged lifecycle plugin, inert without a complete fenced environment),
  Grok (native TUI exec with pinned monitor-arming primer; `RT_GROK_NO_PRIMER` opt-out).
  OpenClaw: `rt-openclaw` is a literal refusal stub; the string is fully removed from the
  seat surface; lab machinery remains under `integrations/openclaw/`.
- **Already-merged 1.4 work**: capability binding; canonical daemon join
  (login agent + `CODEX_APP_SERVER_USE_LOCAL_DAEMON`, doctor checks
  codex-hosts/daemon-join/headroom); watcher lifecycle JSONL (256 KiB rotation) with
  two-layer self-heal (`RT_WATCHER_SELF_HEAL` crash-class re-arm,
  `RT_WATCHER_MAX_LIFETIME_SECONDS` planned retirement) and the doctor `unlogged-death`
  verdict; OpenClaw demotion; journey/xdist test tiers; `tests/_kit`.
- **Launcher**: `bin/pneu` project-first selector (`bin/roundtable` byte-identical alias);
  `_rtlauncher.py` does executable resolution (rejects cmux shims), seat claim, per-harness
  launch.
- **Worktree / rc-host / mobile**: `rt-worktree` lifecycle with surface backends
  (herdr/tmux/print); `rt-rc-host` per-project phone-host (trust-gated, owned hook groups
  in `.claude/settings.local.json`, UUID-named LaunchAgent). Tested; the live phone-side
  spawn remains an open promotion gate.
- **Diagnostics**: `rt-doctor` ~19 report-only probes (never deletes state);
  `roundtable-smoke` isolated terminal baseline; hermetic install smoke in scripts/.

## Test truth

~1,000 test functions / 53 files (last xdist acceptance: 1,114 passed, 1 skipped). Tiers:
unit/integration; per-harness adapter/interop-lab/mutation/soak; journey tier (real pty,
real registry, real subprocesses, only `os.execv` stubbed) with mutation evidence (8
mutations, each proven to redden its journey); packaging/release; architectural-fitness
tests with derived consumer lists. xdist parallel-safe to `-n 16` (~3-4× speedup),
determinism guarded permanently; `-n auto` deliberately not the default. CI: macOS+Ubuntu ×
Python 3.11–3.14, public-safety check, compileall, full serial pytest. Release adds the
deterministic archive build, hermetic install smoke, double checksum verification, and an
isolated install/setup/remove exercise; uploads a 14-day candidate, never publishes.
Not in CI: live Herdr lab, all credentialed harness E2E (promotion gates).

## Known debt (ranked; crosscheck authoritative)

1. **One fact, many copies**: harness identity in ~5–13 tables; wake predicate at 12 sites
   with 3 spellings; `agents.yaml` resolution ×6; safety primitives ×3–4; launchd
   management ×3. Silent-drift risk; no test fails when a copy is missed.
2. **Maildir mutation duplication**: publish and archive logic implemented independently
   in `rt-say`/`rt-inbox`/`rt-ack` (top safety item — a hardening can miss a commit path).
3. **Generic machinery named Codex** (~half the 5.7k-line Codex subsystem is reusable
   shape locked behind Codex nouns) — deliberately deferred until a second
   supervised-service harness proves the boundary.
4. **No abstraction in `rt-doctor`** (85 hand-wired report sites) **and `setup.py`**
   (~37 harness string literals).
5. **Project-ancestor walk ×5** — partially consolidated; `rt-wait-inbox` and
   `rt-stop-gate` still carry private copies; no single `_rtlib` primitive.
6. `rt-stop-gate` ships but belongs to a superseded hook generation (ruling open).
7. `rt-say` still contains the ~391-line legacy cmux keyboard dispatcher (extraction
   agreed, not done).
8. OpenClaw lab adapter lacks the outside-project isolation-root guard (recorded,
   deliberately unfixed while parked).
9. Frozen `_rtmigrate.py` imports 12 private `_rtlib` symbols — permanent facade tax.
10. Two mega test files (~5.5k and ~3k lines) remain unsplit.

Fixed and verified in-tree (the 1.4 review bodies still describe them as open — read the
header status line): doctor OpenClaw family mapping; setup manifest forward-compatibility
(`unknown_harnesses`); manifest-derived import smoke (8 managed helpers, review says 7).

## Known doc drift

The `~/.pneu`-vs-`~/.roundtable` legacy fallback is undocumented; the 1.4 review bodies
re-report closed defects; README/release docs still show environment-specific `mamba run`
incantations; research/README.md omits the Paseo and Warp files that sit beside it.
Everything else checked (refusal stub, capability chain, daemon-join, watcher log
constants, primer opt-outs, version floor) matches the docs exactly.
