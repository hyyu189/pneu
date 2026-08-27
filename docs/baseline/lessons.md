# pneu — external reference lessons

Part of the 2026-08-27 rebaseline. Concise lessons from the external teardowns that shaped
pneu's design, and what pneu deliberately decided differently.
Sources: research/paseo-research.md, research/warp-ux-2026-07-28.md,
research/ade-teardown-2026-07-27.md.

## Paseo

- Paseo has no mailbox and no message primitive: a handoff is an unbounded prose prompt, a
  reply is a bounded injection into the parent's live session. The decisive negative: its
  delivery obligation lives in an in-memory closure — a daemon restart silently drops it
  while both agents look healthy. pneu's "the file is the delivery" exists to make that
  failure impossible.
- Identity there is asserted (caller id + one daemon-wide token); pneu's is a per-call
  revalidated capability chain.
- The honest comparison runs on two axes — *where authority lives* (file vs live process)
  and *who owns the session* (the human's native harness vs a daemon-managed process) —
  not "daemon vs no-daemon": pneu itself runs a registry, a central mail root, layout
  locks, and a canonical Codex daemon. Paseo's daemon buys what pneu structurally cannot:
  multi-surface attach, work-while-unattended, uniform cross-provider UI, remote approval.
- Borrowables under consideration (B1–B5, in BACKLOG): seat notes read before dispatch; a
  typed handoff envelope with one canonical renderer; bounded wake payload + named
  retrieval command; presence-aware nudge ordering (presence may decide whether to *also
  nudge*, never become delivery authority); committee contrast-by-construction. Refused:
  shared-token identity; any live process as the fact source for a message.

## Warp

- Two distinct models: a manual cockpit (directly addressable tabs, glanceable status) and
  an orchestrate mode (coordinator + inspectable child pills). Adopted lessons: preserve
  direct addressability; make orchestration an explicit labeled mode; one-action worktree
  with visible provenance; enumerate exactly which layers survive restart; expose
  reconnection boundaries; skippable onboarding.
- Source facts: all its multi-agent behavior requires an account and a cloud round trip;
  the local transport is a dead enum. And a methods lesson: deleted schema remnants prove
  only that a schema existed — do not build strategy on a counterfactual.

## Orca / ADE

- Its CLI is a pure RPC client — with the app closed you cannot even enqueue; wake is
  bracketed-paste keystroke injection plus a timer; messages bound to dead pane handles
  are never re-addressed; it edits many agents' global configs on every launch without
  consent. Each of these is a named pneu anti-goal (offline delivery; native wake; UUID
  addressing; fenced, manifest-owned config fragments).
- The shared structural price of both competitors: "the seat exists because a pane
  exists." pneu copies the *decoupling* (allocated identity; path demoted to attribute),
  never the hosting (UI process as registry) or the wake (keystrokes, cloud bus).
- Positioning: the moat is one sentence — `rt-say` succeeds with nothing running,
  cross-harness, no account — and it is honestly narrow, real, and time-limited.
- Worth stealing: version-matched skill docs served by the executing binary (kills doc
  drift); an explicit context budget with "more messages queued" deferral.

## Herdr (internal precedent, not a teardown)

Herdr appears in pneu's record as a multiplexer *surface* and as the standing red-line
precedent: an integration install once tripped the Codex hook-trust gate and fail-closed
every Codex wake on the machine — hence "never touch shared harness configuration without
an explicit ruling."

## Missing external comparisons

The rebaseline brief also cited an external product design document and ORG2/DeepSeek/
Herdr comparison documents. None could be located in the repository, local workspaces,
the vault, or session records at rebaseline time (DeepSeek is mentioned nowhere in the
repo; Herdr only as above). If those documents exist elsewhere, their lessons should be
folded in here in a follow-up commit.
