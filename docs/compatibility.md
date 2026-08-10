# Runtime compatibility

pneu's durable maildir core and its harness wake adapters have different
compatibility boundaries. Delivery can succeed while an offline or unsupported
harness remains unwoken.

## Harness onboarding matrix

`roundtable-setup` configures harnesses already installed by the user. It does
not install a harness, create an account, copy credentials, or certify that a
real vendor session can wake.

| Harness | Packaged and automated | Still required before support promotion |
| --- | --- | --- |
| Claude Code | Global skill link; owned asynchronous SessionStart/Stop watchers; absolute lease-fenced mail permissions; plan/apply/status/remove tests; two sequential installed-RC8 development-host wake generations | Clean-account real send-to-wake-to-drain/ack repeat |
| Hermes | Global skill link; packaged lifecycle plugin; marked plugin enablement; plan/apply/status/remove tests; two sequential RC7 development-host wake generations | RC8 artifact and clean-account plugin/wake repeat |
| Codex | Shared executable resolver; global skill link; owned SessionStart auto-bind hook; owned app-server and wake plist generation; fail-closed service preflight tests; development-host cutover and thread/lease identity spike | Clean-account repeat and real send-to-wake-to-drain/ack |
| OpenClaw | Isolated Gateway adapter; fenced lease and identity checks; project-scoped state and loopback Gateway protocol tests | Clean-account/terminal-matrix repeat before public support promotion |
| Grok Build `0.2.118` | Isolated stdlib ACP supervisor; fenced lease and identity checks; bounded HOME/XDG/GROK_HOME/TMP/log state; exact mail-only permission policy; child-death recovery; focused fault, mutation, soak, three-seat interop, refreshed-credential two-generation E2E, and extracted release-artifact smoke | Clean-account/terminal-matrix repeat and an explicit vendor-supported token-refresh/preflight contract before public support promotion |

The Codex plist files are written but not loaded by setup. This is an
intentional safety boundary, not evidence that the daemon is running.
Conversely, removing Codex onboarding requires
`roundtable-setup remove --unload-codex` from outside a Codex session, so a
loaded job cannot be orphaned after its plist and executable are removed.

The normal `pneu` launcher owns the next step: it performs a targeted
Codex service preflight and starts or repairs only states proven safe. Users do
not normally run the two low-level service reload commands themselves.

## Grok ACP adapter

The Grok path is a project-anchored, stdlib-only wake adapter. `rt-grok` claims
the `grok` seat and transfers the fenced identity to `rt-grok-wake`, which
supervises one `grok agent --no-leader stdio` child. It does not install hooks,
plugins, LaunchAgents, a shared leader, or a daemon, and it does not replace
the durable maildir as the delivery fact source.

The child environment is bounded to an adapter-owned root for `HOME`,
`GROK_HOME`, XDG directories, `TMPDIR`, logs, and temporary state. It receives
only the selected executable, project/registry paths, lease identity, and an
existing credential supplied through the runtime boundary. The adapter refuses
missing or ambiguous identity, stale lease revisions, shell operators, and
commands outside the exact fenced `rt-inbox`/`rt-ack` policy. It reports a
successful generation only after the triggered filenames leave `new/`; a
timeout or failed child leaves mail durable for manual recovery.

Focused product tests cover process-down and killed-turn recovery, auth and
permission failures, bounded hung prompts, fence mutations, duplicate-safe
generation handling, 25 wake cycles with resource bounds, and three-seat
Grok/Claude/Codex mail interop. The first 2026-08-06 product-adapter attempt
correctly failed closed on an expired host OAuth credential with ACP 403
`unauthenticated:bad-credentials`. After an external Grok TUI refresh, a
read-only rerun passed two mail-to-wake-to-drain-to-ack generations with the
ACP child killed and restarted between them; the final `new/` was empty and
the auth file was unchanged during the lab.

The lifecycle boundary is deliberate: the adapter reads the current OIDC auth
file key at each child start/restart and supplies it as `XAI_API_KEY`, but does
not refresh tokens or depend on a manually launched TUI. Expired credentials
remain a durable-mail/manual-recovery condition until an approved
vendor-supported refresh/preflight contract exists. Grok is therefore
credentialed-development-host verified but not yet promoted as public support.

## Codex executable selection

Every Codex-facing component uses the same resolver:

1. explicit `RT_CODEX_BIN`;
2. the official standalone cache at
   `$CODEX_HOME/packages/standalone/current/codex`;
3. the common npm installation at `~/.npm-global/bin/codex`;
4. `~/.local/bin/codex`;
5. a `codex` executable on the controlled fallback PATH.

