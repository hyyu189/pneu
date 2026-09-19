# pneu architecture — current state (1.3.5)

> Status: as-built truth, rebaselined 2026-08-27 and verified against source
> at that date. What is going to change, and in what order, lives in
> [`target-architecture.md`](target-architecture.md) and
> [`roadmap.md`](roadmap.md); the dated audit behind this rebaseline is
> [`audit/baseline-2026-08-27.md`](audit/baseline-2026-08-27.md).

The service design described below is the shipped implementation, not a
current Desktop attachment guarantee. [Phase 1a evidence](native-admission.md)
separates current native interfaces from that historical route;
[ADR 0005](adr/0005-native-admission-validation.md) forbids restoring it as
a substitute for proving native attachment. The spike changes no runtime path.
The subsequent [explicit native queries](native-inbox.md) reuse the maildir,
lease and capability records with project-scoped native identity hooks.

## Delivery core

Per-project maildir delivery is the fact source. `rt-say` publishes with an
atomic tmp→`new/` rename — that rename is the delivery commit; a ledger
failure after it is a warning, not a rollback. `rt-inbox` drains and lists
logical messages (duplicate ledger/maildir entries with one message id are
one message; malformed mail is listed explicitly, never hidden). `rt-ack`
hard-links `new/`→`cur/` and sends the quiet `ack-<msgid>.md` receipt via a
subprocess `rt-say` deliberately outside its own lock windows — the process
boundary is the lock discipline. Quiet receipts are archived directly, never
re-acknowledged, and never trigger wake. Delivery requires no daemon,
terminal emulator, or network; offline seats keep their mail. pneu is
terminal-emulator independent: Terminal.app, iTerm2, Ghostty, and other
hosts are not transports, only places harness processes happen to run.

## Registry and runtime

The project registry (`roundtable.projects.v2` schema, with v1 legacy
detection and a per-worktree UUID witness in `.roundtable/project.json`)
separates stable project identity from mutable paths. Project identity does
not depend on Git: any directory can be a pneu project. The state root
prefers `~/.pneu` but silently serves a legacy `~/.roundtable` root when
only it exists. Local vs central mail layout is guarded by two-lock
UUID-keyed layout locks; migration is a one-way copy transaction in frozen
`_rtmigrate.py` with recovery records.

Seat leases are host-local and fenced: session identity, `lease_revision`,
owner PID and start-fingerprint liveness. Wake state is subordinate to the
lease — an old hook or watcher cannot release a newer owner without matching
fences. Ephemeral coordination facts (owner, binding, adapter health,
surfaces, alarms) live only in this runtime, never in committed documents.

## Harness adapters (unequal depth, stated honestly)

- **Claude Code** — owned SessionStart/Stop lifecycle hooks arm
  `rt-wait-inbox`, a long-lived fenced watcher: silent lease renewal while
  `new/` is empty, wake only on mail, bounded wake attempts per pending
  generation, `--expect-reply` one-shot sender alarm. Watcher lifecycle is
  journaled (256 KiB-rotated JSONL) with two-layer self-heal (crash-class
  in-place re-arm; planned retirement before the hook timeout), and
  `rt-doctor` renders an `unlogged-death` verdict.
- **Codex** — identity is bound out of band because the shared app-server,
  not the launcher, spawns tool processes: the launcher records a private
  seat capability, SessionStart queues an atomic bind request, the wake
  bridge validates thread/cwd/lease/launch-window and commits the binding,
  and every fenced tool resolves `CODEX_THREAD_ID` → binding → live lease →
  capability record, revalidated per call. The bound thread is the seat's
  control entry: any client driving that exact thread operates the same
  seat; a `/btw` side child, a fork, or an unrelated thread resolves to
  nothing. The shipped setup contains the historical Desktop-to-daemon
  routing mechanism; current Desktop participation is not established by
  that implementation. Its intended machine-wide connection domain relies
  on pneu's lease/fence/binding layer for seat isolation. Do not activate
  the mechanism to work around the native admission gaps in Phase 1a.
- **Hermes** — a packaged lifecycle plugin, inert unless launched with a
  complete fenced environment; arms on session reset; delivers via the
  native background-notification rail.
