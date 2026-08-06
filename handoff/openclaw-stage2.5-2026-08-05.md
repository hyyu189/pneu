# OpenClaw Stage 2.5 handoff

Date: 2026-08-05
Branch: `wt/openclaw`

## Delivered

- Classified loopback Gateway transport failures as unavailable, isolated token
  failures as authentication errors, and accepted runs that miss their bound
  deadline as `GatewayRunTimeout`.
- Readiness retries transient `UNAVAILABLE`/startup-sidecar responses, but
  refuses an auth failure immediately without a retry storm. Durable mail is
  never acknowledged or archived by the adapter itself.
- Added fault-matrix unit coverage for Gateway down/recovery, Gateway exit
  mid-turn, auth refusal, never-terminal runs, terminal errors, rapid duplicate
  wake idempotency, stale/wrong lease identity, and a private-copy mutation
  test. Each lease or identity bypass mutation turns its contract red.
- Added a maildir-only three-seat lab (openclaw/claude/codex, all six directed
  routes) and a Git sibling `openclaw@<worktree>` route. The lab verifies quiet
  ack routing and no watcher/runtime side effects.
- Extended the real OpenClaw 2026.5.4 lab to an explicit 50-cycle same-Gateway
  soak. Each cycle sends one message, runs the real `exec` tool with fenced
  `rt-ack`, waits for the exact generation to leave `new/`, and samples process
  RSS/FDs. The final assertions cover bounded resource variation, closed
  Gateway port, and an unchanged pre-existing `/tmp/openclaw` tree.

## Evidence

- Focused adapter plus mutation tests: `15 passed`.
- Maildir/inter-worktree interop lab: `2 passed`.
- Real single-cycle isolated Gateway: `1 passed in 28.21s`.
- Real 50-cycle same-Gateway soak: `1 passed in 366.28s`.
- The first soak attempt reached 48/50 before the isolated fake provider's
  declared 16K context budget overflowed; this was diagnosed from the
  Gateway log and the rerun used only a larger isolated fake-model context
  declaration. It did not change product defaults or touch global state.
- The pre-existing full-suite skip remains:
  `tests/test_openclaw_lab.py:182: set RT_OPENCLAW_LAB_BIN to run the real OpenClaw lab`.

## Isolated OAuth preparation

The local OpenClaw 2026.5.4 CLI confirmed the provider id and command shape:
`openclaw models auth login --provider openai-codex --set-default`. The
isolated config is already set to `openai-codex/gpt-5.5`, and `models status
--json` showed its config and auth store under
`/private/tmp/rt-openclaw-oauth.gbDOdK/state`, with no auth profile yet.

Ocean's one interactive command is:

```sh
env HOME=/private/tmp/rt-openclaw-oauth.gbDOdK/home XDG_CONFIG_HOME=/private/tmp/rt-openclaw-oauth.gbDOdK/home/.config XDG_DATA_HOME=/private/tmp/rt-openclaw-oauth.gbDOdK/home/.local/share XDG_CACHE_HOME=/private/tmp/rt-openclaw-oauth.gbDOdK/home/.cache TMPDIR=/private/tmp/rt-openclaw-oauth.gbDOdK/tmp OPENCLAW_STATE_DIR=/private/tmp/rt-openclaw-oauth.gbDOdK/state OPENCLAW_CONFIG_PATH=/private/tmp/rt-openclaw-oauth.gbDOdK/state/openclaw.json OPENCLAW_GATEWAY_TOKEN=isolated-token OPENCLAW_AGENT_ID=main RT_OPENCLAW_ISOLATION_ROOT=/private/tmp/rt-openclaw-oauth.gbDOdK /private/tmp/rt-openclaw-stage2-live/node_modules/.bin/openclaw models auth login --provider openai-codex --set-default
```

The CLI's local flow opens the browser and uses localhost:1455; no browser
flow was started during this preparation. Real-model E2E remains pending that
interactive login and must not be claimed from this handoff.

## Boundaries

- No `~/.openclaw`, public `main`, release tag/assets, or remote was changed.
- The pre-existing user edit in `CLAUDE.md` remains unstaged and untouched.
- This evidence supports T0 durable delivery and provisional T1/T2 lab
  ingress; it does not claim a clean-account or release-level OpenClaw
  support promotion until the real credentialed generation is run.
