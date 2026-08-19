# Codex app-server wake bridge — seat lifecycle handoff

> Status: historical record — the 0.144.3 wake-bridge milestone; docs/compatibility.md is the current support matrix

> This file records the original `0.144.3` implementation milestone and the
> 2026-07-17 validation evidence. It is not the current support matrix. See
> `docs/compatibility.md` for release claims and validation gates.

Implementation target: Codex CLI `0.144.3`. The wire schema and wake path are
version-gated to that exact release.

## Topology

The npm Codex installation cannot use `codex app-server daemon start`: that
manager requires the standalone installer at
`~/.codex/packages/standalone/current/codex`. Roundtable therefore owns a user
LaunchAgent (`com.roundtable.codex-app-server`) which runs the installed Codex
CLI directly as:

```text
codex app-server --listen unix://$HOME/.codex/app-server-control/app-server-control.sock
```

`rt-codex-daemon ensure` performs a real initialize/initialized handshake, uses
a single-flight startup lock, installs the LaunchAgent if absent, kickstarts it,
and waits for the handshake to recover. Socket existence alone is not treated
as health.

A bare scratch TUI with no config overrides auto-detected the default daemon.
The isolated proof correlated the TUI's Unix fd peer with the LaunchAgent's
named control socket via `lsof`. The production cmux wrapper is different: it
injects per-launch `-c` and hook-trust flags, which make the implicit TUI probe
ineligible. `rt-codex` therefore forces `--remote unix://` while preserving the
normal `codex`/cmux wrapper path.

## Bridge safety contract

`rt-codex-wake`:

- watches valid roots from `~/.roundtable/projects.yaml`; `run --project` is a
  process-local diagnostic override and is never persisted by `install`;
