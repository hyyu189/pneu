# Quiet acknowledgements and per-agent delivery (WP2.1)

> Status: current behavior, filed under its original pre-1.0 work-package
> name. Quiet `ack-*` receipts, maildir-only delivery, and the optional cmux
> startup advisory all still work as described here.

Mail remains the fact source. WP2.1 removes unnecessary wakeups and
keyboard nudges without changing message IDs or headers.

## Quiet sync acknowledgements

`rt-ack` still sends a `sync-ack` message whose header contains the normal
message ID, but its maildir filename is `ack-<msgid>.md`. Inbox readers accept
both that filename and the legacy `<msgid>.md` spelling for sync acknowledgements.
Wake and stop-gate consumers ignore `ack-*`; an already-awake agent can drain
them normally.

After the quiet acknowledgement is durably published, `rt-ack` also moves each
exact processed inbound file from the recipient's `new/` directory to `cur/`.
An acknowledgement failure leaves the inbound file untouched; an archive
conflict never overwrites an existing `cur/` entry. This keeps the logical
inbox view and the physical watcher state consistent, so native adapters can
re-arm without relying on an agent to construct a separate `mv` command.

## Sole normal delivery path

This document originally introduced per-agent `maildir` versus `dual`
delivery. That transition is complete: normal `rt-say` is now always
maildir-only for every harness and configured instance. Old `delivery` fields
do not re-enable a nudge. Normal stdout is `sent maildir-only <msgid>`.

`--no-nudge` is retained as a compatibility alias with exactly the same
behavior as normal `rt-say`. `--legacy-nudge-only` is the separate,
human-coordinated keyboard emergency path and never writes maildir state.

App-server turns can lack `RT_FROM`. For a maildir-only send,
`rt-say` may infer Codex from `CODEX_THREAD_ID` only when exactly one Codex
instance is configured, or when one instance has that exact `session_id`.
Ambiguous multi-instance configurations fail closed.

## Startup advisory

`rt-startup-advisory` is an optional cmux integration. If
`CMUX_SURFACE_ID` identifies a caller whose workspace has exactly one peer
Roundtable project, the helper prints one `cd` or `ROUNDTABLE_PROJECT_DIR`
suggestion. It never binds, routes, or writes state; missing or ambiguous cmux
identity and all operational failures are silent success. The retired
`rt-watch-ensure` path is not part of current startup or delivery.
