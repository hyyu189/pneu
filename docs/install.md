# Installation and ownership

pneu installs into a versioned private Python environment and exposes
stable user-level commands. The installer owns only paths recorded in its
manifest and stops before overwriting an unrelated or locally modified path.

## Current status

The source-install path and extracted offline artifact pass automated
clean-home installation, repeated-install, conflict, command, harness-setup,
core-smoke, and uninstall tests. RC5's development-host Codex SessionStart
thread/lease identity and automatic binding spike also passed; an installed
RC7 Hermes TUI passed two sequential real wake generations on that host. The
installed RC8 artifact passed two sequential zero-touch Claude wake generations
there, including Stop re-arm and quiet-ack cleanup. The artifact remains a
release candidate until clean-account tests, the remaining credentialed
harness paths, and the terminal UX matrix pass the promotion gates.

To preview the managed installer without touching a live installation, use
isolated paths:

```bash
mamba run -n general ./scripts/install.sh \
  --prefix /tmp/pneu-preview \
  --link-dir /tmp/pneu-preview-bin
```

## Layout

The default install creates:

- `~/.pneu/versions/<version>`: the private virtual environment;
- `~/.pneu/current`: the active version symlink;
- `~/.pneu/bin`: stable command wrappers;
- `~/.pneu/install-manifest.json`: owned paths and digests;
- `~/.pneu/skills/shared/pneu`: the canonical installed skill link;
- `~/.local/bin/rt-*`: user-visible links to the stable wrappers.

Project registries, persistent UUID layout locks, registry-selected local or
central mailboxes, `.roundtable/mail` bookmarks, migration recovery records,
archival backups, and runtime state are data, not versioned program files.
Central mail lives at
`<registry-parent>/mail/<project-uuid>/`; durable recovery records live at
`<registry-parent>/migration-records/<project-uuid>/`, and verified archival
bundles default to
`<registry-parent>/backups/roundtable-central-mail/<project-uuid>/`.
`RT_MAIL_BACKUP_DIR=/absolute/path` changes the archive default, while an
explicit `--backup-dir` takes precedence. Archives contain plaintext mailbox
history and are retained until the operator removes them; active repair and
rollback use the separate recovery record rather than requiring archive bytes.
The first migrate or rollback operation against an unreleased pre-v1 `0.2`
central marker validates its legacy archive, imports a durable recovery record
under the exclusive layout lock, and atomically rebinds the marker. That
one-time import fails closed if the legacy archive has already been removed.

Run `pneu projects migrate ROOT` for the explicit local-to-central
cutover. The command emits one JSON record containing its durable recovery
record and the file/byte totals, preflight counts, projected hold, layout-lock
wait, exclusive-hold duration, admitted registry-wait cap, copy/fsync
durations, registry-flip duration, commitment state, and recovery warnings.
The reported manifest is the durable recovery record. No install or uninstall
command migrates mail implicitly.

Before taking the exclusive lock, migration counts entries and logical bytes
under a shared lock. It refuses a conservative projected hold above five
seconds, half the normal consumer acquisition timeout. Stop every project
seat and mailbox command before retrying with `--confirm-quiesced`; retry any
send previously rejected by a layout-lock timeout. On the normal unconfirmed
path, exclusive layout admission is capped at five seconds and an embedded
registry-lock wait is capped to the time remaining in a second five-second
hold budget. The projection is an admission heuristic and filesystem calls or
fsync are not asynchronously interrupted.

Use `pneu projects rollback ROOT --manifest PATH` only with the exact
recovery record reported by the active forward migration. Rollback first
creates and verifies a new archival backup and recovery record for current
central mail, including post-cutover deliveries, then changes the registry
pointer back to local. A pre-cutover failure leaves the source layout
authoritative and is retryable; a reported post-cutover failure is repaired
by rerunning the same command. An explicitly unknown registry-commit outcome
fails closed and requires inspecting the registry pointer before retrying.

Harness onboarding is a second ownership layer. After
`roundtable-setup apply`, it also records:

- `~/.pneu/harness-setup.json`: exact config fragments, links, and plist
  files owned by onboarding;
- `~/.pneu/backups/harness-setup/`: private backups of existing config
  files before a managed merge;
