# roundtable-product · BRIEF

## North star

**pneu** is the durable messaging and coordination layer for coding agents
that share a machine: per-project maildir mailboxes as the delivery fact
source, fenced seat identities, and harness-native wake bridges. Open your
harness the way you always do, and mail finds you there.

1.3.5 is shipped and public at [`hyyu189/pneu`](https://github.com/hyyu189/pneu).
The product is real; what remains unclaimed is *support*, which has its own
bar — see `docs/compatibility.md`.

Why the product is shaped the way it is, ranked, lives in `PRINCIPLES.md`.
Read it before proposing a harness adaptation or an architectural change;
it is not restated here.

## Current phase

Consolidation before 1.5. The 1.4 cycle merged six tracks and shipped through
1.3.5, and the whole-project architecture review that came out of it left a
tiered refactor plan whose first tier has not started. The immediate work is
therefore to make the ground solid rather than to add surface: the
documentation sweep that produced `PRINCIPLES.md` and `docs/ux/launcher.md`
is layer 0 of that, and the launcher occupancy work Ocean ruled on
2026-08-18 is the first user-visible thing queued behind it. Nothing new is
promoted until the validation gates it depends on actually pass.

## Roadmap

Open work lives in [`BACKLOG.md`](BACKLOG.md), grouped by area, each item
pointing at the file that holds its real detail. Shipped work is not tracked
here: `decision.md` is the ruling ledger and `handoff/archive/` holds the
records.

Working constraints — provenance, frozen artifacts, review boundaries — are
in `AGENTS.md`.
