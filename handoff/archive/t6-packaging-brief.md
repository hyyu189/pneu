# T6 — packaging defects D3/D4 + D14 record-only note

> Status: historical record — 1.4 track T6 dispatch; merged

Branch: `wt/t6-packaging` (this tree). Branch only, no merge.
Source of truth for scope: `handoff/architecture-review-1.4.md` §4 and
`handoff/architecture-review-1.4-crosscheck-codex.md` "Defect audit" on the
`wt/t5-adapters` branch (`git show wt/t5-adapters:<path>`). Read both D3 and D4
entries in the cross-check before writing code — the cross-check *refines* the
original review, and where they differ the cross-check wins.

Three items, exactly. Nothing else in this track.

## 1. D3 — name the two `_atomic_write` contracts; switch two call sites

`pneu_packaging/setup.py:358-387` and `pneu_packaging/cli.py:181-201` both
define `_atomic_write` with the same signature and materially different
guarantees:

- setup's version validates the leaf via `_inspect_owned(path, kind="file")`,
  validates the **parent directory** via `_inspect_owned(path.parent,
  kind="directory")`, refuses a pre-existing temp path, and opens the temp with
  `O_NOFOLLOW`. It is an *owned-rewrite* contract.
- cli's version does `mkdir(parents=True, exist_ok=True)` and none of the
  ownership/no-follow checks. It is a *bootstrap publisher* contract, correct
  for writing into a fresh version tree that the installer just created.

**Do not globally merge them.** Both contracts are legitimate; the defect is
that one name hides two.

Required work:

a. Give each contract an explicit, self-documenting name and a docstring
   stating which contract it is and when to use which. Keep `setup.py`'s
   implementation as the owned-rewrite contract; keep `cli.py`'s as the
   bootstrap publisher. Naming is your call, but a caller must be able to tell
   from the call site alone which guarantee it gets.

b. `cli.py` needs an ownership-safe rewrite path with `setup.py:358-387`
   semantics (validated owned leaf when it exists, validated owned parent
   directory, refuse pre-existing temp, `O_NOFOLLOW` temp open, `os.replace`,
   explicit chmod). How you share it — import, small shared helper module, or a
   second local function — is your judgment; state the tradeoff you picked and
   why in your report. Watch the packaging import graph: do not create an
   import cycle or drag `setup.py`'s error type into a path that must raise
   `InstallError`. Failures on these two call sites must keep surfacing as
   `InstallError` with their existing message shape.

c. Switch **exactly two** call sites to the ownership-safe rewrite:
   - `cli.py:398` — rewrites a **managed harness configuration file** during
     1.0-prefix migration.
   - `cli.py:424` — rewrites a **managed Codex plist** during the same
     migration.
   The existing target prechecks at `cli.py:369-380` stay; they compensate only
   partially (no parent-directory ownership, no no-follow temp) and are not a
   substitute.

d. Leave the fresh-tree/bootstrap call sites on the publisher contract:
   `cli.py:1071`, `cli.py:1240`, `cli.py:1275`. The remaining `_atomic_write`
   call sites in `cli.py` (`:243`, `:438`, `:446`, `:474`, `:542`, `:615`) are
   **out of scope for this track** — do not change their contract. If you
   believe one of them is a genuine defect, write it in your report; do not act
   on it.

e. Tests: prove the two switched call sites now refuse what the owned-rewrite
   contract refuses and did not before. At minimum, cover a foreign-owned or
   non-directory parent and a symlinked/pre-existing temp path, driven through
   the real migration entry point rather than by calling the writer directly.
   A test that only calls the new helper in isolation does not demonstrate the
   call sites were switched.

## 2. D4 — `_rtrchost` missing from production install verification

`pneu_packaging/cli.py:1015-1017` runs the installed interpreter with
`import _rtcodex, _rtlauncher, _rtlib, _rtmigrate, _rtruntime, _rtsurface,
yaml` — six of the seven managed helpers. A syntactically broken
`_rtrchost.py` passes production install verification today.

`tests/test_packaging.py:307-321` already imports `_rtrchost` in a *separate,
independent* installed-root probe, so the repository suite masks the defect.

Required work:

a. Add `_rtrchost` to the production import command at `cli.py:1015-1017`.
b. Add a test that asserts **that exact production command line** — the real
   argv the installer executes — covers every managed helper. Deriving it from
   `MANAGED_HELPERS` (so a future helper cannot be forgotten) is preferred over
   hardcoding the string, but the assertion must be against the production
   command, not against an independently constructed probe. Leave the existing
   `tests/test_packaging.py:307-321` probe in place.

## 3. Record-only — OpenClaw isolation root may sit inside the project

**Do not change any parked adapter code.** This item produces one short handoff
note and nothing else.

Verified facts:

- `integrations/grok/roundtable/__init__.py` `create_isolation` derives its
  runtime root from `RT_GROK_RUNTIME_DIR` / `RT_RUNTIME_DIR` and then rejects a
  root beneath the project (`if _under(root, project): raise GrokError(...)`,
  around `:274-275`).
- `integrations/openclaw/roundtable/__init__.py` `create_isolation` (around
  `:202-234`) performs the same `RT_OPENCLAW_RUNTIME_DIR` / `RT_RUNTIME_DIR`
  derivation and has **no equivalent guard**. It then `mkdir`s `root`,
  `state_dir`, `home`, `tmp`, and `logs`. An operator who points
  `RT_OPENCLAW_RUNTIME_DIR` beneath the checkout gets adapter state — including
  `openclaw.json`, which carries a generated token — written inside the
  project.

Write `handoff/archive/d14-openclaw-isolation-root.md` recording: the defect, the exact
divergence from grok with file/symbol references, the concrete consequence
(state and a secret-bearing config inside a possibly-tracked working tree),
what the fix would be if D14 retains the adapter, and an explicit statement
that it is deliberately unfixed because the adapter is parked pending the D14
retain-or-park decision (`decision.md`, 2026-08-12). Keep it short — it is a
decision input, not a design doc.

## Out of scope

- **RC1** (installer flat file manifest → package manifest). The cross-check
  demoted it to a 1.5 prototype decision. Do not touch it.
- Any other defect from the 1.4 review (D1, D2, and the extraction proposals
  belong to other tracks).

## Verification gate

All three must be green before you report done:

```
pytest -q
python -m compileall -q bin pneu_packaging scripts tests
python scripts/check_public_safety.py
```

Use the project's dev environment. Do not commit — report to `claude` in this
project with `rt-say` and leave the working tree for review; the reviewing seat
commits. In your report state, per item: what changed, which tests you added,
what the test would have caught that the old code missed, and anything you
judged out of scope with the reason.
