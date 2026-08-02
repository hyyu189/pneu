# Roundtable Messaging v2

Roundtable coordinates the coding agents already running on your machine —
Claude Code, Codex, Hermes — without a daemon, an account, or a network.

A message is a file in the recipient's project mailbox. Writing that file *is*
delivery, so sending works when nothing else is running, and an agent that is
offline finds its mail when it comes back. Wake-up uses each harness's own
lifecycle hooks rather than injected keystrokes, so it does not depend on which
terminal you use.

Validation status and the promotion gates that remain open are tracked in
[Runtime compatibility and validation](docs/compatibility.md).

## Why it exists

Multi-agent terminal workflows become fragile when delivery depends on pane
focus, keyboard timing, or a particular terminal multiplexer. Roundtable
separates durable delivery from wake-up:

1. a sender atomically writes a message into the recipient's project mailbox;
2. an optional harness adapter wakes an online recipient;
3. the recipient acts, then `rt-ack` acknowledges and archives the message;
4. an offline recipient keeps the message until it returns.

The core has no terminal-emulator dependency. Terminal.app, iTerm2, Ghostty,
and other normal terminal hosts use the same delivery and harness-adapter path.
cmux adds optional workspace/topology features rather than a different class of
transport.

## Current compatibility status

| Surface | Status |
| --- | --- |
| Installer runtime | Requires an existing CPython 3.11–3.14; the installer prefers an already-activated environment (VIRTUAL_ENV, then CONDA_PREFIX) before scanning versioned executables on PATH, while the archive bundles package dependencies but not the interpreter |
| Terminal.app, iTerm2, and Ghostty | One first-class terminal baseline; automated core smoke passes, full harness wake UX matrix remains a release gate |
| Claude Code | Owned global skill, asynchronous SessionStart/Stop watchers, and absolute lease-fenced mail permissions are packaged and configuration-tested; the installed RC8 artifact passed two sequential development-host wake generations without permission prompts, while a clean-account repeat remains a release gate |
| Hermes | Owned global skill and plugin links are packaged and configuration-tested; two sequential RC7 development-host wake generations passed, while RC8 artifact and clean-account repeats remain release gates |
| npm Codex CLI `0.144.6` | Exact-release protocol smoke, live RC5 cutover, cold start, launchd-to-socket-peer identity, SessionStart thread/lease identity, auto-bind, and isolated upgrade pass; full credentialed wake E2E remains a release gate; any release at or above the `0.144.6` floor launches when its identity-proven daemon passes a live protocol probe and fails closed otherwise |
| Codex standalone | Canonical resolver path implemented; standalone `0.145.0` launches when its identity-proven daemon passes the live protocol probe and fails closed on probe failure — permitted to run, still not a validated support claim because no standalone install has completed the live gate |
| cmux | The same baseline plus optional project/workspace topology, diagnostics, and notifications |
| tmux | Delivery, ack, cross-worktree addressing, and the Claude wake watcher validated under tmux in isolated labs (both directions, detached sessions included); a full credentialed seat-in-tmux session remains the promotion gate. tmux windows do not inherit a client shell's later exports, so pass environment overrides with `tmux new-window -e` when scripting seats |
| Cross-host SSH | Not yet supported |

## What "done" means

A release is complete only when it provides:

- an idempotent user-level installer and precise uninstaller;
- a five-minute path from a packaged release to a working install;
- verified support for the current npm Codex and an honestly tested standalone
  path;
- a terminal-emulator-independent end-to-end path across the mainstream
  terminal UX matrix;
- accurate diagnostics, recovery, tests, and public-safety checks.

Same-host tmux support is next. Cross-host transport, Linux service
management, and multi-auth switching are roadmap items.

## Install the release candidate

The judge and new-user path begins with the release artifact, not a source
checkout or rebuild. It requires an existing CPython 3.11 through 3.14; the
archive is offline for package dependencies but does not bundle CPython. The
installer tries `python3.14` through `python3.11` before the generic `python3`,
and explains how to select a supported interpreter if none is on PATH:

```bash
tar -xzf roundtable-messaging-<version>-macos.tar.gz
cd roundtable-messaging-<version>
./install
export PATH="$HOME/.local/bin:$PATH"  # once per shell; persist it in your shell profile
roundtable
```

`roundtable` is the normal entry point. It chooses or creates a project folder
(Git is optional), asks which installed and configured Claude, Codex, or Hermes
seat to launch, and performs any missing one-time harness setup only after
showing the owned changes and receiving confirmation. A Roundtable project may
be any ordinary folder, including a non-code folder. Outside a Roundtable
project, the menu says so explicitly and keeps registered projects behind one
second-level `Choose an existing project` selector.

Roundtable-managed Codex launches require that project anchor. This lets the
launcher publish a fenced host-service lease and auto-bind the native thread
without a reload race. Claude and Hermes may still use the explicit
unanchored option; users who want unanchored Codex can run native `codex`
directly, outside Roundtable messaging.

