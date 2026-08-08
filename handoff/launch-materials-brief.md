# Launch-materials workstream brief (rt-launch)

Role of this worktree: draft and iterate the public launch materials for pneu
with Ocean directly — X/Twitter thread, LinkedIn post, resume entry, and
whatever else the discussion produces (post timing plan, demo script notes).
The main worktree (roundtable-product) stays the central dispatch/audit seat;
report deliverables back with a handoff pointer when Ocean says ship.

## Product facts (verified, current)

- **pneu 1.1.0** live and released: https://github.com/hyyu189/pneu
  (v1.1.0 "quiet wake"). Tagline: *Postal Network, Entirely Unplugged.*
  Technical backronym: *Project-Native Envelope Utility.* Name source: Paris
  pneumatique slang — "un pneu" was the message itself.
- Core model: a message is a file in the recipient's per-project maildir —
  the atomic write IS delivery. No daemon, no account, no network. Offline
  seats keep mail; receipts (`ack-*`) are files too.
- The moat is the **native wake layer**: recipients are woken by their own
  harness's lifecycle machinery — Claude Code async lifecycle hooks, Codex
  app-server bridge, Hermes plugin, OpenClaw gateway, Grok ACP supervisor.
  No polling, no keyboard injection, no resident server.
- 1.1.0 additions: an armed idle seat costs **zero model tokens** (long-lived
  silent watcher); `rt-say --expect-reply 30m` one-shot reply alarm (cleared
  by the ack receipt, fires at most once); fail-closed runtime reclamation.
- Cross-worktree addressing `agent@project` (group key from the Git common
  dir, acks route home by origin UUID). 957-test suite, deterministic
  offline-installable artifact. History: built during OpenAI Build Week 2026
  as "roundtable", productized after; attribution in PROVENANCE.md/CREDITS.md.
- Meta-story (true and verifiable in the repo): the product was built by the
  workflow it enables — Claude as product lead (spec, dispatch, adversarial
  acceptance, release engineering), Codex implementing, mutation-tested
  acceptance rounds, live-host hot-swap before every public release.

## Competitive intel (2026-08-08 searches)

Same niche, all lacking the wake layer:
- MCP Agent Mail (Dicklesworthstone) — FastMCP server + Git + SQLite;
  identities/inboxes/threads/file leases; agents reach it via MCP tool calls.
- agent-message-queue (avivsinai) — Maildir-style file queue, transport-only.
- herdr (25k★ terminal runtime) — agent prompting = hardened pty injection
  ("atomically submits text and encoded Enter"), needs a live pane, state
  inferred from screen. Orthogonal category (terminal host), not messaging.

Positioning line that survived review: others are a server, an MCP round
trip, or a keystroke faker; pneu is a file write plus the harness's own
wake-up call. Recommended stance: draw the three-bucket contrast without
naming competitors.

## Draft seeds (Ocean has seen these; iterate freely)

- X thread (4 tweets): hook "a message is a file; writing it IS delivery" →
  how (`rt-say`, atomic write, offline durability) → wake layer + zero-token
  idle → pneumatique name story + meta-story + repo link.
- LinkedIn: lead with the meta-story ("my engineering team was AI agents;
  the product is the tool they coordinate through"), three engineering
  disciplines (adversarial acceptance, mutation testing, hot-swap before
  publish), close with repo.
- Resume: creator & product lead; durable local messaging for coding agents;
  maildir write-is-delivery + fenced seats + 5-harness native wake; ran the
  multi-agent org; 957 tests, deterministic artifacts, 1.0→1.1.

## Open decisions (Ocean to rule; ask before assuming)

1. Chinese-language versions (即刻/V2EX/微博?) alongside English X/LinkedIn?
2. Timing: post now, or hold for the clean-machine demo recording so posts
   carry a GIF? (Prior lean: hold for demo.)
3. Name competitors explicitly or keep the anonymous three-bucket contrast?
   (Prior lean: anonymous.)

Constraints: no session URLs or private paths in anything public; keep
PROVENANCE attribution intact when telling the Build Week story; the
public-safety gate applies to anything committed here.
