# Codex canonical daemon and thread-context proposal

Status: product proposal only. No code changes. Builds on
`handoff/codex-env-channel-proposal.md` (transport gap),
`handoff/btw-thread-semantics.md` (thread lifecycle), the 08-14 adversarial
lock review (writer-lock mechanics), and the live phone-routing capture
(zero daemon traffic during a failing phone open).

## Problem 1 — dual-host conflict

Codex 0.147 enforces a cross-process single writer over every persistent
thread history (OS advisory lock per thread id under one `CODEX_HOME`). Two
app-server hosts now coexist on this machine:

- the Desktop app's private stdio app-server, and
- the pneu shared daemon on `~/.codex/app-server-control/app-server-control.sock`.

Single-writer makes this split explicit: a live thread belongs to exactly one
host; the other host can only cold-take it after the owner exits. Consequences
observed this cycle: threads reachable from the phone only when they live in
the cloud-registered host, wake bridge coverage limited to daemon-hosted
threads, and operator confusion about which conversations are "real" seats.

### Proposal A — one canonical host

Make the pneu/Codex local daemon the canonical app-server host for this
machine:

1. Desktop joins as a client through its built-in
   `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` switch. Per the 0.147 review, a
   second client of the same daemon rejoins a running thread as an additional
   subscribed connection (one engine, multiple views), so Desktop, TUI seats,
   and the wake bridge can coexist on one thread set without writer
   conflicts.
2. Bare `codex` already auto-discovers the local daemon when no
   non-replayable overrides are present. Leave that behavior as is.
3. Explicitly do NOT hijack commands globally (no wrappers, aliases, or PATH
   shims around `codex`). Joining the canonical host is opt-in per surface
   through supported upstream switches only.

Benefits: one writer domain (no cross-host lock fights), full wake-bridge
coverage, one thread inventory for doctor, and a precondition for making
phone reachability coherent (see upstream asks).

Risks and mitigations:

- Single point of failure widens to Desktop. Mitigation: existing launchd
  keepalive, bridge health checks, and doctor coverage already treat the
  daemon as managed infrastructure.
- Resource contention is real: during the demo shoot the shared daemon hit
  `Too many open files` under an OCR batch and a collab agent-thread cap.
  Mitigation: raise file-descriptor limits in the managed plist and add a
  doctor headroom check (fd usage, live thread count) before promoting
  canonical-host status.
- Version coupling: Desktop-bundled expectations versus the standalone
  daemon binary. The existing version floor plus live protocol probe and the
  CLI↔daemon consistency gate already refuse mismatches; keep them as the
  gate for this too.

### Trust-domain statement (must ship with Proposal A)

The shared daemon's remote/connection domain is machine-wide, not Roundtable
seat isolation. Any client of the control socket can see and drive any
thread (the D12.1 finding; no per-seat ACL upstream). Seat isolation remains
pneu's own layer — leases, fences, and bindings above the daemon. Adding
Desktop as a client widens the client set on a single-user machine; that is
acceptable, but documentation and doctor output must say it plainly rather
than imply the daemon enforces seat boundaries.

## Problem 2 — client→daemon thread-context gap

Plain mail tools need no seat context: `rt-say`/`rt-inbox`/`rt-ack` are
maildir-only and can infer the sender from `CODEX_THREAD_ID` plus the
registry. Only seat-state operations need the lease identity
(`RT_SESSION_ID`/`RT_LEASE_REVISION`): `--fenced` forms and
`--expect-reply` (the reply alarm writes per-seat runtime state). The
0.147 explicit-remote TUI does not reliably forward
`shell_environment_policy.set` overrides into daemon thread config, so
daemon-executed tools lack that identity — breaking fenced operations,
reply alarms, and Herdr/pane environment for codex-as-operator.

### Two candidate fixes

**B1 — local capability broker over the existing thread↔seat binding.**
Seat-state tools, when lease variables are absent but `CODEX_THREAD_ID` is
present, resolve identity through the wake-bridge binding
(thread id → project/agent/session/lease revision) and validate against the
live lease before acting; stale bindings refuse. Pane/surface context comes
from the launcher-written surface record (reuse of the D14 `_rtsurface`
detection and `surface.json` schema), consumed by explicit endpoint
arguments or a thin `rt-env` wrapper. Properties: no upstream dependency,
works on stock 0.147 today, enforcement lives in pneu code for pneu tools,
revocation is automatic (lease revision change invalidates the broker path).

**B2 — allowlisted per-thread context forwarding at thread start/resume.**
Upstream change: serialize the parsed `shell_environment_policy` (or an
explicit allowlist field) into the remote thread-start/resume/fork config
map, with round-trip tests. Properties: true environment fidelity inside
tool processes; upstream-gated; snapshot semantics (relaunch to refresh).

**Recommendation: B1 now, B2 as the upstream ask, and keep B1 as the
permanent backstop.** Even after B2 lands, broker-side live-lease validation
is stronger than trusting whatever environment a long-lived thread captured
at start. The two compose: B2 supplies convenience context (locale, TERM,
surface hints); B1 remains authoritative for seat-state authority.

Out of scope, preserved as is: the 0.147 writer lock stays untouched; plain
mail tools stay fence-free; per-turn environment payloads stay rejected
(`turn/start` has no such field and wake turns must not diverge from
interactive turns).

## Staged acceptance

1. **B1 fenced ops**: from a daemon-executed codex turn carrying only
   `CODEX_THREAD_ID`, `rt-ack --fenced` and `rt-say --fenced --expect-reply`
   succeed against the exact active lease; stale binding or rotated lease
   refuses; two concurrent seats in different projects never cross. Proven
   on a stock release artifact, not fixtures.
2. **B1 surface context**: a codex seat drives a herdr surface using the
   launcher-recorded endpoint explicitly; markers confirm no cross-seat
   leakage; missing surface record degrades to a clear refusal, never a
   guessed socket.
3. **Canonical host**: Desktop joined via the supported switch; doctor
   reports a single-host thread inventory and resource headroom; zero
   writer-lock conflicts across Desktop, TUI seats, and bridge wakes on the
   same thread set over a soak window.
4. **Upstream round**: file the asks below; on delivery, add B2 forwarding
   behind the same acceptance gates as the env-channel proposal.

## Upstream asks

1. Carry `shell_environment_policy` (or an explicit per-thread allowlist)
   through remote thread start/resume/fork (B2 enabler; also the
   env-channel proposal's preferred fix).
2. Document or expose how a thread registers for phone/cloud reachability —
   the live capture showed failing phone opens generate zero local traffic,
   so reachability is decided by cloud-side session routing that
   daemon-created threads never join.
3. Per-connection or per-seat ACL on the app-server (reopens D12 phone
   pairing safely).
