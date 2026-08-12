# D14 — grok / OpenClaw TUI-first survey + docs audit backlog

Date: 2026-08-12. Branch: `wt/rt-d14-research`. Dispatch:
`handoff/d14-research.md`. Method: read-only source and host inspection only —
no seats launched, no live state touched, credentials noted by existence only.
Ruling under test: decision.md 2026-08-12 (seats are interactive TUIs; wake
adapters are background channels INTO a TUI session, never a replacement).

## Q1a — Grok Build

### Current pneu shape (confirmed deviation)

`rt-grok` never launches the native TUI: `bin/_rtlauncher.py` swaps in the
managed `rt-grok-wake` bridge, and `integrations/grok/roundtable/__init__.py`
spawns `grok --no-auto-update agent --no-leader stdio` as a supervised
headless ACP child, drives `session/new` / `session/prompt` itself, and
answers `session/request_permission` (default allow-all per decision.md
2026-08-11). The human never sees a Grok surface. This is exactly the
headless-replacement shape the ruling forbids.

### TUI surface

Bare `grok` is the interactive TUI (Grok Build TUI, v1.0.0 inspected; optional
positional PROMPT seeds the first turn; `grok dashboard` adds a multi-session
view). Distribution: npm trampoline (`@xai-official/grok`) resolving a
compiled arm64 binary into `~/.grok/bin/`; not source-readable, but v1.0.0
ships a complete offline user guide (`~/.grok/docs/user-guide/`, 24 chapters)
that documents every surface below.

### Background channels into a live TUI session

| Channel | Status | Notes |
|---|---|---|
| Background tasks: persistent `monitor` tool, `/loop`, `scheduler_create` | **Confirmed wake channel** (docs: "any of them can wake the agent for a new turn… monitors on events"; silent when nothing to say) | Model-armed: a tool call the session itself must make; dies with the session; `scheduler_create durable: true` persists across sessions (≥60s interval) |
| Leader socket (`~/.grok/leader.sock`, `grok agent leader`, `--leader`) | Exists; **injection into a TUI-attached session unverified** | Docs never state whether a second client may `session/prompt` a sessionId a TUI renders; one-hour live two-client lab would settle it |
| Lifecycle hooks (SessionStart/Stop/PreToolUse/…) | Confirmed **cannot start a turn** in an idle TUI | Stop/SubagentStop can only prolong an ending turn; matches the 2026-08-03 finding |
| Session files (`~/.grok/sessions/…/updates.jsonl` etc.) | Append-only, no external-append contract | Not an injection channel |
| `grok --resume` / `session/load` | Cold-start restore only | No way to pre-stage a turn |
| `agent serve` / headless relay | Headless alternatives, same category as current adapter | Not channels into a TUI |

### Verdict: rework (a)

A TUI-first Grok seat is buildable today with zero upstream asks, accepting a
model-mediated wake armer:

- Seat = native `grok` TUI launched by `rt-grok`, seeded (positional prompt
  and/or project rules) to start one `persistent: true` monitor watching the
  seat maildir `new/`; mail → monitor event → new turn in the live TUI, where
  the drain runs `rt-inbox`/`rt-ack` under Grok's own approval UX.
- Delta vs Claude/Codex: the armer lives inside the session (re-seed on every
  launch/resume); doctor should gain a watcher-liveness probe (Grok's Stop
  hook payload exposes `backgroundTasks`, and sessions/logs evidence the
  monitor). A durable scheduler poll is a coarse fallback rail.
- `rt-grok-wake` (ACP supervisor) demotes to internal/lab machinery.
- If Ocean instead requires harness-level injection with no model-armed
  component (full Codex-app-server parity), the parking condition is: an
  upstream-documented way for an external process to start a turn in a live
  interactive session — nearest candidate is leader-socket multi-client
  `session/prompt`, testable in a short live lab before waiting on upstream.

### D13.2 full-permission ACP policy under this verdict

Moot under rework: work happens in the TUI under Grok's native approval UX,
and the adapter stops answering `session/request_permission` entirely. The
D13.2 allow-all policy (and `RT_GROK_WAKE_MAILROOM_ONLY`) stays relevant only
if the headless mail-drain seat is retained as a non-seat internal tool.

