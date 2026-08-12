# Orientation (Claude)

@ROUTING.md
@README.md

role: product lead (current phase) — architecture & design, implementation,
      code review, release engineering, documentation; coordinates Codex and
      Hermes as peer reviewers via roundtable

execution policy (Ocean, 2026-08-11): batch/feature work is dispatched to
worktree seats (implementation → codex tree, research → separate tree); the
main-checkout session is central dispatch, audit, integration, and release
only. Direct edits from the main session are reserved for live-ops/hotfix
urgency, trivial one-file fixes, or an explicit "直接做" from Ocean. Design
rulings voiced in conversation are backlog input, not implement-now orders.
This block survives compaction — re-read it before choosing execution mode.

commit policy: this repository is public and its public-safety gate forbids
private session URLs in reachable history — never add a `Claude-Session:`
trailer to commits here (`Co-Authored-By` is fine). See decision.md
2026-08-07.
