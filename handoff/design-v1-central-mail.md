# v1 — central mail, stable identity, cross-worktree addressing

Status: **converged.** Reviewed by Codex and Hermes; every structural claim
either made was independently verified against the tree before being accepted.
Decided by Ocean unless marked open.

## The need

One agent should be able to work in an isolated git worktree — frontend,
copy, an experiment — while other work proceeds elsewhere, and the two should
still be able to message each other. Today they cannot, and the reason is
structural rather than missing code.

## Why it does not work today

The mailbox's location and the project's identity are the same object, and
both are the current working directory. `find_project_root()` walks up from
`os.getcwd()` for `.roundtable/agents.yaml`; the maildir is a child of
whatever that walk found; the host runtime seat directory is the sha256 of
that same resolved path. A second worktree is a second path, so a second
identity, a second mailbox, and a disjoint namespace. `rt-say <agent> <kind>
<body>` has no project component in its grammar, so there is no slot in the
address for a sibling even if a relation existed.

Two facts narrow the work considerably:

- **Cross-project delivery already works.** Verified:
  `ROUNDTABLE_PROJECT_DIR=<other project> rt-say codex fyi "..."` places the
  message in that project's maildir. The transport needs nothing.
- **`--fenced` refuses it at one line.** `_rtlib.py:48`:
  `if configured_root != canonical: raise`. That check conflates two
  different things, which §4 separates.

## 1. Identity: a UUID, with the path demoted to an attribute

A project is identified by a UUID minted at registration. The filesystem path
becomes a mutable attribute of the registry entry rather than the key.

This also fixes a defect we have today and have not hit yet: renaming or
moving a project silently makes it a different project, because the key is
`sha256(path)`.

## 2. Storage: central, with the in-project symlink as a bookmark

```
~/.roundtable/
  projects.yaml                          registry: uuid, path, group, name, layout
  mail/<project-uuid>/
    <agent>/{new,cur,tmp}                maildir
    messages/                            the ledger, and its locks
```

**The ledger moves with the mail.** `rt-say` and `rt-inbox` both use
`MSG_DIR = STATE_DIR / "messages"` and `rt-inbox` merges those ledger records
with the maildir listing. Moving the maildir without the ledger would log a
cross-project acknowledgement under the wrong root.

**One resolver, no direct path construction.** Every consumer takes the
mailbox path from a single registry-backed resolver: `rt-say`, `rt-inbox`,
`rt-ack`, `rt-wait-inbox`, `rt-stop-gate`, `rt-codex-wake`, `rt-doctor`. A
second place that builds the path by hand is how the two layouts drift apart.

**Host runtime keys are a separate decision, deliberately deferred.**
`_rtruntime.project_hash` is `sha256(path)` and the Codex wake bindings and
project state are keyed by path as well. v1 does *not* migrate those. The
consequence is explicit rather than hidden: renaming a project keeps its mail
and its identity, but requires a clean rebind of its Codex seat. Migrating the
runtime keys as well is a later, separable change.

**The registry needs tombstones.** `load_project_registry` currently skips any
entry whose `.roundtable/agents.yaml` is gone (`if not is_project_root(root):
continue`), so a deleted project disappears from the registry entirely — and
with it, any way for `doctor` to report that its central mail is now orphaned.
A removed project must leave a tombstoned entry carrying at least its UUID.

`agents.yaml` and `runtime.json` stay project-local. `agents.yaml` describes
the project rather than the delivery state; `runtime.json` holds the cmux
workspace binding read by `project_for_current_workspace`
(`_rtlib.py:330`) and is meaningful only where the working tree is.

