# D7 quiet-wake batch — Codex implementation handoff

> Status: historical record — D7 implementation report

Source directive: [`quiet-wake-d7.md`](quiet-wake-d7.md)

## Delivered

- `rt-wait-inbox` is a long-lived watcher. An empty `new/` renews the fenced
  lease silently on a ten-second target cadence; it does not emit a heartbeat
  wake, exit for a Claude Stop-hook turn, or spend model tokens. Mail remains
  the wake edge, including malformed-mail listings.
- Hermes keeps one managed waiter across an empty interval; the old heartbeat
  restart path is removed. Supersession and exact-generation drain behavior
  remain fenced.
- `rt-worktree remove` reclaims only the exact project-hash runtime directory
  after its existing seat checks, and only when every lease owner is stale.
  Live or ambiguous owners retain the directory and produce an advisory.
- `rt-doctor` reports runtime directories whose project root is missing and
  whose registry entry is tombstoned or absent. The doctor path is
  report-only and never deletes runtime state.
- D7's standalone version baseline was `1.0.1`; the sequenced D8 batch
  supersedes it with combined release `1.1.0`.

## Evidence

- Focused D7 suite: **115 passed**.
- Full suite: **941 passed, 1 skipped** in **16m21s**.
- Accelerated empty-watcher soak: **20 samples × 25ms = 0.5s**, with a 2ms
  test cadence; the watcher stayed armed until mail arrived, returned to its
  baseline FD count within one descriptor, and stayed within a 16 MiB RSS
  high-water spread.
- Seat-health matrix: idle Claude remained `active_healthy` with heartbeat age
  below the 30-second TTL; Claude Stop-hook stayed armed while empty; Hermes
  kept one waiter without heartbeat restart; all-stale runtime leases were
  reclaimed; live and ambiguous leases were retained; doctor emitted a
  report-only tombstoned-residue warning while preserving the directory.
- `compileall` passed. The public-safety scan found one pre-existing reachable
  history finding in `799d9df`: a private Claude session URL in that commit's
  metadata. No new worktree finding was reported; the historical commit was
  not rewritten.

Claude acceptance remains: review/merge, release-artifact validation, and
hot-swap or reload handling for the installed runtime.