The selected path is preserved rather than dereferenced. This matters for both
the standalone `current` link and npm's visible CLI shim. The launcher,
app-server LaunchAgent, wake LaunchAgent, daemon checks, and doctor all use that
same path.

One static custom `CODEX_HOME` is supported when it is an absolute owned path
under the user's home and is present consistently during setup and launch. Its
hook, skill link, standalone resolver, socket, and LaunchAgent environment all
use that same root. Switching `CODEX_HOME` between identities on individual
launches is multi-auth lifecycle management and remains outside P0; setup and
preflight fail closed on the resulting ownership drift.

## Terminal launcher portability

`rt-claude`, `rt-hermes`, and `rt-codex` execute absolute harness paths. The
Claude and Hermes resolvers prefer their normal user-level installations, then
search PATH while rejecting cmux's generated `cmux-cli-shims` and wrapper
targets. `RT_CLAUDE_BIN`, `RT_HERMES_BIN`, and `RT_CODEX_BIN` provide explicit
selection; an explicit Claude or Hermes path is still rejected if it resolves
to a cmux wrapper.

With no native Hermes arguments, the pneu seat launches as
`hermes --tui`. Any explicit arguments are passed through unchanged, preserving
oneshot, headless, and management modes; callers can request `--tui` alongside
resume or other native arguments when desired.

The packaged Hermes plugin starts its fenced inbox watcher from the TUI's
initial `on_session_reset` event, before the user has sent a first prompt, and
replaces that watcher on later native session resets. Classic interactive CLI
sessions use Hermes' direct message-injection API. TUI sessions use Hermes'
public managed background-completion rail, addressed with the exact native
session key. A token handshake releases the bounded notification helper only
after Hermes confirms `notify_on_complete`, avoiding its spawn-before-notify
race. If the host does not explicitly confirm asynchronous delivery, the
adapter stops rather than adopting or silently losing the mail. This path was
inspected against Hermes Agent `0.19.0`; older releases are not yet a support
claim. These paths have focused automated coverage, but the real credentialed
send-to-wake-to-drain/ack gate remains required before promotion.

Inside a pneu project, each launcher exports `RT_FROM` automatically when
exactly one configured instance uses that harness. A multi-instance
configuration must select its identity explicitly, for example
`RT_FROM=claude-review rt-claude`. This identity path is configuration-based
and does not require a cmux surface, so it works in ordinary terminal apps.
The cmux topology commands remain optional integration tools; full tmux support
is not claimed until its end-to-end gate passes.

### Ctrl-C and terminal prompts

The launcher replaces itself with the selected harness executable. When you
run `pneu`, `rt-claude`, `rt-codex`, `rt-hermes`, `rt-openclaw`, or `rt-grok`
from an existing interactive shell, that shell remains the parent and returns
to its prompt when the harness exits, including after Ctrl-C. The launcher does
not create or manage tmux windows or panes.

If a tmux window was created with the launcher itself as the window command,
there is no parent shell for the window to return to; the window can therefore
close or show its exit status after Ctrl-C. Start the launcher from a shell in
that window, or make the window command run a shell that invokes the launcher.

## Claude lifecycle and unattended drain

For an anchored bare Claude launch, pneu supplies a fresh
`--session-id <UUID>`. This makes the P0 default a new, addressable chat even
when the user's native startup preference is Remote Control/FleetView. An
unanchored launch or any explicit native arguments keep Claude's native
behavior unchanged.

The owned SessionStart hook matches `startup|resume|clear|compact` and starts
the first `asyncRewake` inbox watcher. After a normal completed turn, the owned
Stop hook normally starts its successor. A pending non-ack filename generation
receives one initial wake and at most one Stop-hook retry. If that exact
generation remains undrained, automatic re-wake pauses to prevent a model-turn
loop; a changed generation receives a fresh bounded attempt set. Durable mail
remains in `new/` throughout.

Setup owns only these absolute command rules:

```text
Bash(<prefix>/bin/rt-inbox --fenced --archive-quiet-acks -f json)
Bash(<prefix>/bin/rt-ack --fenced *)
Bash(<prefix>/bin/rt-say --fenced --no-nudge *)
```

Every `--fenced` action revalidates the canonical project, agent, session ID,
lease revision, and current active lease. The send rule requires `--no-nudge`,
so it cannot select the archived keyboard route. The fenced inbox operation
also archives validated quiet `sync-ack` files for that seat before listing
normal mail. A mail file in `new/` that cannot be parsed or validated is
listed explicitly as a `malformed` record with its raw file id instead of
being hidden while it keeps triggering the watcher.

