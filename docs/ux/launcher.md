# UX-SPEC — the `pneu` entry surface

> Status: current, as built in 1.3.5. Every screen, string, and mutation below
> was read out of `bin/pneu` and `bin/_rtlauncher.py`, not out of memory. The
> §5 to-be section is the only part that describes behavior that does not
> exist.

This is the first front-to-back UX spec in this repository and the format
other surface specs should copy: **screens**, then **what each rendered
element reads**, then **what each key writes**. A surface described only as a
list of behaviors reliably produces a correct mechanism behind an incoherent
screen (`PRINCIPLES.md`, principle 5).

Scope: bare `pneu` with no arguments — project selection, the first-run
welcome, and the seat card. Sources: `bin/pneu` (`main`, `onboard`,
`choose_project`, `seat_inventory`, `_addable_harnesses`, `_render_seat_card`,
`choose_seat`, `choose_seat_card`, `show_first_run_welcome`) and
`bin/_rtlauncher.py` (`HARNESS_LABELS`, `COMMANDS`, `harness_bin`,
`harness_unavailable_detail`, `configured_sender_ids`, `project_at_or_above`).

**Everything in this document is written to stderr.** Only `help`, `guide`,
and `version` write to stdout. `pneu 2>/dev/null` therefore shows nothing at
all, and a caller that captures stdout gets a clean stream.

---

## 1. Screens

### 1.1 Entry routing

```text
pneu <command>  ──▶  alias table ──▶ exec rt-<tool>        (process replaced)
pneu help|guide|version ──▶ stdout, exit 0
pneu <unknown>  ──▶ "pneu: unknown command: X" + usage, exit 2

pneu  (no args)
  ├─ stdin is not a TTY ──▶ usage + exit 2            (no prompt, ever)
  └─ stdin is a TTY
       ├─ project selection (§1.2)
       ├─ first-run welcome, once per project (§1.3)
       └─ seat selection
            ├─ stdin AND stderr are TTYs ──▶ seat card (§1.4)
            └─ otherwise           ──▶ numbered selector (§1.8)
```

The rich card requires **both** `stdin.isatty()` and `stderr.isatty()`
(`_rich_card_available`). Redirecting stderr alone is what produces the
line-oriented fallback; redirecting stdin produces no menu at all.

### 1.2 Project selection

Inside a project (any ancestor directory holding a project anchor), there is
no menu — one line, then straight to the seat surface:

```text
pneu project: <home>/Code/acme
```

(`<home>` stands in for the user's home directory throughout; the launcher
prints real absolute paths.)

Outside one, with at least one registered project on the host:

```text
This folder is not a pneu project yet: <home>/scratch
Choose a pneu project:
  1) Choose an existing project
  2) Set up this folder safely: <home>/scratch
  3) Set up another existing folder
  4) Create a new folder
Select project:
```

Option 1 appears only when the registry has at least one active, available
project outside `$HOME` and `/`. Option 2 disappears when the current
directory is `$HOME` or the filesystem root. Choosing option 1 opens a
second-level list:

```text
Choose an existing pneu project:
  1) <home>/Code/acme
  2) <home>/Code/acme-worktree/docs
Select existing project:
```

On a host with **no** registered projects at all, the menu is preceded by an
orientation sketch, because a first-time user has nothing else to go on:

```text
  [project folder]
         |
  [durable mailboxes]
     /    |    \
 Claude  Codex  Hermes

A pneu project can be any folder; Git is optional.
```

Every prompt on this screen gives three attempts before it fails
(`PROMPT_ATTEMPTS`); an invalid entry reprints as
`pneu: invalid project selection: 'x'; please try again.` A closed stdin ends
onboarding with `pneu: input closed during onboarding`.

### 1.3 First-run welcome

Shown once per project, gated on `welcomePending` in
`.roundtable/launcher.json`, and only on the rich-card path:

```text
Welcome to pneu — acme
Local agent seats share durable project mailboxes.
Tutorial now? no  [?]
Claude phone connection now? no  [p]
The Claude phone connection affects only Claude mobile/web remote sessions
for this project; desktop seats and other harnesses are untouched.
Enter continue · ? guide · p Claude phone · q quit
```

(The advisory line is one wrapped paragraph in the source, not three lines.)

The `p` key here has a deliberate shortcut: if the toggle leaves phone access
**on**, the welcome is dismissed and the flow continues to the seat card in
the same keystroke. If the toggle fails or leaves it off, the screen redraws
with the failure notice appended. `q` dismisses the welcome permanently and
exits 0 without launching anything.

### 1.4 Seat card — the main screen

