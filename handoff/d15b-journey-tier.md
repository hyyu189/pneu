# D15(b) — journey tier and its mutation evidence

Track T2, 1.4 cycle. The deliverable is the tests; this file records what each
journey claims to pin and the mutation that proves the claim.

## Why the previous print-fallback test did not count

The draft journey test left in the demo clone ran `pneu worktree open` with
`--surface print` against a **vacant** seat. That made it a mutation survivor:
deleting the `if selection.kind != "print":` guard at `bin/rt-worktree:793`
left it green, because `_require_launchable_seat` (`bin/rt-worktree:751`) only
raises for `active_healthy`, `active_unhealthy`, and `ambiguous` seats. With a
vacant seat there is nothing for the gate to refuse, so the guard was never
load-bearing for that assertion.

The landed test claims the seat first. Holding a live lease is what forces the
gate to matter: with the guard deleted the command exits 2 with
`seat 'codex' is already active`, so the test turns red.

## The journey set

| Test | File | Journey |
| --- | --- | --- |
| `test_open_journey_print_fallback_prints_over_an_active_seat_without_touching_it` | `tests/test_open_journey.py` | explicit `--surface print` over a claimed seat: prints, spawns nothing, records nothing, leaves the lease untouched |
| `test_open_journey_ambient_fallback_prints_when_no_surface_is_available` | `tests/test_open_journey.py` | end-to-end subprocess with no `HERDR_ENV`, no `TMUX`, and no `tmux` on `PATH` → ambient fallback to print |
| `test_launcher_navigation_journey_arrows_select_and_launch_the_next_seat` | `tests/test_journey_core.py` | real pty: Enter dismisses the welcome, `\x1b[B` moves the card cursor, Enter launches that exact seat |
| `test_seat_open_journey_reaches_an_active_healthy_lease_and_releases_it` | `tests/test_journey_core.py` | vacant → claimed (`active_unhealthy`) → armed watcher (`active_healthy`) → released (`vacant`) |
| `test_mail_journey_send_wakes_the_armed_watcher_and_ack_archives_the_message` | `tests/test_journey_core.py` | `rt-say` → watcher wakes → `rt-inbox` → `rt-ack` archives `new/`→`cur/` and returns the quiet receipt |

The ambient test scrubs `PATH` down to a private directory holding only a
`git` symlink, so `tmux` is genuinely absent. Without that, the test would
pass for the wrong reason on a host with no tmux installed and fail on a host
that has one.

`tests/test_journey_core.py` stubs exactly one thing: `pneu`'s final
`os.execv`. Everything else is real — a real `roundtable-init` project, the
real registry, a real pty, real `claim`/`release`, a real `rt-wait-inbox`
process, and real `rt-say` / `rt-inbox` / `rt-ack` subprocesses.

## Mutation evidence

`tests/test_journey_mutation.py` copies `bin/`, `templates/`, `skills/`, and
the journey modules into a private tree, deletes one condition, and asserts the
journeys naming that condition turn red. A baseline case runs the unmutated
copy first, so a red result cannot be an artefact of a broken harness.

| Mutation | Source | Journey that must fail | Observed failure |
| --- | --- | --- | --- |
| `print-skips-the-launchable-seat-gate` | `rt-worktree` | print over active seat | exit 2, `seat 'codex' is already active` |
| `printing-is-not-a-launch` | `_rtsurface.py` | both print journeys | exit 2, `print launch succeeded without a surface reference` |
| `ambient-detection-falls-back-to-print` | `_rtsurface.py` | ambient fallback | exit 2, `herdr surface requires HERDR_ENV=1` |
| `down-arrow-moves-the-card-cursor` | `pneu` | launcher navigation | second frame still shows `> Claude Code — claude` |
| `watcher-claims-the-wake-slot` | `rt-wait-inbox` | seat open | `token.watcher_pid` is `None`, not the watcher pid |
| `watcher-wakes-on-new-mail` | `rt-wait-inbox` | mail loop | watcher never exits; wake times out |
| `ack-archives-out-of-new` | `rt-ack` | mail loop | message still present in `new/` after ack |
| `ack-returns-a-quiet-receipt` | `rt-ack` | mail loop | no `ack-*` receipt delivered to the sender |

Every mutation was additionally inspected by hand to confirm the run fails on
the intended assertion rather than on a collection or import error.

## Also landed alongside

`tests/test_collection_determinism.py` is a D15(a) artefact rather than a
journey, but it was built with the same discipline: it collects the suite twice
in separate processes and requires identical node ids, and reintroducing the
original `str(uuid.uuid4())` parametrize value was confirmed to turn it red.

## Note for reviewers

The mutation module is the slowest in the suite (~50 s serially) because each
case pays for a private tree plus a nested pytest process. It parallelizes
cleanly under `pytest -n auto`; see `handoff/d15a-xdist-verdict.md`.
