# D2 — the harness setup manifest must be forward-compatible

Seat: codex (gpt-5.6-terra, high reasoning effort) in this worktree, branch
`wt/t1-codex-core`. Dispatched by the claude seat, who reviews and commits.

## The defect

`pneu_packaging/setup.py`, in `_load_manifest`:

```python
if not isinstance(harnesses, dict) or any(name not in HARNESSES for name in harnesses):
    raise SetupError(f"invalid harness ownership entries at {path}")
```

`HARNESSES` is `("claude", "hermes", "codex")`. The moment a build ships a
fourth harness and writes its name into the ownership manifest, an older build
can no longer read that manifest at all — so after a downgrade, the older
`roundtable-setup` cannot run `plan`, `status`, `apply`, or `remove` for the
harnesses it does own. Its onboarding becomes unremovable by that binary.
(Package uninstall is a separate path and is not blocked; do not overstate this
in comments or docs.)

This has to land before the first new harness ships, not after — an older build
already in the field is exactly the one that needs the tolerance.

## What correct looks like

An unknown harness record is data this build does not understand. It must be
**preserved opaquely**: never interpreted, never mutated, never dropped, and
never a reason to refuse. The operator should be told it is there.

`_selected` already filters every command's harness list through `HARNESSES`,
so no unknown record can reach a prepare/apply/validate/remove path. That is
what makes pass-through safe; keep it that way rather than adding new
special-casing downstream.

Structural validation of the manifest itself stays: schema, prefix/home scope,
and `harnesses` being an object are still hard failures. Only the
unknown-*name* rejection is wrong.

## Acceptance

- A manifest containing a record for an unknown harness loads, and `plan`,
  `status`, `apply`, and `remove` all work for the known harnesses in it.
- A test proves the unknown record survives **byte-for-value** across a
  known-harness `apply` and a known-harness `remove` — same keys, same values,
  including nested structure it does not understand.
- The unknown name is surfaced to the operator rather than swallowed. Pick the
  reporting shape that fits how this module already talks to its caller; it
  must be visible in both the human and `--json` renderings if that is what the
  existing shape implies.
- Removing the last known harness must not delete a manifest that still holds
  an unknown record. Check the current unlink-when-empty path.
- Full `pytest -q`, `python -m compileall -q bin pneu_packaging scripts tests`,
  and `python scripts/check_public_safety.py` stay green.

## Constraints

- Scope is this defect only. No refactors, no renames, no drive-by fixes in
  neighbouring code, no new dependencies.
- Do not commit. Leave the work in the tree and report; the claude seat reviews
  adversarially and commits.
- English-only artifacts, public-safe (no personal absolute paths, no session
  URLs). Match the surrounding comment density and idiom — this file's own
  style is not the target, `setup.py`'s is.
- Report back with `rt-say --fenced --no-nudge claude update "..."` naming the
  files you touched and anything you deliberately did not do.

## Reference

The finding is D2 in `git show wt/t5-adapters:handoff/architecture-review-1.4.md`,
refined by the D2 section of
`git show wt/t5-adapters:handoff/architecture-review-1.4-crosscheck-codex.md`.
Read both before starting; the second one narrows the first.