```text
pneu — acme
seats
 > Claude Code — claude
   Codex — codex (bound thread)
   Hermes — hermes
   unavailable: Grok Build — not configured in this project — press a to add
active worktrees: 2
unread mail: claude=3 codex=0 hermes=0
Claude phone connection: on  [p]
↑↓/1-9 select · Enter launch · a add seat · p Claude phone · w worktrees · ? guide · q quit
```

Layout facts that matter for anyone editing this renderer:

- the screen is cleared and homed (`\x1b[2J\x1b[H`) on every redraw, so the
  card never scrolls;
- seat rows are `" {marker} {label} — {agent}"` — one leading space, then
  `>` for the cursor row or a space, then a space. Unavailable rows carry
  three leading spaces and no marker, and can never be selected;
- the label is the human name (`Claude Code`, `Grok Build`), not the harness
  key;
- ` · a add seat` is present in the footer only when at least one installed
  harness has no seat in this project;
- the three status lines are always rendered, including on an empty project;
- **no row shows whether the seat is occupied.** A seat with a live owner
  renders identically to a vacant one; the only lease-derived signal on the
  card is the `(bound thread)` suffix, and that appears precisely when the
  seat is *not* live. §5.4 is the accepted fix.

### 1.5 Unavailable rows — two kinds

Both kinds are rendered by the same row, and the difference is the remedy.

**Installed but not configured here** — the row invites the fix that the
footer's `a` key performs:

```text
   unavailable: Grok Build — not configured in this project — press a to add
```

**Configured but the executable is missing** — the row keeps the install
guidance, because `a` cannot help:

```text
   unavailable: Grok Build — missing executable `grok` (it is not installed). Install Grok Build using its vendor installer, then run pneu again; or set RT_GROK_BIN to its executable path.
```

The `; or set …` clause appears only for the three harnesses with an
executable override (`RT_CLAUDE_BIN`, `RT_HERMES_BIN`, `RT_GROK_BIN`); Codex
has none, because its resolver is shared with the daemon and the bridge. When
a candidate path exists but is not executable, the first clause becomes
``the grok executable at /path/to/grok is missing or not executable``.

A third case — **neither installed nor configured** — renders on the card too,
with both facts in one row:

```text
   unavailable: Grok Build — not configured in this project; missing executable `grok` (it is not installed). Install Grok Build using its vendor installer, then run pneu again; or set RT_GROK_BIN to its executable path.
```

The card's override at `bin/pneu:1144-1147` rewrites the detail only for
harnesses in the addable set, and a harness is addable only when its
executable resolves. Every other unavailable row prints the detail
`seat_inventory` produced, unchanged. So all three cases reach the card;
what differs is whether the row can promise that `a` will fix it.

### 1.6 Empty project

A project whose roster has no launchable seat still renders the full card, so
the user can see why and act:

```text
pneu — acme
seats
   unavailable: Claude Code — not configured in this project — press a to add
   unavailable: Codex — not configured in this project — press a to add
active worktrees: 0
unread mail: none
Claude phone connection: off  [p]
↑↓/1-9 select · Enter launch · a add seat · p Claude phone · w worktrees · ? guide · q quit
No configured seat yet; press a to add one
```

`unread mail: none` is the empty-seat-list rendering, not an error. The last
line is the notice produced by pressing Enter with nothing selectable.

When there is **neither** a launchable seat **nor** an addable harness, there
is no card at all — the launcher refuses with one line naming every reason:

```text
pneu: no launchable configured harness seats in <home>/Code/acme; claude: not configured in this project; missing executable `claude` (it is not installed). Install Claude Code from Anthropic, then run pneu again; or set RT_CLAUDE_BIN to its executable path. | codex: …
```

### 1.7 Notice line and bound-thread suffix

The notice is a single optional line under the footer, cleared on the next
keystroke. Its possible contents:

| Trigger | Notice |
| --- | --- |
| `a` succeeded | `added Claude Code seat claude` |
| `a` refused | the `pneu: refusing add-seat because …` reason |
| `p` succeeded | `Claude phone connection enabled` / `disabled` |
| `p` failed | the last line of `rt-rc-host`'s output |
| Enter with no seats | `No configured seat yet; press a to add one` |
| Enter, handoff refused | the last line of `rt-codex-wake handoff`'s output |

The ` (bound thread)` suffix appears on a Codex seat row when **all** of these
hold, re-evaluated on every redraw (`_bound_codex_thread`):

- the host runtime holds a `roundtable.codex-wake.v1` state file, owned by
  this UID, a regular file, no symlink, at most 4 MiB;
