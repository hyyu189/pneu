# D13 research findings (rt-d13-research)

> Status: historical record — D13 research evidence

Date: 2026-08-11 (evening, PDT). Researcher: Claude seat `claude@rt-d13-research`.
Dispatch: `handoff/archive/d13-research.md`; requesting message
`20260812T005542Z-claude-to-claude-8471` from `claude@roundtable-product`.

Test environment: Claude Code **2.1.228** (`~/.local/bin/claude`), macOS
Darwin 27.0.0, headless `claude -p` runs in throwaway scratch projects under
the session scratchpad, `--setting-sources project,local` (so only the
scratch project's own SessionStart hook fired, except run 1 which also loaded
user settings). Codex app-server daemon **0.147.0** on
`~/.codex/app-server-control/app-server-control.sock`.

Raw artifacts (hook logs, transcripts, probe JSON) lived in the session
scratchpad; every load-bearing value is quoted verbatim below.

## Q1 — CLAUDE_ENV_FILE empirical contract (Claude Code 2.1.228)

### Q1.1 Sync SessionStart hook

Scratch project with one command SessionStart hook (plain sync: exit 0, no
JSON). Hook logged its env and stdin, then created the env file itself and
appended `export RT_PROBE_SYNC=sync_hello_1786498461`.

- `CLAUDE_ENV_FILE` **is set** for the hook process:
  `~/.claude/session-env/<session_id>/sessionstart-hook-<N>.sh`, where `<N>`
  is the hook's index among configured SessionStart hooks (run 1 loaded user
  settings too: the global pneu hook was index 0 and the scratch hook got
  `sessionstart-hook-1.sh`; with only local settings the scratch hook got
  `sessionstart-hook-0.sh`).
- Parent dir `~/.claude/session-env/<session_id>/` **pre-existed** (created
  by Claude Code before hooks ran) in every desktop/headless run.
- The env **file itself never pre-exists** (`file_exists=no` in every first
  `startup` firing; all ~190 historical `~/.claude/session-env/<sid>/` dirs
  on this host are empty — no session on this machine had ever written one).
- After the hook wrote `export RT_PROBE_SYNC=...`, the session's Bash tool
  saw it: `RT_PROBE_SYNC=[sync_hello_1786498461]`. **Hook-created files are
  genuinely sourced; injection works.**

### Q1.2 Async SessionStart hook

Same setup, but the hook printed `{"async": true, "asyncTimeout": 60000}`,
kept running, and wrote two markers: `RT_PROBE_ASYNC_T0` at hook start
(epoch 1786498861, before printing the async JSON) and `RT_PROBE_ASYNC_T10`
ten seconds later (1786498871), well after the async declaration.

- `CLAUDE_ENV_FILE` **is set for async hooks too**, same path scheme, same
  dir-yes/file-no pre-existence.
- Bash call at epoch 1786498867: `T0=[t0_1786498861] T10=[]` — T0 visible,
  T10 (not yet written) absent.
- Bash call at epoch 1786498896: `T0=[t0_1786498861] T10=[t10_1786498871]`.
- Conclusion: the env file is **not** read once at sync-phase end; it is
  (re-)applied per Bash tool invocation. A write made at any time while the
  session lives — including mid-async-phase — is visible to every subsequent
  Bash call. There is no "too late for async hooks" failure mode on 2.1.228.

### Q1.2b Resume semantics (bears directly on O_EXCL)

`claude -p --resume <same session>` re-fired the hook with
`source: "resume"`, the **same** `CLAUDE_ENV_FILE` path, and
`file_exists=yes` (the file persisted from the first run; it also persists
after session end). The hook appended a fresh
`export RT_PROBE_ASYNC_T0=t0_1786499021`; the resumed session's Bash then saw
`T0=[t0_1786499021] T10=[t10_1786498871]` — the whole file is re-sourced and
the later export of a repeated name wins.

Consequence: **a bare `O_CREAT|O_EXCL` create fails on every
resume/clear/compact re-fire** of the same session. Creation must tolerate an
existing file (validated append), and repeated appends of the same variable
are safe (last export wins).

### Q1.3 `claude remote-control --spawn worktree` sessions

**Untested** (needs a live phone-side RC pairing; not cheap from this seat).
The one live data point is the crash under investigation
(worktree `bridge-cse_01UXb6DTgRWKWtgQomZCmPbT`, session
`00666b27-d2f3-4965-8036-a7a11a37b620`): `CLAUDE_ENV_FILE` was set, and the
**parent directory did not exist** (`FileNotFoundError` on
`.../session-env/00666b27-.../sessionstart-hook-0.sh`; the dir is still
absent-empty now). So the desktop guarantee "per-session dir pre-exists" does
NOT hold on the phone-spawn path; a correct hook must `mkdir -p` the parent.

### Q1.4 Alternative env-injection channels

A sync hook that emitted
`{"hookSpecificOutput": {"hookEventName": "SessionStart", "env": {"RT_PROBE_JSON": "json_hello"}, "additionalContext": "probe-context-marker"}}`
produced: `RT_PROBE_JSON=[]` (empty — **no env injection via JSON output**),
while `probe-context-marker` did reach the model as SessionStart additional
context. So `hookSpecificOutput` injects *context*, not *environment*;
**CLAUDE_ENV_FILE is the only supported env-injection channel** on 2.1.228.

### Side observation — why the live hook crashed, and a loop hazard

- Crash site: `bin/rt-wait-inbox` `_claude_env_file()` (`lstat` on
  `CLAUDE_ENV_FILE` → any `OSError` raises `RuntimeStateError: cannot
  inspect ...`). Since the file *never* pre-exists (Q1.1), the current code
  can only succeed if something else created the file first; on the
  phone-spawn path even the parent dir is missing. The contract the code
  assumed ("file exists, inspect it") does not match the real contract
  ("path is reserved for you; create it yourself").
