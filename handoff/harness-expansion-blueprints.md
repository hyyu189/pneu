# Harness expansion blueprints — 1.4 Track T5

> Status: current — design proposals awaiting operator scheduling; no adapter was built

Date: 2026-08-14. Branch: `wt/t5-adapters`. Dispatch: `handoff/archive/t5-brief-1.4.md`.
Inputs: `handoff/harness-expansion-survey.md` (2026-08-12 verdict table),
`handoff/archive/d14-tui-first-survey.md`, `decision.md` 2026-08-12 ("Seats are
interactive TUIs"), and a read of the current tree's launcher, watcher, setup,
and doctor seams.

**This cycle produces paper, not adapters.** Nothing here was installed, no
third-party harness was launched, no shared harness configuration was touched,
and no credential store was read. Every implementation claim below is a design
proposal awaiting operator scheduling; every support claim remains ungranted
until the live gates in §3 and §6 pass.

## 0. Method and evidence tags

Tags follow the survey's discipline so a reader can tell verification apart
from inference:

- **[S]** Source-of-truth read in this repository at this commit (file and
  symbol named).
- **[D]** Official vendor documentation or source, re-verified during this
  track. Every [D] claim carries a URL in the per-harness source list.
- **[D-2026-08-12]** Carried unchanged from the survey and *not* re-verified
  here; treat as older evidence.
- **[I]** Design inference by this track. Not evidence.
- **[L]** A live test that must pass before a support claim. Fixtures never
  substitute.

The re-verification pass (brief item 2) ran as read-only web research against
each vendor's current documentation and release notes. §6.0 summarizes the
deltas — including four survey claims that did not survive — and §9 records what
could not be verified from documentation alone and therefore becomes a lab
question rather than a design assumption.

## 1. The seam map — what a new harness costs in this tree

A new harness is not one file. Reading the Grok integration (the newest and the
only one built after the TUI-first ruling) end to end, these are the exact
seams [S]:

| # | Seam | File / symbol | Needed by |
| --- | --- | --- | --- |
| 1 | Launcher shim | `bin/rt-<name>` (≈6–10 lines, delegates to `_rtlauncher.main`) | all |
| 2 | Harness tables | `bin/_rtlauncher.py`: `COMMANDS`, `HARNESS_LABELS`, `HARNESS_INSTALL_HINTS`, `EXECUTABLE_OVERRIDES` (`RT_<NAME>_BIN`), `CONFIG_HARNESSES` | all |
| 3 | Executable resolver | `harness_bin()` branch; optionally `_adapter_module` / `_adapter_harness_bin` when the resolver ships inside `integrations/<name>/` | all |
| 4 | Anchor policy | `launch()` root-is-None branch (Codex/OpenClaw/Grok refuse unanchored) and the `unanchored_index` suppression in the project selector | all TUI seats with a durable mailbox |
| 5 | Credential preflight | `preflight_<name>_credentials()` — presence-only, never parsed, never logged | all |
| 6 | Seat seed / primer | `<NAME>_SEAT_PRIMER*`, `<name>_seat_primer_args()`, `<name>_primer_skip_reason()`, `print_<name>_primer_skip_advisory()` | model-armed families only (§2 F4) |
| 7 | Exec assembly | the `if harness == ...` ladder at the end of `launch()` | all |
| 8 | Packaged integration asset | `integrations/<name>/pneu/…` (Hermes plugin, Grok/OpenClaw adapter modules) | in-process families (§2 F2) |
| 9 | Owned host setup | `pneu_packaging/setup.py`: `HARNESSES` tuple, `_prepare_<name>` / `_validate_<name>`, link plan, manifest ownership, backup, rollback, `remove` | only when pneu writes into the harness's own config tree |
| 10 | Watcher | `bin/rt-wait-inbox` — fenced lease validation, single-watcher fencing, quiet renewal, reply alarms, emit mode | families F1 and F3 |
| 11 | Doctor | `bin/rt-doctor`: harness-family map, evidence probe, `report_<name>_*` wired into the report body | all |
| 12 | Orientation | `templates/<NAME>.md.tmpl`, `templates/agents.yaml.tmpl` seat block, `roundtable-init` creation path | all |
| 13 | Docs | `docs/compatibility.md` onboarding matrix row, a per-harness section, validation/terminal matrices | all |
| 14 | Tests | launcher exact-argv tests, setup plan/apply/status/remove tests, doctor fixture tests | all |

### 1.1 Structural finding: the launcher ladder does not scale

`launch()` in `bin/_rtlauncher.py` is a single function carrying per-harness
special cases as inline branches: an anchor refusal per harness, a credential
preflight for Hermes and Grok, primer assembly for Codex and Grok, a
Remote-Control composition for Claude, a `--tui` default for Hermes, an
`os.execv` detour for OpenClaw, and five `harness == "..."` comparisons in the
final exec assembly [S]. Five candidates share the tables at seam 2 but each
adds two to four more branches.

**Prerequisite refactor (proposed, ~S effort, no behavior change):** replace the
tables and the ladder with one `HarnessDescriptor` record per harness holding
the fields the ladder currently expresses — `command`, `label`,
`install_hint`, `bin_env`, `config_aliases`, `requires_anchor`,
`anchor_refusal_text`, `resolver`, `credential_preflight`, `default_args`,
`seed_args`, `seed_skip_reason`, `exec_transform` — with the existing behavior
pinned by exact-argv tests before and after. This is not cosmetic: it converts
"add a harness" from *edit eight branches in a 1400-line module* into *add one
record plus its adapter*, and it is the single highest-leverage item in the 1.5
plan (§7). Recommend landing it before the first new harness, not after.

### 1.2 What the watcher already gives for free

`bin/rt-wait-inbox` is the reusable engine, and it is more general than its
Claude framing suggests [S]. `run()` already provides:

- lease validation against the fenced project seat (`load_validated_lease`)
  before touching the durable inbox;
- single-watcher fencing via `update_wake(..., expected_watcher_pid=...)`, so a
  superseded watcher stands down instead of racing its successor;
- duplicate-arm suppression (`watcher_is_live`) for lifecycle events that fire
  more than once (Claude's post-compaction SessionStart);
- silent lease renewal on an empty inbox — no heartbeat wake, no model turn
  (decision.md 2026-08-07);
- `--expect-reply` alarm reconciliation;
- the Stop-hook retry breaker that stops one unchanged pending generation from
  looping;
- quiet-ack accounting;
- migration-lock tolerance (`ProjectLayoutLockTimeout` is quiescence, not a
  broken watcher).

The only harness-specific part is the last three lines: *how the accumulated
wake payload leaves the watcher*. Today that is
`return 2 if claude_hook else 0`, with the drain instructions written to stderr
under `claude_hook`.

**Proposed generalization:** an explicit `--emit=<mode>` selector over the same
loop, with modes `exit-rewake` (exit 2 + payload on stderr; Claude today, Qoder
unchanged), `exit-zero` (exit 0 + payload on stdout; Hermes plugin consumer
today), and `inject:<command>` (payload handed to a pneu-owned injector
argv, for external-injector families). No new watcher, no second lifecycle, no
new lease semantics — one dispatch point at the end of `run()`. Every new
harness in §5 lands in one of those three modes.

## 2. Adapter families

The survey classified candidates as Shape A (harness-native injection into the
live session) or Shape B (model-armed monitor). That is the right admission
rule but too coarse for implementation: two Shape-A harnesses can need entirely
different pneu machinery. The implementable classification is by *who owns the
watcher process and how the wake payload crosses into the session*:

| Family | Watcher lives in | Wake mechanism | Shipped precedent | New candidates |
| --- | --- | --- | --- | --- |
| **F1 hook-rewake** | a harness-spawned hook process (pneu-owned executable) | process exit code + payload stream → harness constructs a reminder and starts a turn | Claude Code (`asyncRewake`, exit 2) | **Qoder**; conditionally Devin, Droid |
| **F2 in-process extension** | the harness's own process, running pneu-owned code | direct call to an in-session injection API | Hermes plugin | **Pi**, **OMP** |
| **F3 external injector** | a pneu-owned process outside the harness | RPC/HTTP/CLI call addressed at the exact live session id | Codex wake bridge; OpenClaw Gateway adapter | **OpenCode**, **Kilo**, **agy** (A-candidate) |
| **F4 model-armed monitor** | the harness's own background-task subsystem, armed by a seeded first turn | the harness's own monitor/scheduler starts a turn | Grok Build | **agy** (B-shape), Copilot (`/every`) |

Family determines cost far more than the survey's S/M sizing does:

- **F1 is the cheapest possible integration.** It reuses `rt-wait-inbox`
  verbatim under a different flag, needs no packaged extension, no injector,
  and no session-identity discovery. The entire risk is the harness's hook
  timeout policy.
- **F2 costs a packaged, versioned extension in a foreign language (JS/TS) plus
  owned setup (seam 9) plus a per-vendor API-drift surface.** The Hermes plugin
  is 634 lines [S]; a Pi/OMP extension is smaller but is still a second
  codebase in the release artifact.
- **F3 costs session-identity discovery, transport authentication, and a
  supervised long-lived process.** The Codex bridge's readiness contract in
  `docs/compatibility.md` is the honest measure of that cost. F3 is where
  "the TUI renders what the injector submitted" must be *proved*, not assumed.
- **F4 costs almost no code** (a pinned primer string and a doctor advisory)
  **and buys the weakest guarantee**: the armer is a model turn, it dies with
  the session, and every resume needs a re-arm. It is the accepted shipped
  shape (decision.md 2026-08-12), not the preferred one.

### 2.1 Slot template

Every blueprint in §5 fills the same slots. A candidate whose slot cannot be
filled from documentation becomes a lab question in §6, never a guess.

1. **Currency delta** — what the re-verification pass changed vs 2026-08-12.
2. **Family** and the reason.
3. **Launcher shape** — argv, anchor policy, credential preflight, seed.
4. **Seat identity & lease** — what fenced tuple the wake path must prove
   before it is allowed to inject or rewake.
5. **Wake adapter** — the exact injection face, cited.
6. **Orientation payload** — what `<NAME>.md` must say that the harness does
   not teach on its own.
7. **Doctor probes** — liveness and drift, one drift check per undocumented
   internal (D14 backlog item 4's principle, applied at design time).
8. **Validation lab [L]** — what a live run must prove.
9. **Risks and unknowns.**

### 2.2 Invariants every blueprint inherits

These are not per-harness decisions; they are the project's existing rules
restated so no blueprint quietly relaxes one.

1. **The seat is the native interactive TUI.** No headless replacement session
   is a seat (decision.md 2026-08-12).
2. **Delivery is the maildir write.** Every wake path is an optional adapter on
   top of a delivery that already succeeded; an unwoken seat is late, not
   lost.
3. **The communication layer is not a permission gate** (decision.md
   2026-08-11). Work happens in the TUI under the harness's own approval UX.
   No new adapter answers permission requests.
4. **Quiet by default** (decision.md 2026-08-07). An empty inbox must never
   produce a model turn. This disqualifies any design whose only mechanism is
   a short-cadence poll executed by the model.
5. **Fenced identity before any wake.** Project root, agent id, pneu session
   id, and lease revision must all validate; the wake must additionally prove
   the harness-side session identity it targets.
6. **Credential boundary.** Presence checks only. Never parse, copy, refresh,
   or log a credential; recovery is always the vendor's own login command.
7. **Fail closed on drift.** Pin and report the harness version; probe the real
   contract; make doctor name the missing executable or unsupported capability.
   Version numbers and fixtures never establish support.
8. **Never touch shared harness configuration without an explicit ruling.**
   The precedent is on record: a herdr integration install once tripped the
   Codex hook-trust gate and fail-closed every Codex wake on the machine.
   Anything under seam 9 is approval-gated work, not adapter work.

## 3. The common live-validation protocol (the L-gate)

Every candidate's lab plan is the same protocol with per-harness deltas. Writing
it once keeps the blueprints honest: a harness is not "easier to validate"
because its blueprint author wrote fewer bullets.

**L0 — isolation.** A dedicated scratch project registered with
`roundtable-init --here`, a unique cwd, and a seat name that exists nowhere
else. No shared harness configuration is modified; if the harness requires a
config file pneu would own in production, the lab supplies it through a
per-invocation override or a sandboxed config root, and the run is recorded as
*sandboxed*, not as evidence for the owned-setup path.

**L1 — version pin.** Record the exact harness version and its resolution path
before the run. A run whose version was not recorded is not evidence.

**L2 — same-seat identity.** Capture the harness-side session/conversation/
thread id the visible TUI is rendering, plus cwd, project UUID, agent address,
and lease revision. Every later wake must be shown to target that exact tuple.
This is assertion one; a wake that cannot prove it is a failure even if it
looks like it worked.

**L3 — generation one, idle arrival.** With the seat idle and visible, deliver
one message with `rt-say`. Require: the wake appears **in that TUI**, no
parallel headless conversation consumed the mail, the drain runs the
package-managed absolute `rt-inbox --fenced --archive-quiet-acks -f json`, and
exactly one `rt-ack` is emitted per logical `msg_id` with duplicate
maildir/ledger records collapsed to one message.

**L4 — re-arm.** Show the wake channel is armed again after generation one
without human action (F1/F2/F3) or with exactly the documented re-arm turn
(F4). Record which.

**L5 — generation two, busy arrival.** Deliver a second message while the seat
is mid-turn. Require ordering preserved, no message lost, no duplicate ack, and
no interleaving that corrupts the first turn.

**L6 — quiet interval.** Measure model requests over a quiet interval with an
empty inbox. **The pass condition is zero.** A timeout that ends the turn
disarms the seat; a timeout that fabricates another model turn is not a quiet
watcher. For F1 candidates this is the decisive measurement, not a formality.

**L7 — resume and reconnect.** Resume the session by every documented route
(`--continue`, `--resume`, in-session `/resume`, server reattach), then repeat
L3. Record for each route whether the channel re-armed automatically.

**L8 — interrupt and cleanup.** Ctrl-C the seat, kill the watcher/injector, and
kill the harness. Require: no orphaned pneu process, no stranded lease, no
wake state that survives its owner, and mail still durable in `new/`.

**L9 — fence rejection.** Deliberately mismatch the lease revision or session
id and require the wake path to refuse rather than inject.

**L10 — doctor truth.** Run `rt-doctor` in each of: armed, unarmed, harness
absent, harness version drifted. Require each state to be named correctly, with
a remedy string that actually clears the finding.

**L11 — clean account / terminal matrix.** Repeat L3–L5 on a clean harness
account and across Terminal.app, iTerm2, and Ghostty, consistent with the
existing terminal acceptance matrix in `docs/compatibility.md`.

Promotion vocabulary, unchanged from the current docs: passing L0–L10 makes a
candidate *launchable and documented*, not *supported*. A public support claim
requires L11 plus the credentialed end-to-end wake gate, and the
`docs/compatibility.md` onboarding matrix row must name what is still missing
until then.

## 4. Orientation payload conventions

`templates/HERMES.md.tmpl` is a four-line seat stub; `templates/GROK.md.tmpl`
adds a mailroom section because Grok's seat needs to know something the harness
does not teach [S]. That is the rule to generalize:

> An orientation file carries the seat block plus **exactly the wake facts the
> harness cannot teach itself**, and nothing else.

Concretely, `<NAME>.md` should contain:

1. The seat block (`<agent>: cwd / role / always_read`) matching
   `templates/agents.yaml.tmpl`.
2. The three package-managed absolute command forms
   (`rt-inbox --fenced --archive-quiet-acks -f json`,
   `rt-ack --fenced <id>[,<id>...]`,
   `rt-say --fenced --no-nudge <agent> <kind> <message>`) with the
   PATH-precondition reason: hook-injected absolute forms are canonical in a
   woken turn because the narrow setup permissions and lease validation match
   only them.
3. The drain rule: act on every non-ack message before acknowledging it once;
   quiet receipts are archived by the fenced inbox command and never
   acknowledged again.
4. **The family-specific line, and only for families that need one:**
   - F1/F2/F3 — nothing further. The channel is machinery; the model must not
     be taught to maintain it, because a model that believes it owns the
     watcher will try to restart it (decision.md 2026-07-23: model turns must
     never arm).
   - F4 — the re-arm contract: what the armer is, that it dies with the native
     session, that resume and skipped-primer launches need exactly one re-arm
     turn, and that a second armer must never be created in an already-armed
     session.

The negative rule matters as much as the positive one. Three of the five new
candidates are F1/F2/F3, so three of the five `<NAME>.md` files should be
*shorter* than `GROK.md`, not longer.

## 5. Family reference designs

Each family gets one reference design here; the per-harness blueprints in §6
then carry only their deltas. This is deliberate — if a candidate's blueprint
needs to restate the whole design, it is not really in the family, and that
mismatch is itself the finding.

### 5.1 F1 — hook-rewake reference design

**Precedent:** Claude Code. The owned SessionStart hook launches
`rt-wait-inbox --claude-hook`; the process blocks on the maildir with silent
lease renewal; on mail it prints the message list to stdout, the drain
instructions to stderr, and exits 2; the harness turns that into a system
reminder and starts a turn. The Stop hook launches the successor [S].

**Generalized components:**

| Component | Content |
| --- | --- |
| Arming | The harness's earliest session lifecycle hook (`SessionStart` or equivalent) spawns the pneu watcher in the harness's documented background/async mode. |
| Watcher | `rt-wait-inbox --emit=exit-rewake` (§1.2), unchanged otherwise. |
| Wake | Documented non-zero exit status + payload stream. |
| Re-arm | The harness's turn-completion hook (`Stop` or equivalent) spawns the successor, with `watcher_is_live` suppressing duplicates. |
| Ownership | pneu owns only its own hook entries in the harness's hook config, written through `pneu_packaging/setup.py` with backup, manifest ownership, `status`, and `remove`. |

**The three questions that decide whether a harness is really F1:**

1. **Does the async/background hook mode exist, and does a documented exit
   status start a turn in the live session?** If the hook only *prolongs an
   ending turn* (Grok, Cursor), the harness is not F1 — that is a chained turn,
   not an idle wake.
2. **Can the hook block indefinitely with zero model cost?** L6. A bounded
   default timeout is survivable only if it is configurable to a long value
   *and* the timeout behavior is fail-safe (the watcher is respawned, the seat
   is not disarmed). A fail-open timeout that lets the turn finish converts
   wake-on-mail into never-wake.
3. **Is there a consecutive-block cap?** Copilot's documented eight-block limit
   is the cautionary case. A cap is acceptable only if pneu's normal operation
   never approaches it, which requires that a wake that produces no mail is
   impossible — true for the maildir design, but it must be verified against
   the harness's counting rule, not assumed.

**Anti-pattern to reject explicitly:** implementing F1 on top of a Stop hook
that *blocks the end of a turn* rather than running asynchronously. That design
holds the seat's turn open, is indistinguishable to the user from a hung
session, and inherits the vendor's loop guard. Devin and Droid are exactly this
shape, which is why the survey rated them "A-conditional" — the condition is
question 2 above.

**Cost:** the cheapest family. No packaged extension, no injector, no
session-identity discovery; the entire delta from the Claude path is a hook
config schema and one emit mode.

### 5.2 F2 — in-process extension reference design

**Precedent:** the Hermes plugin (`integrations/hermes/pneu/`, 634 lines) [S].
It arms from the TUI's first `on_session_reset`, before the user's first
prompt; replaces its watcher on later session resets; runs the watcher as a
subprocess it supervises; and injects through Hermes' own message-injection or
managed background-completion rail addressed with the exact native session key.
It refuses to adopt or silently drop mail when the host does not confirm
asynchronous delivery, and it shuts the watcher down with the session.

**Generalized components:**

| Component | Content |
| --- | --- |
| Asset | A pneu-owned extension in the harness's extension language, shipped under `integrations/<name>/`, linked into the harness's extension directory by owned setup. |
| Arming | The harness's earliest in-process session event. Must fire before the user's first prompt, or the seat has a zero-turn arming hole (decision.md 2026-07-23). |
| Watcher | Either the harness's own managed watcher/timer API (preferred — the harness owns cleanup) or a supervised `rt-wait-inbox --emit=exit-zero` subprocess (the Hermes shape). |
| Wake | The in-session injection call, with the turn-triggering flag set. |
| Fence | The extension must read `RT_PROJECT_ROOT`, `RT_FROM`, `RT_SESSION_ID`, `RT_LEASE_REVISION` from the launcher-exported environment and refuse to arm when they are absent or do not validate — this is what keeps the extension inert in a user's ordinary, non-pneu session of the same harness. |
| Cleanup | Session shutdown must stop the watcher; an extension exception must be visible, not silent. |

**Design rules learned from the Hermes plugin, to be applied verbatim:**

- **Inert outside a complete lease.** A globally installed extension fires in
  every project. Standing down silently is the required default.
- **Never adopt-or-lose.** If the injection call cannot be confirmed, stop and
  leave the mail in `new/`. Losing a message to an unconfirmed injection is
  strictly worse than not waking.
- **Idempotent re-arm.** Lifecycle events fire more than once (reset, compact,
  resume). Replace, never duplicate.
- **Handle the busy case explicitly.** Document, per harness, what the
  injection API does mid-turn (queue, steer, or reject) and test it (L5). If it
  is "reject", the extension must retry rather than drop.

**Cost:** a second language in the release artifact, an owned-setup surface
(seam 9, approval-gated), and a vendor API-drift surface with no protocol
handshake to probe. This family is *not* cheap despite the survey's "S" sizing
for Pi/OMP: the sizing measured the injection call, not the packaging,
ownership, and removal machinery around it.

### 5.3 F3 — external injector reference design

**Precedent:** the Codex wake bridge and the OpenClaw Gateway adapter [S]. The
Codex readiness contract in `docs/compatibility.md` — twelve conjunctive
conditions covering version floor, live protocol probe, socket peer identity,
launchd ownership, and handshake liveness — is the honest measure of what this
family costs when done to the project's standard.

**Generalized components:**

| Component | Content |
| --- | --- |
| Session identity | A binding record mapping the fenced pneu seat (project, agent, session id, lease revision) to the harness-side session/conversation id the visible TUI is rendering. **Acquiring this is the hard part of every F3 integration.** |
| Discovery | How the injector finds the harness's server: port, socket path, lockfile, config, or env var — and how it authenticates. |
| Watcher | `rt-wait-inbox --emit=inject:<argv>` (§1.2), or a supervised long-lived bridge process where the harness needs a persistent connection. |
| Wake | The injection call, addressed at the bound session id, with a success confirmation that distinguishes "the TUI rendered it" from "the server accepted it". |
| Liveness | A read-only protocol probe the launcher and doctor can run, following the Codex `initialize`/read-only-surface precedent. |
| Ownership | If the injector needs a supervised process (launchd), that is owned-setup territory and approval-gated. |

**The decisive question — and it is the same question for every F3 candidate:**
*how does an external process learn which session id the running TUI is
rendering, without guessing?* There are only four acceptable answers, in
descending order of quality:

1. **The harness tells pneu.** A lifecycle hook fires with the session id and
   cwd, and pneu records it — the Codex auto-bind shape, and the only one that
   is robust across `/clear`, resume, and multi-session hosts.
2. **pneu tells the harness.** The launcher supplies the session id
   (`--session <uuid>`), as the Claude bare-launch path already does with
   `--session-id` [S]. Requires the harness to accept a caller-chosen id.
3. **A queryable server endpoint** that reports the session a given attached
   TUI client is rendering. Acceptable if the mapping from *client* to *pneu
   seat* is itself unambiguous — which usually means one server per project.
4. **A state file the harness writes.** Weakest: undocumented internals, needs
   a doctor drift check, and races on multi-seat hosts.

"Submit to the most recent session" is not on that list and must never be
implemented. It is the exact failure the no-headless-substitution gate exists to
catch: it works in a one-seat demo and silently steals mail on a real fleet.

**Cost:** the most expensive family, and the only one that can genuinely
deliver Codex-grade wake semantics — event-driven, no model-armed component,
surviving resume without a re-arm turn.

### 5.4 F4 — model-armed monitor reference design

**Precedent:** Grok Build [S]. A pinned positional primer instructs exactly one
`monitor` background task with `persistent: true` watching the seat's
authoritative absolute maildir `new/`; explicit native arguments and
`RT_GROK_NO_PRIMER=1` skip the primer and print a prominent re-arm advisory;
`rt-doctor` reports bounded, read-only evidence from session records as an
explicit non-lease advisory.

**Generalized components:** primer template (pinned by exact-argv tests),
skip-reason function, skip advisory, orientation re-arm contract, doctor
evidence advisory. Nothing else — F4 deliberately has no pneu-owned process.

**Rules:**

- **The primer is a no-action instruction.** It runs as a genuine model turn;
  under a full-auto approval configuration an inviting prompt means unattended
  workspace changes (decision.md 2026-07-30). Arm the monitor, reply one word,
  touch nothing.
- **The maildir path is absolute and resolved by the launcher**, not by the
  model. `grok_seat_maildir()` resolves it through the registry before the seat
  is claimed, so a path-resolution failure cannot strand a lease [S].
- **Doctor evidence is advisory, forever.** Session records are an undocumented
  observation surface. The report must say "report-only; session evidence is
  not a lease" in the same string as the finding.
- **Never claim resume parity.** The armer dies with the session. Every resume
  needs one re-arm turn, and the orientation file, the launcher advisory, and
  `docs/compatibility.md` must all say so.

**Cost:** near zero to build, permanently second-class to operate. Correct as a
*fallback rail* for a harness whose A-shape is unproven, and correct as the
*shipped* shape only when no A-shape exists.

### 5.5 Cross-cutting: doctor, drift, and the default seat set

**One drift check per undocumented internal.** D14 backlog item 4 states the
principle and records three existing violations (the `thread/name/set` RPC,
Claude's `~/.claude.json` trust-record shape, and `CLAUDE_ENV_FILE` semantics).
Applied at design time, it means: any blueprint slot filled by an undocumented
path, file format, port, or field must ship with a doctor probe that detects the
format changing, and the probe's output must state that the surface is
undocumented. The Grok monitor advisory is the model implementation — bounded
walk, symlink refusal, size cap, file-count cap, `present`/`absent`/`unreadable`
tri-state, and a remedy string that actually clears the finding [S].

**Three probe classes, and every harness needs all three:**

1. **Presence** — is the executable resolvable, and at what version? Report the
   resolved path, never a bare "installed".
2. **Arming** — is this seat's wake channel actually armed right now? F1/F2/F3
   can answer from pneu's own runtime records (the watcher fence, the binding
   record); F4 can only offer advisory evidence and must say so.
3. **Drift** — does the harness-side contract still look the way the adapter
   assumes? Version skew for protocol families, config-schema shape for hook
   families, API presence for extension families.

**The default seat set stays small.** `templates/agents.yaml.tmpl` ships exactly
three seats — claude, codex, hermes — and neither Grok nor OpenClaw was added
to it [S]. That is correct and must hold: a project template that mints eight
seats creates eight mailboxes nobody reads. New harnesses ship their
`<NAME>.md` orientation template and a documented "add this block to
`agents.yaml`" snippet, not a default seat. Any 1.5 implementation that grows
the default template should be rejected in review.

## 6. Per-harness blueprints

### 6.0 Re-verification summary (brief item 2)

Read-only documentation research on 2026-08-14 against each vendor's current
docs and release notes. **Four survey claims did not survive.**

| Candidate | Version then → now | Injection face | Change |
| --- | --- | --- | --- |
| Qoder CLI | — → **1.1.21** (2026-08-13) | `asyncRewake` + exit 2 | **Confirmed**, with more field detail. Two corrections: Homebrew is *not* a documented install path (installer script and npm only), and command hooks carry a **600 s default timeout** the survey did not record. |
| Pi | — → **0.84.1** (2026-08-07) | `sendMessage(..., {triggerTurn})` | **Confirmed** in full, with exact signatures. New fact: Pi has **no managed timer/watcher API** — docs say so explicitly. |
| Oh My Pi | — → **17.3.3** (2026-08-14) | same | **Confirmed and then some**: OMP *does* ship managed `ctx.setInterval/setTimeout/clearTimer` with auto-cleanup, which Pi lacks. Release cadence ~10 releases in the week of 2026-08-06..14. |
| OpenCode | 1.17.3 → **1.18.18** (2026-08-13) | `/tui/*` + session endpoints | **Confirmed, with one blocking gap discovered** (below). |
| Kilo CLI | — → unresolved | `kilo attach` | **Two survey claims failed re-verification** (below). Demoted. |
| Antigravity `agy` | 1.1.12 → **1.1.13** (2026-08-14) | sidecar + `agentapi send-message` | **Still no SessionStart/resume hook.** The 2026-08-05 parking rationale holds. The A-candidate has an unresolved blocker (below). |
| Copilot CLI | — → **1.0.79** (2026-08-10) | `/every` scheduling | **Survey claim failed**: no `-i/--interactive PROMPT` flag exists. Demoted from B-ready to parked. |
| Devin CLI | — → **3000.4.25** (2026-08-13) | blocking Stop hook | Confirmed, including that **no timeout default or maximum is documented at all**. |
| Factory Droid | — → **0.195.0** (2026-08-13) | blocking Stop hook | Confirmed: 60 s default, configurable, no documented maximum. |
| Mastra Code | not re-verified | official controller API | Carried as [D-2026-08-12]. Deferred, not endorsed. |

The four failures, stated plainly:

1. **Kilo's `kilo run ... --attach <url> --session <id>` does not exist as
   documented.** The real surface is a separate `kilo attach <url> --session
   <id>` subcommand [D]. More seriously, Kilo's official documentation
   publishes **no HTTP endpoint list, no OpenAPI spec, and no plugin API page**
   — the survey's endpoint and plugin-event claims trace to an unofficial
   AI-generated wiki, not a primary source [D].
2. **Copilot CLI has no interactive prompt-seeding flag.** Current docs
   document `-p/--prompt` as non-interactive (exits after one turn) and bare
   `copilot` for interactive with no seed flag [D]. Shape B requires seeding
   the first visible TUI turn; without it Copilot's `/every` schedule cannot be
   installed unattended, which is exactly the condition that keeps Kimi Code
   parked.
3. **OpenCode has no way for an external process to learn which session the
   TUI is rendering.** The `/tui/*` API is write-only; an upstream issue
   requesting a read endpoint states directly that there is currently no way to
   know what the prompt contains [D]. This does not sink OpenCode — §6.3 routes
   around it — but it invalidates any design that starts by "resolving the
   TUI's session".
4. **Antigravity documents no conversation-id discovery mechanism for
   sidecars.** `agentapi` documents `new-conversation` (create) and
   `send-message` (inject into a known id) [D]; nothing documents how a sidecar
   learns the id of the conversation the human's TUI is rendering. A sidecar
   that calls `new-conversation` and then messages its own conversation has
   built a headless replacement session — the exact shape decision.md
   2026-08-12 forbids.

Two of these matter beyond their candidate: they are both cases where the
survey's [D] tag rested on a secondary source or a plausible-sounding flag
name. **Recommend a standing rule: a [D] tag requires the vendor's own page,
and a claim sourced from a wiki, cheat sheet, or changelog summary is tagged
[D?] and cannot support a readiness verdict.**

---

### 6.1 Qoder CLI — F1, recommended first implementation

**Currency delta.** Confirmed at 1.1.21 [D]. `asyncRewake: true` on a command
hook runs it in the background; **exit code 2 makes the CLI build a system
reminder from the hook's output and wake the model** — a near-exact analog of
Claude's contract. Release 1.0.17 added `rewakeMessage`/`rewakeSummary` for
custom reminder text. `SessionStart` matchers are `startup`, `resume`, `clear`,
`compact`, `new`. Hook input carries `session_id`, `transcript_path`, `cwd`,
`hook_event_name`. Config lives at `~/.qoder/settings.json`,
`<project>/.qoder/settings.json`, and `<project>/.qoder/settings.local.json`,
hot-loaded. Exit codes: 0 success, 2 blocking, anything else non-blocking
error. Command and HTTP hooks default to a **600-second timeout**; `prompt`
hooks 30 s, `agent` hooks 60 s. Install is the vendor installer script or
`npm install -g @qoder-ai/qodercli`; auth is browser/token `/login` or the
`QODER_PERSONAL_ACCESS_TOKEN` variable, which takes priority when both exist.

**Family: F1.** Every slot maps onto the shipped Claude path.

**Launcher shape.** `rt-qoder` → `_rtlauncher.main("qoder")`. Anchor required
(refuse unanchored with the Grok-style message naming native `qodercli` as the
escape hatch). Credential preflight is presence-only over Qoder's own
credential location plus `QODER_PERSONAL_ACCESS_TOKEN`; recovery is native
`/login`. **No primer and no seed** — F1 arms from the hook, so a bare launch
is just the TUI. `RT_QODER_BIN` override. *Open item:* the executable name
(`qodercli` vs `qoder`) is not settled by the docs read; the resolver must
accept both and report which it selected.

**Seat identity and lease.** Standard: the launcher claims the fenced seat and
exports `RT_PROJECT_ROOT` / `RT_FROM` / `RT_SESSION_ID` / `RT_LEASE_REVISION`;
the watcher validates them before touching the inbox. Qoder's hook input adds
`session_id` and `cwd`, so the hook wrapper can additionally assert that the
firing session's `cwd` equals the leased project root and refuse otherwise —
this is the guard that keeps a globally installed hook inert in the user's
non-pneu Qoder sessions, mirroring `_claude_hook_is_managed()` [S].

**Wake adapter.** `rt-wait-inbox --emit=exit-rewake` (§1.2), spawned by an
owned `SessionStart` hook entry with `asyncRewake: true`, re-armed by an owned
`Stop` hook entry, duplicate arms suppressed by `watcher_is_live`. Payload:
message list on stdout, drain instructions on stderr, exit 2. Consider setting
`rewakeSummary` to a fixed short string so the reminder is legible in the TUI.

**The one real design problem: arming lifetime.** Claude's owned hook group
sets `CLAUDE_HOOK_TIMEOUT_SECONDS = 15_000` — pneu already solves "the watcher
must outlive a long quiet period" by asking for a very large timeout [S]. Qoder
exposes the same `timeout` field, so the design is identical: request ~15000 s
explicitly rather than inheriting 600 s. **But no maximum is documented**, so a
silent clamp to 600 s would mean the seat quietly disarms after ten idle
minutes with no error anywhere. This must be measured, not assumed
(gate LQ-1 below). If Qoder clamps, the fallback is *not* a `/loop` poll — that
burns model turns and violates the quiet-wake ruling — it is an honest
documented arming window plus a doctor probe that reports the watcher as stale,
and a decision from the operator about whether that ships.

**Orientation `QODER.md`.** Seat block, the three absolute command forms, the
drain rule. **No re-arm paragraph** — the channel is machinery (§4).

**Doctor probes.**
- *Presence*: resolve the executable, report path and `--version`.
- *Arming*: reuse the existing lease/watcher reporting — F1 watchers are pneu
  processes under the existing fence, so no new evidence surface is needed.
  This is a concrete advantage over Grok's advisory-only story.
- *Drift A (documented)*: the owned hook group is still present in
  `~/.qoder/settings.json`, still carries `asyncRewake: true`, and still
  carries the requested timeout — detects a user or vendor rewrite.
- *Drift B (undocumented)*: the effective timeout ceiling. Record the requested
  value and the last observed watcher lifetime; report when observed lifetime
  consistently truncates near 600 s.

**Owned setup (approval-gated).** Writing into `~/.qoder/settings.json` is
seam 9 and reuses the `_prepare_claude` machinery verbatim: own only pneu's own
hook group, snapshot and back up, record in the manifest, support
`plan/apply/status/remove`, refuse on foreign ownership. *Open question:*
whether Qoder has a Claude-style permission allowlist that also needs owned
entries for the `rt-*` commands — not established by this read.

**Validation lab.** L0–L11 (§3), plus:
- **LQ-1 (blocking):** with an explicit large `timeout`, hold an idle armed seat
  for over 20 minutes and confirm one continuous watcher process, zero model
  requests, and a wake on mail delivered at minute 20+. A watcher death near
  600 s fails this gate.
- **LQ-2:** confirm exit 2 wakes an *idle* session, not merely a session that
  was about to finish a turn.
- **LQ-3:** exercise `SessionStart` under each of `startup`, `resume`, `clear`,
  `compact`, `new` and confirm exactly one live watcher after each.
- **LQ-4:** confirm the hook stands down in a non-pneu Qoder session in another
  directory.
- **LQ-5:** confirm hot-loaded config changes do not orphan a running watcher.

**Risks.** The 600 s ceiling (LQ-1) is the only serious one. Secondary:
`QODER_PERSONAL_ACCESS_TOKEN` outranking `/login` means an inherited CI
variable could silently change identity — the preflight should report which
credential source it found, without reading it.

---

### 6.2 Pi and Oh My Pi — F2, one extension, two packagings

**Currency delta — Pi (0.84.1).** Extensions load from
`~/.pi/agent/extensions/*.ts` or `.../<name>/index.ts` globally, and from
`.pi/extensions/…` per project *only after the project is trusted*; `pi -e
./path.ts` loads one ad hoc. The factory is
`export default function(pi: ExtensionAPI): void | Promise<void>`.
`session_start` carries `reason: "startup"|"reload"|"new"|"resume"|"fork"`;
`session_shutdown` carries the same vocabulary plus `targetSessionFile`.
Injection is
`pi.sendUserMessage(content, {deliverAs?: "steer"|"followUp"})` and
`pi.sendMessage(message, {triggerTurn?, deliverAs?: "steer"|"followUp"|"nextTurn"})`.
Mid-stream calls are **never rejected**: `steer` delivers after the current tool
batch, `followUp` after the turn settles, `nextTurn` queues. Context exposes
`ctx.sessionManager.getSessionId()`, `ctx.cwd`, `ctx.mode`
(`"tui"|"rpc"|"json"|"print"`), and `ctx.hasUI`. **Pi documents that it has no
managed timer or watcher API** — extensions own their Node resources [D].

**Currency delta — OMP (17.3.3).** Roots are `<cwd>/.omp/extensions` and the
active agent directory's `extensions/`; OMP still honors the legacy
`pi.extensions` manifest key and rewrites `@earendil-works/*` specifiers, and
its own changelog calls upstream Pi extensions a legacy-compat tier. It adds
managed `ctx.setInterval` / `ctx.setTimeout` / `ctx.clearTimer` — auto-`unref`'d,
auto-cleared on `session_shutdown`, callback exceptions caught and logged. It
adds an `mcp_notification` event and a superset of Pi's turn/tool events.
Calling `pi.sendMessage()` during extension *load* throws
`ExtensionRuntimeNotInitializedError` — handlers must be registered first [D].

**Family: F2**, and the two are one build target, not two.

**The design decision that makes this cheap.** The obvious reading of the
timer divergence is "OMP is easier, Pi needs its own watcher loop". That is the
wrong conclusion. pneu must not reimplement lease validation, watcher fencing,
reply alarms, and the quiet-renewal loop in TypeScript — that logic is 964
lines of audited Python and it is the part that must not drift [S]. The
extension therefore **supervises `rt-wait-inbox --emit=exit-zero` as a child
process and injects what it reports**, exactly as the Hermes plugin does [S].
Under that design the managed-timer difference is not load-bearing: neither
harness needs a timer for the wake path. OMP's timers become a nice-to-have for
a periodic child-liveness re-check, feature-detected at runtime.

Result: **one extension source shipped under `integrations/pi/pneu/`,
packaged twice** — linked into `~/.pi/agent/extensions/` and into OMP's
extension root — with a small capability shim for the two divergences
(`ctx.setInterval` presence; module-default vs module-as-function export).

**Launcher shape.** `rt-pi` and `rt-omp`, both anchor-required, both
presence-only credential preflight over the harness's own credential location,
both no-primer (F2 arms from `session_start`). `RT_PI_BIN` / `RT_OMP_BIN`.
Executable names `pi` and `omp`. *Open item:* Pi's exact `npm install -g`
specifier did not surface on a primary page; the install hint should name the
vendor page rather than a command string until it does.

**Seat identity and lease.** The extension arms only when **all** hold:
`ctx.mode === "tui"` (a `print`/`rpc`/`json` invocation is not a seat, and this
is the cleanest inertness gate any candidate offers); the four `RT_*` variables
are present and validate against the host lease; and `ctx.cwd` equals the
leased project root. Otherwise it returns silently — a globally linked
extension fires in every session of that harness, and silence is the required
default (§5.2).

**Wake adapter.** On `session_start`, spawn the fenced watcher child; on each
reported generation, call
`pi.sendMessage(<drain instructions>, { triggerTurn: true, deliverAs: "followUp" })`.
`followUp` rather than `steer`: mail is not an interrupt, and steering a live
tool batch to go read mail is a worse user experience than waiting for the turn
to settle. On `session_shutdown` (any reason), stop the child. On a
`session_start` with reason `reload`/`resume`/`fork`, replace rather than
duplicate. Never adopt-or-lose: if the injection call throws, leave the mail in
`new/` and surface the failure (§5.2).

**Orientation `PI.md` / `OMP.md`.** Minimal — seat block, three command forms,
drain rule. No re-arm paragraph.

**Doctor probes.** Presence (executable + version). Arming (pneu's own watcher
fence — the child is a pneu process). Drift: the packaged extension link is
present and points at pneu's copy; and a capability assertion that the
extension API still exports the injection function under the expected name.
Because the injection face is a JS API with no handshake, the drift check is
necessarily weaker than Codex's protocol probe — say so in the probe output.

**Owned setup (approval-gated).** A symlink into each harness's global
extension directory, owned exactly like the Hermes plugin link [S]. Do **not**
use the project-scoped `.pi/extensions` path: it is gated on Pi's project-trust
decision, which is a shared harness trust state and precisely the class of
thing the brief's precedent warns about.

**Validation lab.** L0–L11 (§3), plus:
- **LP-1:** injection while idle and while streaming, for each `deliverAs`
  value, confirming ordering and no loss (Pi's documented no-reject behavior
  must be observed, not assumed).
- **LP-2:** the inertness gate — run `pi` in a non-pneu directory, in `print`
  mode, and in `rpc` mode, and confirm the extension arms in none of them.
- **LP-3:** `session_shutdown` for each documented reason leaves no orphan
  child.
- **LP-4:** extension load-order — confirm the OMP
  `ExtensionRuntimeNotInitializedError` path cannot be reached by pneu's
  registration order.
- **LP-5 (OMP only):** re-run the whole gate at a pinned OMP version, and
  record it. At ~10 releases a week, an OMP support claim is a claim about a
  version, not a product.

**Risks.** Pi's project-trust requirement pushes pneu to a global link, which
widens the blast radius of a bad extension to every Pi session on the host —
the inertness gate is therefore load-bearing, not cosmetic. OMP's cadence makes
sustained support expensive; its API is a superset today, but forks drift.
Recommend building against Pi, shipping OMP as a validated second packaging,
and being willing to park OMP if its API moves faster than pneu's release
cycle.

---

### 6.3 OpenCode — F3, highest value, highest cost

**Currency delta (1.18.18).** The TUI-scoped endpoints are confirmed and are
now three: `POST /tui/append-prompt`, `POST /tui/submit-prompt`, and
`POST /tui/execute-command`. Session-scoped endpoints are
`POST /session/:id/message` and `POST /session/:id/prompt_async` (the latter
accepting a flag to inject context without triggering a reply). The server
self-hosts an OpenAPI 3.1 document at `/doc`. Default bind is 127.0.0.1:4096,
overridable with `opencode serve --port/--hostname`. Authentication is HTTP
Basic via `OPENCODE_SERVER_PASSWORD` (username defaults to `opencode`,
overridable with `OPENCODE_SERVER_USERNAME`); no token-file mechanism is
documented. `opencode attach <url>` connects a TUI to an existing server.
Plugins load from `.opencode/plugins/` or `~/.config/opencode/plugins/` and
receive a much wider event set than the survey listed, including
`session.idle`, `session.status`, `permission.asked/replied`, `file.edited`,
`server.connected`, and `tui.prompt.append` / `tui.command.execute` [D].

Two findings change the design:

- **Blocking gap:** there is no read side to `/tui/*`. An external process
  cannot ask which session the attached TUI is rendering, and an upstream issue
  confirms there is currently no way even to read the prompt box's contents
  [D]. Any design that begins "resolve the TUI's session id" is dead.
- **Unconfirmed lead:** a server registry of state files under
  `~/.local/state/opencode/servers/<sha256-of-cwd>.json` carrying pid, port,
  and secret, so a bare TUI launch can find an existing server for the same
  cwd. The tracking issue is closed but shipped status was not confirmed from
  metadata alone [D?]. Treat as a lab check on a live host, never as a design
  assumption.

**Family: F3.**

**The design that routes around the gap: pneu owns the server.** Rather than
discovering a server and guessing a session, `rt-opencode` constructs the
binding:

1. Generate a per-seat `OPENCODE_SERVER_PASSWORD` into the host-local runtime
   beside the seat lease — never into the project tree, never logged.
2. Start `opencode serve --hostname 127.0.0.1 --port <ephemeral>` as a
   supervised child, recorded in the runtime state with its pid, port, and the
   owning lease revision.
3. Claim the fenced seat, then exec `opencode attach http://127.0.0.1:<port>`
   as the visible TUI.

The seat is still the native TUI the human sits in, so the ruling is satisfied:
the server is infrastructure, not a seat. And the unanswerable question — *which
session is the TUI rendering?* — is replaced by an answerable one: *which
server did pneu start for this seat?* pneu knows that by construction.

**Preferred variant, if the flag exists.** If `opencode attach` accepts
`--session <id>`, pneu should create the session through the server API first
and launch the TUI onto that exact id. That upgrades the binding from identity
answer #3 to answer #2 in §5.3 — pneu tells the harness — and lets the injector
use the session-scoped endpoints instead of the prompt box. **This is the first
thing the lab should test**, because it decides the whole injection path.

**Why the prompt-box path is the fallback, not the default.**
`/tui/append-prompt` appends to whatever the human has already typed. Waking a
seat by appending mail text to a half-written user prompt and submitting it
would corrupt the user's input — an unacceptable failure mode for a tool whose
premise is that the human keeps working in their own TUI. If the
`--session` binding is unavailable, the fallback must first establish that a
clear-prompt operation exists and is atomic with append+submit; if it does not,
**OpenCode should be parked rather than shipped on the prompt-box path.**

**Wake adapter.** `rt-wait-inbox --emit=inject:<argv>` (§1.2), where the
injector is a small pneu command that reads the runtime binding record,
authenticates over loopback Basic auth, and POSTs to the bound session (or, in
the fallback, clear→append→submit). Success means the server confirmed the
submission *and* the seat's session shows the new message — the injector must
distinguish "server accepted" from "TUI rendered", per §5.3.

**Seat identity and lease.** The binding record ties (project root, agent id,
pneu session id, lease revision) to (server pid, port, credential handle,
session id if known). The injector revalidates the whole tuple before every
POST and refuses on any mismatch. A server whose recorded lease revision is
stale is shut down, not reused.

**Server lifecycle — the real cost.** The launcher's normal model is
`os.execv`: the launcher *becomes* the harness [S]. A supervised sibling server
does not fit that model, so this integration needs an explicit ownership
decision: a process-group teardown that survives Ctrl-C and abnormal TUI exit,
or an owned per-project launchd job (seam 9, approval-gated), with `rt-doctor`
reporting orphaned servers the way it already reports runtime residue. **Do not
leave an authenticated loopback server running after its seat is gone.** A
second `opencode attach` to a pneu-owned server is also a fleet hazard: two
TUIs, one injection target. The launcher should refuse or the doctor should
report it — and whether the server can even enumerate attached clients is a lab
question.

**Orientation `OPENCODE.md`.** Minimal. No re-arm paragraph.

**Doctor probes.** Presence and version. Server liveness via an authenticated
read-only call, following the Codex read-only-probe precedent — `/doc` or a
session list, never a mutation. Binding validity (recorded lease revision still
current). Orphan detection (recorded servers with no live seat). Drift: the
`/tui/*` and session endpoint set still matches what the injector calls,
detectable by fetching `/doc` and comparing operation ids; and, if the registry
files are used at all, a shape check on
`~/.local/state/opencode/servers/*.json`, marked as an undocumented surface.

**Validation lab.** L0–L11 (§3), plus:
- **LO-1 (decides the design):** does `opencode attach` accept `--session`?
- **LO-2:** confirm an injected prompt renders in the attached TUI and does not
  spawn a parallel session; confirm no headless conversation consumed the mail.
- **LO-3:** injection while the seat is mid-turn.
- **LO-4:** kill the TUI, kill the server, kill both — no orphan, no stranded
  lease, mail still durable.
- **LO-5:** two TUIs attached to one pneu server — observe and then decide the
  refusal policy.
- **LO-6:** does the server registry exist on a live host, and does a bare
  `opencode` launch adopt a pneu-owned server? If yes, that is a *contamination*
  risk to design against, not a convenience.
- **LO-7:** credential handling — confirm the password never reaches the
  project tree, process listings visible to other users, or any log.

**Risks.** Highest-cost integration in this cohort, and the only one that adds a
long-lived authenticated network listener to a product whose selling point is
that it needs no daemon or network. That tension deserves an explicit operator
decision before implementation, not after.

---

### 6.4 Kilo Code CLI — demoted; source audit before any blueprint

**Currency delta.** `kilo serve`, `kilo daemon start|status|stop|restart`, and
`kilo attach <url> [-c] [-s <session>] [--fork] [--no-replay]` are real, with
HTTP Basic auth via `KILO_SERVER_USERNAME`/`KILO_SERVER_PASSWORD` and optional
mDNS discovery (`--mdns`, default domain `kilo.local`) [D]. But the survey's
`kilo run ... --attach <url> --session <id>` form does not exist, and Kilo's
official documentation contains **no HTTP endpoint reference, no OpenAPI
document, and no plugin API page at all** — the endpoint list and plugin-event
claims in the survey trace to an unofficial AI-generated wiki [D].

**Verdict: A-plausible, not A-ready.** The architecture is visibly
OpenCode-shaped, and the source is public, so this is very likely buildable —
but the project's own rule is that documentation-grade evidence, not
plausibility, admits a candidate. Writing a blueprint on an unofficial wiki's
endpoint list would repeat exactly the failure §6.0 flags.

**Unblocking step (S effort, no implementation):** a read-only source audit of
the public repository answering five questions, after which Kilo either gets a
full blueprint or a parking condition:

1. What HTTP surface does `kilo serve` expose, and is there a TUI-scoped
   endpoint family analogous to OpenCode's `/tui/*`?
2. Does `kilo attach --session <id>` bind the TUI to a caller-chosen session
   (identity answer #2), or only select an existing one?
3. Is there any read side — can an external process learn what the attached TUI
   is rendering?
4. Is there a plugin/extension loading mechanism with a documented or
   source-evident event API, and can a plugin own a long-lived watcher?
5. Does `kilo daemon` change the ownership picture — is the daemon per-project,
   per-user, or global? A global daemon shared across projects would make seat
   fencing materially harder than OpenCode's per-cwd server.

The mDNS default is worth a flag on its own: a harness that advertises itself on
the local network by default interacts badly with a product that promises
local-only, network-free operation. If mDNS is on by default rather than
opt-in, the launcher must disable it for pneu seats.

---

### 6.5 Antigravity `agy` — stays parked; the A-candidate has a specific blocker

**Currency delta (1.1.13, 2026-08-14).** Hooks remain exactly five —
`PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop` — with
the documentation stating explicitly that session-start, conversation-start,
and resume hooks are not present [D]. Releases 1.1.11 through 1.1.13 added Vim
mode, plugin enable/disable hardening, JSON output formats, `schedule`-tool
refinements, and resume-picker fixes; **none added a lifecycle hook** [D]. The
2026-08-05 parking rationale is therefore still correct today.

Sidecars are real and better documented than the survey recorded: config at
`~/.gemini/config/sidecars/<id>/sidecar.json` (or plugin-scoped) with
`command` or `builtin`, `args`, `env`, `description`, `display_name`, and
`restart_policy` ∈ {`always`, `on-failure`, `never`}, default `always`;
enablement and a `projectId` live in `~/.gemini/config/config.json`; the CLI
launches and restarts them; the documented environment handoff is
`ANTIGRAVITY_EXECUTABLE_DATA_DIR`; and `agentapi` is placed on the sidecar's
PATH with `new-conversation <prompt>` and `send-message <conversation_id>
<prompt>` [D].

**The blocker, stated precisely.** `send-message` requires a conversation id,
and **no documented mechanism gives a sidecar the id of the conversation the
human's TUI is rendering** [D]. The `projectId` setting is described as the
project in which `agentapi` *creates* conversations, which points the surface
at creation rather than injection. A sidecar that calls `new-conversation` and
then messages that conversation has built a headless replacement session —
forbidden by decision.md 2026-08-12. So the A-candidate is not "unproven"; it is
missing its first required primitive.

**Exact unpark condition (unchanged in spirit, sharpened in content).** On a
current release, demonstrate that a sidecar can obtain the conversation id of a
live, TUI-rendered conversation through a supported mechanism — an environment
variable, a state file, an `agy` subcommand, or an `agentapi` query — and that
`send-message` to that id wakes and renders in that exact conversation, idle and
busy, across `--continue` and the resume picker. Absent that, `agy` stays at T0:
durable mail, manual drain, no wake claim.

**The B-shape is available but weaker than Grok's, and should not ship.** The
`schedule` tool is model-installable and time-based; combined with a seeded
first turn it satisfies Shape B on paper. But it is a *timer*, not a file-event
monitor: it wakes on a cadence whether or not mail exists, which is exactly the
empty-turn burn that decision.md 2026-08-07 retired. Grok's `monitor` is
acceptable as F4 precisely because it is event-driven and silent when there is
nothing to say; a cron-shaped substitute is not the same product. Additionally,
the survey's `-i/--prompt-interactive` seeding flag could not be confirmed on a
primary vendor page [D?] — the same failure mode that just demoted Copilot.

**Approval note.** Everything about a sidecar integration writes into
`~/.gemini/config/` — shared harness configuration. No exploratory install or
config write should happen without an explicit ruling, per the brief's
precedent.

---

### 6.6 Secondary cohort — corrections and standing

**GitHub Copilot CLI — demoted from B-ready to parked.** Current documentation
(1.0.79) documents `-p/--prompt` as *non-interactive* — it runs one turn and
exits — and documents bare `copilot` for interactive use **with no
prompt-seeding flag** [D]. The survey's `-i/--interactive PROMPT` was not found
under any name. Shape B requires seeding the first visible TUI turn so the
session can install its own schedule; without a seed, installing `/every`
requires the human to type it, which is not an adapter. This is the identical
condition that parks Kimi Code. Everything else re-verified cleanly: `/every`,
`/loop`, and `/after` exist behind `/experimental`, cadence runs 10 s to 1 day,
schedules restart on `--continue`/`--resume` measured from reopen, `agentStop`
can return `decision:block` with the reason used as the next prompt, and the CLI
overrides the hook after **8 consecutive block continuations** [D]. No local
enqueue API into a running local TUI exists.
*Exact unpark:* a documented way to seed the first visible interactive turn
from argv or a lifecycle hook.

**Devin CLI — A-conditional, and the condition got worse.** Confirmed at
3000.4.25: `{"decision":"block","reason":"..."}` from a `Stop` hook turns the
reason into a new user message in the same session, with `stop_hook_active` as
the only recursion guard and no numeric cap [D]. The decisive fact is that
**no default timeout, no maximum, and no on-timeout behavior are documented
anywhere** — the research pass checked the hooks overview, lifecycle-hooks, and
config-file reference pages and found only a bare `"timeout"` field in an
example [D]. Undocumented is worse than long: pneu cannot design an arming
lifetime against it, and cannot tell a user what happens when it lapses.
Devin is also the wrong *shape* — a blocking Stop hook holds the turn open
rather than waking an idle session (§5.1 anti-pattern).
*Standing:* lab-first. No implementation before a live L6 measurement.

**One cross-harness item to verify, low severity.** Devin's hook discovery
includes Claude-compatible locations — project `.claude/settings.json` and
`.claude/settings.local.json`, and user-level `~/.claude.json` [D]. pneu's owned
Claude watcher hooks live in `~/.claude/settings.json` [S], which is *not* in
that list, so the main integration appears unaffected. But `pneu rc-host
enable` does write `WorktreeCreate`/`WorktreeRemove` hook groups into the
project's untracked `.claude/settings.local.json` [S] — a file Devin reads. The
event names are Claude-specific and should be inert to Devin, but "should be
inert" is exactly the assumption that produced the herdr/Codex trust-gate
incident. Worth one verification pass and, if it holds, one sentence in
`docs/compatibility.md`. This is a check, not an alarm.

**Factory Droid — A-conditional, unchanged.** Confirmed at 0.195.0: both the
exit-2-with-stderr and `decision:block` paths are documented, the `timeout`
field defaults to 60 seconds and is configurable per command with no documented
maximum, hooks are snapshotted at startup with a warning on external
modification, and `SessionStart` distinguishes `startup`/`resume`/`clear`/
`compact` [D]. Same anti-pattern as Devin, but with a *documented* timeout,
which makes it the better of the two to lab if either is ever labbed.

**Mastra Code — deferred, not re-verified.** No re-verification was run this
cycle; its survey entry stands as [D-2026-08-12]. Its "A-ready via official
wrapper" verdict also sits uneasily against the TUI-first ruling: a pneu-built
wrapper that renders an exported TUI component is not the vendor's native
interactive binary, and whether that counts as "the native TUI seat" is an
operator question, not a research one. Recommend leaving it out of 1.5 entirely
and asking the ruling question only if the cohort above is exhausted.

**Kiro, Kimi, Cursor — unchanged, still parked** on the survey's exact unpark
conditions [D-2026-08-12]. They were not re-verified this cycle and should not
consume implementation time.

## 7. Prioritized 1.5 proposal

Effort uses the survey's S/M/L vocabulary. "Gate" columns follow the
`docs/compatibility.md` convention: what is packaged and automated, versus what
is still required before a support claim.

| Rank | Item | Family | Effort | Ships as | Gate to a support claim |
| --- | --- | --- | --- | --- | --- |
| **0** | `HarnessDescriptor` refactor + `rt-wait-inbox --emit` modes | — | S | Internal, no new surface, no doc change | Existing suite green with exact-argv tests before and after; no new claim to make |
| **1** | **Qoder CLI** seat | F1 | S | Launcher, owned hook group, `QODER.md`, doctor probes | LQ-1 arming-lifetime measurement, then L0–L10, then L11 clean-account/terminal matrix |
| **2** | **Pi** seat | F2 | M | Launcher, packaged extension, owned global link, `PI.md`, doctor probes | LP-1..LP-4, then L0–L10, then L11 |
| **3** | **Oh My Pi** seat | F2 | S incremental on #2 | Second packaging of the same extension | LP-5 at a pinned version, plus L0–L11 |
| **4** | **Kilo** source audit | — | S | A findings note, no code | n/a — output is a blueprint or a parking condition |
| **5** | **OpenCode** seat | F3 | M–L | Launcher owning a supervised server, injector, `OPENCODE.md`, doctor probes | LO-1 decides the design; then LO-2..LO-7, L0–L10, L11 |
| **6** | Devin **or** Droid measurement lab | F1-shaped | M | A findings note, no code | L6 quiet-interval measurement is the whole deliverable |
| — | agy, Copilot, Kiro, Kimi, Cursor, Mastra | — | — | Parked | Each carries its exact unpark condition above |

### 7.1 Recommended 1.5 scope

**Items 0, 1, and 2 — plus item 4 if there is room.** That is one refactor, two
seats, and a read-only audit.

The reasoning is cost, not enthusiasm. The Grok integration — the cheapest
family, F4, almost no adapter code — still shipped four dedicated test modules
(`test_grok_adapter`, `test_grok_interop_lab`, `test_grok_mutation`,
`test_grok_soak`), a launcher branch set, a doctor evidence probe, an
orientation template, and a `docs/compatibility.md` section [S]. A harness is
not the size of its wake call. Planning three or four new seats into one cycle
would repeat the M2 scope-displacement cost that decision.md 2026-07-29 records.

Sequencing rationale:

- **Item 0 first, and only first.** After two more harnesses land in the
  current ladder, the refactor becomes a risky rewrite of live launch paths
  instead of a mechanical one. It also directly reduces items 1–5.
- **Qoder before everything else with a seat.** It is the only candidate whose
  wake path is a shipped, audited pneu design under a different hook schema.
  Its one open risk (LQ-1) is measurable in an afternoon and answerable before
  any code is written — run the measurement first and let it veto the item.
- **Pi before OMP**, sharing one extension. Pi is the slower-moving upstream
  and the one whose API OMP still accepts in legacy form; building against Pi
  and packaging for OMP is strictly cheaper than the reverse.
- **OpenCode after, not during.** It is the only item that adds a supervised
  authenticated listener to the product, and it deserves its own cycle and its
  own operator decision (§8, Q3).
- **Devin/Droid stay a measurement, not an implementation**, until L6 produces
  a number.

### 7.2 What each item adds to `docs/compatibility.md`

To keep the promotion vocabulary honest, each shipped seat adds one onboarding
matrix row phrased like the existing Grok row — packaged and automated on the
left, *what is still missing* on the right — plus one per-harness section
stating the wake mechanism, the fence, the undocumented surfaces it depends on,
and the exact remaining gate. No row may say "supported" while its right-hand
column is non-empty. Qoder additionally needs one sentence recording the
measured arming lifetime, because that number is the seat's real contract.

## 8. Open questions for the operator

These are decisions, not research gaps. Each blocks or reshapes an item above.

1. **Is item 0 (the launcher refactor) authorized as 1.5 work?** It touches
   every live launch path and produces no user-visible feature. Recommendation:
   yes, first, with exact-argv tests pinned before and after.
2. **Owned-setup approvals.** Items 1–3 require pneu to write into
   `~/.qoder/settings.json` and to link an extension into Pi's and OMP's global
   extension directories. Per the brief's hard constraint these are
   approval-gated. Recommendation: approve as *design targets* now, with the
   actual writes gated behind the normal `plan` → confirm → `apply` flow and a
   `remove` path proven in the same cycle.
3. **Does pneu accept owning a long-lived authenticated loopback server?**
   That is what OpenCode's design (§6.3) requires, and it sits against the
   product's "no daemon, no network" framing. A "no" parks OpenCode cleanly
   and saves the largest item in the plan; a "yes" should come with the
   ownership model (process group versus launchd) decided up front.
4. **Does a vendor-provided TUI component count as a native TUI seat?** This is
   the Mastra question and it will recur. Recommendation: no — the seat is the
   vendor's own interactive binary — but it is Ocean's ruling to make.
5. **Adopt the [D?] evidence tag?** §6.0 found four survey claims resting on
   secondary sources or plausible flag names, two of which were wrong.
   Recommendation: yes — a readiness verdict may not cite a wiki, cheat sheet,
   or changelog summary.
6. **Confirm the Devin `.claude/settings.local.json` cross-read** (§6.6) before
   the next `rc-host` change, and record the result either way.

## 9. What this track did not do

- Installed nothing, launched no third-party harness, read no credential store,
  and modified no shared harness configuration.
- Did not re-verify Mastra Code, Kiro, Kimi, or Cursor; their survey entries
  stand as older evidence and are marked as such.
- Did not fetch OpenCode's live OpenAPI document (it requires a running
  server), so the endpoint schemas above are documentation prose, not verbatim
  spec.
- Did not confirm OpenCode's server-registry state files, Qoder's executable
  name, Qoder's hook-timeout ceiling, Kilo's HTTP surface, or Antigravity's
  conversation-id discovery. Each is written above as a lab question, not as a
  design assumption.
- Produced no adapter code, no launcher change, and no `docs/compatibility.md`
  edit. Every claim here remains a proposal until an operator schedules it and
  its live gate passes.
