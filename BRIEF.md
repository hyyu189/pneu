# roundtable-product · BRIEF

## North star

Roundtable is the durable messaging and coordination layer for coding agents
that live on the same machine: per-project maildir mailboxes as the delivery
fact source, fenced seat identities, and harness-native wake bridges. `v0.1.8`
proved it under Build Week conditions; `0.2` makes it dependable for daily use
by people who did not build it.

## 0.2 roadmap

1. **Hermes native wake, redesigned.** Fix the TUI lifecycle
   (`on_session_reset` vs `on_session_start` arming), the reply-template
   argument construction bug (flags leaking into message bodies, tokens landing
   in `kind`), and the drain contract (archive processed mail `new/` → `cur/`
   so the watcher re-arms; RC10 field evidence in the archive worktree).
2. **Inbox robustness.** Fenced `rt-inbox` must surface malformed mail as
   malformed instead of silently hiding it while the file keeps waking the
   watcher (two reproduced specimens on 2026-07-21).
3. **Wake ergonomics.** Product-owned guidance for tripwire arming (no shell
   `&`; harness-native background), and re-examine wake latency expectations
   for slow-turn seats.
4. **Clean-machine and terminal matrix validation.** Fresh-account install,
   Terminal.app / iTerm2 / Ghostty wake UX matrix — the promotion gates the
   Build Week window did not close.
5. **Onboarding and brand.** First-run polish; continue the rename search.
   The earlier working name Orca is rejected (2026-07-23, see decision.md:
   stablyai/orca occupies the coding-agent category and GNOME Orca owns the
   `orca` command). A candidate needs an unclaimed command name on
   brew/apt/PyPI/npm, a clean first search page for "<name> agent" and
   "<name> CLI", and no collision with an active developer-tool brand. A
   GitHub repo rename preserves history and redirects.
6. **README productization.** Product-first front page; Build Week narrative
   moves to a history section. `PROVENANCE.md`, `CREDITS.md`, and `v0.1.8`
   remain untouched.

## Constraints

- Review-window freeze: no pushes to `main`, no tag/Release changes until
  winners are announced (conservatively 2026-08-12).
- Provenance and attribution rules in `AGENTS.md` are non-negotiable.
- Judging-period availability: the released `v0.1.8` behavior on this machine
  must stay reproducible until at least 2026-08-05.
