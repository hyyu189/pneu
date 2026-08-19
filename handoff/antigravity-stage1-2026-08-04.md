# Antigravity CLI Stage 1 validation

> Status: historical record — Antigravity is parked at T0 (decision.md 2026-08-05). Kept at this path because that ledger entry cites it.

Status: verified lab evaluation only. No Roundtable runtime wiring, adapter,
daemon, or support-table claim was added. This update supersedes the
documentation-only conclusions in `antigravity-harness-research-2026-08-03.md`.

## Scope ruling

The requested target was Antigravity CLI (`agy`) only. Gemini CLI was not
installed or adapted; its documentation remains out of scope except as prior
design reference.

## Result

| Question | Evidence | Finding |
| --- | --- | --- |
| Install path | Official installer fetched the macOS arm64 package, verified its SHA-512, extracted `agy`, and accepted `--dir <dir>`. Native `agy install` then edited shell profiles. | Default target is `~/.local/bin/agy`; a custom target is possible, but the installer/native handoff has side effects that must be treated as managed setup. |
| Version/runtime | Lab binary reported `1.1.10`; SHA-256 was `e0ad51799b5ba74023e100107e7d96c2ea894aa0ad92c3cd2a69e437a8d9a7dc` on macOS arm64. | Real executable validated. |
| Installer touches | With a custom directory, the installer appended a PATH export to `.zshrc`, `.zprofile`, `.bashrc`, `.bash_profile`, `.profile`, and `~/.config/fish/config.fish`. It created `~/.cache/antigravity`; it did not create an Antigravity LaunchAgent. The script's own help documents `--dir`, while native `agy install --help` exposes `--skip-path` and `--skip-aliases`. | Installation is not binary-only; profile edits need exact rollback. No system daemon was observed. |
| Authentication | Auth used the browser/device flow and then silent OS-keyring auth for subsequent headless runs. Account identifiers and OAuth codes are intentionally omitted. | Authentication is a separate prerequisite; do not claim connector readiness from an installed binary. |
| Session identity | `--new-project` created a project registry entry and made the workspace hook discoverable. A headless run returned a UUID in `conversation_id`. `--conversation <id>` and `--continue` both returned the same UUID and reused the conversation. Hook payloads showed `initialNumSteps` growing across those turns. | `conversationId` is the native session identity for this path; explicit resume and continue are conversation-scoped. A project registration step may be required before workspace customizations are active. |
| Hook surfaces | After project registration, the lab hook ran in order `PreInvocation`, `PostInvocation`, `Stop`. Captured JSON included `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`, plus event-specific fields. | The earliest observed hook is `PreInvocation`, immediately before a model call. The documented surface has no startup or resume event; `--conversation`/`--continue` did not add one. |
| Fenced environment | The hook process inherited `RT_PROJECT_ROOT`, `RT_FROM`, `RT_SESSION_ID`, and `RT_LEASE_REVISION` exactly from the launcher. The hook payload canonicalized the macOS workspace path to `/private/var/...` while the injected env used `/var/...`. | Environment inheritance works, but binding must canonicalize paths before comparing them. |
| Headless mode | `agy -p <prompt> --output-format json --mode plan --sandbox --print-timeout 60s` completed successfully with one JSON object containing `conversation_id`, `status`, `response`, duration, turn count, and usage. | A bounded noninteractive probe is real. It is not evidence of a long-lived wake listener. |
| Process topology | Each run started a local language server on a random localhost port and shut it down with the CLI. No persistent launchd/system service was observed. Logs also showed missing `/usr/bin/cs` and a Playwright driver download 404; neither prevented the no-tool headless probe. | Treat the language server as per-process implementation detail, not a supported external daemon or wake RPC. |

## Proposed tier claim

Claim **T0 only** for Antigravity CLI: durable Roundtable delivery remains
available independently, and a user/launcher can run a bounded headless
`agy` turn. Do not claim T1 native wake, automatic startup/resume arming, or
Roundtable adapter support. The observed hook boundary starts at an invocation,
not at process startup or conversation resume, and no documented external wake
protocol was found. Any future T1 work needs a separate process-owned adapter
and a real send-to-wake-to-drain/ack smoke test.

## Exact uninstall record

For a user-level install, in this order:

1. Stop all `agy` processes.
2. In an interactive `agy` session, use the CLI's actual `/logout` command to
   clear credentials/cache; if the account was authenticated through GCP, use
   the corresponding `gcloud auth revoke`. A headless `-p "/logout"` prompt is
   not the logout command and was not treated as credential revocation here.
3. Remove the exact installed binary: `~/.local/bin/agy` for the default
   installer, or the exact custom `--dir` path.
4. Remove only the installer-owned PATH lines from the six profiles listed
   above; do not rewrite the surrounding profile contents.
5. Remove `~/.cache/antigravity` and `~/.gemini/antigravity-cli` only when they
   were absent before this installation and no other Antigravity session uses
   them. Remove any project registry file created solely by `--new-project`.
6. Preserve pre-existing `~/.gemini/config`, its hooks, and unrelated shell or
   launch-agent state.

In this lab, the six PATH lines, temporary permission settings, and the
lab-created project registry entry were reverted. The recursive deletion of
the newly created CLI/session and cache directories was intentionally left
pending explicit destructive-cleanup approval by the local safety policy; the
exact targets are the three paths named in step 5. No live Roundtable files or
runtime configuration were changed.

## Official references

- [Antigravity CLI installation and authentication](https://antigravity.google/docs/cli/install)
- [Antigravity CLI hooks](https://antigravity.google/docs/hooks)
- [Antigravity CLI projects](https://antigravity.google/docs/cli/projects)
- [Antigravity CLI permissions](https://antigravity.google/docs/cli/permissions)