- it records a binding for this exact project root **and** this exact agent;
- the binding's `projectUuid` and `projectRegisteredAt` still match the
  registry row for this root — a tombstoned-then-re-registered path is
  discarded, so Enter cannot resume a thread belonging to a previous project
  identity. A binding written before those fields existed makes no claim
  either way and stays usable;
- the seat's own lease is `vacant` or `stale`. A live seat is never offered
  for resume.

Any read error, parse error, or schema mismatch is a quiet miss: the suffix
simply does not appear.

### 1.8 Numbered selector (stderr is not a terminal)

The fallback is a different rendering of the same inventory, and it uses the
harness **key** where the card uses the label:

```text
Choose a harness seat:
  1) claude — claude
  2) codex — codex (starts with a visible automatic pneu activation turn)
  unavailable: grok (Grok Build) — not configured in this project; missing executable `grok` (it is not installed). Install Grok Build using its vendor installer, then run pneu again; or set RT_GROK_BIN to its executable path.
Select harness:
```

Differences from the card, all deliberate and all worth preserving in any
refactor:

- Codex and Grok rows carry the activation-turn hint; the card does not show
  it, because the card has no room and the guide covers it;
- there is no add-seat affordance, no phone toggle, no status lines, and no
  bound-thread resume — this path exists for scripted and captured runs;
- with no launchable seat it raises the same refusal as §1.6, exit 2.

### 1.9 Sub-screens

`?` and `w` replace the card with a full-screen reference and return on Enter:

```text
<ASCII guide, or the output of `pneu worktree list`>
Press Enter to return.
```

One wrinkle, recorded because it is as-built and surprising: `_run_card_command`
resolves its output as `(stderr or stdout)`, and `rt-worktree list` prints the
listing on stdout while printing registry warnings on stderr. A host with any
registry warning therefore sees the warnings under `w` **instead of** the
sibling list, not alongside it. The same precedence governs the `p` notice.

`a` prints one inline prompt line instead of a screen:

```text
Add seat: 1 Claude Code · 2 Grok Build · q cancel
```

It consumes keys until a valid digit or `q`; nothing else does anything.

After a seat is selected and before the harness starts, an unconfigured
harness integration interposes one plan:

```text
One-time codex integration setup:
  - <action>
  - <action>
Apply these managed changes? [y/N]:
```

Declining is not a silent no-op — it aborts. Because `onboard` routes every
`OnboardingError` raised after project selection through
`project_ready_recovery`, the printed line carries that suffix too:

```text
pneu: codex setup was not applied; nothing was launched; project already created at <home>/Code/acme; run pneu again and choose it from the list
```

Exit 2. Applying
Codex setup additionally prints the hook-trust advisory: `Codex may ask once
to review the pneu SessionStart hook. Approve it with /hooks; pneu never
bypasses hook trust.` This step is skipped entirely for Grok (which installs
no hooks, plugins, or LaunchAgents) and for source-tree runs with no
`ROUNDTABLE_INSTALL_PREFIX`.

---

## 2. Fact-source table

Every element is re-read on each redraw. The card is a view; it caches
nothing across keystrokes.

| Rendered element | Fact it renders | Read from |
| --- | --- | --- |
| `pneu — acme` | the selected project root's basename | `Path.name` of the resolved anchor |
| seat rows | one row per configured instance id whose harness executable resolves | `configured_sender_ids` over `.roundtable/agents.yaml` × `harness_bin` |
| row label | the harness's human name | `HARNESS_LABELS` |
| row agent | the configured instance id | `agents.<name>.instances[].id`, defaulting to the block name |
| row order | fixed harness order, then configuration order within a harness | `HARNESS_ORDER` = claude, codex, hermes, grok |
| cursor position | the last seat launched from this project | `lastSeat` in `.roundtable/launcher.json`; index 0 when absent or unmatched |
| ` (bound thread)` | a resumable Codex thread for a non-live seat | `<runtime>/rt-codex-wake-state.json` × project registry × `inspect_seat` |
| `unavailable:` rows | a harness with no launchable seat here | `seat_inventory` + `_addable_harnesses` |
| unavailable detail | why, and the next action | `harness_unavailable_detail`, or the addable override string |
| `active worktrees: N` | other available active registered projects sharing this project's derived group | `~/.pneu/projects.yaml` via `load_project_registry` |
| `unread mail: a=N` | files in each seat's `new/` excluding dotfiles and `ack-*` | registry-selected mailbox via `resolve_project_mailbox_checked` |
| `Claude phone connection` | whether this project has rc-host state | `rc-hosts/<project-uuid>.json` via `load_state_for_project` |
| ` · a add seat` in footer | at least one installed harness has no seat here | `_addable_harnesses` |
| welcome screen | first run for this project | `welcomePending` in `.roundtable/launcher.json` |