Setup fails before writing when the same `~/.claude/settings.json` contains a
matching `permissions.ask`/`permissions.deny` rule or
`disableAllHooks: true`. It does not rewrite those user choices. Organization
policy, project/local settings, or command-line policy can still override the
user file, so live acceptance remains the final proof.

Claude does not run Stop after a user interrupt, and API failures use
StopFailure, whose output and exit status cannot perform this asynchronous
re-wake. After either event the watcher can remain unarmed until a later normal
interaction, resume, or restart. Mail is still durable and diagnostics expose
the unhealthy adapter; fully autonomous recovery from those failures is a
post-P0 improvement.

## Wake latency and zero-turn sessions

Watcher arming belongs to the harness-native lifecycle hooks. A model turn
must never start its own watcher, and specifically must never run
`rt-wait-inbox ... &`: shell backgrounding trips the harness's
background-operation approval prompt and freezes an unattended seat behind a
dialog no one is watching. An unarmed seat is recovered through a normal
interaction or a relaunch.

A freshly launched seat with zero interactions may hold mail until its first
turn. SessionStart hook output alone does not start a model turn on current
harnesses, so the session has not yet entered the armed wake loop; the mail
stays durable in `new/`. The workaround is to give an unattended seat one
initial interaction after launch.

Wake is edge-triggered and best-effort; durability, not latency, is the
guarantee. A busy seat finishes its current turn before draining — a
roughly five-minute Codex drain was observed in the field — while backlog
accumulates in `new/` and one drain handles all of it. The bounded
initial-wake, single Stop-hook retry, and pause semantics above govern
repeated wakes for one pending generation. Senders must not re-send merely
because a reply is slow.

## Readiness contract

Codex wake is ready only when all of the following are true:

- the selected CLI is at or above the `0.144.6` floor and its identity-proven
  daemon passed the live read-only protocol probe run at this readiness check;
- the daemon reports `running`;
- the requested and reported Unix sockets match;
- the daemon response has the validated required fields and does not claim a
  foreign Codex-managed lifecycle backend;
- the owned/current app-server plist matches the selected executable and the
  live pneu LaunchAgent reports that same program, arguments, and
  critical environment;
- Darwin's kernel-reported Unix-socket peer PID is that LaunchAgent process or
  one of its same-user descendants, and the LaunchAgent PID is stable across
  the check;
- the daemon's CLI and app-server versions both equal the selected CLI version;
- when the selected executable actually is Codex's standalone management slot,
  its reported managed version also equals the selected version;
- no authenticated, digest-bound setup marker says the current app-server
  plist is still awaiting activation or reload;
- the wake heartbeat reports the fingerprint of the currently installed bridge
  and its local dependencies;
- the WebSocket-over-Unix-socket `initialize` / `initialized` handshake works.

`managedCodexPath` and `managedCodexVersion` are schema-checked metadata, not a
general process-identity proof: the gate-exercised 0.144.6 implementation was
source-verified to always report the fixed
`$CODEX_HOME/packages/standalone/current/codex` management slot, even when the
responsive app-server was launched from npm by pneu. Every release must
satisfy the same runtime schema and launchd/socket-peer identity checks or it
fails closed. Handshake liveness alone is not readiness. A daemon left running
after a CLI upgrade fails closed until it is reloaded and revalidated.

Codex release acceptance is a version floor plus a live protocol handshake, not
a per-version allowlist. pneu ships far more slowly than the harness, so
enumerating and validating every release number is impractical; the support
contract is instead the `0.144.6` floor plus a live read-only protocol probe of
the running daemon at each readiness check. Releases below the floor are always
rejected because no gate ever exercised that protocol surface. Any release at
or above the floor is accepted only when its identity-proven daemon passes a
probe of the bridge's read-only surface (`initialize`/`initialized`,
`thread/loaded/list`, `hooks/list`); a probe failure fails closed with the probe
detail. The probe cannot exercise `turn/start` wake semantics, so a passing
probe is protocol evidence for launch, not a full end-to-end support claim; a
release still earns a support claim only after its complete wake path passes
the release gate. Setup applies only the floor-plus-parse check and never
probes, because it neither starts nor contacts a daemon; the probe runs later,
in the launch preflight and the wake bridge, once a daemon has answered. The
app-server remains an experimental integration surface, so live protocol
evidence, not a version number, is what admits each release.

## Codex service preflight

The Codex launcher checks the service pair before publishing a project-seat
lease. Its state machine is deliberately narrower than a generic repair tool:

