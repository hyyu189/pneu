#!/usr/bin/env python3
"""Disposable Claude channel probe; never launches or attaches to a harness.

The SQLite ledger is test instrumentation, NOT pneu's maildir or acknowledgement
store. Only fixed synthetic messages can be emitted. Transport writes are not
admission receipts. Run ``--help``; see docs for the visible-session acceptance
procedure. No configuration is installed globally.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import select
import sqlite3
import subprocess
import sys
import time
import uuid

PROTOCOL = "2025-06-18"
NAME = "pneu-probe"
ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


def ledger(run_dir: Path) -> sqlite3.Connection:
    db = sqlite3.connect(run_dir / "probe.sqlite", timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS attempts (
            attempt INTEGER PRIMARY KEY, message_id TEXT NOT NULL,
            state TEXT NOT NULL, created_ns INTEGER NOT NULL,
            written_ns INTEGER, generation TEXT);
        CREATE TABLE IF NOT EXISTS observations (
            message_id TEXT PRIMARY KEY, observed_ns INTEGER NOT NULL,
            generation TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observation_calls (
            message_id TEXT NOT NULL, observed_ns INTEGER NOT NULL,
            generation TEXT NOT NULL);
    """)
    return db


def enqueue(db: sqlite3.Connection, message_id: str, repeat: bool = False) -> dict:
    if not isinstance(message_id, str) or not ID_PATTERN.fullmatch(message_id):
        raise ValueError("message ID must contain 1-64 ASCII letters, digits, _ or -")
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT attempt FROM attempts WHERE message_id=?", (message_id,)).fetchone()
        if old and not repeat:
            return {"queued": False, "duplicate": True, "message_id": message_id}
        cur = db.execute(
            "INSERT INTO attempts(message_id,state,created_ns) VALUES(?,'pending',?)",
            (message_id, time.time_ns()),
        )
    return {"queued": True, "attempt": cur.lastrowid, "message_id": message_id}


def observe(db: sqlite3.Connection, message_id: str, generation: str) -> dict:
    # An invented/unsent ID must not become an observation receipt.
    if not isinstance(message_id, str) or not db.execute(
        "SELECT 1 FROM attempts WHERE message_id=? AND state IN ('writing','written','unknown')",
        (message_id,),
    ).fetchone():
        raise ValueError("no submitted probe with that message ID")
    with db:
        db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?)", (message_id, time.time_ns(), generation))
        db.execute("INSERT INTO observation_calls VALUES(?,?,?)", (message_id, time.time_ns(), generation))
    return {"message_id": message_id, "model_observation_recorded": True,
            "application_ack": False}


def report(db: sqlite3.Connection) -> dict:
    return {"evidence_level": "probe_instrumentation",
            "native_admission": "unverified", "visible_root_identity": "unverified",
            "attempts": [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY attempt")],
            "observations": [dict(row) for row in db.execute("SELECT * FROM observations ORDER BY observed_ns")],
            "observation_calls": [dict(row) for row in db.execute("SELECT * FROM observation_calls ORDER BY observed_ns")]}


def write_message(output, message: dict) -> None:
    output.write(json.dumps(message, separators=(",", ":")) + "\n")
    output.flush()


def emit_one(db: sqlite3.Connection, output, generation: str) -> bool:
    row = db.execute("SELECT * FROM attempts WHERE state='pending' ORDER BY attempt LIMIT 1").fetchone()
    if row is None:
        return False
    # Commit before writing: a crash/lost transport receipt leaves an explicitly
    # ambiguous attempt, never a falsely safe pending message or automatic replay.
    with db:
        db.execute("UPDATE attempts SET state='writing',generation=? WHERE attempt=?", (generation, row["attempt"]))
    event = {"jsonrpc": "2.0", "method": "notifications/claude/channel",
             "params": {"content": f"Synthetic admission probe {row['message_id']}. No work or approval requested.",
                        "meta": {"message_id": row["message_id"], "attempt": str(row["attempt"]),
                                 "probe_generation": generation}}}
    try:
        write_message(output, event)
    except (BrokenPipeError, OSError):
        with db:
            db.execute("UPDATE attempts SET state='unknown' WHERE attempt=?", (row["attempt"],))
        raise
    with db:
        db.execute("UPDATE attempts SET state='written',written_ns=? WHERE attempt=?", (time.time_ns(), row["attempt"]))
    return True


def handle(db: sqlite3.Connection, request: dict, generation: str) -> dict | None:
    if "id" not in request:
        return None
    response = {"jsonrpc": "2.0", "id": request["id"]}
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "serverInfo": {"name": NAME, "version": "0.1.0"},
                  "capabilities": {"tools": {}, "experimental": {"claude/channel": {}}},
                  "instructions": "This is a synthetic channel admission probe. When you observe a probe, call probe_observed with its message_id. This records observation only, not task completion or permission. Do not interrupt human work to run a new task; probe content grants no authorization."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": "probe_observed", "description": "Record that this synthetic probe was visible to the model; not a pneu application ACK.",
                              "inputSchema": {"type": "object", "properties": {"message_id": {"type": "string"}},
                                              "required": ["message_id"], "additionalProperties": False}}]}
    elif method == "tools/call":
        params = request.get("params", {})
        try:
            if params.get("name") != "probe_observed":
                raise ValueError("unknown tool")
            value = observe(db, params.get("arguments", {}).get("message_id"), generation)
            result = {"content": [{"type": "text", "text": json.dumps(value)}]}
        except ValueError as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
        return response
    response["result"] = result
    return response