- treats maildir `new/` as the fact source and never moves or deletes mail;
- validates every mail header/id/target and rejects symlinks or non-files;
- binds a project to one root, non-ephemeral TUI thread and persists the mapping
  (`cli`, or app-server's `vscode` + `threadSource=user|None` for `--remote`);
- requires `CODEX_THREAD_ID` self-registration (`rt-codex-wake bind`) by
  default; legacy embedded-CLI discovery is available only through the
  explicit `--auto-discover` compatibility switch, because discovery caused a
  real cross-project wake during the 2026-07-17 cutover;
- resumes and revalidates the bound thread after every daemon reconnect;
- starts one short pointer turn for a non-empty generation only when status is
  idle; an active thread waits for its matching `turn/completed` notification;
- persists generation/turn state, uses bounded exponential retry for a failed,
  ambiguous, or completed-but-undrained wake, and leaves every message in
  `new/` on failure;
- opens a circuit after three consecutive interrupted-undrained wakes for the
  same `(generation, thread)`, persists `NEEDS_HUMAN`, attempts one desktop
  notification, and starts no further wake until a human explicitly recovers
  the project;
- preflights hook trust with `hooks/list`, and precisely associates approval,
  user-input, and MCP-elicitation requests with the bridge-owned wake turn;
- uses a kernel `flock` singleton (released even on SIGKILL) and locked atomic
  state writes so a live bridge cannot overwrite an external rebind;
- revalidates the CLI/app-server version on every connection and stops issuing
  wake RPCs outside the exact validated release;
- writes only metadata to its ignored runtime log, never mail bodies.

The app-server currently has no atomic “start only if idle” RPC. The immediate
status recheck minimizes, but cannot eliminate, the status-to-`turn/start`
TOCTOU window because `turn/start` can steer an active regular turn. Strict
elimination requires an upstream conditional wake/CAS method.

App-server human-decision requests are multicast to thread subscribers and the
first response wins. The bridge deliberately does not race the TUI with
synthetic approval or user-input responses. A headless wake **cannot cross any
human decision gate**: modified/untrusted hooks, command or file approvals,
permission requests, user input, and MCP elicitation all require a live human
surface. Hook trust is checked before starting a wake. Approval/input requests
for an active bridge-owned turn are persisted while a live TUI has a chance to
resolve them; if that exact turn ends `interrupted` with a request unresolved,
the bridge atomically latches the project in `NEEDS_HUMAN`. In either gate case
it leaves the original mail pending, records one metadata-only event, and makes
one best-effort cmux desktop notification. It never sends a Roundtable message
as its own alert, because that would change the inbox generation and create a
feedback loop.

## Operations

```bash
# daemon persistence and health
~/.roundtable/bin/rt-codex-daemon install
~/.roundtable/bin/rt-codex-daemon ensure

# required from a remote target Codex turn at the coordinated cutover
~/.roundtable/bin/rt-codex-wake bind /absolute/project/root

# remove a stale or retired binding without deleting project mail
~/.roundtable/bin/rt-codex-wake unbind /absolute/project/root

# after attaching the target TUI and resolving a trust/approval/input gate or
# the repeated interruption cause, explicitly re-bind to clear NEEDS_HUMAN
~/.roundtable/bin/rt-codex-wake bind /absolute/project/root

# register the root, then install/reload the registry-backed bridge
~/.roundtable/bin/rt-projects add /absolute/project/root
~/.roundtable/bin/rt-codex-wake install --reload

# five operational checks, nonzero only for FAIL (WARN is legacy fallback)
~/.roundtable/bin/rt-doctor
```

### Thread-preserving seat handoff

When a Codex seat must be restarted without losing its native thread, use the
guarded handoff command from outside the active seat:

```bash
~/.roundtable/bin/rt-codex-wake handoff /absolute/project/root \
  --thread-id "$CODEX_THREAD_ID"
```

It verifies the thread's project identity, refuses a live or ambiguous Codex
seat, clears only a launch intent proven stale, removes the old binding, and
prints the exact `rt-codex resume <thread-id>` command plus the next binding
step. It prepares the restart; it does not launch a detached Codex process.

The manual fallback remains the four-step sequence: verify the old seat is
stale, run `rt-codex-wake unbind <project>`, remove only the matching stale
`codex-launch-intent.json`, then run `rt-codex resume <thread-id>` and send
one activation turn. If SessionStart does not bind the resumed thread, use
the diagnostic `rt-codex-wake bind <project> --thread-id <thread-id>` from
that Codex turn.

## Project registry and launchers

`~/.roundtable/projects.yaml` is the sole discovery source for the wake bridge,
doctor, startup advisory, workspace lookup, and harness launch menu. Manage it
with `rt-projects list|add|rm`; `roundtable-init` registers a project only after
its bootstrap succeeds. Consumers validate that each registered root still has
`.roundtable/agents.yaml`, warn about invalid entries, and never delete them
implicitly.

`rt-codex`, `rt-claude`, and `rt-hermes` share one selector. Inside a project
they preserve the existing direct-launch behavior. Outside a project they show
the registry menu only on a TTY; non-interactive use fails with exit 2 instead
of waiting for input. Codex retains its `--remote unix://` injection. Claude
receives its original arguments unchanged. A bare Hermes seat defaults to
`hermes --tui`; explicit Hermes arguments remain unchanged for compatibility
with its scripted and management modes.

At the time of this historical validation proof, the local harness configurations
called `rt-watch-ensure`. That watcher and its SessionStart path are now
retired. The current release treats `rt-startup-advisory` as an optional cmux
integration and still requires ownership-safe Claude/Hermes hook onboarding;
see `architecture.md`.

Installing the bridge does not migrate an already-running embedded TUI. The
current cmux Codex session must only be restarted through `rt-codex` during the
separately coordinated production cutover.

## Verification

The regression suite covers WebSocket-over-UDS framing and the exact initialize
envelope, multi-mail single wake, busy completion gating, fail-closed identity
and mail validation, failed/undrained wake backoff, the interrupted-wake circuit
breaker, trust and human-decision gates, one-shot notification, daemon-resume
recovery, crash-safe singleton locking, concurrent rebind preservation, exact
version gating, launchd root persistence, daemon self-heal orchestration, and
doctor failure/WARN/socket-mismatch states.

```bash
cd ~/.roundtable
mamba run -n general python -m pytest -p no:rerunfailures -q \
  tests/test_rt_codex.py tests/test_rt_tooling.py
```

The local `pytest-rerunfailures` plugin opens a TCP status socket during pytest
configuration, so it is disabled above; the app-server regression itself uses
only an isolated Unix socket.

### Isolated live acceptance (2026-07-17)

The production cmux Codex session was not restarted. All four acceptance paths
ran against `/private/tmp/rt-phase3-accept-20260717` and its scratch TUI:

1. A `--no-nudge` message woke an idle remote TUI, created
   `acceptance-1.txt`, emitted a quiet ack, and moved the mail from `new/` to
   `cur/` without keyboard input.
2. A message delivered during a controlled 12-second turn remained pending.
   The bridge started its one wake only after `turn/completed`, then created
   `acceptance-2.txt` and drained the mail.
3. The app-server LaunchAgent was booted out (old PID `91975`). The next
   pending delivery caused the bridge startup path to reinstall and start the
   daemon (new PID `92782`), resume the persisted thread, create
   `acceptance-3.txt`, acknowledge it, and return the project state to
   `EMPTY`.
4. Live `rt-doctor` reported daemon, socket ownership, JSON-RPC handshake,
   exact version, and bridge heartbeat as `OK`. Automated tests exercise the
   daemon/socket/RPC/bridge failure matrix, unsupported-version `WARN`, and
   socket/plist drift remediation.

Final verification: `101 passed` in the combined bridge and tooling suite.
