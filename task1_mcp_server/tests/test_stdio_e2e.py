"""Spawn the real server and speak raw JSON-RPC over its stdin/stdout.

Asserts that every byte on stdout is a JSON-RPC 2.0 message and that logs
only appear on stderr.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def proc():
    p = subprocess.Popen(
        [sys.executable, "-m", "task1_mcp_server.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    yield p
    p.kill()
    p.wait(timeout=5)


def _send(p, msg):
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()


def _recv(p):
    line = p.stdout.readline()
    assert line, "server closed stdout"
    msg = json.loads(line)  # fails the test if anything non-JSON hits stdout
    assert msg["jsonrpc"] == "2.0"
    return msg


def _call(p, id_, name, arguments):
    _send(p, {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return _recv(p)


def test_full_session(proc):
    _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}})
    init = _recv(proc)
    assert init["id"] == 1 and "result" in init
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"]: t for t in _recv(proc)["result"]["tools"]}
    assert set(tools) == {"lookup_customer", "issue_refund"}
    assert tools["issue_refund"]["inputSchema"]["additionalProperties"] is False

    ok = _call(proc, 3, "lookup_customer", {"customer_id": "CUST-000001"})
    assert ok["result"]["isError"] is False

    bad = _call(proc, 4, "issue_refund", {"customer_id": "CUST-000001", "amount": -5, "reason": "too short"})
    assert bad["error"]["code"] == -32602

    unknown = _call(proc, 5, "nope", {})
    assert unknown["error"]["code"] == -32602

    refund = _call(proc, 6, "issue_refund", {"customer_id": "CUST-000002", "amount": 12.5, "reason": "Late delivery refund"})
    assert refund["result"]["isError"] is False

    proc.stdin.close()
    rest_stdout = proc.stdout.read()
    stderr = proc.stderr.read()
    for line in rest_stdout.splitlines():
        json.loads(line)  # anything left on stdout must still be JSON-RPC
    assert "starting MCP server" in stderr
    assert "refund issued" in stderr