## Setup and day-to-day use

For normal users, the first `roundtable` launch is the onboarding flow. The
standalone setup commands are review and expert controls:

```bash
roundtable setup          # preview only; never writes configuration
roundtable setup apply    # explicit expert/scriptable apply
roundtable setup status
```

For Codex, setup installs an owned SessionStart hook and two owned macOS
LaunchAgent definitions. On first use, Codex may require one `/hooks` review of
the user-level hook. Roundtable does not bypass that trust decision.

After trust is granted, a new Codex thread normally binds automatically on its
first turn, however long that turn is delayed: the SessionStart hook atomically
queues the native session identity after resolving its UUIDv7 launch window,
current lease revision, and live launcher-owner identity. The wake bridge then
validates the exact project cwd and thread shape before recording the binding.
The callback does not re-enter the app-server while the thread is starting.

A bare Roundtable Codex launch primes that first turn itself: the launcher
appends a fixed, visible no-action activation prompt (the launcher-primed
first turn, verified against standalone Codex 0.146), so the thread normally
binds before the first human message. Explicit native prompts, flags, and
subcommands pass through untouched and skip the primer, as does
`RT_CODEX_NO_PRIMER=1`; those launches bind on their first real turn as
before. `/clear` rebinds on the next real prompt, and a parked seat that
never takes a turn has no wake promise. The activation reply is cosmetic —
binding health is read from the host runtime record, never from the model's
answer. Manual binding remains a troubleshooting fallback, not a normal
onboarding step:

```bash
rt-codex-wake bind /absolute/path/to/project
```

The Codex launcher also performs a targeted service preflight and claims the
project seat inside the same host lock as its final readiness check. A ready
pair is silent; an unambiguous cold daemon or stopped
wake bridge is repaired automatically. If the app-server definition or version
requires a coordinated reload, Roundtable offers it only when no active or
ambiguous Codex lease exists and the caller is not already inside Codex, then
asks before proceeding. Busy, unsupported, foreign, or unsafe states fail
closed with diagnostics. This replaces the old normal-user ritual of manually
running daemon and wake reload commands. The low-level commands remain expert
recovery tools.

The most common day-to-day commands are:

```text
roundtable                         project-first onboarding and launch
roundtable setup                  read-only harness integration preview
roundtable doctor                 diagnose setup, leases, and wake services
rt-say AGENT[@PROJECT] KIND "MESSAGE"
                                  deliver durable local or sibling mail
rt-inbox                          inspect waiting mail
rt-ack ID                         acknowledge and archive a message
roundtable projects migrate ROOT  move one local mailbox to central storage
roundtable projects rollback ROOT --manifest PATH
                                  copy current central mail back to the project
```

The bare `AGENT` syntax and its current-project target selection are unchanged;
its durable wire bytes are not. Every newly emitted envelope, including bare
local sends and quiet acknowledgements, records the sender project's stable
UUID. `AGENT@PROJECT` selects an exact registered sibling worktree name inside
the sender's derived Git group; zero matches, duplicate names, stale
registrations, and unrelated projects fail closed. The agent or instance is
then validated against the target worktree's own `agents.yaml`. `rt-ack`
returns its quiet confirmation to the recorded UUID even after the origin path
or registry name changes and the moved worktree's UUID witness has reconciled
its registry entry.

Comma-batched acknowledgements commit per origin project: each successfully
confirmed origin group is archived before the next group is attempted.
If a later origin group fails, an exact retry treats the earlier groups'
`cur/` refs as already confirmed and does not mint their quiet receipts again.
Receipt publication and archival are not one atomic transaction: an archival
failure after delivery is reported as committed and retrying that current
group can resend its receipt. `rt-inbox -f json` marks UUID-aware mail whose
origin is dead or whose `new/` and `cur/` copies conflict with a `problem` and
`remedy: "manual-move"`.

Migration holds that project's exclusive layout lock through the verified
copy and registry cutover. Its JSON result reports the durable recovery-record
path, preflight/projected-hold data, file/byte counts, `lock_wait_ms`,
`exclusive_hold_ms`, the admitted `registry_wait_cap_ms`, copy/fsync time,
registry-flip time, commitment state, and recovery warnings. Verified archival
backups default to
`<registry-parent>/backups/roundtable-central-mail/<project-uuid>/`; set the
absolute `RT_MAIL_BACKUP_DIR` or pass `--backup-dir` to override it. Recovery
records live separately under `<registry-parent>/migration-records/`, so
losing an archival bundle does not disable post-cutover repair or rollback.
An unreleased pre-v1 `0.2` central marker is imported once, under the
exclusive layout lock, while its legacy archive is still verifiable; the
marker is then rebound to the durable recovery record.

