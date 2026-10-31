import time

import httpx
import pytest

from task2_mcp_gateway.gateway import create_app, is_admin_tool
from task2_mcp_gateway.mock_mcp_server import TOOLS, create_mock_app
from task2_mcp_gateway.tokens import sign

SECRET = "test-secret"


def token(role, secret=SECRET, exp_in=3600):
    return sign({"sub": "u1", "role": role, "exp": int(time.time()) + exp_in}, secret)


@pytest.fixture
def mock():
    return create_mock_app()


@pytest.fixture
async def gw(mock):
    app = create_app(
        downstream_url="http://mock/mcp",
        secret=SECRET,
        transport=httpx.ASGITransport(app=mock),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        yield c


def call(name, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": name, "arguments": {}}}


def auth(role):
    return {"Authorization": f"Bearer {token(role)}"}


# ---------- tools/list passthrough ----------

async def test_tools_list_forwarded_unchanged(gw, mock):
    r = await gw.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, headers=auth("user"))
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": 7, "result": {"tools": TOOLS}}
    assert r.headers["mcp-session-id"] == "mock-session-1"
    assert mock.state.received == [{"jsonrpc": "2.0", "id": 7, "method": "tools/list"}]


# ---------- authorization ----------

@pytest.mark.parametrize("role", ["user", "viewer", "Admin", "ADMIN", "admin ", ""])
async def test_non_admin_blocked_from_admin_tools_and_downstream_not_called(gw, mock, role):
    r = await gw.post("/mcp", json=call("admin_delete_user", 42), headers=auth(role))
    body = r.json()
    assert r.status_code == 403
    assert body == {"jsonrpc": "2.0", "id": 42, "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
    assert mock.state.received == []  # never reached the server


@pytest.mark.parametrize("name", ["ADMIN_delete_user", "Admin_rotate_keys", " admin_delete_user", "ａdmin_delete_user"])
async def test_prefix_tricks_are_blocked(gw, mock, name):
    r = await gw.post("/mcp", json=call(name), headers=auth("user"))
    assert r.json()["error"]["code"] == -32001
    assert mock.state.received == []


async def test_admin_can_call_admin_tools(gw, mock):
    r = await gw.post("/mcp", json=call("admin_rotate_keys"), headers=auth("admin"))
    assert r.status_code == 200
    assert r.json()["result"]["content"][0]["text"] == "ran admin_rotate_keys"
    assert len(mock.state.received) == 1


@pytest.mark.parametrize("role", ["user", "admin"])
async def test_normal_tools_allowed_for_everyone(gw, mock, role):
    r = await gw.post("/mcp", json=call("get_weather"), headers=auth(role))
    assert r.json()["result"]["content"][0]["text"] == "ran get_weather"


async def test_tool_named_admin_without_underscore_is_not_admin():
    assert is_admin_tool("administer_report") is False
    assert is_admin_tool("admin_x") is True


# ---------- authentication ----------

@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Basic dXNlcjpwYXNz",
        "Bearer not-a-jwt",
        f"Bearer {token('admin', secret='wrong-secret')}",
        f"Bearer {token('admin', exp_in=-10)}",
    ],
)
async def test_bad_or_missing_token_is_401_and_not_forwarded(gw, mock, header):
    headers = {} if header is None else {"Authorization": header}
    r = await gw.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == -32000
    assert mock.state.received == []


async def test_forged_role_in_unsigned_token_rejected(gw, mock):
    # alg=none token claiming admin: header {"alg":"none"}, payload {"role":"admin"}
    forged = "eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYWRtaW4ifQ."
    r = await gw.post("/mcp", json=call("admin_delete_user"), headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert mock.state.received == []


async def test_authorization_header_not_forwarded(gw, mock):
    await gw.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth("user"))
    assert "authorization" not in mock.state.headers[0]


# ---------- JSON-RPC parsing ----------

async def test_parse_error(gw, mock):
    r = await gw.post("/mcp", content=b"{not json", headers=auth("user"))
    assert r.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    assert mock.state.received == []


@pytest.mark.parametrize(
    "body",
    [
        b'"just a string"',
        b'{"id": 1, "method": "tools/list"}',
        b'{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}',
        b'{"jsonrpc": "2.0", "id": 1}',
        b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}',
        b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 5}}',
    ],
)
async def test_invalid_request(gw, mock, body):
    r = await gw.post("/mcp", content=body, headers=auth("user"))
    assert r.json()["error"]["code"] == -32600
    assert mock.state.received == []


async def test_batch_rejected_so_admin_call_cannot_hide_inside(gw, mock):
    batch = [call("get_weather", 1), call("admin_delete_user", 2)]
    r = await gw.post("/mcp", json=batch, headers=auth("user"))
    assert r.json()["error"]["code"] == -32600
    assert mock.state.received == []


async def test_duplicate_name_key_smuggling_blocked(gw, mock):
    # Some parsers keep the first key and some keep the last. Reject both.
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_weather","name":"admin_delete_user"}}'
    r = await gw.post("/mcp", content=body, headers=auth("user"))
    assert r.json()["error"]["code"] == -32600
    assert mock.state.received == []


async def test_notification_forwarded(gw, mock):
    r = await gw.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth("user"))
    assert r.status_code == 202
    assert mock.state.received[0]["method"] == "notifications/initialized"


# ---------- downstream failure ----------

async def test_downstream_down_gives_clean_error():
    def boom(request):
        raise httpx.ConnectError("connection refused to 10.0.0.5:9001")

    app = create_app(downstream_url="http://mock/mcp", secret=SECRET, transport=httpx.MockTransport(boom))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        r = await c.post("/mcp", json=call("get_weather", 9), headers=auth("user"))
    assert r.status_code == 502
    assert r.json() == {"jsonrpc": "2.0", "id": 9, "error": {"code": -32603, "message": "Downstream MCP server unavailable"}}
    assert "10.0.0.5" not in r.text
