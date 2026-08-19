# D14 OpenClaw — source audit against 2026.7.1-2

> Status: current — the latest OpenClaw source audit (2026.7.1-2); gates any future TUI-first rework

Date: 2026-08-17. Method: Ocean authorized a local install for analysis
(2026-08-17 evidence ruling: research conclusions must rest on primary sources —
official docs or source, open or installed for analysis).

Installed `openclaw@latest` = **2026.7.1-2 (0790d9f)** via `npm i -g`, into the
user-owned `~/.npm-global` prefix. The npm package ships its full `src/` and
`docs/` trees, so this audit reads the vendor's own shipped protocol
documentation and source layout at the exact installed revision.

**No Gateway was started and no live test was run** — see §4. Everything below
is documentation- and source-level evidence at [D] strength. Per the project's
support-claim discipline that is not a support claim; it is enough to scope the
rework and to retire three questions that were previously open.

Prior state: `handoff/archive/d14-tui-first-survey.md` audited **2026.5.4** and left
three blockers. All three are now answered, and two of them differently than the
survey assumed.

## 1. The decisive question is answered, and it is documented rather than inferred

D14 recorded "rendering of an externally injected turn by a live attached
TUI/client" as **inferred (strong), needs one live probe**. It is now documented.

`docs/gateway/protocol.md` §"Broadcast event scoping", citing
`src/gateway/server-broadcast.ts`:

> Chat, agent, and tool-result frames (streamed `agent` events, tool-result
> events) require at least `operator.read`. Sessions without it skip these.

> Each client connection keeps its own per-client sequence number, so broadcasts
> fan out to different scope-filtered subsets of the event stream.

So the Gateway **broadcasts streamed `agent` events to connected clients**, scope
gated. An externally triggered run is not private to its caller — attached
clients holding `operator.read` receive its stream. That is the mechanism the
TUI-first rework needs, stated by the vendor, with the implementing source file
named.

The injection face itself is unchanged on this release: the `agent` RPC still
takes `deliver` (`protocol.md:710`, `:716`, with `result.deliveryStatus`), and
`POST /hooks/agent` and `POST /hooks/wake` are still documented against
`127.0.0.1:18789` (`docs/automation/cron-jobs.md:415-437`,
`docs/gateway/configuration-reference.md:773`).

## 2. `openclaw tui` session attach — the second open item, resolved

`docs/cli/tui.md` documents the current contract:

- `--session <key>`, default `main` (or `global` at global scope); inside an
  agent workspace it auto-selects that agent unless the key is explicitly
  prefixed `agent:<id>:...`.
- `--url` (Gateway WebSocket), `--token`, `--password`, with configured Gateway
  auth SecretRefs resolved from `env`/`file`/`exec` providers.
- `--history-limit <n>`, default 200 — "History entries to load **on attach**".
- `--deliver`, default false — whether assistant replies go out through
  configured channels.
- "With no explicit URL or port, `tui` follows the active local Gateway port
  recorded by the running Gateway."

Two consequences. First, history-on-attach confirms the session is a
server-side object clients render, not client-owned state — consistent with §1.
Second, **the Gateway records its active local port**, which is a discovery
mechanism an adapter can use instead of guessing.

Note `--local` is a different thing: an embedded runtime with no Gateway. It is
not a seat we could inject into and must not be confused with the attached mode.

## 3. Trust boundary — the blocker that turned out to be much smaller

D14 said reversing the never-attach invariant "demands a deliberate
trust-boundary redesign (token scope, which sessionKey is the seat, zero
mutation of `~/.openclaw` state)". Upstream already ships the primitives.

**Scoped operator tokens** (`docs/gateway/operator-scopes.md`):

| Scope | Grants |
| --- | --- |
| `operator.read` | read-only status, lists, catalog, logs, session reads |
| `operator.write` | mutating operator actions: sending messages, invoking tools, node command relay. Also satisfies `operator.read` |
| `operator.admin` | administrative: config mutation, updates, native hooks, reserved namespaces, high-risk approvals |

