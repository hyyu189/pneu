# pneu baseline (2026-08-27)

The compact current baseline of the repository, produced on the `cleanup/rebaseline`
branch after the 2026-08-27 topology cleanup. It replaces the scattered handoff narrative
as the single orientation point: a new contributor (human or agent) should be able to read
this directory and know what pneu is, what is true today, and where it is going — without
the development history.

| File | Contents |
|---|---|
| [product.md](product.md) | North star, goals, non-goals, object model, ranked UX principles |
| [implementation.md](implementation.md) | Verified implementation truth at v1.3.5, test truth, ranked debt, doc drift |
| [architecture.md](architecture.md) | Target architecture: one-home-per-fact modules, tiers, untouchables |
| [roadmap.md](roadmap.md) | Committed work, accepted-unscheduled, open rulings, watch items |
| [adapters.md](adapters.md) | Ranked adapter principles, settled Codex App Server findings, per-harness status |
| [lessons.md](lessons.md) | Concise external reference lessons (Paseo, Warp, Orca/ADE) |
| [adr.md](adr.md) | Accepted decisions still in force, distilled from the append-only ledger |

Boundaries of this baseline: it deliberately does not import old handoff archives, role
assignments, RC state, or the full development narrative — those remain where they are
(handoff/, decision.md, git history) as the record, not the orientation. Where a source
document contradicted the code, the code won and the drift is noted in implementation.md.

Known gaps at creation time: an external product design document and ORG2/DeepSeek/Herdr
comparison documents were cited for this rebaseline but could not be located; see the
closing note in lessons.md.