Routing resolves through the registry to the real central path. A convenience
symlink may exist for humans to `ls`, but **no code path ever traverses it** —
and for that to hold it must not sit at a path existing code already builds.
`<project>/.roundtable/inbox` is exactly such a path: `rt-doctor`'s
`report_legacy_markers` globs `inbox.glob("*/.armed-*")`, and pathlib follows
symlinked directories, so a bookmark placed there would be walked by code that
believes it is reading a real mailbox. The bookmark is therefore
`<project>/.roundtable/mail`, a name nothing constructs. Renaming it is
cheaper and more durable than adding an `is_symlink()` guard to every existing
glob, and it fails safe: old code that builds the `inbox` path simply finds
nothing rather than silently traversing.

That distinction is what keeps the existing symlink guards intact.
`rt-ack:71-75` and `rt-inbox:318-323` refuse a symlinked `INBOX_DIR`, `inbox`,
`new` or `cur`, because both tools *move* a file from `new/` to `cur/` and a
substituted symlink would redirect that move outside the mailbox. Those guards
protect the mutation, not the path. If routing goes through the registry the
guards need no change at all, and the central tree is created and permissioned
by us — stricter than today, where mailbox permissions depend on the user's
umask.

## 3. Groups: derived, never declared

A group answers exactly one question: which projects may address each other
with the short `@name` form.

```
git project      group = hash(git rev-parse --path-format=absolute --git-common-dir)
non-git project  group = its own UUID
```

Verified across six git cases: linked worktrees group with their repo, a
worktree of a bare repo groups with the bare repo, a submodule gets its own
key (correct — a submodule is its own repository), and with
`--separate-git-dir` the key follows the repository object rather than the
worktree location, so it survives a move.

Every project has a group key and the field is never null, so resolution has
one code path and no special case. A non-git project is a group of one, which
falls out rather than being designed.

Groups are derived only. Declared groups would let two unrelated folders
address each other, but they need configuration, validation and conflict
handling, and they can be wrong. Adding them later is additive; removing them
would be breaking. Unrelated projects can already talk today via an explicit
`ROUNDTABLE_PROJECT_DIR`, so this is missing syntax, not missing capability.

## 4. Addressing and the fence

```
rt-say claude            ...    unchanged
rt-say claude@frontend   ...    a sibling worktree in the same group
```

Resolution: sender's group → entry whose name is `frontend` → target UUID.

**The agent name must be validated against the target's `agents.yaml`, not the
sender's.** This is the easiest thing to implement wrongly: `rt-say:572` calls
`configured_target(ROOT, target)` where `ROOT` is the *sender's* project root,
so extending it naively would check whether the sender declares `claude`
rather than whether the target does. Target validation has to happen after the
target root is resolved.
Name resolution must return **exactly one** entry; duplicate names in a group
fail closed rather than picking one. Sender and target are pinned from a
single registry snapshot so the pair cannot change underneath the send, and a
stored derived group is revalidated at use — otherwise a worktree that was
detached or repointed keeps stale authority over its former siblings.

`require_fenced_seat` today does two things in one predicate. They separate:

```
authenticate the sender   unchanged, always against the sender's own project:
                          load_validated_lease + inspect_seat must be active_*
                          and the lease must not have changed during validation

authorize the target      new, independent:
                            same group key
                            target registered
                            target's own agents.yaml declares that agent
```

Only the first is a security property — it is what makes `[FROM→TO]`
trustworthy. The second is a confinement that fell out of the design; it is
being replaced by an explicit, narrower rule rather than removed.

The envelope records the **origin UUID**, not a path and not a mutable name,
so the recipient can see that a message came from a sibling and `rt-ack` can
route the quiet acknowledgement home even after the origin has been renamed or
moved.

## 5. Lifecycle and GC

Mail is task-scoped. A worktree closing means the subtask ended; the durable
outcome is in the merge and in whatever was written down. Losing the thread is
correct scoping, not data loss.

GC is therefore hygiene, not a prerequisite:

- `doctor` reports mailboxes whose project path no longer exists.
- Purging is explicit. Nothing deletes message content automatically.

