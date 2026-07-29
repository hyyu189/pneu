"""Hermes user-plugin bridge for Roundtable's durable inbox.

The plugin is deliberately inert unless it was launched through a Roundtable
launcher that supplied a complete, fenced session environment.  It never reads
message bodies or credentials.  ``rt-wait-inbox`` remains responsible for
validating the lease and watching the durable maildir.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any


_REQUIRED_ENV = (
    "RT_PROJECT_ROOT",
    "RT_FROM",
    "RT_SESSION_ID",
    "RT_LEASE_REVISION",
)
_MAIL_MARKER = "rt-wait-inbox: mail after "
_NEW_DIR_MARKER = "rt-wait-inbox: new-dir "
_HEARTBEAT_MARKER = "rt-wait-inbox: heartbeat timeout after "
_SUPERSEDED_MARKER = "rt-wait-inbox: seat lease or watcher was superseded"
_MAIL_DRAIN_POLL_SECONDS = 0.25
_STOP_JOIN_SECONDS = 2.0
_TUI_SENTINEL_POLLS = 150
_TUI_SENTINEL_POLL_SECONDS = 0.1
_TUI_RELEASE_TIMEOUT_SECONDS = 5
# 2026-07-21 finding #7: a busy seat can take ~5 minutes to drain one
# generation. Both bounds must exceed that so a slow-but-healthy drain never
# receives a spurious re-notice.
_PENDING_RENOTIFY_SECONDS = 600
_PENDING_PAUSE_SECONDS = 1200

_MAIL_MESSAGE = (
    "[Roundtable] New durable mail is waiting. Run "
    "`rt-inbox --fenced --archive-quiet-acks -f json`, act on each non-ack "
    "message, and acknowledge it with `rt-ack <id>[,<id>...]`; successful "
    "acknowledgement also archives that message to `cur/`. Reply with "
    "`rt-say <agent> <kind> \"body\"` where `kind` is one flag-free token "
    "such as `reply` and any referenced message id belongs in the body — "
    "`--kind`/`--refs` options do not exist. The watcher re-arms "
    "automatically after the triggered messages are archived."
)
_PENDING_MESSAGE = (
    "[Roundtable] The triggered mail generation is still pending in `new/` "
    "after repeated notices. Automatic notices are paused for this "
    "generation to prevent a loop; the mail remains durable and the watcher "
    "re-arms automatically once `rt-ack` archives it."
)
_FENCE_MESSAGE = (
    "[Roundtable] This Hermes watcher stopped because its session lease was "
    "superseded. Continue from the newer Roundtable-launched session."
)
_CONFIG_MESSAGE = (
    "[Roundtable] Inbox watching stopped because the Roundtable session or "
    "installation is invalid. Run `rt-doctor` before restarting Hermes."
)


def _activation_environment() -> dict[str, str] | None:
    """Return a stable environment snapshot, or ``None`` when not opted in."""

    values = {name: os.environ.get(name, "").strip() for name in _REQUIRED_ENV}
    if not all(values.values()):
        return None
    environment = os.environ.copy()
    environment.update(values)
    return environment


def _resolve_waiter(environment: dict[str, str]) -> str | None:
    """Resolve the installed waiter once, preferring the managed prefix."""

    prefix = environment.get("ROUNDTABLE_INSTALL_PREFIX", "").strip()
    if prefix:
        candidate = (
            Path(prefix).expanduser() / "bin" / "rt-wait-inbox"
        )
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return shutil.which("rt-wait-inbox", path=environment.get("PATH"))


def _triggered_non_ack_mail(output: str) -> tuple[str, ...] | None:
    """Return the exact mail generation reported by ``rt-wait-inbox``.

    A second message can arrive while Hermes is processing the first. Waiting
    for the whole ``new/`` directory to drain would silently absorb that newer
    message into the first wake. The waiter output is the generation boundary:
    once those exact files leave ``new/``, a fresh waiter can immediately wake
    Hermes for anything that arrived later.
    """

    lines = output.splitlines()
    marker_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith(_MAIL_MARKER)
        ),
        None,
    )
    if marker_index is None:
        return None

    names: list[str] = []
    for raw_name in lines[marker_index + 1 :]:
        if raw_name.startswith("rt-wait-inbox:"):
            break
        name = raw_name.strip()
        if not name:
            continue
        if (
            name != raw_name
            or Path(name).name != name
            or name.startswith(("ack-", "."))
            or "\x00" in name
        ):
            return None
        names.append(name)
    return tuple(dict.fromkeys(names)) or None


def _triggered_new_dir(output: str) -> Path | None:
    """Return the resolver-owned maildir path emitted for this wake."""

    values = [
        line.removeprefix(_NEW_DIR_MARKER)
        for line in output.splitlines()
        if line.startswith(_NEW_DIR_MARKER)
    ]
    if len(values) != 1:
        return None
    try:
        raw = json.loads(values[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path.name != "new":
        return None
    return path


def _generation_is_pending(
    new_dir: Path,
    generation: tuple[str, ...],
) -> bool:
    try:
        return any((new_dir / name).exists() for name in generation)
    except OSError:
        # Fail closed: do not start another watcher while the durable inbox
        # cannot be inspected.
        return True


class _RoundtableBridge:
    """Own one fenced watcher process and its daemon supervisor thread."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        # _closed is session-scoped: a later on_session_reset may re-arm after
        # the failed watcher is proven dead. _terminal is process-permanent:
        # a broken installation or a superseded lease cannot heal inside this
        # Hermes process, so no reset may re-arm past it.
        self._closed = False
        self._terminal = False
        self._diagnostic_sent = False

    def on_session_start(self, **kwargs: Any) -> None:
        environment = _activation_environment()
        if environment is None:
            return
        session_id = str(kwargs.get("session_id") or "").strip()
        platform = str(kwargs.get("platform") or "").strip().lower()

        with self._lock:
            if self._closed or self._terminal or (
                self._thread is not None and self._thread.is_alive()
            ):
                return
            waiter = _resolve_waiter(environment)
            if waiter is None:
                self._inject_diagnostic(_CONFIG_MESSAGE)
                # PATH cannot heal mid-process; the diagnostic already says
                # to restart Hermes, so stay down across session resets.
                self._closed = True
                self._terminal = True
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._watch,
                args=(environment, waiter, session_id, platform),
                name="roundtable-hermes-watcher",
                daemon=True,
            )
            self._thread.start()

    def on_session_reset(self, **kwargs: Any) -> None:
        # The TUI gateway publishes its initial built session, as well as
        # /new and /reset replacements, through on_session_reset.  Stop any
        # prior watcher first so an unexpected unpaired reset can never leave
        # two consumers racing on one durable inbox.  A later first-turn
        # on_session_start is idempotent while this replacement is alive.
        self._shutdown(permanent=False)
        self.on_session_start(**kwargs)

    def on_session_finalize(self, **_kwargs: Any) -> None:
        # Hermes can finalize one native session and start another inside the
        # same CLI process (for example after /new). Stop the old fenced
        # watcher without permanently disabling this plugin instance.
        self._shutdown(permanent=False)

    def close(self) -> None:
        self._shutdown(permanent=True)

    def _shutdown(self, *, permanent: bool) -> None:
        with self._lock:
            if permanent:
                self._closed = True
                self._terminal = True
            self._stop.set()
            process = self._process
            thread = self._thread

        self._terminate(process)
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=_STOP_JOIN_SECONDS)
            if thread.is_alive():
                with self._lock:
                    process = self._process
                self._kill(process)
                thread.join(timeout=_STOP_JOIN_SECONDS)
        with self._lock:
            if not permanent:
                if thread is not None and thread.is_alive():
                    # Never overlap a new session with a watcher whose shutdown
                    # could not be proved.
                    self._closed = True
                else:
                    self._thread = None
                    self._process = None
                    self._diagnostic_sent = False
                    # The old watcher is proven dead, so a session-scoped
                    # failure may re-arm on this reset; a terminal one stays
                    # closed for the life of the process.
                    self._closed = self._terminal
                    self._stop.clear()

    def _watch(
        self,
        environment: dict[str, str],
        waiter: str,
        session_id: str,
        platform: str,
    ) -> None:
        project_root = Path(environment["RT_PROJECT_ROOT"]).expanduser()
        agent = environment["RT_FROM"]

        while not self._stop.is_set():
            try:
                process = subprocess.Popen(
                    [waiter, agent],
                    cwd=str(project_root),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except (OSError, ValueError):
                self._fail(_CONFIG_MESSAGE)
                return

            with self._lock:
                if self._stop.is_set():
                    self._terminate(process)
                    return
                self._process = process

            try:
                output, _unused_stderr = process.communicate()
            except (OSError, ValueError):
                if not self._stop.is_set():
                    self._fail(_CONFIG_MESSAGE)
                return
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None

            if self._stop.is_set():
                return

            output = output or ""
            if process.returncode == 0 and _MAIL_MARKER in output:
                generation = _triggered_non_ack_mail(output)
                new_dir = _triggered_new_dir(output)
                if generation is None or new_dir is None:
                    self._fail(_CONFIG_MESSAGE)
                    return
                if not self._deliver(
                    _MAIL_MESSAGE,
                    session_id=session_id,
                    platform=platform,
                ):
                    self._fail(_CONFIG_MESSAGE)
                    return
                # RC10 finding #3: an unacknowledged generation used to park
                # this watcher in a silent unbounded wait. Mirror the bounded
                # Claude Stop-hook retries in rt-wait-inbox: one re-notice,
                # then one pause diagnostic, then keep polling silently so a
                # late `rt-ack` still re-arms a fresh waiter automatically.
                pending_since = time.monotonic()
                renotified = paused = False
                while (
                    not self._stop.is_set()
                    and _generation_is_pending(
                        new_dir,
                        generation,
                    )
                ):
                    waited = time.monotonic() - pending_since
                    if not renotified and waited >= _PENDING_RENOTIFY_SECONDS:
                        renotified = True
                        if not self._deliver(
                            _MAIL_MESSAGE,
                            session_id=session_id,
                            platform=platform,
                        ):
                            self._fail(_CONFIG_MESSAGE)
                            return
                    elif not paused and waited >= _PENDING_PAUSE_SECONDS:
                        paused = True
                        if not self._deliver(
                            _PENDING_MESSAGE,
                            session_id=session_id,
                            platform=platform,
                        ):
                            self._fail(_CONFIG_MESSAGE)
                            return
                    self._stop.wait(_MAIL_DRAIN_POLL_SECONDS)
                continue

            if process.returncode == 0 and _HEARTBEAT_MARKER in output:
                continue

            if _SUPERSEDED_MARKER in output:
                # The RT_* session environment is fixed for this process, so
                # a superseded lease can never become valid again here.
                self._fail(_FENCE_MESSAGE, terminal=True)
                return

            self._fail(_CONFIG_MESSAGE)
            return

    def _deliver(
        self,
        message: str,
        *,
        session_id: str,
        platform: str,
    ) -> bool:
        """Deliver through the native host path for this Hermes surface."""

        if self._inject(message):
            return True
        if platform != "tui" or not session_id:
            return False

        # Hermes deliberately exposes inject_message only to the classic
        # interactive CLI.  Its public, session-agnostic dispatch_tool API is
        # the supported bridge for gateway/TUI plugins.  Run a real bounded
        # process whose completion carries the exact durable session key;
        # Hermes' TUI notification poller then positive-proofs ownership and
        # starts the recipient turn.
        dispatcher = getattr(self._ctx, "dispatch_tool", None)
        if not callable(dispatcher):
            return False

        # terminal(background=True) starts its process before Hermes records
        # notify_on_complete.  A one-shot command could therefore exit inside
        # that host race. Use a unique capability token and a token-salted
        # hashed /tmp path instead: the helper cannot exit successfully until the
        # foreground release call returns from the same exact session key. The
        # random token also salts the hashed filename, preventing a stale file
        # or predictable /tmp symlink from colliding with a new activation.
        token = secrets.token_hex(32)
        sentinel_digest = hashlib.sha256(
            f"{session_id}\0{token}".encode("utf-8")
        ).hexdigest()
        sentinel = f"/tmp/roundtable-hermes-{sentinel_digest}.sentinel"
        quoted_sentinel = shlex.quote(sentinel)
        quoted_token = shlex.quote(token)
        wait_command = (
            f"rt_sentinel={quoted_sentinel}; rt_token={quoted_token}; rt_i=0; "
            f"while [ \"$rt_i\" -lt {_TUI_SENTINEL_POLLS} ]; do "
            "if [ -f \"$rt_sentinel\" ] && "
            "[ \"$(/bin/cat \"$rt_sentinel\" 2>/dev/null)\" = "
            "\"$rt_token\" ]; then "
            f"/usr/bin/printf '%s\\n' {shlex.quote(message)}; exit 0; "
            "fi; rt_i=$((rt_i + 1)); "
            f"/bin/sleep {_TUI_SENTINEL_POLL_SECONDS}; done; exit 1"
        )
        release_command = (
            "umask 077; set -C; "
            f"/usr/bin/printf '%s' {quoted_token} > {quoted_sentinel}"
        )

        try:
            raw_background = dispatcher(
                "terminal",
                {
                    "command": wait_command,
                    "background": True,
                    "notify_on_complete": True,
                    "pty": False,
                },
                task_id=session_id,
            )
        except Exception:
            return False

        background = self._decode_dispatch_result(raw_background)
        raw_process_id = background.get("session_id") if background else None
        process_id = (
            raw_process_id.strip()
            if isinstance(raw_process_id, str)
            else ""
        )
        if not (
            process_id
            and background is not None
            and background.get("notify_on_complete") is True
            and not background.get("error")
        ):
            if process_id:
                self._cleanup_dispatched_process(
                    dispatcher, process_id=process_id, session_id=session_id
                )
            return False

        try:
            raw_release = dispatcher(
                "terminal",
                {
                    "command": release_command,
                    "background": False,
                    "timeout": _TUI_RELEASE_TIMEOUT_SECONDS,
                    "pty": False,
                },
                task_id=session_id,
            )
        except Exception:
            raw_release = None
        release = self._decode_dispatch_result(raw_release)
        if release is not None and release.get("exit_code") == 0 and not release.get(
            "error"
        ):
            return True

        self._cleanup_dispatched_process(
            dispatcher, process_id=process_id, session_id=session_id
        )
        return False

    @staticmethod
    def _decode_dispatch_result(raw_result: Any) -> dict[str, Any] | None:
        try:
            result = (
                json.loads(raw_result)
                if isinstance(raw_result, str)
                else raw_result
            )
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _cleanup_dispatched_process(
        dispatcher: Any, *, process_id: str, session_id: str
    ) -> None:
        try:
            dispatcher(
                "process",
                {"action": "kill", "session_id": process_id},
                task_id=session_id,
            )
        except Exception:
            # Delivery already failed closed. Cleanup is best-effort and an
            # explicit process kill consumes any completion Hermes observed.
            pass

    def _inject(self, message: str) -> bool:
        try:
            return self._ctx.inject_message(message, role="user") is True
        except Exception:
            # The plugin must never bring down Hermes if CLI injection becomes
            # unavailable during shutdown or in a non-CLI host.
            return False

    def _inject_diagnostic(self, message: str) -> None:
        with self._lock:
            if self._diagnostic_sent:
                return
            self._diagnostic_sent = True
        self._inject(message)

    def _fail(self, message: str, *, terminal: bool = False) -> None:
        with self._lock:
            self._closed = True
            if terminal:
                self._terminal = True
            self._stop.set()
        self._inject_diagnostic(message)

    @staticmethod
    def _terminate(process: subprocess.Popen[str] | None) -> None:
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
        except (OSError, ProcessLookupError, ValueError):
            pass

    @staticmethod
    def _kill(process: subprocess.Popen[str] | None) -> None:
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
        except (OSError, ProcessLookupError, ValueError):
            pass


def register(ctx: Any) -> None:
    """Register lifecycle hooks without activating outside an RT session."""

    bridge = _RoundtableBridge(ctx)
    ctx.register_hook("on_session_start", bridge.on_session_start)
    ctx.register_hook("on_session_reset", bridge.on_session_reset)
    ctx.register_hook("on_session_finalize", bridge.on_session_finalize)
    atexit.register(bridge.close)