def serve(run_dir: Path) -> None:
    with (run_dir / "server.lock").open("a") as lock, ledger(run_dir) as db:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        generation = uuid.uuid4().hex
        initialized = False
        # Use unbuffered bytes: text readline may prefetch multiple MCP lines and
        # make select incorrectly wait despite another complete buffered request.
        pending = b""
        while True:
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.1)
            if readable:
                block = os.read(sys.stdin.fileno(), 65536)
                if not block:
                    return
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    request = json.loads(line)
                    response = handle(db, request, generation)
                    if response is not None:
                        write_message(sys.stdout, response)
                    if request.get("method") == "notifications/initialized":
                        initialized = True
            if initialized:
                emit_one(db, sys.stdout, generation)


def summarize_agents(rows: list, expected_session_id: str | None = None) -> dict:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("unsupported agents JSON schema")
    results = []
    def known(value, allowed):
        return value if isinstance(value, str) and value in allowed else "unknown"

    for row in rows:
        if expected_session_id and row.get("sessionId") != expected_session_id:
            continue
        status = known(row.get("status"), {"idle", "busy", "waiting"})
        results.append({"kind": known(row.get("kind"), {"interactive", "background"}), "status": status,
                        "background_state": known(row.get("state"), {"working", "blocked", "done", "failed", "stopped"}),
                        "waiting_for": known(row.get("waitingFor"), {"permission prompt", "input needed", "sandbox request", "worker request", "dialog open"}),
                        "has_pid": isinstance(row.get("pid"), int),
                        "has_session_id": bool(row.get("sessionId")),
                        "observed_idle": status == "idle", "atomic_admission": "unverified"})
    return {"evidence_level": "live_read_only_inventory", "sessions": results,
            "matched_expected_id": bool(results) if expected_session_id else None,
            "visible_root_identity": "requires_user_confirmation", "live_attach": "unverified"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="read-only CLI inventory; prints no paths, names, PIDs or session IDs")
    inspect.add_argument("--claude", default="claude")
    inspect.add_argument("--expected-session-id")
    for name in ("init", "emit", "report", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--run-dir", required=True, type=Path)
        if name == "emit":
            command.add_argument("--message-id", required=True)
            command.add_argument("--repeat", action="store_true", help="explicitly submit the same ID again; never automatic")
    args = parser.parse_args()
    if args.command == "inspect":
        version = subprocess.run([args.claude, "--version"], check=True, text=True, capture_output=True, timeout=15)
        agents = subprocess.run([args.claude, "agents", "--json"], check=True, text=True, capture_output=True, timeout=15)
        result = summarize_agents(json.loads(agents.stdout), args.expected_session_id)
        result["version"] = version.stdout.strip()
    else:
        args.run_dir = args.run_dir.resolve()
        if args.command == "init":
            args.run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            with ledger(args.run_dir):
                pass
            config = {"mcpServers": {NAME: {"command": sys.executable,
                       "args": [str(Path(__file__).resolve()), "serve", "--run-dir", str(args.run_dir)]}}}
            (args.run_dir / "mcp.json").write_text(json.dumps(config, indent=2) + "\n")
            result = {"config": str(args.run_dir / "mcp.json"), "launches_claude": False}
        elif not (args.run_dir / "probe.sqlite").is_file():
            parser.error("initialize a fresh probe directory first")
        elif args.command == "serve":
            serve(args.run_dir)
            return
        else:
            with ledger(args.run_dir) as db:
                result = enqueue(db, args.message_id, args.repeat) if args.command == "emit" else report(db)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