The command refuses a mailbox whose conservative projected exclusive hold
exceeds half of the normal ten-second consumer lock timeout. Stop every seat
and mailbox command for that project, then explicitly retry with
`--confirm-quiesced`; rejected sends must still be retried by their caller.
Without that confirmation, exclusive layout admission is capped at five
seconds and registry-lock wait is capped again to the time remaining in a
five-second hold budget. The projection remains an admission heuristic rather
than an interruptible filesystem deadline. Rollback accepts only the exact
active forward recovery record and snapshots current central mail first, so
messages delivered after migration are retained.

All participants in one Roundtable currently run on the same host. The durable
mailbox core does not require cmux and uses the same path in Terminal.app,
iTerm2, Ghostty, or another normal terminal; cmux supplies optional topology and
workspace affordances only. tmux lifecycle integration and cross-host SSH
transport are not P0 features.

The development machine has proved that the trusted hook's `session_id` is the
same native thread ID read through the app-server and that the launcher's
private runtime intent resolves to the same current fenced lease. The remaining
Codex promotion gate is a clean-account repeat plus credentialed
send-to-wake-to-drain/ack E2E; automatic binding therefore remains
release-candidate behavior rather than a public support claim.

## Development install

The current source tree can be installed into a versioned private environment.
This is for development and verification; the public judge path above uses the
artifact:

```bash
mamba run -n general ./scripts/install.sh
roundtable
```

`roundtable setup` remains a read-only preview. `roundtable setup apply`
configures only detected harnesses, or an explicit selection such as
`--harness claude --harness hermes --harness codex`. It merges owned hook or
plugin fragments, records backups and ownership, and links the installed
Roundtable skill into each selected harness's global skill directory. A normal
user does not copy or pull the skill into every project.

The menu can adopt the current non-Git directory without replacing user files,
select a registered project from a compact second-level list, choose another
existing folder, or create a new one. Git is always opt-in. A Hermes seat
launched without native arguments uses TUI mode by default. Scriptable users
can use `roundtable init`,
`roundtable claude`, `roundtable hermes`, or `roundtable codex`; the underlying
`roundtable-init` and `rt-*` commands remain available.

Stable commands are linked under `~/.local/bin`. Installation fails closed
when an existing path is not owned by its managed manifest. Remove harness
configuration before removing the package. If Codex was configured, run the
teardown from a normal terminal outside Codex so Roundtable can inspect and
unload only its two owned jobs:

```bash
roundtable-smoke
roundtable-setup status
roundtable-setup remove --unload-codex
roundtable-uninstall
```

Claude/Hermes-only onboarding uses plain `roundtable-setup remove`.
Uninstallation preserves the project registry, UUID layout locks, every
registry-selected local or central mailbox, project bookmarks, migration
manifests/backups, and host runtime state. `--purge-runtime` removes only the
ephemeral host runtime. To return a central mailbox to its project, run an
explicit manifest-bound rollback before uninstalling; uninstall never migrates
or rolls back data implicitly.

See [Installation and ownership](docs/install.md) for isolated preview paths,
offline release mode, upgrade gates, and precise removal behavior.

## History

Roundtable has had three phases. Each replaced the previous one's central
mechanism rather than extending it.

**Phase one — cmux-era prototype.** The original system drove agents by
injecting keystrokes into terminal panes, and depended on pane focus, keyboard
timing, and a particular multiplexer. It remains public and unchanged as the
MIT-licensed [`hyyu189/h2o`](https://github.com/hyyu189/h2o) at commit
`50683056c896bdb1ae2f74f6ac0740106b43bd36`.

**Phase two — Messaging v2.** Built during the 2026 OpenAI Build Week
submission period, this replaced keyboard injection entirely: a durable
per-project maildir became the delivery fact source, and wake-up moved to each
harness's own lifecycle hooks. The delivery core stopped depending on any
terminal emulator. That work was submitted as `v0.1.8` and is preserved
unchanged.

**Phase three — productization.** The current phase, in this repository: making
the system dependable for people who did not build it. Central identity,
cross-worktree addressing, and installation that does not require reading the
source.

Ocean directed the product throughout. GPT-5.6 through Codex was the primary
implementation environment for phase two and continues to lead productization.
Fable 5 contributed specified early code, documentation, configuration, design,
and review. Those contributions are recorded commit by commit rather than
described as any single model's work.

- [Development and attribution boundary](PROVENANCE.md)
- [Contributor roles](CREDITS.md)
- [Source commit ledger](docs/provenance/source-commits.tsv)
- [Architecture and adapter boundaries](docs/architecture.md)
- [Release artifact process](docs/release.md)

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE). Applicable material retained from the MIT-licensed h2o
predecessor keeps its full MIT notice there.

<!-- BEGIN Roundtable -->
## Roundtable status

Agent routing is in [`ROUTING.md`](ROUTING.md). Keep the current shared status in this README so every harness sees the same state.
<!-- END Roundtable -->