## Q1b — OpenClaw

### Current pneu shape (confirmed deviation, doubly isolated)

`rt-openclaw` execs `rt-openclaw-wake`, which spawns a **private,
project-isolated Gateway** (own HOME/XDG/state root, hooks and cron disabled)
and drives it over the Gateway WebSocket protocol (`agent` +
`agent.wait`, `deliver: false`) from a headless maildir-watcher loop. It not
only replaces the TUI — it deliberately never attaches to the user's own
OpenClaw instance (`bin/_rtlauncher.py` invariant). No human surface exists.

### User surface

OpenClaw is a Gateway hub with multiple client surfaces: chat channels
(Telegram etc. — the primary surface in practice), a web control UI, and a
real terminal client — `openclaw tui` — which attaches to any session key
(`/session agent:<id>:<key>`), renders the chat log and tool cards, and used
the host's Gateway as recently as the installed 2026.5.4 era
(`~/.openclaw/tui/last-session.json`). So a TUI exists, but as one peer
client of the session, not the center of gravity.

### Background channels into a live session

This is OpenClaw's native idiom — sessions are client-agnostic and
multi-client by design:

| Channel | Status |
|---|---|
| Gateway WS RPC `agent` targeting an arbitrary `sessionKey` (with `deliver` semantics) | Confirmed (the current adapter already uses it; stage-1 lab live-validated on 2026.5.4) |
| `openclaw agent --message … --session-id …` CLI | Confirmed (same Gateway client layer) |
| Authenticated webhooks `POST /hooks/agent`, `/hooks/wake` | Confirmed, live-validated in stage-1 |
| In-process plugin/hook system (`registerHook`, `registerGatewayMethod`, …) | Confirmed — the clean long-term home for a maildir watcher |
| Cron + durable delivery queues | Confirmed on host |
| Rendering of an externally injected turn by a live attached TUI/client | **Inferred (strong) from the multi-client session model; needs one live probe** |

### Verdict: rework path exists (a), gated on host/version reality

- Seat = a session on the **user's own Gateway**; the human sits in whichever
  client they prefer (`openclaw tui` for the terminal seat; the same session
  serves their chat surface). "TUI-first" translates cleanly to *inject into
  the session, let the user's attached clients render it* — the closest
  analog of Codex app-server thread injection in this whole survey.
- Wake channel = WS `agent` RPC / `/hooks/agent` against the seat sessionKey,
  or an OpenClaw plugin watching the maildir. The adapter's WS client code
  reuses nearly as-is; what dies is the isolated-Gateway-as-seat supervisor.
- Cost drivers: reversing the "never attach to the personal Gateway"
  invariant demands a deliberate trust-boundary redesign (token scope, which
  sessionKey is the seat, zero mutation of `~/.openclaw` state), and
  TUI-attach liveness has no known probe yet (needs an upstream source check).
- Precondition before any rework lands: the audited source (2026.5.4) is two
  minor lines behind upstream (2026.7.x), and at survey time this host had no
  healthy install to validate against. Unpark/precondition: a healthy
  user-owned OpenClaw at a current release plus a source re-audit confirming
  (i) `openclaw tui` session-attach semantics and (ii) live-client rendering
  of externally triggered `agent` runs.

### D13.2 analog under this verdict

Moot: execution happens in the user's own OpenClaw under its own approval
model (exec approvals live harness-side); the pneu adapter stops owning any
execution policy. (OpenClaw was never in the D13.2 approver role anyway —
only the Grok ACP client was structurally forced there.)

## Q1 summary

| Harness | TUI exists | Injection into live session | Verdict |
|---|---|---|---|
| Grok Build 1.0.0 | Yes (bare `grok`) | Yes — model-armed monitor wake (docs-confirmed); leader-socket client injection untested | **Rework** (adapter demotes to wake channel; lab the leader socket for parity) |
| OpenClaw 2026.5.x→7.x | Yes (`openclaw tui`, one client among channels) | Yes — Gateway `agent` RPC / webhooks / plugins (native idiom); live-render needs one probe | **Rework path exists**; gated on healthy current-version install + re-audit |

