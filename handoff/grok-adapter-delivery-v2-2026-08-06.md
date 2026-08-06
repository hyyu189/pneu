# Grok ACP adapter delivery — 2026-08-06

Status: implementation, focused verification, credentialed two-generation
recovery E2E, and extracted-artifact smoke complete; production/public support
remains unclaimed pending the broader release gates.

Commits: `26bd0a068d95583ee041871ec570548b15c9c2cb` (implementation) and
`3962b7d788d95e7109d0d465a6eadfef8bbadd09` (provenance).

## Delivered

- Added `integrations/grok/roundtable/__init__.py`, a stdlib-only ACP
  supervisor with one fenced `grok agent --no-leader stdio` child per seat.
- Added `bin/rt-grok` and `bin/rt-grok-wake`, launcher lease transfer, bounded
  HOME/XDG/GROK_HOME/TMP/log state, and exact durable-mail generation checks.
- Added default-deny permission handling for only the exact fenced Roundtable
  inbox and acknowledgement commands. Shell operators, stale identity, and
  stale lease revisions fail closed.
- Added packaging and release allowlist entries for the Grok integration and
  both wrappers; the project version is now `0.3.0`.
- Added the repeatable isolated lab at
  `scripts/grok_adapter_stage25_lab.py`. It accepts an already-exported
  credential only, never reads or copies the host auth file, never stores the
  credential in evidence, and checks that the temporary lab did not contain
  it.

## Verification

Focused adapter, mutation, soak, and three-seat interop tests pass: **15
passed**. The focused matrix covers:

- child process down and killed-turn recovery with one bounded restart;
- invalid/auth-failed initialization and prompt classification;
- hung prompt timeout without acknowledgement or archival;
- permission rejection and shell/operator injection negatives;
- lease, identity, and revision mutation checks;
- duplicate-safe exact-generation drain behavior;
- 25 wake cycles with one resident fake session and RSS bound;
- six directed routes across Grok, Claude, and Codex durable mailboxes.

The earlier isolated Stage 2 lab remains the evidence for one valid
credentialed ACP mail-to-wake-to-drain/ack generation. It is explicitly lab
evidence, not public support.

The complete repository suite passed **922 passed, 1 skipped**. `compileall`
and the public-safety scan passed. An extracted `0.3.0` artifact built from
the commits above passed both checksum layers, contained all three Grok wheel
surfaces, installed into a disposable HOME, and passed `roundtable-smoke`.
Artifact SHA-256: `f8e80b4516530c315c21c1d8f753f072c3787230a459fabd8aab1c9563ace875`.

## Credentialed adapter E2E and token lifecycle

The first read-only adapter attempt correctly failed closed: the host OAuth
credential was expired and ACP returned HTTP 403
`unauthenticated:bad-credentials`; the exact message stayed in `new/` and was
not acknowledged. No login, refresh, credential copy, or credential logging
was attempted by the adapter.

After Ocean launched the Grok TUI separately, the read-only auth metadata
showed a later expiry. The adapter lab then passed two consecutive
mail-to-wake-to-drain-to-ack generations, killing the ACP child between them.
Both generations returned `end_turn`; the combined event log recorded two
`initialize`, two `session/new`, and two `session/prompt` requests. The final
mailbox had zero files in `new/` and two archived files in `cur/`. The auth file
size/mtime was unchanged across the lab, and the temporary lab's credential
scan was clean.

The lifecycle boundary is explicit: the host TUI refreshes its OIDC login
state outside this adapter; the adapter reads the current auth-file `key` only
at child start/restart and passes it to ACP as the child `XAI_API_KEY`. It does
not refresh tokens, use a keychain, or depend on a TUI being manually launched
as part of wake. An expired credential must therefore fail closed with durable
mail left in `new/`; public production support still needs an approved
vendor-supported refresh/preflight contract rather than a hidden refresh side
effect.

Therefore the current tier claim is:

- **T2-lab evidence:** one-generation credentialed ACP path from the prior
  isolated lab;
- **adapter implementation:** focused fault, mutation, soak, and interop
  checks pass;
- **credentialed development-host E2E:** two generations plus child restart
  and extracted-artifact smoke pass;
- **production/public support:** **unclaimed** pending the broader clean
  account, terminal-matrix, and token-lifecycle promotion gates.

## Remaining release evidence

No further Grok credentialed lab is required for this implementation handoff.
The remaining work is the broader clean-account, terminal-matrix, and explicit
token-refresh promotion gate. No public `main` push or release asset mutation
is authorized.
