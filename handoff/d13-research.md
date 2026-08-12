# D13 research dispatch (rt-d13-research)

Owner: Claude (product lead) on behalf of Ocean. Branch: `wt/rt-d13-research`.
Deliverables: an evidence file `handoff/d13-research-findings.md` on this
branch, a design recommendation mailed to `codex@rt-d13-impl` (they are
blocked on you for D13.9-fix), and a status mail to
`claude@roundtable-product`.

## Question 1 — CLAUDE_ENV_FILE real contract (empirical, highest priority)

Live failure being fixed: phone-spawned session (worktree
bridge-cse_01UXb6DTgRWKWtgQomZCmPbT, session
00666b27-d2f3-4965-8036-a7a11a37b620) — the pneu SessionStart adoption hook
crashed:

```
FileNotFoundError: '~/.claude/session-env/00666b27-.../sessionstart-hook-0.sh'
_rtruntime.RuntimeStateError: cannot inspect CLAUDE_ENV_FILE ...
```

The parent directory did not exist. Desktop sessions' session-env dirs are
empty too. Establish empirically on Claude Code 2.1.228:

1. For a SYNC SessionStart hook: is CLAUDE_ENV_FILE set? Does the file/dir
   pre-exist? If the hook creates the file itself and writes
   `export FOO=bar`, does a later Bash tool call in that session see FOO?
2. Same questions for an ASYNC SessionStart hook (one that prints
   `{"async": true, ...}`): is the env file read at sync-phase end, async
   end, or never?
3. Any difference for `claude remote-control --spawn worktree` sessions —
   if you can test cheaply; otherwise mark untested.
4. Is there an alternative supported env-injection channel (e.g.
   hookSpecificOutput JSON on stdout)?

Method: scratch project under /tmp or a throwaway dir with a
settings.local.json SessionStart hook that logs its environment and
creates/writes the env file; drive `claude -p` or a pty TUI. Do NOT modify
this repo's hooks or any live project's hooks. Keep artifacts in the
worktree.

## Question 2 — thread/name/set RPC shape (live probe, scratch thread only)

Against the live app-server socket
(`~/.codex/app-server-control/app-server-control.sock`, daemon 0.147.0):
start a scratch thread (thread/start in a throwaway cwd), call
`thread/name/set`, read back via thread/read or thread/list, and record the
exact params/response shapes plus the `thread/name/updated` notification.
Read-only otherwise: do not touch existing fleet threads, do not restart
the daemon, single socket is shared — errors must leave it clean.

## Question 3 — D13.9 fix recommendation

From Q1 evidence, recommend to codex@rt-d13-impl one design:
- (a) adoption creates the env file safely when absent
  (O_CREAT|O_EXCL, dir 0700 file 0600, ownership checks) — only if Claude
  actually sources hook-created files;
- (b) if env injection is unreliable/never for async hooks: adoption still
  claims the lease, and Stop/SessionStart managed-detection falls back to
  payload session_id ↔ lease match so the watcher arms without env; model
  shells then rely on the plist PATH (already shipped) + explicit agent
  args;
- (c) both, or something better the evidence supports.
State the recommendation as MUST/SHOULD items with the evidence line each
rests on.

## Discipline

- No changes to bin/ product code on this branch (evidence + docs only).
- No `Claude-Session:` trailers in commits.
- Mail: `RT_FROM=claude rt-say codex@rt-d13-impl handoff "..."` for the
  recommendation; status to `claude@roundtable-product`.
