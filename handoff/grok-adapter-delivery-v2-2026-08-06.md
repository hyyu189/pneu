# Grok ACP adapter delivery — 2026-08-06

Status: implementation and focused verification complete; production support
remains unclaimed because the current read-only credentialed adapter E2E was
blocked by an expired host OAuth credential.

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

## Credentialed adapter E2E boundary

The new adapter lab was attempted read-only with two generations and optional
child restart. ACP `initialize` and `session/new` reached the provider, but the
first `session/prompt` returned HTTP 403
`unauthenticated:bad-credentials` because the host OAuth credential had
expired. The adapter left the exact message in `new/` and did not acknowledge
it. The live auth file's metadata was unchanged. No login, refresh, credential
copy, or credential logging was attempted.

Therefore the current tier claim is:

- **T2-lab evidence:** one-generation credentialed ACP path from the prior
  isolated lab;
- **adapter implementation:** focused fault, mutation, soak, and interop
  checks pass;
- **production/public support:** **unclaimed** until a fresh valid credential
  proves two generations, recovery, and extracted-artifact smoke.

## Remaining release evidence

Run the adapter lab again with a fresh valid credential under the same
read-only boundary. Then run the extracted `0.3.0` artifact install/smoke and
record the artifact surface checks. No public `main` push or release asset
mutation is authorized.