- an owned Codex SessionStart fragment in `~/.codex/hooks.json`, when Codex is
  selected;
- `~/.claude/skills/pneu`, `~/.hermes/skills/pneu`, and/or
  `~/.codex/skills/pneu`: selected harnesses' global discovery links to
  the one canonical installed skill.

Those global links mean a user does not download or copy the skill separately
for each new pneu project.

Stable wrappers export one absolute host-local runtime root. Set
`RT_RUNTIME_DIR` to override the default `<prefix>/.runtime`;
`RT_CODEX_RUNTIME_DIR` remains a compatibility alias. If both are present they
must name the same path, otherwise the command fails before launching a
harness. The installer and Codex LaunchAgents create this root with user-only
permissions. Keep a chosen override stable across setup and launch; changing it
requires an ownership-safe setup upgrade before Codex starts.

A single static custom `CODEX_HOME` is also supported when it is absolute,
owned, and below the selected user home. Codex setup puts the hook and global
skill link there and uses the matching app-server socket. Per-launch switching
between multiple auth homes remains outside the P0 lifecycle contract.

## New-user artifact journey

The host must already have CPython 3.11 through 3.14. The archive bundles the
pneu wheel and every Python package dependency, but not the interpreter;
stock macOS alone does not guarantee this prerequisite. The installer first
honors an already-activated environment (`VIRTUAL_ENV`, then `CONDA_PREFIX`)
before scanning `python3.14`, `python3.13`, `python3.12`, `python3.11`, and then
`python3` on PATH; this keeps a higher-numbered interpreter on PATH from
shadowing the environment the operator actually activated. If none is the
intended supported interpreter, set
`ROUNDTABLE_BOOTSTRAP_PYTHON=/absolute/path/to/python3`.

Then extract the release archive and run its installer. No source checkout,
build, or network dependency download is part of this path:

```bash
tar -xzf pneu-<version>-macos.tar.gz
cd pneu-<version>
./install
export PATH="$HOME/.local/bin:$PATH"  # once per shell; persist it in your shell profile
pneu
```

`pneu` is the ordinary product entry. It selects or creates a project
folder, lists only seats whose harness executable is available (and marks
configured-but-missing harnesses unavailable), previews missing one-time
integration for the chosen harness, and asks before applying any owned
configuration. Git is optional and a project may be a non-code folder.

New project scaffolds are path-portable: the committed
`.roundtable/agents.yaml` records `project: "."`, while runtime inboxes remain
ignored. Harness orientation files likewise use the project-relative cwd, and
the portable `.claude/skills -> ../skills` bridge is included in the optional
initial Git commit. An existing user-managed Claude skills directory is
preserved and never hidden by a generated ignore rule. Existing absolute-path
pneu configs remain readable and are not rewritten behind the user's
back.

The equivalent standalone controls are intentionally explicit:

```bash
pneu setup          # read-only preview
pneu setup apply    # expert/scriptable apply
pneu setup status
```

Running `pneu setup` without `apply` never writes configuration, creates
runtime state, or invokes `launchctl`.

## Source install

Source installation requires CPython 3.11 through 3.14 with PyYAML, setuptools
77 or newer, and wheel available to the bootstrap interpreter. On the
development machine:

```bash
mamba run -n general ./scripts/install.sh
```

The source fallback builds a local project wheel without network access and
creates its private environment with access to the bootstrap interpreter's
PyYAML. Because this mode runs the wheel build backend, interpreter discovery
additionally requires `setuptools.build_meta` in the candidate: a supported
interpreter that cannot build (for example a Homebrew `python3.14` without
setuptools) is skipped with a one-line note and discovery continues to the next
candidate. An explicit `ROUNDTABLE_BOOTSTRAP_PYTHON` that cannot build fails
closed instead of falling back. A release `--wheel-dir` install performs no
build and therefore does not apply this check. Installation verifies that the
command scripts and their managed runtime helpers are both present and records
their digests, so a same-version reinstall cannot silently reuse a missing or
locally modified lease helper. This mode is for development and verification.

Verify the installed maildir core in an isolated HOME and PATH:

```bash
roundtable-smoke
```