| State | Launcher behavior |
| --- | --- |
| `ready` | Continue silently |
| `cold` | Under a host repair lock, re-check and start only a clear liveness failure |
| `bridge_down` | Revalidate the app-server, then restart only the wake bridge |
| `reload_required_idle` | Explain possible disconnection and ask before coordinated reload |
| `reload_deferred_busy` | Refuse because the caller or another active, unhealthy-live, or ambiguous Codex lease may be disrupted |
| `setup_required` | Stop and direct the user to managed setup |
| `unsupported` | Stop because the selected Codex release is below the floor or its identity-proven daemon failed the live protocol probe |
| `unsafe` | Stop on foreign plist/socket ownership, permissions, malformed runtime state, or non-liveness protocol failure |

Runtime directories whose recorded project root was migrated through a
compatibility symlink remain a manual, fail-closed cleanup case. The preflight
names the exact stale directory, the old and canonical roots, and the action
that clears the refusal; `rt-doctor` reports the same residue without deleting
it, including tombstoned registry rows whose old path still exists.

Every launch takes one host-wide repair lock plus the install setup-state lock
and re-runs the inspection inside them. The final `ready` observation and
project-seat claim happen before those locks are released, so a concurrent
setup or reload cannot slip between them. A version or owned-plist mismatch is
never interpreted as permission to silently restart the shared app-server.
pneu Codex therefore requires a project anchor in P0;
unanchored users can run native `codex` without pneu messaging. True
zero-downtime upgrades would require versioned blue/green sockets and are P1
rather than a P0 claim.

## Codex automatic binding

The owned Codex SessionStart hook matches `startup`, `resume`, and `clear`, but
not `compact`. It only writes a private atomic request and exits; it never calls
the app-server recursively during thread startup. The wake bridge consumes that
request later and accepts it only when all of these identities agree:

- the hook's native session/thread ID exists in the app-server;
- the app-server reports the exact canonical project cwd;
- the thread is an interactive root thread, not a child or ephemeral thread;
- the request's project, agent ID, pneu session ID, and lease revision
  match the current fenced host lease;
- the project has not acquired a conflicting current binding.

Codex queues SessionStart during session construction but dispatches it from
the first turn. The hook's arrival time therefore does not expire an unclaimed
intent. Instead, the Codex-generated UUIDv7 thread ID must place thread
creation inside the bounded launch window, while the exact lease revision must
still be current and its owner PID plus process-start fingerprint live. This
lets a delayed first interaction bind without turning the whole owner lifetime
into an unrestricted same-cwd first-claim window.

Exact replays are idempotent. A trusted `clear` event may move the same current
lease to its replacement native thread; a request from an older lease cannot
replace a newer claim. If `clear` replaces a request while the bridge is
draining it, the bridge quarantines the superseded binding and processes the
replacement request before it can wake pending mail. User-level Codex hooks may
require a one-time `/hooks` trust review, and pneu does not bypass that
decision. The manual
`rt-codex-wake bind /absolute/project/path` command remains a diagnostic
fallback.

`rt-codex-wake handoff` prints `rt-codex resume <thread-id>`. The positional
`resume` subcommand is accepted by both the pinned `0.144.6` floor and the
live-checked `0.147.0` CLI; the removed legacy `--resume` flag is never emitted.

In registry-backed mode the bridge re-reads the current registry before it
rejects an otherwise unknown bind request. It also refreshes the watched
project set every five-second bridge heartbeat interval even when the registry
stat tuple appears unchanged, so newly active projects are added and
tombstoned projects are removed without a bridge restart.

The first fresh `startup` request whose UUIDv7 creation time falls inside the
current lease's launch window wins, even when Codex defers that hook until a
much later first turn. A first `resume` may name a historical thread, so it is
accepted under the exact current live lease fence; a resumed UUIDv7 created
after the launch window is still rejected. Once claimed, an interactive Codex
started later from one of that thread's tool shells cannot replace it merely by
sharing the project cwd. The creation window narrows fresh-start candidates but
is not a cryptographic client identity; P0 still relies on the
one-interactive-Codex-seat-per-project cooperative boundary because Codex does
not expose the app-server client identity needed to prove which same-cwd thread
belongs to the launcher. A `clear` event is allowed to replace the current
native thread for the same lease. P0 treats an operator deliberately running
`/clear` in another same-cwd remote client as the same cooperative boundary,
not as a supported multi-client routing topology; stronger per-client lifecycle
identity is deferred to P1.