The rule that matters is narrower than "build a GC", and comes from the
2026-07-24 incident: an orphaned runtime record blocked the coordinated reload
not because orphans existed but because `inspect_host_harness_seats` strictly
resolves every record and fails closed. Mail orphans are inert.

The round-1 draft stated this as "never introduce a code path that enumerates
all mailboxes and fails closed". That was wrong: such paths already exist.
`rt-codex-wake` discovers every registered project and steps each inbox,
`rt-doctor` walks the registry, and `inspect_host_harness_seats` deliberately
walks every private runtime record and fails closed — which is exactly what
blocked the reload on 07-24. The rule has to be about which paths may
enumerate, not about introducing new ones:

> **Send, receive, ack, wake and service preflight resolve one exact UUID
> mailbox and never require a scan of the mail root. `doctor` and GC may
> enumerate, best-effort, with per-mailbox error isolation. Central mail stays
> out of any fail-closed host-service enumeration.**

Supporting evidence that inert orphans are survivable at scale: Claude Code
keeps conversations in `~/.claude/projects/<path-slug>/` — 825 MB here, and 10
of 17 slugs already point at directories that no longer exist. Codex keeps 815
MB in `~/.codex/sessions/`. Both harnesses already centralise conversation
storage outside the project and already accumulate orphans, without incident.
Note the consequence for us: today, deleting a worktree destroys the
agent-to-agent mail while the human-to-agent conversation survives in the
harness's own store. Central mail removes that inconsistency.

## 6. No bridge in v1

A central "flagship bridge" project was in the round-1 draft and is removed.
It contradicted this document's own rules. `~/.roundtable/meta/` is not a git
repository, so by §3 its group is its own UUID, so by §4 it can address
nothing. The only way to make it work is an exception that says the bridge may
cross groups — and §6 of the draft itself argued that the moment something is
true of the bridge and false of an ordinary seat, the topology has stopped
being flat.

If unrelated-project routing is wanted later, the answer is an explicit
long-form address naming the target UUID, available to every seat equally.
Not a privileged project.

## 7. Migration

Existing project-local mailboxes are migrated into the central store. Ocean's
decision; running both layouts permanently was rejected as a standing tax.

The sequence has to survive being interrupted at any point, which means one
authoritative layout at all times and never a merge of two.

1. Ship the UUID-aware resolver first, with every entry recording
   `layout=local`. Nothing moves yet; this step is separately releasable.
2. Every mutator and scanner takes a per-project **shared** layout lock, so a
   migration can exclude them.
3. The migrator takes the **exclusive** lock and quiesces legacy writers.
4. It creates a manifest backup under `~/Documents/Workspace/backups/` and
   **verifies it** before anything is moved.
5. It copies `inbox/` and `messages/` into a temporary central UUID directory,
   fsyncs, and renames that directory into place atomically.
6. It flips one registry `layout` pointer, atomically. **This is the cutover.**
   Before it, local is authoritative; after it, central is. A restart resumes
   from the pointer and never reads both.
7. Only then does it install the human bookmark symlink.

**Rollback after the cutover is not "restore the backup".** Mail that arrived
after the flip lives only in the central store, and restoring the pre-cutover
backup alone would discard it. A rollback takes the same exclusive lock and
copies the current central state back before flipping the pointer.

## 8. Codex seat binding: preallocate the thread

**In scope for v1.** The launcher calls `thread/start` itself, takes the id
from `ThreadStartResponse`, binds it to the lease immediately, and launches the
TUI as `codex --remote unix:// resume <id>`. `thread/start` creates a thread
without a model turn, so the human opens a normal empty session and the token
cost is zero.

The reason is not the convenience. It removes the SessionStart hook from the
binding path, replacing an event we have to wait for with a call whose result
we already hold. That hook has misled this project twice in one week: its
turn-gating was misdiagnosed as non-dispatch, and a deadline measured from the
wrong instant left three seats silently unbindable for days.

Requirements, from review:

