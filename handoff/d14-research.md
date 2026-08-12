# D14 research + docs audit dispatch (rt-d14-research)

Owner: Claude (product lead) on behalf of Ocean. Branch: `wt/rt-d14-research`.
Two deliverables on this branch: `handoff/d14-tui-first-survey.md` (Q1) and
a docs-audit commit series (Q2). Status mail to `claude@roundtable-product`
when done.

## Q1 — grok / OpenClaw TUI-first survey (design input, no product code)

Owner ruling 2026-08-12 (decision.md): user-launched seats are interactive
TUIs; wake adapters are background channels INTO a TUI session, never a
replacement. Grok's ACP supervisor and OpenClaw's Gateway adapter predate
the ruling. Survey, for each harness:

1. What is the interactive TUI surface? (`grok` TUI; OpenClaw's user
   surface — identify what a human actually sits in.)
2. Does a background channel exist INTO a live TUI session — the
   equivalent of Codex app-server thread injection or the Hermes
   background-completion rail? Enumerate concretely: local RPC/socket,
   session files, plugin/hook systems, resume semantics. Source-level
   evidence preferred (both tools are installed on this host; read-only
   inspection only — do not launch seats, do not touch live state,
   credentials are strictly read-only).
3. Verdict per harness: (a) rework path exists — sketch the adapter shape
   (TUI = seat, current adapter demoted to wake channel); or (b) no
   injection surface today — recommend honest parking per the Antigravity
   precedent, stating exactly what upstream capability would unpark it.
4. Note what happens to the D13.2 full-permission ACP policy under each
   verdict (expected: moot under (a) because work happens in the TUI under
   the harness's own approval UX; still relevant under (b) only if the
   mail-drain seat is retained).

## Q2 — docs 实然/应然 audit (fix-in-place for docs, list-only for code)

Sweep the repository's teaching layers for machine-specific or
history-shaped claims presented as universal contracts. Known seeds — verify
each, fix doc-only items in place, and record code-behavior items as a
backlog list in the survey file (do NOT change bin/ or integrations/):

- README Development section: `mamba run -n general ...` presents a
  machine convention as the project's dev requirement. Rewrite to a
  neutral form (any CPython 3.11–3.14 env + pip install of dev deps, with
  the mamba line as one example).
- skills/shared/pneu/SKILL.md: teaches bare-name `rt-*` (breaks under
  bare-PATH contexts — proven by the phone-spawn incident) and unfenced
  command forms while the Stop hook teaches `--fenced --no-nudge`. Align:
  teach the hook-injected forms as canonical, name the PATH precondition,
  keep principle-over-spelling.
- Skill trigger description still cites "cmux surface-routing bugs";
  ROUTING.md trigger `surface_or_routing_debug`; AGENTS.md cmux adapter
  line; templates/*.tmpl seed the same into every new project. cmux
  retired 2026-08-01 — update wording (cmux stays mentioned only as an
  optional adapter where factually true).
- Orientation asymmetry: HERMES.md is a root-level first-class file while
  OpenClaw/Grok have no orientation at all (history: first-three
  harnesses). List what a GROK.md/OPENCLAW.md would need IF the Q1 verdict
  is rework — do not create them yet.
- Principle item for the backlog list: every dependency on an
  UNDOCUMENTED harness internal (trust records in ~/.claude.json,
  CLAUDE_ENV_FILE semantics, `thread/name/set` RPC) should carry a doctor
  drift check; note which exist and which are missing.

## Discipline

- Doc edits must pass `scripts/check_public_safety.py` (no personal
  absolute paths/usernames in committed text — quote paths as `~/...`).
- Full suite + compileall after doc changes (some tests pin doc strings).
- No `Claude-Session:` trailers. Read-only toward live state and other
  harnesses' credentials.
