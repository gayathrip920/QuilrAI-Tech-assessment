"""Protocol compliance and stdout hygiene, tested on the real process."""
import ast
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "task1_mcp_server"


@pytest.fixture
def server():
    p = subprocess.Popen(
        [sys.executable, "-m", "task1_mcp_server.server"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in p.stdout], daemon=True).start()

    def send(raw: str):
        p.stdin.write(raw + "\n")
        p.stdin.flush()

    def recv():
        msg = json.loads(lines.get(timeout=10))
        assert msg["jsonrpc"] == "2.0"
        return msg

    send(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}}))
    assert "result" in recv()
    send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    yield send, recv
    p.kill()
    p.wait(timeout=5)


@pytest.mark.parametrize("raw", ["{not json", "[1, 2", "\x00\x01"])
def test_invalid_json_gets_parse_error(server, raw):
    send, recv = server
    send(raw)
    assert recv() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}


@pytest.mark.parametrize("raw", ['{"hello": 1}', '"just a string"', "42", '{"jsonrpc": "1.0", "id": 1, "method": "x"}'])
def test_json_that_is_not_jsonrpc_gets_invalid_request(server, raw):
    send, recv = server
    send(raw)
    assert recv() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}


def test_unknown_method(server):
    send, recv = server
    send(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "resources/list"}))
    r = recv()
    assert r["id"] == 5 and r["error"]["code"] == -32601


@pytest.mark.parametrize("params", [{"arguments": {}}, {"name": "get_customer_record", "arguments": "oops"}])
def test_malformed_tools_call_params(server, params):
    send, recv = server
    send(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": params}))
    r = recv()
    assert r["id"] == 6 and r["error"]["code"] == -32602


def test_server_keeps_working_after_bad_input(server):
    send, recv = server
    send("{garbage")
    recv()
    send(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-00001"}}}))
    r = recv()
    assert r["id"] == 7 and r["result"]["isError"] is False


def _source_files():
    return [p for p in PKG.rglob("*.py") if "tests" not in p.parts]


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_no_print_or_stdout_writes_in_server_code(path):
    """Static check: nothing in the server code can write to stdout by accident."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            pytest.fail(f"{path.name}:{node.lineno} calls print()")
        if isinstance(node, ast.Attribute) and node.attr == "stdout":
            pytest.fail(f"{path.name}:{node.lineno} touches sys.stdout")