The command exercises the common terminal baseline—send, inbox, quiet
acknowledgement, and drain—without touching the real registry, projects, or
daemon. Its isolated test environment loads no optional terminal adapter.

This core smoke deliberately does not use credentials, launch a real harness,
load a macOS service, or bind a Codex thread.

## Host onboarding details

The normal `pneu` flow invokes the same planner for only the harness the
user selected. It displays the plan and requests confirmation before applying
it. To inspect all detected harnesses without launching one, run setup with no
subcommand:

```bash
roundtable-setup
```

That is the same as `roundtable-setup plan`: a read-only preflight that reports
which detected harnesses it would configure. It does not create runtime state,
write configuration, or invoke `launchctl`. `apply` and `status` also never
load or unload a service. Harnesses can be selected explicitly and repeatedly:

```bash
pneu setup \
  --harness claude \
  --harness hermes \
  --harness codex
```

After reviewing the plan:

```bash
pneu setup apply \
  --harness claude \
  --harness hermes \
  --harness codex
pneu setup status
```

`apply` completes every collision and ownership check before its first
mutation, preserves pre-existing configuration, records private backups, and
is idempotent. It performs these harness-specific actions:

| Harness | Managed onboarding |
| --- | --- |
| Claude | Merges owned asynchronous SessionStart and Stop inbox watchers plus three absolute, lease-fenced mail-command allow rules into `~/.claude/settings.json`; links the global pneu skill |
| Hermes | Adds one marked `pneu` plugin entry to `~/.hermes/config.yaml`; links the packaged plugin and global skill |
| Codex | Merges one SessionStart auto-bind hook into `~/.codex/hooks.json`; writes the app-server and wake-bridge plist files under `~/Library/LaunchAgents`; links the global skill |

Setup never installs Claude, Hermes, or Codex itself and never copies
credentials. It configures only harnesses already detected, unless
`--harness` is supplied explicitly.

Claude setup fails before writing when the same settings file has
`disableAllHooks: true` or an `ask`/`deny` rule that overrides pneu's
three narrow mail commands. Review those choices with `/hooks` or
`/permissions`; setup never deletes them. Organization-managed, project-local,
or command-line policy can still take precedence outside that file, so a
successful plan/apply is not a substitute for the real wake acceptance test.

Codex may require a one-time `/hooks` review before it trusts the installed
user-level SessionStart hook. That user decision cannot be automated and
pneu never bypasses it.

Setup writes service definitions but still never calls `launchctl`. The normal
Codex launcher performs a targeted service preflight afterward. When setup
writes a new app-server plist, it also writes a private, digest-bound pending
reload marker under `<prefix>/.runtime`; this prevents a still-responsive
same-version daemon from being mistaken for the newly configured service:

- `ready`: launch silently;
- `cold`: start an unambiguously absent or stopped app-server;
- `bridge_down`: restart only the wake bridge after validating the app-server;
- `reload_required_idle`: explain the drift and ask before a coordinated
  service-pair reload;
- `reload_deferred_busy`: refuse the reload because a Codex caller, active
  lease, unhealthy live lease, or ambiguous lease may be disrupted;
- `setup_required`, `unsupported`, or `unsafe`: fail closed without launching.

The preflight serializes repairs with a host lock, serializes its
marker/plist/manifest snapshot with the setup lock, and re-checks state after
acquiring both. A marked cold service is activated from the exact new plist and
the marker is then cleared; a responsive shared daemon still requires the
normal coordinated-reload decision. The low-level `rt-codex-daemon` and
`rt-codex-wake` commands remain recovery tools for expert diagnosis; they are
not steps in the normal onboarding journey.

## Project onboarding

The supported project-first entry for an interactive user is:

```bash
pneu
```

Outside an anchored project, its menu offers registered pneu projects,
safe setup of the current or another existing folder, and creation of a new
folder. It then lists every configured Claude, Codex, and Hermes seat and
launches the selection with a fenced identity. It never offers the user's home
directory or the filesystem root as a project.

The scriptable project commands remain available:

```bash
pneu init --here
pneu init my-project
pneu init my-git-project --git

# Equivalent low-level spelling:
roundtable-init --here
roundtable-init my-project
roundtable-init my-git-project --git
```