Three failure modes are rendered as ordinary values rather than errors, so an
unhealthy host still gets a usable card: an unreadable registry renders
`active worktrees: 0`, an unresolvable mailbox renders every seat's unread
count as `0`, and an rc-host state error renders `off`.

---

## 3. Mutation table

| Key | Writes | Where |
| --- | --- | --- |
| `↑` `↓` `1`–`9` | nothing | — |
| `?` `w` | nothing | `w` runs `rt-worktree list`, which is read-only |
| `a` | **appends one seat block** | `<project>/.roundtable/agents.yaml` |
| `p` | enables or disables the project's phone host | `rc-hosts/<uuid>.json`, one per-project LaunchAgent, and the project's untracked `.claude/settings.local.json` |
| `q` | nothing | returns exit 0 |
| `Enter` | records the launched seat; for a bound thread, performs the Codex handoff first | `.roundtable/launcher.json` (git-ignored), then the Codex binding |
| Enter/`q` on the welcome | clears `welcomePending` | `.roundtable/launcher.json` |

**`a` is the only write to durable, committed project state.** It is the one
key that changes the project's collaboration authorization list, and it is
deliberately narrow:

- the file must be a user-owned regular file, opened `O_NOFOLLOW`;
- it must parse, carry schema `roundtable.agents.v1`, and have an `agents`
  mapping;
- the harness must not already have a block, under either its canonical name
  or any alias in `CONFIG_HARNESSES`;
- the layout must be recognizable — exactly one `agents:` line, and nothing
  unindented after it;
- the write is `O_EXCL` temp file → `fsync` → **compare-and-swap on
  `(st_dev, st_ino, st_size, st_mtime_ns)` against the file read at the
  start** → `os.replace` → directory `fsync`. A concurrent edit is refused,
  not clobbered.

The appended block is exactly the `roundtable-init` shape:

```yaml
  grok:
    harness: grok-build
    instances:
      - id: grok
```

`p` is a second mutation, and it is not project-*repository* state: it writes
the registry-adjacent rc-host record, a LaunchAgent, and an untracked
`.claude/settings.local.json`. It is delegated wholesale to `rt-rc-host
enable|disable`, which owns every one of its own guards — including the
refusal when the project has no accepted Claude workspace trust decision. The
card shows only the last line of that command's output.

Enter's `launcher.json` write is host-local run state, not project
configuration: the file is in the generated `.roundtable/.gitignore`.

---

## 4. Agent-facing surface notes

- **Streams.** The entire interactive surface is stderr. `help`, `guide`, and
  `version` are the only stdout writers.
- **Exit codes.** `0` success or a deliberate quit; `2` for every
  `OnboardingError`, unknown command, and the non-TTY refusal; `130` for
  Ctrl-C, which prints `pneu: cancelled by user (Ctrl-C); no agent session
  was launched.` and, when a project was already created, tells the user to
  rerun and pick it from the list.
- **Non-TTY contract.** Bare `pneu` never prompts without a terminal on
  stdin. Scripts use the explicit subcommands (`pneu claude`, `pneu send`,
  `pneu inbox`), which exec their `rt-*` target directly.
- **Process model.** A launch is `os.execv`, not a subprocess: the launcher
  replaces itself, `RT_FROM` is exported, and the working directory becomes
  the canonical project root. The invoking shell is the harness's parent and
  gets its prompt back when the harness exits.
- **Raw mode.** The card reads single bytes in cbreak mode and restores the
  previous termios settings in a `finally`. Arrow keys are decoded from
  `ESC [ A` / `ESC [ B` with a 50 ms readiness probe per byte; anything else
  escape-prefixed becomes `unknown` and is ignored.
- **Idempotence.** Drawing the card mutates nothing, so it is safe to open,
  read, and quit at any time — including while seats are live.

---

## 5. To-be — accepted design, not implemented

These are design deltas awaiting scheduling. Nothing in this section
describes 1.3.5 behavior, and this track shipped no change under `bin/`.

### 5.1 Census the roster at project birth

`roundtable-init` writes a fixed `claude` / `codex` / `hermes` template
regardless of what is installed on the host. That is why Grok Build is never
in a new project's roster, and why a Grok user's very first card shows an
`unavailable … press a to add` row for a harness they do have.

**To be:** census the installed harnesses once, at init, and write the roster
that matches the machine. Once only — the roster is an authorization list,
not a live scan (`PRINCIPLES.md`, principle 3), so a harness installed later
is added by an explicit act, not by the next render.

### 5.2 Inherit the roster at worktree birth

