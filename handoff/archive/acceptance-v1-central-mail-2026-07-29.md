# Final acceptance — v1/central-mail

> Status: historical record — final v1/central-mail acceptance

Date: 2026-07-29

Accepted commit: `e13bd69` (`fix: close central migration acceptance gaps`)

Claude verdict message: `20260729T164303Z-claude-to-codex-19146`

**Verdict: ACCEPT.** Claude accepted `v1/central-mail` through `e13bd69`
after a serial spot-check of the committed diff. M1, M2, and M3 are
implemented and accepted. M4 remains deferred on the documented upstream
Codex zero-turn resume limitation.

The final spot-check confirmed:

- active repair and rollback depend on durable registry-adjacent recovery
  records, not continued availability of archival backup bytes;
- archival backups default below the registry parent and can be redirected
  with `RT_MAIL_BACKUP_DIR` or `--backup-dir`;
- migration preflight, conservative lock timing, bounded admission, and the
  absolute remaining registry deadline close the availability finding;
- detached local-mail deletion is outside the exclusive section, while
  bookmark mutation deliberately remains inside to avoid a rollback race;
- same-version source installation detects input drift instead of silently
  reusing a different build;
- registry root/layout guards have direct mutation coverage;
- Codex arm failure releases its claimed lease on every handled path;
- the shipped environment failpoint was replaced by a module-private test
  hook and guarded by the public-safety scan.

Final gates at `e13bd69`:

- `775 passed in 548.35s`;
- compileall passed;
- public-safety passed over 109 tracked files and reachable history;
- working tree clean;
- nothing pushed or merged.

The two detailed review artifacts are preserved as:

- [M1 + M2 review](acceptance-m1-m2-2026-07-29.md)
- [M3 review](acceptance-m3-2026-07-29.md)

They are historical records at their named commits. Their interim rejection
or follow-up findings do not supersede the final `e13bd69` ACCEPT recorded
here.