Initialization creates missing pneu files and appends clearly marked
blocks to supported existing orientation files. Repeating it is safe. No Git
repository is created by default; `--git` initializes and makes an initial
commit only when the target is not already inside a Git worktree. Existing
repositories and user-owned documents are preserved.

Once registered, launch from the project or run bare `pneu` elsewhere.
Its first menu groups all registered roots behind one `Choose an existing
project` option, then shows their paths in a second-level menu. The explicit
harness commands remain available for direct/scriptable launches:

```bash
pneu claude
pneu hermes
pneu codex

# Low-level aliases:
rt-claude
rt-hermes
rt-codex
```

With no native arguments, the Hermes launcher defaults to `hermes --tui`.
Explicit native arguments are passed through unchanged so scripted/headless
Hermes modes remain available. Before an anchored Hermes seat is claimed, the
launcher checks for either the active profile's `auth.json` or the shared Nous
OAuth store (normally `~/.hermes/shared/nous_auth.json`). If both are missing,
run native `hermes` once outside pneu to complete browser login and then
relaunch the seat. This is intentionally a presence check, not a freshness
check; `RT_HERMES_SKIP_AUTH_CHECK=1` is the explicit emergency bypass.

A project-anchored bare Claude launch supplies a fresh native `--session-id`,
so pneu opens an addressable chat even when Claude is configured to start
in Remote Control/FleetView. Explicit Claude arguments and unanchored launches
are passed through unchanged.

pneu Codex requires an initialized/registered project anchor. The anchor
is what lets the launcher claim a fenced seat under the host service lock and
lets SessionStart bind the correct native thread. The unanchored launcher
choice remains available for Claude and Hermes; use native `codex` directly
when no pneu project or messaging is wanted.

Claude's installed hooks and the Hermes plugin handle their native inbox wake
lifecycle. A fresh Codex thread binds when Codex dispatches SessionStart on its
first turn, even if that interaction is delayed. The trusted hook writes an
atomic request containing the native session ID, cwd, and fence resolved from
the launcher's private runtime intent, then returns without making an
app-server RPC. Intent resolution validates the UUIDv7 launch window, current
lease revision, and live owner identity; the wake bridge later validates the
exact thread ID, exact cwd, interactive source, and root-thread status before
committing the binding.

If diagnostics show that auto-bind was blocked or the hook has not yet been
trusted, manual binding remains available as a fallback:

```bash
rt-codex-wake bind /absolute/path/to/project
```

Manual bind is not part of the normal user journey. The development machine has
run the live spike proving that Codex's hook `session_id` equals the app-server
thread ID and that the private runtime launch intent resolves to the same
current fenced lease. A clean-account repeat and the real
send-to-wake-to-drain/ack path remain release promotion gates even though the
configuration and queueing paths are automated and tested.

An anchored resume must name its thread explicitly:

```bash
rt-codex resume THREAD_ID
```

Before the launcher claims the seat, it reads that thread from the managed
app-server and requires the persisted cwd to canonicalize to the selected
project. The bind, stale-binding adoption, and handoff paths enforce the same
gate. Symlink aliases are accepted after canonicalization; a moved, missing, or
different worktree is refused with both paths named. To make a deliberate path
change, use the explicit operator action printed by the refusal:

```bash
rt-codex-wake reanchor /absolute/path/to/project --thread-id THREAD_ID
```

The command sends an app-server `thread/resume` with an explicit cwd override
and revalidates the returned thread before telling the operator to relaunch it.
Picker and `--last` resume modes remain available through native `codex`, but
cannot claim a Roundtable seat because their target is unknown at preflight.

## Project phone access

Claude mobile/web worktree spawn is an explicit project trait, not part of
global harness setup. From an initialized, registered Git project with exactly
one Claude seat:

```bash
pneu rc-host enable
pneu rc-host status
pneu rc-host disable
```

Before enablement, open `claude` once in that exact directory and accept its
workspace trust dialog. A missing trust decision is reported before any file
or LaunchAgent mutation. Enablement then adds only owned WorktreeCreate and
WorktreeRemove groups to the project's untracked
`.claude/settings.local.json` and loads one UUID-named per-project LaunchAgent.
It does not modify global hook settings. The native Remote Control server runs
in worktree-spawn mode and uses the pneu seat/project name on the phone session
list.