- **Grok Build** — native TUI with a pinned monitor-arming first turn
  (`RT_GROK_NO_PRIMER` opts out); resume requires one manual re-arm turn.
  The weakest shipped family by design: the armer dies with the session.
- **OpenClaw** — no user-facing seat. `rt-openclaw` is a refusal stub; the
  Gateway adapter is packaged only as internal lab machinery.

Invariants shared by every adapter: launchers resolve real executables and
reject generated PATH shims; core paths never inject keyboard input; a
missing or unhealthy optional integration cannot invalidate a maildir
delivery; fail closed on unsupported harness protocol behavior.

## Entry surfaces

`bin/pneu` (with `roundtable` as byte-identical alias) is the project-first
selector: a pure-view seat card that re-reads registry, mailboxes, leases,
and the rc-host record on every redraw and mutates nothing by being looked
at. Each seat row carries its occupancy (vacant / active with the holder's
locus / stale / ambiguous) from one resolver in `_rtlauncher.py` that the
direct `rt-<harness>` and `pneu worktree open` refusals share; Enter on an
active seat offers jump (surface navigation only), a guarded takeover (a
fenced compare-and-replace of exactly the holder's lease under the card's own
pid, which the exec'd launcher re-enters), or cancel. The screen-level
contract is [`ux/launcher.md`](ux/launcher.md). `pneu worktree add/open`
manages registered linked trees under `<repo-parent>/<repo-name>-worktree/`,
with surface backends (Herdr, tmux, print) writing only advisory navigation
records after a lease is active.
`pneu rc-host enable` is the expert, project-anchored opt-in for Claude
phone/web worktree spawn (per-project LaunchAgent plus that project's
untracked `.claude/settings.local.json` hooks — never global). `rt-doctor`
provides ~19 report-only probes and never deletes state.

## Project scaffold (current, known-intrusive)

`roundtable-init` still writes the legacy scaffold: AGENTS/CLAUDE/HERMES/
GROK/ROUTING marker blocks, README/BRIEF/decision templates, `handoff/`,
`log/`, `skills/` directories, and root `.gitignore` entries. This is safe
in the file-ownership sense but product-intrusive; replacing it with minimal
default adoption plus optional packs is roadmap Phase 3. Until then, new
projects receive more repository scaffolding than the product model wants.

## Test truth

~1,000 test functions across 53 files. Tiers: unit/integration per
subsystem; per-harness adapter/interop-lab/mutation/soak; a journey tier
(real pty, registry, subprocesses; only `os.execv` stubbed) with mutation
evidence; packaging/release tests; architectural-fitness tests with derived
consumer lists. The suite is xdist-parallel-safe on a non-saturated host
with a permanent collection-determinism guard; serial remains the default
read. CI: macOS+Ubuntu × CPython 3.11–3.14, public-safety scan, compileall,
full serial pytest. Release adds the deterministic archive build, hermetic
install smoke, and double checksum verification. Not in CI: live surface
labs and all credentialed harness end-to-end runs (promotion gates in
[`compatibility.md`](compatibility.md)).

## Known limitations of the current shape

- Harness facts and common primitives are duplicated across modules (one
  fact, many copies): harness identity tables, the wake predicate at ~12
  sites in 3 strictness spellings, `agents.yaml` resolution ×6, safety
  primitives ×3–4, launchd management ×3.
- Maildir publish/archive mutation logic is implemented independently in
  `rt-say`/`rt-inbox`/`rt-ack`.
- Roughly half of the Codex subsystem is reusable supervised-service shape
  locked behind Codex nouns (deliberately deferred until a second such
  harness exists).
- `rt-doctor` and `setup.py` dispatch on hand-wired per-harness branches.
- The packaging manifest is a flat exact helper list, making module
  decomposition expensive — fixed first in roadmap Phase 2.
- Some tests pin source shape or documentation wording rather than
  contracts; they are replaced as touched.

These are boundary problems, not reliability problems: the atomic
publication, ownership checks, UUID-pinned identity, layout locks, fenced
leases, setup backup/rollback, and migration recovery under them are strong
and deliberately not rewritten during documentation or boundary work.