This path has focused automated coverage and the installed RC5 proved on the
development host that Codex's SessionStart `session_id` is the ID returned by
`thread/read`, while the private runtime launch intent resolves to the same
current fenced lease. It is still not a public support claim: a clean-account
repeat and the complete credentialed send-to-wake-to-drain/ack gate remain.

## Validation matrix

The `0.144.6` rows record the exact pairings a live gate has exercised. The
floor rows are the acceptance policy, not exercised pairings: acceptance is
decided at runtime by the live protocol probe, so any release at or above the
floor is admitted when its daemon passes that probe and rejected when it fails.

| Codex distribution | CLI | App-server | Result |
| --- | ---: | ---: | --- |
| npm | `0.144.6` | isolated `0.144.6` | `initialize`, thread read/list, hooks list, and turn-history protocol smoke passed |
| npm | `0.144.6` | pneu launchd `0.144.6` | RC5 live cutover, cold start, launchd-to-socket-peer ownership, SessionStart thread/lease identity, auto-bind, and isolated upgrade passed; credentialed wake E2E remains pending |
| standalone | not installed | not installed | resolver and fixtures only; support is not yet claimed |
| below the `0.144.6` floor | any | any | rejected always, before any probe |
| at/above the floor (e.g. standalone `0.145.0`) | any | any | policy row, not an exercised pairing: launchable when its identity-proven daemon passes the live read-only protocol probe, rejected on probe failure; a passing probe permits launch but is not yet an end-to-end support claim until a live gate runs |

Before the Build Week release, npm `0.144.6` still needs a clean-account repeat
plus the real send-to-wake-to-drain/ack gate.
Standalone support requires an official standalone installation followed by
the same gate; an app-bundled internal Codex binary does not qualify as the
standalone distribution.

## Terminal acceptance matrix

The core smoke runs without a terminal adapter and proves durable send, inbox,
acknowledgement, and drain. It does not prove interactive wake UX.

| Host | Core transport | Real Claude/Hermes/Codex wake |
| --- | --- | --- |
| Terminal.app | Same maildir core | Pending promotion gate |
| iTerm2 | Same maildir core | Pending promotion gate |
| Ghostty | Same maildir core | Pending promotion gate |
| cmux | Same maildir core; optional topology features | Pending baseline and separate optional-adapter gate |
| tmux | Delivery/ack loop, cross-worktree addressing, watcher wake mechanics (attached and detached), app-server socket and launchd reachability all validated under tmux in isolated labs on 2026-08-02; static audit found no terminal-sensitive code on the delivery or wake paths | Promotion gate: one full credentialed seat launched inside tmux; docs must carry the env-propagation caveat (a running tmux server does not see a client shell's later exports — use `tmux new-window -e VAR=...` or `set-environment` plus a fresh pane) |
| Cross-host SSH | No P0 transport | Unsupported |

Every supported P0 participant currently shares one host filesystem and one
host-local runtime root. This is independent of terminal emulator: Terminal,
iTerm2, Ghostty, and cmux can all reach the same maildir and harness adapters.
It is not cross-host transport. Two coding sessions on different Macs do not
share a pneu merely because both were launched over SSH; a future
cross-host transport must preserve durable delivery and identity fencing across
machines.

## Legacy delivery boundary

The current source tree replaces the earlier cmux keyboard-nudge delivery path.
cmux surface IDs and project-local `.armed-*` markers are not delivery or
liveness facts in Messaging v2. Existing maildir state remains durable, while
session leases and heartbeats live under the host-local
`~/.pneu/.runtime/` tree.

## Official surface notes

OpenAI documents the standalone installer cache under
`$CODEX_HOME/packages/standalone`, with the visible command normally installed
under `~/.local/bin` on macOS and Linux. The public CLI supports `--remote` with
Unix-socket endpoints. OpenAI currently labels `codex app-server` experimental,
so pneu intentionally keeps an exact compatibility matrix instead of
assuming semver compatibility.

For the pinned 0.144.6 implementation, Codex constructs
[`managedCodexPath` from the fixed standalone slot](https://github.com/openai/codex/blob/5d1fbf26c43abc65a203928b2e31561cb039e06d/codex-rs/app-server-daemon/src/managed_install.rs#L19-L64)
and [probes `managedCodexVersion` from that path](https://github.com/openai/codex/blob/5d1fbf26c43abc65a203928b2e31561cb039e06d/codex-rs/app-server-daemon/src/lib.rs#L443-L454)
independently of the socket-serving process. pneu therefore treats those
fields as management-slot metadata and authenticates its externally managed
server through launchd plus Darwin's Unix-socket peer PID.