The create hook routes through `pneu worktree add`: its default tree is
`<repo-parent>/<repo-name>-worktree/<name>`, the new project is registered,
and the hook returns exactly one absolute path. The main checkout never moves
into this container. A custom WorktreeCreate hook means Claude does not process
`.worktreeinclude`; any extra file copy must be a separate explicit step.
Removal uses the normal live-seat and registry fences and leaves a blocked tree
in place with an advisory.

Any native `claude rc` started in an enabled project sees these project-local
hooks. A never-enabled project keeps native behavior. Phone-spawned sessions
are adopted only in exact registered pneu projects and never displace a live
or ambiguous lease.

## Offline release install

A generated release archive includes a `wheels/` directory containing the
pneu wheel and compatible PyYAML wheels. From the unpacked archive:

```bash
./install
```

Release mode uses `--no-index --only-binary` and does not download
dependencies. If an unpacked archive has a top-level `wheels/` directory,
`install.sh` selects it automatically.

See [Release artifact process](release.md) for locked inputs, deterministic
archive generation, checksums, and promotion gates.

## Upgrade gate

Installing a new version atomically advances `~/.pneu/current`; stable
wrappers and owned LaunchAgent definitions use that path. A repeated
`pneu setup apply` may update only plists and hook fragments whose old
digests are proven by the setup manifest; foreign drift still fails closed. A
running Codex app-server does not change executable in place.

On the next `pneu` Codex launch, the service preflight compares the
selected CLI, running app-server, current plist payloads, live LaunchAgent
program and arguments, kernel-reported Unix-socket peer process lineage, wake
bridge heartbeat, and every host-local Codex lease. It offers a coordinated
reload only when that snapshot is idle and asks before disruption. When any
consumer may still be live, it defers the reload and tells the user to close or
resolve those sessions first. `rt-doctor` remains the read-only diagnostic
view.

## Uninstall

Harness configuration must be removed while the managed commands and canonical
skill still exist. If Codex was configured, its two jobs may have been loaded
after setup. Run teardown from Terminal.app, iTerm2, Ghostty, or another normal
shell outside Codex:

```bash
roundtable-setup status
pneu rc-host disable  # repeat from every project where phone access is enabled
roundtable-setup remove --unload-codex
roundtable-uninstall
```

The `--unload-codex` path refuses to run when `CODEX_THREAD_ID` says the caller
is inside Codex. It first verifies setup ownership, asks `launchctl` about only
`com.roundtable.codex-app-server` and `com.roundtable.codex-wake`, bootouts
either one only when loaded, and then deletes its managed plist files. A
Claude/Hermes-only setup uses plain `roundtable-setup remove` and never invokes
`launchctl`.

Claude onboarding removal and package uninstall both refuse while any
per-project rc-host state remains. This keeps the project hooks and running
phone host reversible through the still-installed command instead of orphaning
them.

From an unpacked release, `./uninstall` can replace the last command. The
package uninstaller refuses to proceed while
`~/.pneu/harness-setup.json` exists, which prevents dangling harness
configuration. Setup removal verifies owned fragments for drift, removes only
what setup added, and preserves unrelated user configuration. The package
uninstaller then verifies its own manifest ownership and digests before removal
and preserves:

- `~/.pneu/projects.yaml` and its lock;
- persistent UUID admission/resource locks beside the registry;
- registry-selected central mail under the registry parent;
- durable migration recovery records under the registry parent;
- global runtime state under `~/.pneu/.runtime`;
- every project-local `.roundtable` mailbox, ledger, and exact central-mail
  bookmark;
- verified payload archives, including an operator-selected external archive
  root.

`--purge-runtime` additionally removes the global ephemeral runtime directory.
It does not remove the registry, layout locks, local/central mail, bookmarks,
recovery records, or migration archives. Uninstall never runs a migration or
rollback. Use
`pneu projects rollback ROOT --manifest PATH` explicitly before
uninstall when local placement is desired.
