# D12 — research: Codex mobile pairing; implement: resume validation + Hermes preflight

> Status: historical record — D12 dispatch; shipped in 1.3.0

Owner decisions (Ocean, 2026-08-11): D12.1 is RESEARCH ONLY (no code).
D12.2 and D12.3 are full implementations, sequenced behind D11 on
`wt/rt-d11` (combined release 1.3.0).

## D12.1 — RESEARCH ONLY: pairing phones against OUR Codex daemon

Context: the ChatGPT desktop app hosts its own codex app-server; phone
sessions there never execute user hooks (proven empirically) and cannot be
adopted. The standalone codex source exposes remote-control
enable/disable/status + pairing + controller RPC methods on the app-server
protocol, plus a `codex remote-control [start|stop|pair]` CLI that manages
"an app-server daemon".

Questions to answer from the 0.147.0 source (read-only survey, same rigor
as the worktree survey):

1. Does `codex remote-control start` spawn/own its own daemon process, or
   can it attach to an already-running app-server on an explicit socket?
   Exact daemon-discovery/ownership logic, and the collision behavior if
   our launchd-owned daemon already holds the control socket.
2. Can the remote-control enable/pairing RPC methods be invoked directly on
   OUR running app-server (e.g., by the wake bridge) without the
   `remote-control start` supervisor? What state do they persist, where,
   and does enabling change anything our identity gates
   (require_daemon_identity, fingerprint checks) inspect?
3. Pairing/controller lifecycle: code expiry, controller storage,
   revocation, how a paired phone addresses threads (does it see ALL
   threads on the daemon — i.e., our whole fleet's seats — or a scoped
   subset? security implications for fenced seats).
4. Recommended pneu integration design + risk register, including the
   fail-closed rollback story. Explicitly flag anything that touches the
   single-socket danger zone.

Deliverable: `handoff/archive/codex-mobile-pairing-design.md` committed to main
(no session trailers), rt-say pointer with the go/no-go recommendation.

## D12.2 — IMPLEMENT: resume-time seat↔thread↔path validation

Live near-miss (2026-08-09 upgrade): an MMF thread was almost resumed in
the p1a-quant worktree; only the bind-side identity check caught it. The
Codex source survey confirmed threads persist their cwd and resume defaults
to the rollout's old path.

- Before a resumed Codex seat binds (launcher resume path and
  `rt-codex-wake` bind/adopt/handoff), read the thread's recorded cwd from
  the app-server and require it to canonicalize to the seat's project
  root. Mismatch ⇒ refuse with a plain-language error naming both paths
  and the two remedies (resume it in its own project, or explicitly
  re-anchor with an operator command you name).
- Cover: moved/renamed worktrees (recorded cwd no longer exists), symlink
  aliases (canonicalize both sides), and the happy path (exact match binds
  as today).
- Condition-level tests + mutation checks on the comparison.

## D12.3 — IMPLEMENT: rt-hermes credential preflight

Live incident (2026-08-10): a Hermes self-update invalidated the Nous
portal credential; the pneu-launched seat showed a missing-credential state
that cannot be fixed inside the TUI, costing a confusing multi-step
recovery. Upstream bug filed separately (hermes-agent#83577).

- `rt-hermes` preflight: before claiming the seat, check the Hermes
  credential file(s) (`~/.hermes/shared/nous_auth.json`; confirm the exact
  set the installed Hermes reads). Missing ⇒ refuse with the remedy in
  plain language: run `hermes` once outside pneu to complete the browser
  login, then relaunch the seat. `RT_HERMES_SKIP_AUTH_CHECK=1` bypasses.
- Presence check only — validity cannot be cheaply verified; say so in the
  refusal wording (a present-but-stale credential will surface inside
  Hermes itself).
- Tests: missing/present/bypass conditions.

## Constraints and release

- D12.2/3 start only after D11 is green and reported; same branch
  `wt/rt-d11`, combined release version stays 1.3.0.
- Full suite + compileall + public-safety; no `Claude-Session:` trailers.
- Report root causes + fixes with a handoff pointer via
  `rt-say claude@roundtable-product`; I accept D11+D12 together.
