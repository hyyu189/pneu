"""Kernel transport checks are distinct from native-harness support evidence."""

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import socket
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
loader = importlib.machinery.SourceFileLoader(
    "native_hook_transport_cli", str(ROOT / "bin" / "rt-native")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
native = importlib.util.module_from_spec(spec)
loader.exec_module(native)


def use_output_fds(monkeypatch, stdout, stderr):
    original_dup = os.dup
    selected = {1: stdout, 2: stderr}
    monkeypatch.setattr(native.os, "dup", lambda fd: original_dup(selected.get(fd, fd)))


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin kernel peer-PID contract")
def test_real_socketpairs_identify_creator_and_remain_open(monkeypatch):
    # Keep both peers alive while validating the kernel connection.
    out, out_peer = socket.socketpair()
    err, err_peer = socket.socketpair()
    try:
        use_output_fds(monkeypatch, out.fileno(), err.fileno())
        native.require_hook_transport("claude", os.getpid())
        out.sendall(b"still-open")
        assert out_peer.recv(10) == b"still-open"
        err.sendall(b"stderr-open")
        assert err_peer.recv(11) == b"stderr-open"
    finally:
        for channel in (out, out_peer, err, err_peer):
            channel.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin kernel peer-PID contract")
def test_self_created_socketpair_cannot_impersonate_native_owner(monkeypatch):
    first, second = socket.socketpair()
    try:
        use_output_fds(monkeypatch, first.fileno(), second.fileno())
        with pytest.raises(native.NativeQueryError, match="native owner"):
            native.require_hook_transport("claude", os.getpid() + 1)
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin kernel peer-PID contract")
@pytest.mark.parametrize("pipe_output", [1, 2])
def test_each_output_must_be_a_native_socket_not_a_shell_pipe(monkeypatch, pipe_output):
    first, second = socket.socketpair()
    read_fd, write_fd = os.pipe()
    try:
        outputs = {1: first.fileno(), 2: second.fileno()}
        outputs[pipe_output] = write_fd
        use_output_fds(monkeypatch, outputs[1], outputs[2])
        with pytest.raises(native.NativeQueryError, match="cannot be authenticated"):
            native.require_hook_transport("claude", os.getpid())
        os.fstat(write_fd)  # Failure must not close the original process stream.
    finally:
        first.close()
        second.close()
        os.close(read_fd)
        os.close(write_fd)


def test_other_platform_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")
    with pytest.raises(native.NativeQueryError, match="Darwin") as failure:
        native.require_hook_transport("claude", os.getpid())
    assert failure.value.status == "unsupported"


def test_codex_does_not_claim_socket_transport_attestation():
    native.require_hook_transport("codex", os.getpid())