Neither harness meets the Antigravity parking precedent (no wake surface at
all); both have concrete injection surfaces today. Antigravity stays parked;
these two should be reworked, not parked — with the OpenClaw start gated as
above.

## Q2 — docs 实然/应然 audit

### Fixed in place (this branch)

- `README.md` Development: dev commands are now environment-neutral (any
  CPython 3.11–3.14 + `requirements-dev.txt`), with the conda/mamba runner
  reduced to one example. Same neutralization in `AGENTS.md` (shared-env
  rule), `docs/release.md` (build commands), `docs/install.md` (preview and
  source-install commands).
- `skills/shared/pneu/SKILL.md` (v8.4.0): trigger description no longer cites
  cmux surface-routing bugs; new PATH-precondition paragraph teaches the
  hook-injected absolute `--fenced` / `--no-nudge` forms as canonical in
  woken turns, bare `rt-*` names only in a shell whose PATH you control
  (principle over spelling); `rt-resolve`/`rt-refresh` row reframed as
  optional-cmux-adapter diagnostics.
- `ROUTING.md` + `templates/ROUTING.md.tmpl`: trigger
  `surface_or_routing_debug` → `wake_or_delivery_debug`; header no longer
  frames routing as "surface" routing; root `ROUTING.md` block renamed
  `roundtable` → `pneu` to match the template and the installed skill name.
- `templates/agents.yaml.tmpl`: dropped the cmux topology / legacy
  keyboard-submit seeding comment. `templates/README.md.tmpl`: snapshot
  comment no longer addresses "cmux users"; states maildir-only delivery with
  `rt-resolve` as optional diagnostics.
- Kept as factually true optional-adapter mentions: `AGENTS.md` product
  constraint ("must work without cmux"), README architecture note,
  `docs/compatibility.md` / `docs/architecture.md` cmux rows,
  `templates/root-gitignore.tmpl` `.cmux/cache/` ignore (functional for cmux
  users, harmless otherwise).

### Backlog — code/content items (list-only, not changed here)

1. **Grok rework** (per Q1a): TUI-first `rt-grok` launcher path; monitor
   seeding contract; demote `rt-grok-wake`; doctor probe for monitor
   liveness; decide leader-socket lab.
2. **OpenClaw rework precondition** (per Q1b): current-version re-audit;
   trust-boundary design for personal-Gateway attach; live-render probe;
   then adapter rework.
3. **Orientation files if reworks proceed**: `HERMES.md` is a 4-line seat
   stub (role + `always_read`). A `GROK.md` / `OPENCLAW.md` would need the
   same stub **plus** the one thing those harnesses lack natively: how the
   seat is woken (Grok: monitor re-seed expectation per launch/resume;
   OpenClaw: which sessionKey is the seat and which client renders it). Do
   not create until the rework verdicts are accepted.
4. **Doctor drift checks for undocumented harness internals** (principle:
   every dependency on an undocumented internal carries a drift check):
   - `thread/name/set` RPC (`rt-codex-wake`): partial — doctor's
     `codex_protocol_probe` exercises the handshake, `thread/loaded/list`,
     and `hooks/list`, and flags CLI/app-server version skew, but the
     side-effectful `thread/name/set` itself is unprobed (deliberately);
     drift would surface only at wake time. Acceptable, but undocumented-API
     status deserves a note in the probe docstring/docs.
   - Claude trust records in `~/.claude.json` (`workspace_trusted`, rc-host
     enable gate): runtime gate only — **no doctor drift check** validating
     the record shape still matches Claude's current format; a silent format
     change would make `rc-host enable` mis-report trust.
   - `CLAUDE_ENV_FILE` semantics (`rt-wait-inbox` fence persistence):
     extensive fail-closed runtime validation — but **no doctor drift
     check** that the variable still exists/behaves on the installed Claude
     version.
5. **AGENTS.md Build-Week-era sections** (flagged, needs product-lead
   wording, not mechanically fixable): Mission still says "current cycle is
   `0.2`"; the review-window freeze section is expired by its own terms.
   Both predate the 1.x product phase.
