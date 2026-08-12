# D13 implementation dispatch (codex@rt-d13-impl)

Owner: Claude (product lead) on behalf of Ocean. Branch: `wt/rt-d13-impl`.
Base: main@a678ff4 (D13 batch 1 already landed there: rc-host plist PATH,
doctor rc-host-path check, grok full-permission wake default, wording).

Deliver items D13.1/3/4/5 now; D13.9-fix is blocked on the research seat
(codex@rt-d13-research will send you findings) — start it only after that
mail arrives. Ship as commits on this branch + a report file
`handoff/d13-impl-codex.md` + `rt-say claude@roundtable-product` when done.

## D13.1 — bridge names codex threads at bind

At every successful bind/adoption in `bin/rt-codex-wake` (bind_command and
the auto-bind/SessionStart consumption path), call app-server RPC
`thread/name/set` to name the thread `{agent}@{project_name}` (project
basename, same rendering as launcher RC naming). Requirements:
- Idempotent; a rename failure must NOT fail the bind — log to the wake log
  and continue (naming is cosmetic, binding is load-bearing).
- Exact params shape: verify against the live daemon 0.147.0 via a scratch
  thread first (the research seat is independently confirming the RPC; do
  your own probe before wiring). Do not touch existing fleet threads during
  probing.
- Tests: unit-level with a fake client asserting the RPC is issued after
  bind and that RPC failure leaves the bind result unchanged.

## D13.3 — seat-occupied refusal names the owning process location

Current refusal: `rt-codex: seat 'codex' is active in <proj>; owner pid N is
running; wake heartbeat age=...`. Ocean could not find the process (it lived
in a tmux session). Extend the refusal detail in the launcher path
(bin/_rtlauncher.py seat-claim refusal) with best-effort location:
- `ps -o tty= -p <pid>` → `tty=ttysNNN` (or `tty=none` for headless).
- If tmux is running, scan `tmux list-panes -a -F '#{pane_pid} #{session_name}:#{window_name}'`
  (also match descendant pids: the pane pid is the shell, owner may be a
  child — compare against the pid's ancestor chain) → append
  `tmux=<session>:<window>`.
- All best-effort: any failure → omit the fragment, never block or slow the
  refusal path noticeably. Applies to claude/codex/hermes launchers (shared
  helper).
- Tests: unit test the formatter with fake ps/tmux outputs; mutation-check
  the "omit on failure" branch.

## D13.4 — launcher card offers thread resume for bound, non-active codex seats

In `bin/pneu` seat card: when the selected codex seat is vacant/stale AND
`rt-codex-wake` state has a binding for the project, render the seat line
with a `(bound thread)` marker and make Enter perform a thread-preserving
resume instead of a fresh launch: run handoff (`rt-codex-wake handoff
<project> --thread-id <tid>`) then exec `rt-codex resume <tid>`.
- If handoff refuses (e.g. seat actually active), fall back to the current
  behavior with the refusal shown as the card notice.
- Read the binding via a small helper reading the wake state file
  read-only; missing/corrupt state → no marker, current behavior.
- Tests: pty test with a fabricated state file asserting marker rendering
  and the exec argv; corrupt-state test asserting silent fallback.

## D13.5 — one-key seat init for installed-but-unconfigured harnesses

Card `unavailable:` entries currently tell the user to edit agents.yaml.
When the harness executable RESOLVES but the project has no seat for it,
offer an in-card action instead: hotkey `a` opens a one-line chooser of
addable harnesses; choosing one appends the seat block to
`.roundtable/agents.yaml` (same schema/format `roundtable-init` writes,
default instance id = harness's conventional agent name) and re-renders the
card with the new seat selectable.
- Missing-executable harnesses keep the current install hint (not addable).
- agents.yaml edit must be atomic (write temp + rename), preserve existing
  content byte-for-byte outside the appended block, and refuse if the file
  has uncommitted schema drift it cannot parse.
- Footer gains `a add seat` only when at least one harness is addable.
- Tests: pty add-flow test (choose harness → agents.yaml gains seat →
  card shows it), refusal test on unparseable agents.yaml.

## D13.9-fix — phone-spawn adoption (BLOCKED: wait for research mail)

Root cause (already established): `_adopt_unleased_claude_session` in
`bin/rt-wait-inbox` fail-closes when `CLAUDE_ENV_FILE` points at a
non-existent file; live phone-spawned session showed
`~/.claude/session-env/<sid>/sessionstart-hook-0.sh` ENOENT (parent dir
absent too), so adoption crashed before lease claim. Desktop session-env
dirs are empty as well — the "env file pre-exists" assumption is wrong in
general. The research seat is establishing the real CLAUDE_ENV_FILE
contract (does Claude source a hook-created file? async timing?). Their
mail will carry a recommended design; implement per that design. Candidate
shapes they are evaluating:
- (a) create the env file (and parent dir) safely when absent
  (O_CREAT|O_EXCL, 0600/0700, ownership checks) and keep the append path;
- (b) additionally make the Stop-hook managed-detection fall back to
  payload session_id ↔ lease match, so the watcher arms even when env
  injection is unavailable.

## Discipline

- Full suite (`mamba run -n general pytest -q`), compileall, and
  `scripts/check_public_safety.py` green before reporting.
- No `Claude-Session:` trailers in commits (public repo); Co-Authored-By ok.
- Do not touch `~/.roundtable` live state; repo-only work plus read-only
  probes of the app-server socket on scratch threads.
- Report: `handoff/d13-impl-codex.md` + `RT_FROM=codex rt-say claude@roundtable-product status "..."`.
