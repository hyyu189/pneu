# Release artifact process

The release builder produces a deterministic, offline-installable macOS archive
from one clean Git commit. Repeated builds are byte-identical under the same
recorded Python, pip, setuptools, and wheel toolchain. It never tags, pushes,
creates a GitHub Release, or submits to Devpost.

This repository is the new Messaging v2 release line. The public,
MIT-licensed cmux-centric predecessor remains unchanged at
[`hyyu189/h2o`](https://github.com/hyyu189/h2o), commit
`50683056c896bdb1ae2f74f6ac0740106b43bd36`; that repository has no tag or
GitHub Release as of this audit. It is not overwritten or republished as this
artifact.

## Locked inputs

The builder accepts source only from `git archive HEAD`. It refuses a dirty
worktree, builds the Roundtable wheel as `py3-none-any`, and includes the eight
official PyYAML 6.0.3 wheels for:

- CPython 3.11, 3.12, 3.13, and 3.14;
- macOS arm64 and x86_64.

Each dependency filename, official `files.pythonhosted.org` URL, and SHA-256
digest is pinned in `scripts/build_release.py`. Downloads and caller-provided
offline wheels are verified against those digests.

The archive does not bundle a Python interpreter. Installation first honors an
already-activated environment (`VIRTUAL_ENV`, then `CONDA_PREFIX`), then searches
the versioned `python3.14` through `python3.11` commands before generic
`python3`, requires CPython 3.11 through 3.14, and fails before writing anything
if that prerequisite is absent or unsupported. The archive installs from its
bundled wheels and never builds from source, so it accepts a supported
interpreter regardless of whether setuptools is present. This is an explicit
candidate support boundary, not a claim that stock macOS alone is sufficient.

CI uses the exact package versions in `requirements-release.txt`. The builder
records the active Python, pip, setuptools, and wheel versions in
`BUILD-METADATA.json`; it does not claim byte identity across different
toolchains.

## Build

From a clean committed checkout:

```bash
mamba run -n general python scripts/build_release.py
```

To avoid network access, pre-populate a directory with the eight locked wheels:

```bash
mamba run -n general python scripts/build_release.py \
  --dependency-wheel-dir /path/to/locked-wheels
```

The output directory contains:

- `roundtable-messaging-<version>-macos.tar.gz`;
- `SHA256SUMS` for that archive.

The archive has its own `SHA256SUMS` covering every payload file and
`BUILD-METADATA.json` recording the version, exact source commit, source epoch,
project wheel digest, and dependency matrix. It also includes `PROVENANCE.md`,
`CREDITS.md`, the Apache license, `NOTICE` with the applicable predecessor MIT
notice, the compatibility matrix, and the source-commit ledger. Top-level
`install` and `uninstall` launchers run directly from the extracted artifact.

## Verify and smoke

```bash
cd artifacts && shasum -a 256 --check SHA256SUMS
cd ..
tar -xzf artifacts/roundtable-messaging-<version>-macos.tar.gz
cd roundtable-messaging-<version>
shasum -a 256 --check SHA256SUMS
export HOME=/tmp/roundtable-release-home
export CODEX_HOME="$HOME/.codex"
export RT_RUNTIME_DIR="$HOME/.roundtable/.runtime"
export RT_CODEX_RUNTIME_DIR="$RT_RUNTIME_DIR"
prefix="$HOME/.roundtable"
link_dir="$HOME/.local/bin"
mkdir -p "$HOME"
./install --prefix "$prefix" --link-dir "$link_dir"
"$link_dir/roundtable-setup" \
  --home "$HOME" \
  --prefix "$prefix" \
  --harness claude \
  --harness hermes
"$link_dir/roundtable-setup" apply \
  --home "$HOME" \
  --prefix "$prefix" \
  --harness claude \
  --harness hermes
"$link_dir/roundtable-setup" status \
  --home "$HOME" \
  --prefix "$prefix" \
  --harness claude \
  --harness hermes
"$link_dir/roundtable-smoke"
"$link_dir/roundtable-setup" remove \
  --home "$HOME" \
  --prefix "$prefix" \
  --harness claude \
  --harness hermes
./uninstall --prefix "$prefix"
```

The exported `HOME`, `CODEX_HOME`, and runtime variables keep the entire manual
exercise inside the disposable home. The first setup invocation has no
subcommand and is a read-only plan. This manual example omits Codex because
plist generation requires an executable Codex installation; the CI exercise
supplies a harmless fake executable, and the promotion gate uses the real
validated CLI.

The GitHub `release-artifact` workflow runs the full tests and safety gate,
builds the same archive, verifies both checksum layers, installs the extracted
payload into an isolated HOME and prefix, and selects all three harnesses using
harmless fake executables. It proves that:

- the default setup plan creates no manifest, config, runtime directory,
  harness skill link, plugin link, or plist;
- `apply`, `status`, and `remove --unload-codex` succeed from the installed
  artifact;
- removal leaves no owned harness config, links, or plist files;
- plan, apply, and status never invoke `launchctl`; explicit teardown queries a
  fake `launchctl` that reports both owned labels not loaded and never reaches
  its bootout sentinel;
- the installed core smoke and package uninstall still pass afterward.

The workflow uploads a 14-day candidate artifact and deliberately does not
publish a GitHub Release. Configuration automation is not a substitute for
credentialed real-harness E2E.

## Judge journey

The intended five-minute path begins with the archive:

```bash
tar -xzf roundtable-messaging-<version>-macos.tar.gz
cd roundtable-messaging-<version>
./install
export PATH="$HOME/.local/bin:$PATH"  # once per shell; persist it in your shell profile
roundtable
```

`roundtable` is the normal entry: select or create a project folder, select a
harness, review any missing one-time integration, and launch. `roundtable
setup` is always preview-only; `roundtable setup apply` is the explicit expert
path. On first Codex use, the judge may need to review the user hook once with
`/hooks`. Subsequent SessionStart binding is intended to be automatic, with
manual `rt-codex-wake bind` retained only as a fallback.

## Promotion gates

Before tagging or attaching the archive to a public release:

1. all CI matrix jobs and the artifact workflow pass;
2. the extracted archive passes install, terminal-baseline smoke, and
   uninstall on a clean macOS account, including the
   `plan -> apply -> status -> remove --unload-codex` setup cycle;
3. clean-account Claude and Hermes setup passes skill discovery, lifecycle
   hook, tripwire, and real send-to-wake-to-drain/ack acceptance;
4. npm Codex `0.144.6` passes the coordinated default-daemon reload, proves the
   socket peer belongs to the exact Roundtable LaunchAgent process tree,
   verifies that trusted SessionStart `session_id` matches the app-server
   thread and the private runtime launch intent resolves to the same current
   fenced lease, then completes real send-to-wake-to-drain/ack acceptance;
5. standalone Codex passes that same acceptance before support is claimed;
6. the same harness acceptance passes in Terminal.app, iTerm2, and Ghostty;
7. the five-minute judge path creates or adopts a non-Git directory, launches
   a project-anchored harness, and completes one visible message round trip;
8. `README.md`, `docs/compatibility.md`, provenance, and Devpost copy describe
   only the gates that actually passed.

At this release-candidate stage, RC5's npm live host cutover, cold start,
corrected launchd-to-socket-peer identity, SessionStart thread/lease identity,
and automatic binding have passed on the development machine. Clean-account
repetition and full credentialed wake E2E have not yet passed. They must not be
presented in a video, README support table, or Devpost submission as completed
evidence.

On 2026-07-21, the installed RC7/0.1.4 Hermes TUI on the development host
passed two sequential message generations in one freshly started session.
Message `20260721T205151Z-codex-to-hermes-86049` was acknowledged and answered
`RC7_A_OK`; after re-arm,
`20260721T205253Z-codex-to-hermes-87191` was independently acknowledged and
answered `RC7_B_OK`. The live terminal observation establishes the second
wake; the durable records establish both deliveries, replies, acknowledgements,
and final archive state. This is development-host RC7 evidence, not an RC8
artifact, clean-account, or terminal-matrix support claim.

On 2026-07-21, artifact RC8/0.1.5 from commit
`b520cb5e3ead7f41eb5d51bcbbdd86afc1fa2419` (archive SHA-256
`6660a5ff2f8ff87c6f02eed2dc1e672a55a2335ac510f039bb8c24ce0627ebc5`)
passed both the isolated install/setup/status/smoke/remove/uninstall exercise
and a real Claude Code `2.1.217` development-host test. A fresh anchored bare
Claude chat armed before any user prompt. Message
`20260721T214245Z-codex-to-claude-68440` woke it without a permission prompt,
was acknowledged, and received reply `20260721T214304Z-claude-to-codex-69599`
with `RC8_A_OK`. After the Stop hook re-armed the watcher, message
`20260721T214356Z-codex-to-claude-70880` produced a second independent wake,
was acknowledged, and received reply `20260721T214414Z-claude-to-codex-71681`
with `RC8_B_OK`. The second fenced inbox run also reported that it archived one
quiet acknowledgement left by the first cycle. This proves the packaged
development-host path, not a clean-account or terminal-matrix support claim.
