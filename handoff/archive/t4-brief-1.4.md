# 1.4 Track T4 — grok-4.6 introduction (supervised)

> Status: historical record — 1.4 track T4 dispatch; merged

Seat: claude (Opus 5, max reasoning effort; fall back to xhigh if max is
unavailable). You are authorized to use ultracode or the Workflow tool at
your discretion. Your role: SUPERVISOR of a grok worker — dispatch, review,
land; do the mechanical setup yourself, but the trial deliverables are
grok's to produce.

## Phase 1 — validation gate (do first)

The grok seat's wake path is model-armed (TUI-first: a pinned activation
turn makes the model create one persistent maildir monitor). The model just
moved to grok-4.6, so the arming behavior must be re-proven before any
delegation:

- Use the existing lab project `~/Code/rt-grok-e2e` (registered, LOCAL
  layout — mail lives under the project's `.roundtable/inbox/...`).
- Launch the grok seat (`rt-grok`), confirm the activation turn arms the
  monitor on 4.6, then run the live wake E2E: send a local test mail →
  monitor fires → grok drains and acks. Precedent run passed in 14s on the
  prior model. Record timings and any 4.6 behavior differences.
- Grok credentials are the operator's and READ-ONLY: never refresh, copy,
  or log them. In grok dialogs, choose conservative defaults only.

## Phase 2 — trial work orders (grok produces, you review and land)

1. **E1**: update `docs/compatibility.md`'s grok section to reflect the
   passed live wake E2E (TUI-first seat, model-armed monitor, re-arm on
   launch/resume, doctor advisory) — wording consistent with the file's
   evidence-based conventions.
2. **A3**: evolve `scripts/herdr_open_lab.py` (operator-only lab) into a
   one-command automated live lab for herdr named-session validation:
   create isolated `--session`, run the open→seat-active→mail-loop checks,
   tear down, report pass/fail. Must never touch the operator's main herdr
   session.

Review grok's output critically (mutation-mindset), commit approved work to
THIS worktree branch with clear attribution in the commit message.

## Phase 3 — leader-socket research (after trials land)

Investigate the grok leader-socket as a code-armed wake channel (B→A
upgrade): can pneu inject a wake turn into the live grok TUI via its leader
socket instead of relying on the model-armed monitor? Lab it (~1h scale),
document feasibility, risks (undocumented internal — drift check needed),
and a recommendation. Research + doc only; no adapter rewrite.

## Rate-limit protocol (important)

The operator's grok subscription is inexpensive and MAY hit usage limits.
When grok stalls on limits: you monitor, wait out the window, and restart
or re-issue its task; pace grok's work to avoid burning quota on retries.
Do not switch the trial deliverables to yourself unless grok is hard-blocked
for the rest of the cycle — in that case do E1 yourself, keep A3 queued for
grok, and note the substitution in your report.

## Constraints

- English-only, public-safe artifacts. Commit to THIS worktree branch only;
  do NOT merge — operator merges manually.
- Report per-phase via `rt-say claude@roundtable-product update ...`.
