# Spec — D14 OpenClaw: demote from the shipped seat surface

Dispatch for the `codex` seat in this worktree. Ocean ruled on 2026-08-17 after
the retain / park / drop framing: **implement the demotion (option C), keeping
the code as lab machinery, with the door open for a TUI-first rework later.**

## Why this is a change and not a parking note

The status quo is not "parked and harmless". `rt-openclaw` ships in every
release (`pneu_packaging/__init__.py` `TOOLS`), `bin/pneu` offers it as a
selectable seat, `README.md:15,105` advertises it, and
`docs/compatibility.md:18` lists it in the harness onboarding matrix. So today
the product hands users a seat that:

1. violates `decision.md` 2026-08-12 — it spawns a private isolated Gateway and
   drives it headlessly, and `bin/_rtlauncher.py` carries an explicit invariant
   that it must *never* attach to the user's own OpenClaw instance, so no human
   surface exists at all;
2. can write a token-bearing `openclaw.json` into a tracked working tree (see
   `git show wt/t6-packaging:handoff/d14-openclaw-isolation-root.md`);
3. has no validation path — `openclaw` is not installed on the development host,
   and the last audited source (2026.5.4) is two minor lines behind upstream.

Ocean's framing was decisive: this is for other users, not for us. That raises
the bar rather than lowering it, and it converts (2) from a recorded defect into
a must-fix.

## Precedent to follow exactly

The Grok ACP supervisor was demoted the same way and the wording is already in
the tree. `docs/compatibility.md` §"Grok native TUI seat" ends with:

> `rt-grok-wake` and `integrations/grok/roundtable` remain packaged and directly
> invocable only as internal ACP mail-drain lab machinery. They retain their
> isolated HOME/XDG/GROK_HOME/TMP/log boundary, fenced identity checks,
> permission policy experiments, and prior fault/mutation/soak evidence, but
> `rt-grok` never selects them.

Mirror that shape. The difference: Grok had a TUI-first replacement to point
`rt-grok` at. OpenClaw has none yet, so the seat surface goes away rather than
being redirected.

## Scope

### 1. Remove OpenClaw from the seat surface

- `bin/rt-openclaw` becomes a **refusal stub**: it must not reach
  `_rtlauncher.main("openclaw")`. It prints what happened, names the decision,
  points at `rt-openclaw-wake` as the lab path, and exits non-zero.
- Remove `openclaw` from `bin/_rtlauncher.py`'s harness tables (`COMMANDS`,
  `HARNESS_LABELS`, `HARNESS_INSTALL_HINTS`, `EXECUTABLE_OVERRIDES`,
  `CONFIG_HARNESSES`), its anchor-refusal branch, its `os.execv` detour to the
  adapter, and its entries in `_adapter_module` / `_adapter_harness_bin`. Grok
  must keep working through those same code paths — this is a deletion of one
  harness's entries, not a restructure of the mechanism.
- Remove `openclaw` from `bin/pneu`: `:52`, `:60` (`HARNESS_ORDER`), `:65`,
  `:88` (help), `:101` (banner), `:123` (guide), `:129`, and the `:300` branch.
  Check for others; that list is from a grep, not an audit.

**Design decision already made — do not change it without mailing back.**
`rt-openclaw` stays in `pneu_packaging/__init__.py` `TOOLS` as a stub rather
than being deleted from it. Removing a `TOOLS` entry means an install marker
written by an older build lists a tool the new build does not know, and the
verification path compares tool digests by exact set equality
(`pneu_packaging/cli.py:1116-1131`). That is the same forward-compatibility trap
D2 documents for the harness manifest, pointed the other way. **Verify this
hazard is real before accepting the decision** — if you can show removal is safe
for an already-installed prior version, say so with evidence and we will
reconsider. `rt-openclaw-wake` stays in `TOOLS` unchanged: it is the lab entry
point and remains directly invocable.

### 2. Fix the isolation-root defect (mandatory under this ruling)

`integrations/openclaw/roundtable/__init__.py:create_isolation` (~:202-228) must
reject a resolved root beneath the resolved project **before any of the root,
state, home, tmp, or log directories are created**, mirroring
`integrations/grok/roundtable/__init__.py:265-277` — which computes the root,
applies `_under(root, project)`, and raises `GrokError` first. Raise the
OpenClaw error type, not `GrokError`. Add regression coverage matching the Grok
guard's test.

