# Project instructions

These instructions apply to the entire repository.

## Mission

pneu is a thin, local-first coordination control plane for native
coding-agent sessions. The current public release is 1.3.5. The ranked
principles that govern design decisions live in `PRINCIPLES.md`; accepted
decisions live in `docs/adr/`; the dependency-ordered roadmap lives in
`docs/roadmap.md`. Prefer small, reliable increments over speculative
features.

## Collaboration boundary

- Ocean is the human product lead and final decision-maker.
- Harness roles are assigned per task, not fixed: any harness seat may
  review one change and implement the next. Do not encode persistent
  reviewer/implementer/researcher identities anywhere.
- Do not start implementation work another agent was not asked for; product
  decisions are reserved for the human lead.
- Preserve historical attribution exactly as documented in
  `docs/PROVENANCE.md` and `docs/CREDITS.md`. The Build Week phase was
  GPT-5.6/Codex-led; do not restate history to match the current phase.

## Frozen artifacts

- Never move, rebuild, or retarget the `v0.1.8` tag, and never modify its
  Release assets.
- The competition archive worktree (`../2026-OAI-Build-Week`) is not a
  development surface.

Development happens on `main` and on branches in registered worktrees.

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
- Core send, receive, acknowledge, recovery, and diagnostics must work
  without a terminal multiplexer; never inject keyboard input on core paths.
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
  interpreter.
- Every behavior change needs focused regression coverage. Prefer contract
  tests over source-shape or wording-snapshot tests.
- Before a commit, run the focused tests, the full suite, compile checks, and
  the repository's public-safety scan.
- Installation and uninstallation must be idempotent and modify only managed
  files, symlinks, launch agents, and marked configuration blocks.

## Documentation lifecycle

- Current product truth → `PRINCIPLES.md`, `docs/product-model.md`.
- Current implementation truth → `docs/architecture.md` and the docs it
  links.
- An important decision → one ADR in `docs/adr/`.
- Active tasks → issues or a temporary local change record, not a committed
  status document.
- Ordinary handoffs and raw findings are ephemeral: keep them in mail,
  issues, or uncommitted notes, and delete them after integration. Do not
  accumulate them on `main`.

## Release claims

- A supported platform/runtime combination needs a real end-to-end smoke
  test.
- A new user must reach a working install from a release artifact, without a
  source rebuild, in five minutes or less.
- Keep README support statements and limitations honest and current.
