# Draft upstream issue: make a zero-turn app-server thread resumable

> Status: current — draft upstream issue, not yet filed (BACKLOG.md, "Good
> citizenship")

## Title

`thread/start` returns a zero-turn thread that another client cannot resume

## Body

### Codex version

`codex-cli 0.145.0`, using the managed app-server WebSocket endpoint.

### Reproduction

1. Initialize app-server client A with the experimental API enabled.
2. Call:

   ```json
   {
     "method": "thread/start",
     "params": {
       "cwd": "/tmp/rt-zero-turn-probe",
       "ephemeral": false
     }
   }
   ```

3. Do not start a turn.
4. From client B, call `thread/resume` with `result.thread.id`, or run:

   ```text
   codex --remote unix:// resume <thread-id> -C /tmp/rt-zero-turn-probe
   ```

### Actual result

`thread/start` returns an idle, non-ephemeral root thread with an empty preview
and zero turns. Its response advertises a rollout path, but no rollout exists
yet. Both `historyMode: "legacy"` and `"paginated"` then fail to resume:

```text
no rollout found for thread id <thread-id>
```

`thread/delete {"threadId":"<thread-id>"}` successfully cleans up the
unmaterialized thread.

### Expected result

Please provide either:

- a `thread/start` mode that materializes enough zero-turn state for a later
  client or TUI to resume it; or
- a supported `thread/resume`/attach path for an idle, non-ephemeral,
  unmaterialized zero-turn thread.

The use case is a launcher that must learn and bind the native thread id before
the first human turn, without sending a fake prompt, starting a model turn, or
injecting synthetic conversation history.
