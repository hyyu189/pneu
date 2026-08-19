# OpenClaw Stage 2 Gateway adapter handoff

> Status: historical record — Stage 2 Gateway adapter; now internal lab machinery only

Date: 2026-08-05
Branch: `wt/openclaw`

## Delivered

- Added the stdlib-only `integrations/openclaw/roundtable` adapter and the
  `rt-openclaw` / `rt-openclaw-wake` launcher path.
- The adapter claims and validates the fenced OpenClaw seat, starts one
  loopback/token-authenticated Gateway child, sends `agent`, polls
  `agent.wait` to terminal completion, and waits for the exact mail generation
  to leave `inbox/openclaw/new/` before returning success.
- The child environment bounds HOME, XDG, TMP, OpenClaw state/config, logs,
  Roundtable runtime context, and the Roundtable/Python executable paths.
  Gateway `logging.file` and `tools.exec.pathPrepend` are forced into those
  managed boundaries; an escaping log path fails closed.
- Added fake-Gateway unit coverage and an opt-in real Gateway lab using
  OpenClaw 2026.5.4 plus a local fake OpenAI-compatible model. The lab runs a
  real tool call containing `rt-ack`, then verifies `new/` is empty and the
  message is in `cur/`.

## Evidence

- Focused adapter test: `7 passed`.
- Packaging/release/launcher regression set: `99 passed`.
- Full suite: `874 passed, 1 skipped`.
- Public-safety test: `4 passed`.
- `compileall`: passed with exit code 0.
- Real isolated Gateway lab: `1 passed in 30.11s`.
- The lab observed the real `agent` acceptance, `agent.wait` final result,
  OpenClaw `exec`, fenced `rt-ack`, and durable `new/` to `cur/` archival. It
  also verified that the pre-existing `/tmp/openclaw` tree did not change and
  that the configured Gateway log is inside the isolated runtime root.

## Support tier

- T0 durable maildir delivery remains covered by the existing full suite.
- T1 Gateway ingress is verified provisionally against the real isolated
  Gateway path.
- T2 supervised launch/lease/wake/drain/ack has one real lab generation and
  is therefore lab-only evidence. Recovery across generations, release
  artifact installation, and a broader platform/runtime matrix remain open;
  this handoff does not claim release-level OpenClaw support.

No public `main`, release asset, tag, or remote was changed.