- the already-bound path must be idempotent, since the queued `Startup` source
  still drains on the first human turn;
- ownership and configuration must transfer correctly to the resumed TUI;
- a thread created but not bound must be cleaned up rather than leaked;
- an end-to-end smoke test, because this is deeper app-server coupling than
  the launch intent it replaces.

The launch intent is **not** deleted. It becomes the fallback for any seat that
was not preallocated — a hand-started `codex` the user later wants bound, or a
preallocation that failed.

## 9. Non-goals

Stated so the design does not accrete:

- No scheduler and no DAG. Structure lives in the participants, not in a
  planner.
- No coordinator privileges. See §6.
- No daemon. `rt-say` must keep working when nothing is running — this is the
  one property neither competitor has.
- No account, no network, no cloud.
- No GUI dependency. If a GUI is ever built it is a viewer over the on-disk
  format. The test: could someone else write their own against the format,
  without our cooperation?

## Review outcome — converged

Both reviewers answered. They independently reached the same conclusion on the
one structural question, which is the strongest signal in the review: **delete
the bridge.** Codex showed it could address nothing under the document's own
group rule; Hermes called it over-engineering for an overview use case. Two
independent paths to the same verdict on the section that was mine to defend.

Review is closed at one round rather than the three available. Every remaining
point was concrete, verifiable and accepted, so a further round would be
spending the budget rather than using it.

### Codex

Four objections. All four were verified against the tree and all
four were accepted:

- The bridge could not address anything under this document's own group rule.
  §6 deleted rather than given an exception.
- §2 omitted the `messages` ledger and its locks, did not name a single
  resolver, and ignored that host runtime keys are path-based. Added, plus
  registry tombstones — `load_project_registry` really does drop an entry whose
  `agents.yaml` is gone, which would have made orphan reporting impossible.
- §5's rule was simply wrong: enumerate-all paths already exist in
  `rt-codex-wake`, `rt-doctor` and `inspect_host_harness_seats`. Replaced with
  a rule about which paths may enumerate.
- §7's migration lacked a lock, a cutover pointer, and an honest rollback.
  Replaced wholesale; the rollback correction — that restoring the backup alone
  discards post-cutover mail — was the most valuable single point of the review.

### Hermes

Four, all verified before acceptance:

- Target agent validation resolves against the sender's `agents.yaml`. Fixed
  in §4.
- §2 omitted `runtime.json`, the ledger location, and `agents.yaml` session
  scoping. Added.
- `rt-doctor`'s `report_legacy_markers` globs through symlinks. Resolved by
  moving the bookmark off the `inbox` path entirely rather than guarding the
  glob, which is a narrower fix than it first appeared.
- Delete the bridge. Agreed independently with Codex.

## Deferred past v1, on purpose

Recorded so they are decisions rather than omissions.

1. **Host runtime keys stay path-based.** `_rtruntime.project_hash` and the
   Codex wake bindings keep using `sha256(path)`. Renaming a project therefore
   keeps its mail and its identity but requires relaunching its Codex seat.
   Ocean rarely renames project folders, so the cost is small and the change
   would touch exactly the binding code that took a full day to stabilise.
   Revisit when renaming becomes common or when that code is being changed for
   another reason.
2. **Binding still requires a first turn.** SessionStart is turn-gated in
   Codex, so a seat binds when the human first says something. Yesterday's fix
   removed the five-minute deadline, so the wait is now unbounded rather than
   fatal, but the requirement remains. See the open question below.

## Still open

1. Does deferring the host-runtime key migration leave a state where a renamed
   project has central mail under its UUID but a Codex binding under its old
   path hash, and is the required rebind detectable by `doctor`?
2. Resolved — see §8. Preallocation is in scope for v1; the three options and
   the constraint that prompt text is never a security fence are recorded
   there, together with the fact that the fence variables provably do not
   reach a remote thread's shell, which no preamble can fix.
