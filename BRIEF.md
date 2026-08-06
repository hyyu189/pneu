# roundtable-product · BRIEF

## North star

**pneu** (naming decided 2026-08-06; ships as 1.0.0) is the durable messaging
and coordination layer for coding agents that live on the same machine:
per-project maildir mailboxes as the delivery fact source, fenced seat
identities, and harness-native wake bridges. Backronyms: "Project-Native
Envelope Utility" (technical), "Postal Network, Entirely Unplugged"
(tagline). `v0.1.8` proved the design under Build Week conditions; the 0.2
line made it dependable for daily use; 1.0.0 gives it its name and takes it
public.

## Completed (0.2 line, evidence in handoff/ and decision.md)

Central mail v1 (UUID identity, central layout, crash-safe migration,
cross-worktree `agent@project` addressing) · launcher-primed Codex first
turn · M5 onboarding/communication batch · D6 uniform addressing display ·
seat/thread lifecycle package (handoff verb, resume lease idempotency,
binding adoption) · OpenClaw Gateway adapter (credentialed real-model E2E) ·
inbox malformed/dead-mail robustness · live central migration of this host ·
tmux transport/wake validation · Antigravity evaluated and parked at T0.

## Launch-phase roadmap (target: pneu 1.0.0 and the public push)

1. **In flight — Grok ACP adapter** (wt/grok): productize on the OpenClaw
   supervisor template; credentialed E2E is the support bar.
2. **In flight — worktree lifecycle commands** (wt/worktree-cmd):
   `roundtable worktree add/remove/list`, restate-before-act, group-aware
   fail-closed.
3. **The rename → 1.0.0.** Dedicated worktree after 1–2 merge; scope: brand,
   top-level command (`pneu`), package/artifact names, install prefix with
   deployed-state migration (hooks, permission allowlists, plists reference
   absolute paths — setup owns and rewrites them), skill rename, README
   rewrite with the new name and taglines, repo rename (GitHub redirects).
   `rt-*` tool names and `RT_*` env vars are retained as pneu's tool prefix.
   0.3.0 is skipped as a public release; its content rolls into 1.0.0.
4. **Clean-machine validation + demo recording** (combined): fresh user
   account walkthrough of the five-minute install, screen-recorded as the
   launch demo. Blocks the public "five minutes" claim.
5. **Launch materials**: repo page rewrite, Twitter/LinkedIn posts, resume
   entry — all unblocked by the name.
6. **Good citizenship**: file the zero-turn-resume upstream issue draft
   (awaiting Ocean's nod).

## Deferred / later

Hermes wake redesign residuals (BRIEF-0.2 #1; plugin works, RC10-era bugs to
re-verify) · wake-latency ergonomics study · v2 candidates: naming-system
unification (`rt-*`→`pn-*` at most in a later major), `-f json` everywhere,
teammate-as-instance, statusline unread counts · v3: switchboard GUI.

## Constraints

- Review-window freeze on the competition repo (`origin`, push-disabled):
  no pushes, no tag/Release changes until winners are announced
  (conservatively 2026-08-12). The product repo (`product` remote) is not
  frozen.
- Provenance and attribution rules in `AGENTS.md` are non-negotiable.
- The judged `v0.1.8` artifact and the pinned archive worktree
  (`archive/build-week`) remain untouched.