There is no pneu mechanism for this today. A linked worktree gets whatever
`agents.yaml` the branch has committed, by ordinary Git checkout, and
`pneu worktree add` then runs `roundtable-init`, which fills in only missing
files.

**To be:** a new tree explicitly inherits its parent checkout's roster at
creation, so the inheritance is a pneu fact rather than a Git side effect —
and so it still holds for a tree whose `agents.yaml` is not committed.

### 5.3 `pneu seat add` / `pneu seat rm`

The card's `a` key is the only roster write that ships. There is no scriptable
equivalent and no removal path at all: taking a seat out of a project means
editing `agents.yaml` by hand.

**To be:** `pneu seat add HARNESS` and `pneu seat rm AGENT` as explicit
commands, with `a` becoming the card's shortcut into the same code. Removal
needs its own refusal rules — at minimum, refusing to remove a seat with a
live lease or undrained mail.

### 5.4 Seat rows must show occupancy

Ruled 2026-08-18. The three items below (§5.4–§5.6) are one design, driven by
a real incident: a Claude phone/web session — a `claude --print --sdk-url
.../cse_…` process — held the main project's seat. The user ran the launcher
to get back to work, and got a raw lease refusal with no options and no
indication that the holder was their own phone session. Every piece of what
they needed to know existed in the runtime; none of it reached the screen.

**As built:** the card renders no occupancy at all, and the refusal comes
later, from inside `rt-<harness>` after the card has already exited.

**To be:** each seat row carries its occupancy state — **vacant**, **active**,
or **stale** — and, when the runtime knows it, the owner's locus:

```text
pneu — acme
seats
 > Claude Code — claude          active — phone session since 09:58
   Codex — codex                 active — pane w1:p3
   Hermes — hermes               vacant
   Grok Build — grok             stale — owner pid 41822 is not running
```

The locus comes from what the lease and the advisory surface record already
hold — the recorded pane, workspace, or endpoint for a surface-launched seat;
the session shape and start time otherwise. It is navigation metadata, never
an ownership fact (`PRINCIPLES.md`, principle 3, and the `surface.json`
contract in `docs/architecture.md`), so the row must degrade cleanly: an
unknown locus renders `active` alone rather than a guess.

`(bound thread)` stays exactly as it is and does not compete with this
column: it appears only on a vacant or stale Codex seat, so the two are
mutually exclusive by construction.

### 5.5 Enter on an active seat must offer a choice, not a dead end

**As built:** the card does not check occupancy before Enter. It records the
seat, `execv`s `rt-<harness>`, and the launcher's `claim_launch_seat` raises
`SeatOccupied`, which the harness wrapper prints and exits on. The card is
gone by then, so there is nothing to return to and no option to pick.

**To be:** the card resolves occupancy before it launches, and an active seat
opens a decision, in the same design language as the existing Codex
guarded-handoff:

```text
Claude Code — claude is active (phone session since 09:58)
  j  jump to that surface
  t  take over the seat  (the current session loses it)
  q  cancel
```

Three rules for that panel:

- **jump** appears only when a surface record exists and names a reachable
  surface. It is navigation, not a claim: nothing about the lease changes.
- **take over** is a guarded action, not a force flag. It reuses the existing
  fenced replacement path and states in one line what the current holder
  loses. A seat whose liveness cannot be established safely is not
  takeover-eligible — it fails closed exactly as `claim` does today.
- **cancel** returns to the card with the row still marked active. Cancelling
  must be free; nothing is written on the way in or out.

A stale seat needs no panel: it is already replaceable, and Enter proceeds as
it does now.

### 5.6 Refusals outside the card must name the holder and the next action

Non-card contexts still refuse in one line: `pneu` with a non-TTY stderr, a
direct `rt-claude`, `pneu worktree open`, a script. Today that line is
forensics:

```text
rt-claude: seat 'claude' is active in <home>/Code/acme; owner pid 41822 is running; wake heartbeat age=7.3s
```

`_owner_process_location` tries to enrich it, but it only knows how to find a
controlling tty or a tmux pane. A phone/web session has neither, so in the
incident above it contributed nothing and the user was left with a pid.

**To be:** the refusal answers who, where, and what next, and keeps the
forensics as a trailing detail rather than the whole message:

```text
rt-claude: seat 'claude' is held by a Claude phone session started 09:58.
  Resume it there, or run `pneu` and take the seat over from the card.
  (owner pid 41822 live, wake heartbeat age 7.3s)
```

The locus vocabulary is shared with §5.4 — one resolver, used by both the row
and the refusal, so the card and the command line never disagree about who
holds a seat.
