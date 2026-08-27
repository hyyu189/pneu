Postal Network, Entirely Unplugged

# pneu

**pneu** (Project-Native Envelope Utility) is a thin, local-first coordination
control plane for native coding-agent sessions — a local coordination layer
for the coding tools you already use. It adds stable addressing, durable mail,
seat occupancy, native wake, resume, and navigation to Claude Code, Codex,
Hermes, and Grok Build sessions sharing a machine. It does not ask you to move
the conversation, editor, terminal, approval UI, or agent loop into pneu.

The defining experience:

> Open the harness the way you normally do; mail finds the real session there.

## The mental model

- A **workspace** is one checkout/worktree (or one registered directory).
- A **seat** is a stable address: one workspace × one harness family
  (`codex`, `codex@feature-auth`). Mail is addressed to the seat.
- A **native session** — a Codex thread, a Claude Code session — *occupies*
  the seat. Wake goes to the session currently bound to the seat; the mailbox
  remains when no session is running.
- A **surface** (terminal pane, Codex Desktop, a phone client) is where you
  see the session. Several clients showing one thread are one seat.

An agent sends a message with `rt-say`. The command atomically writes a file
into the recipient seat's `new/` mailbox; **that write is delivery** — it
works with no daemon, multiplexer, account, or network, and offline seats
lose nothing. A harness-native integration may then wake the occupying
session. The recipient acts and runs `rt-ack`, which sends a quiet receipt
and archives the message to `cur/`.

```text
rt-say  ->  project mailbox: new/  ->  agent acts  ->  rt-ack  ->  cur/
              optional native wake
```

The `rt-*` names and `RT_*` environment variables are pneu's stable tool
prefix. Project state lives under `.roundtable/`, and `roundtable.*` wire and
registry schema identifiers are unchanged; `roundtable` is a silent
compatibility alias for the primary `pneu` command.

## Install

The release archive is the supported new-user path. It contains the pneu
wheel and locked dependencies but not Python; the host needs CPython 3.11
through 3.14.

```bash
tar -xzf pneu-1.3.5-macos.tar.gz
cd pneu-1.3.5
./install
export PATH="$HOME/.local/bin:$PATH"
pneu
```

The default install prefix is `~/.pneu`. If `~/.roundtable` contains an
existing install, the installer moves its managed and durable state to
`~/.pneu`, writes a recovery manifest, and leaves `~/.roundtable` as a
compatibility symlink. The installer owns only its recorded wrappers, links,
version trees, and managed onboarding assets; project registries, mail,
runtime state, and backups are preserved during uninstall unless an explicit
runtime purge is requested. See [`docs/install.md`](docs/install.md).

## Daily commands

```text
pneu                         project-first onboarding and launch
pneu guide                   show the local mailroom model
pneu setup                   preview harness setup (read-only)
pneu setup apply             apply owned harness setup
pneu doctor                  diagnose the current project and seat
pneu worktree add NAME       create a registered tree in ../<repo>-worktree/
pneu worktree open NAME      open one configured seat in a visible surface
pneu rc-host enable          enable project-only Claude phone worktree spawn
rt-say AGENT KIND "MESSAGE"  durable local or sibling delivery
rt-inbox -f json             inspect waiting mail
rt-ack MESSAGE_ID            acknowledge and archive handled mail
```

On a full TTY, `pneu` presents one compact seat card; arrow keys or digits
move the selection and a single Enter launches or resumes that seat. Bare
`pneu` never prompts without a terminal on stdin — it prints usage and exits
2, so scripts use the explicit subcommands. The explicit tool forms
(`roundtable-init`, `roundtable-setup`, `roundtable-smoke`,
`roundtable-uninstall`, and every `rt-*` command) retain their names for
script compatibility.

## Support boundary

Harness integrations do **not** have equal depth. Codex, Claude Code, Hermes,
and Grok Build have working adapters of different strengths; OpenClaw has no
user-facing seat (lab machinery only). A supported platform/runtime claim
requires a real end-to-end smoke test — version comparisons and fixtures
alone never establish support. The one home for what has actually been
exercised, and for open promotion gates, is
[`docs/compatibility.md`](docs/compatibility.md). Cross-host transport,
Linux service management, and multi-auth switching are out of scope for
1.3.5.

## Learn more

- [`PRINCIPLES.md`](PRINCIPLES.md) — the ranked constitution.
- [`docs/product-model.md`](docs/product-model.md) — objects, invariants,
  message states, switchboard UX.
- [`docs/architecture.md`](docs/architecture.md) — what 1.3.5 actually
  implements.
- [`docs/target-architecture.md`](docs/target-architecture.md) and
  [`docs/harness-adapters.md`](docs/harness-adapters.md) — where the
  implementation is going.
- [`docs/roadmap.md`](docs/roadmap.md) — dependency-ordered outcomes.
- [`docs/adr/`](docs/adr/) — accepted decisions.

## History and provenance

The working name was **roundtable**; Ocean selected **pneu** — Paris
pneumatique slang where *un pneu* is the message itself. The Build Week phase
and its attribution are documented exactly in
[`docs/PROVENANCE.md`](docs/PROVENANCE.md) and
[`docs/CREDITS.md`](docs/CREDITS.md); the `v0.1.8` tag and its release assets
are immutable.

## Development

Any CPython 3.11–3.14 environment with `pip install -r requirements-dev.txt`.
Checks:

```bash
pytest -q
python -m compileall -q bin pneu_packaging scripts tests
python scripts/check_public_safety.py
```

`pytest -q -n auto` is the faster parallel loop on a non-saturated host; the
serial form stays the default. See [`docs/release.md`](docs/release.md) for
the deterministic artifact workflow.