- Loop hazard reproduced incidentally (run 1): in a session whose env
  carries *partial* RT_* lease vars (e.g. `RT_PROJECT_ROOT`/`RT_SESSION_ID`
  present, `RT_FROM` absent — inherited env, not a real lease),
  `_claude_hook_is_managed()` treats it as managed and the Stop hook exits
  with the usage error every turn; Claude Code re-fires Stop after each
  failed stop, giving an unbounded error loop with no backoff. Fail-closed
  here burns tokens indefinitely; worth a separate hardening item.

## Q2 — thread/name/set RPC shapes (app-server 0.147.0, live socket)

Probe: one scratch thread only (`thread/start` in a throwaway cwd);
everything else read-only; all requests via the repo's own
`AppServerClient` (WebSocket-over-UDS, `initialize` with
`capabilities.experimentalApi: true`). Socket left clean (clean WebSocket
close; no daemon restart; no other thread touched).

- `thread/start` params `{"cwd": "<dir>"}` → result
  `{"thread": {...}, "model": "gpt-5.6-sol", ...}`; the thread object carries
  `"id"`, `"sessionId"` (same UUID), `"name": null`, `"cwd"`, `"path"`
  (rollout jsonl), `"status": {"type": "idle"}`, `"source": "vscode"`, etc.
  Scratch thread id: `019ff3a1-7449-7820-9aa9-e15baf299430`.
- **`thread/name/set`** params:

  ```json
  {"threadId": "019ff3a1-7449-7820-9aa9-e15baf299430", "name": "rt-d13-q2-probe-name"}
  ```

  → result: `{}` (empty object; success carries no payload). First-guess
  shape accepted; no alternate shapes were needed.
- Notification emitted on the same connection:

  ```json
  {"method": "thread/name/updated",
   "params": {"threadId": "019ff3a1-7449-7820-9aa9-e15baf299430",
              "threadName": "rt-d13-q2-probe-name"}}
  ```

  Note the field is **`threadName`** (not `name`) in the notification, while
  request param and thread object use `name`.
- Read-back: `thread/read {"threadId": ..., "includeTurns": false}` →
  `{"thread": {... "name": "rt-d13-q2-probe-name" ...}}`. Persisted.
- `thread/list` page shape is `{"data": [...], "nextCursor",
  "backwardsCursor"}`; the server caps a page at 25 even when
  `pageSize: 50` is requested. The fresh zero-turn scratch thread did **not**
  appear in the newest pages (its `recencyAt` was newer than every listed
  entry), so `thread/list` appears to exclude turn-less threads —
  **verify naming via `thread/read`, not `thread/list`.**

The scratch thread remains in `~/.codex/sessions/` (zero turns, named
`rt-d13-q2-probe-name`, cwd under the session scratchpad); deleting it was
out of scope for a read-only probe.

## Q3 — recommendation to codex@rt-d13-impl (D13.9-fix)

Design **(a) + the lease-first half of (b)** — create the env file, but never
let env persistence gate adoption:

1. **MUST — adoption creates `CLAUDE_ENV_FILE` when absent.** Evidence: the
   file never pre-exists on 2.1.228 (Q1.1) yet injection genuinely works for
   both sync and async hooks once the hook creates it (Q1.1/Q1.2), so
   creation is both necessary and sufficient; the current
   inspect-must-exist code can never succeed on a fresh session.
2. **MUST — create the parent directory too**: `mkdir -p` semantics, dirs
   0700, file 0600. Evidence: dir pre-exists on desktop but did not on the
   live phone-spawn crash (Q1.3); `~/.claude/session-env` itself is
   user-owned, so 0700 subdirs are safe.
3. **MUST — do not use bare `O_CREAT|O_EXCL` as the only path.** Evidence:
   resume/clear/compact re-fires hand the hook the *same existing* file
   (Q1.2b). Correct pattern: `open(O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW,
   0600)` then `fstat` validation (regular file, `st_uid == getuid()`,
   optionally `st_nlink == 1`) — the existing `_persist_adopted_environment`
   append/quote logic is already right; only the "must already exist"
   inspection is wrong. Appending duplicate exports on re-adoption is safe:
   the whole file is re-sourced and the last export wins (Q1.2b).
4. **MUST — claim the lease before touching the env file, and treat env
   persistence failure as degraded, not fatal.** Evidence: today the
   `RuntimeStateError` aborts the whole SessionStart hook (live crash), so a
   single filesystem hiccup loses adoption *and* watcher arming. With the
   lease claimed, Stop/SessionStart managed detection can fall back to
   payload `session_id` ↔ lease matching (the hook payload always carries
   `session_id` + `cwd`; no env needed for that), and model shells still
   have the plist PATH + explicit agent args already shipped in D13 batch 1.
5. **SHOULD — keep the b-style fallback wired even with (a) working**, for
   sessions where the env write succeeded but the *model shell* env is
   stale-or-absent (e.g. env file written after a Bash call already ran, or
   future harness changes): detection by `session_id` ↔ lease match is
   strictly more robust than env sniffing, and Q1.2 shows env-file timing is
   forgiving but not contractual.
6. **SHOULD — separate hardening: bound the managed-detection fail-closed
   path.** Evidence: partial RT_* env (no `RT_FROM`) makes every Stop-hook
   invocation exit with a usage error and Claude re-fires it each turn —
   an unbounded token-burning loop observed live during this research
   (Q1 side observation).

No evidence supported (b)-only: env injection is not "unreliable/never" for
async hooks — it works whenever the file gets written (Q1.2) — so giving up
on env injection would discard a working mechanism that the shipped PATH
fallback only partially replaces.