Plus `operator.pairing` and `operator.approvals`. So a pneu adapter need not hold
admin — it needs `operator.write` to inject and `operator.read` to observe, and
nothing more. That is a real answer to "token scope: narrow or all-or-nothing".

**Device pairing approval** is also already the model: attaching a new client
goes through a pairing approval the human grants. That is arguably a *better*
trust boundary than anything we would have designed — the human explicitly
admits pneu as a device rather than pneu helping itself to an existing secret.

**Client presence** — D14 said TUI-attach liveness "has no known probe yet".
Partially wrong: `system-presence` "returns the current presence snapshot for
connected operator/node devices", keyed by device identity
(`protocol.md:258-265`, `:338`). Stated precisely: there **is** a
device-level presence signal; what is still unproven is whether it resolves the
narrower question "is a client currently attached to *this session key*". Treat
the narrow form as the one remaining unknown, not the whole question.

## 4. What was deliberately not done, and why

No Gateway was started. `~/.openclaw` holds 161 MB of the operator's real state
from January–June 2026, including `credentials`, `delivery-queue`,
`session-delivery-queue`, `cron`, `qqbot`, `flows`, and `tasks`. Starting a
three-month-dormant personal Gateway could flush queued deliveries to real
contacts, resume cron jobs, and reconnect live chat channels. That is an
outward-facing, hard-to-reverse action and it is the operator's call, not this
seat's.

A full backup was taken first regardless: `~/openclaw-backup-2026-08-17.tar.gz`
(33 MB compressed, 161 MB source). The vendor's own updating doc asks for this —
it warns that the automatic pre-update copy "does not create a full state
recovery point".

The install itself created only `~/.openclaw/state`; every pre-existing
directory retains its original mtime.

## 5. What this changes, and what it does not

**Does not change the demotion.** The shipped adapter is still the wrong shape
under `decision.md` 2026-08-12, still ships in `TOOLS`, and still carries the
isolation-root defect. `handoff/archive/d14-openclaw-demotion-spec.md` stands as
written. This audit is about the *rework*, which was always a separate,
later question.

**Changes the rework's standing** from "unknown whether buildable" to "buildable,
mechanism documented, scoped". Concretely, the shape is now describable:

- Seat = a session on the **user's own Gateway**, keyed `agent:<id>:<key>`.
- The human sits in whichever client they prefer — `openclaw tui --session <key>`
  for a terminal seat, or their existing chat surface.
- pneu holds a **paired device identity with `operator.write`**, never admin,
  and never reads or copies an existing credential.
- Wake = `agent` RPC or `POST /hooks/agent` against the seat's session key, with
  `deliver` chosen deliberately.
- Gateway discovery via the active-local-port record, not a guess.
- pneu mutates nothing under `~/.openclaw`.

This is family F3 in `handoff/harness-expansion-blueprints.md` §2, and it is now
the *cheapest* F3 candidate rather than the most speculative one: unlike
OpenCode, the session-identity question has a documented answer, and unlike
Antigravity, the injection face addresses a session the human is already
watching.

**Remaining before any support claim:**

1. Live confirmation of §1 — an externally triggered `agent` run rendering in an
   attached `openclaw tui`. This is now a *confirmation* probe, not a discovery
   probe, and it should run against an isolated Gateway with its own state root,
   not the operator's personal one.
2. Whether presence resolves per-session-key attachment (§3), or only per-device.
3. The normal L-gate from `handoff/harness-expansion-blueprints.md` §3.

## 6. Correction to the record

`handoff/archive/d14-tui-first-survey.md` should be read with this file beside it. Two
of its statements are now superseded by primary evidence at a newer release:
live-client rendering is documented rather than inferred, and TUI-attach liveness
has a device-level probe rather than none. Its verdict — "rework path exists,
gated on a healthy current-version install plus a source re-audit" — was correct,
and this is that re-audit.