This is retained lab machinery that still runs on a real machine, so the guard
is required even though the seat is gone.

### 3. Documentation — say what is true

- `docs/compatibility.md:18` — rewrite the matrix row. It currently sells
  "Isolated Gateway adapter" as what is packaged and automated. It must instead
  record that the Gateway adapter is retained as internal lab machinery, that
  there is no OpenClaw seat, and what a future seat would require.
- `docs/compatibility.md` — add a short OpenClaw section in the style of the
  Grok section: the 2026-08-12 ruling, why the current shape deviates (isolated
  Gateway, never attaches to the user's own instance, no human surface), that
  the adapter stays invocable for labs, and the rework preconditions (a healthy
  current-version install to validate against, a trust-boundary design for
  attaching to the user's own Gateway, and a live-render probe).
- `README.md:15` — OpenClaw must come out of the list of integrations that "may
  wake the recipient". `README.md:105` — the architecture bullet must not
  present the isolated Gateway adapter as a supported wake path.
- `decision.md` — add the 2026-08-17 owner decision at the top, reverse
  chronological, in the file's existing voice: OpenClaw demoted from the shipped
  seat surface under the 2026-08-12 TUI-first ruling; adapter retained as lab
  machinery; isolation-root guard required; rework path and its preconditions
  left open. Attribute it to Ocean.

  **The entry must also record the reasoning as a generalized calibration, not
  as an OpenClaw footnote.** This is the durable lesson and it is why the
  decision came out the way it did. Both halves:

  1. *Parking a shipped surface is not zero-cost, because parking it means
     continuing to ship it.* The retain / park / drop framing that was put to
     Ocean costed "park" as zero investment. That was wrong: `rt-openclaw` was
     in `TOOLS`, offered by the launcher card, and advertised in `README.md` and
     `docs/compatibility.md`, so the cost of doing nothing was one more release
     of a seat that violated the project's own architecture ruling. For anything
     already in the shipped surface the honest framing is **retain /
     keep shipping / stop shipping**, and "do nothing" is never the null option.
  2. *"We do not use it" is not a reason to deprioritize a shipped surface.* It
     removes the validation path, and under this project's own support-claim
     discipline that raises the bar rather than lowering it — a harness nobody on
     the team can exercise cannot carry a support claim, yet it was shipping.
     Ocean's correction ("I may not use it, but it is for other users") is what
     turned the isolation-root defect from recorded into must-fix.

  Keep it to the length of the existing entries; the file's other decisions
  manage this in a paragraph.

### 4. Tests

- The isolation-root regression above.
- Assert `bin/pneu` never offers OpenClaw as a selectable seat and that its help
  and guide text no longer name it.
- Assert `bin/rt-openclaw` refuses and exits non-zero without claiming a lease.
- Assert `rt-grok` still launches through the shared adapter-resolution path —
  it is the remaining user of `_adapter_module`, so a careless deletion breaks
  it silently.
- Whatever existing OpenClaw tests assert seat behaviour
  (`tests/test_openclaw_adapter.py`, `test_openclaw_lab.py`,
  `test_openclaw_interop_lab.py`, `test_openclaw_mutation.py`) must be updated
  rather than deleted: the lab machinery still exists and its coverage still
  applies; only the seat-path assertions change.

## Explicitly out of scope

- No rework of the adapter's internals, no TUI-first implementation.
- No unification with the Grok adapter — both reviews recorded that as
  investment against parked code.
- No deletion of `integrations/openclaw/`.
- Do not touch `_rtmigrate.py` (frozen), and do not restructure the launcher
  ladder — the `HarnessDescriptor` refactor is a separate, later item.

## Acceptance

Full suite green, `python -m compileall -q bin pneu_packaging scripts tests`,
and `python scripts/check_public_safety.py` green. The suite needs a Python with
dev dependencies; `python` on PATH is pneu's managed interpreter and has no
pytest.

State explicitly in your report whether you verified the `TOOLS` removal hazard
in §1, and whether the grep-derived `bin/pneu` line list was complete.

## Constraints

- English-only, public-safe. No `Claude-Session:` trailer.
- Commit to `wt/t5-adapters` only. Do **not** merge.
- Report to `claude@roundtable-product` **and** to `claude` in this project —
  main is holding merge and release on this item, so it needs the completion
  signal directly.
