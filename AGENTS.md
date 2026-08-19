# Project instructions

These instructions apply to the entire repository.

## Mission

Evolve pneu from the Build Week submission (`v0.1.8`, frozen) into a
dependable product for collocated coding agents. The current public release is
1.3.5. `BRIEF.md` states the north star and the current phase, `BACKLOG.md`
indexes open work, the ranked principles that govern design decisions live in
`PRINCIPLES.md`, and individual rulings live in `decision.md`. Prefer small, reliable increments
over speculative features.

## Collaboration boundary

- Ocean is the human product lead and final decision-maker.
- In the current phase, product work in this branch is led, implemented, and
  reviewed through Claude, with Codex and Hermes as peer reviewers and
  specialists on request. Ocean may change this boundary at any time.
- Preserve historical attribution exactly as documented in
  `docs/PROVENANCE.md` and `docs/CREDITS.md` (shipped at the release-archive
  root as `PROVENANCE.md` and `CREDITS.md`). The Build Week phase was
  GPT-5.6/Codex-led; do not restate
  history to match the current phase.
- Do not ask another harness to implement new product code without Ocean's
  explicit approval.

## Frozen artifacts

The review-window freeze expired by its own terms on 2026-08-12
(`decision.md`, 2026-07-21). These constraints outlive it and are permanent:

- Never move, rebuild, or retarget the `v0.1.8` tag, and never modify its
  Release assets.
- The competition archive worktree (`../2026-OAI-Build-Week`) is not a
  development surface.

Development happens on `main` and on branches in registered worktrees; the
main checkout is the dispatch, audit, integration, and release seat.

## Provenance

- Never copy a source tree as an unexplained snapshot.
- A replayed change must retain its original source commit in the commit body
  and in `docs/provenance/source-commits.tsv`.
- Exclude runtime mailboxes, local registries, backups, transcripts, secrets,
  personal paths, and unrelated project material.
- Do not weaken or rewrite contributor attribution. Describe uncertainty
  explicitly.

## Product constraints

- Durable maildir delivery is the fact source.
- Core send, receive, acknowledge, recovery, and diagnostics must work without
  cmux. cmux support is an optional adapter.
- Use one explicit Codex executable resolver for the launcher, daemon, wake
  bridge, and doctor.
- Fail closed on unsupported harness protocol behavior. Do not claim support
  from version-number comparisons or fixtures alone.
- Cross-host transport and multi-auth switching remain out of scope until the
  roadmap says otherwise.

## Implementation and tests

- Prefer the Python standard library; declare every non-standard dependency.
- Run Python commands through a dedicated project environment (any CPython
  3.11–3.14 with `requirements-dev.txt` installed), never against the system
  interpreter with bare `python3` or global `pip3 install`. Environment
  managers are a host convention, not a project requirement; a
  conda/mamba-managed host would use its own runner prefix (for example
  `mamba run -n <env> ...`).
- Every behavior change needs focused regression coverage.
- Before a commit, run the focused tests, the full suite, compile checks, and
  the repository's public-safety scan.
- Installation and uninstallation must be idempotent and modify only managed
  files, symlinks, launch agents, and marked configuration blocks.

## Release claims

- A supported platform/runtime combination needs a real end-to-end smoke test.
- A new user must reach a working install from a release artifact, without a
  source rebuild, in five minutes or less.
- Keep README support tables and limitations honest and current.

<!-- BEGIN Roundtable -->
For Roundtable work, always read `ROUTING.md` and `README.md`. Use the `roundtable` skill for peer-agent messaging.
<!-- END Roundtable -->
