import json

import pytest
from mcp.shared.exceptions import MCPError

from task1_mcp_server.server import INVALID_PARAMS, dispatch

GOOD_REASON = "Item arrived damaged"


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("cid", ["CUST-000001", "CUST-000002"])
def test_lookup_ok(cid):
    r = dispatch("lookup_customer", {"customer_id": cid})
    assert r.is_error is False
    assert _payload(r)["customer_id"] == cid


@pytest.mark.parametrize(
    "cid",
    ["", "CUST-1", "CUST-0000001", "cust-000001", "CUST_000001", " CUST-000001", "CUST-000001\n", "CUST-00000a", 123, None],
)
def test_lookup_bad_id(cid):
    with pytest.raises(MCPError) as e:
        dispatch("lookup_customer", {"customer_id": cid})
    assert e.value.code == INVALID_PARAMS


def test_lookup_missing_and_extra_args():
    for args in [{}, None, {"customer_id": "CUST-000001", "extra": 1}]:
        with pytest.raises(MCPError) as e:
            dispatch("lookup_customer", args)
        assert e.value.code == INVALID_PARAMS


def test_lookup_unknown_customer_is_tool_error_not_protocol_error():
    r = dispatch("lookup_customer", {"customer_id": "CUST-999999"})
    assert r.is_error is True
    assert _payload(r)["error"] == "customer_not_found"


@pytest.mark.parametrize("amount", [0.01, 1, 25.5, 10000])
def test_refund_ok(amount):
    r = dispatch("issue_refund", {"customer_id": "CUST-000001", "amount": amount, "reason": GOOD_REASON})
    assert r.is_error is False
    body = _payload(r)
    assert body["status"] == "processed" and body["amount"] == amount


@pytest.mark.parametrize(
    "amount",
    [0, -1, -0.01, 0.001, 1.999, 10000.01, float("inf"), float("nan"), "10", True, None],
)
def test_refund_bad_amount(amount):
    with pytest.raises(MCPError) as e:
        dispatch("issue_refund", {"customer_id": "CUST-000001", "amount": amount, "reason": GOOD_REASON})
    assert e.value.code == INVALID_PARAMS


@pytest.mark.parametrize("reason", ["", "short", "123456789", "   too short   ", " " * 20, "x" * 501, 1234567890])
def test_refund_bad_reason(reason):
    with pytest.raises(MCPError) as e:
        dispatch("issue_refund", {"customer_id": "CUST-000001", "amount": 5, "reason": reason})
    assert e.value.code == INVALID_PARAMS


def test_refund_reason_exactly_10_ok_and_trimmed():
    r = dispatch("issue_refund", {"customer_id": "CUST-000001", "amount": 5, "reason": "  0123456789  "})
    assert _payload(r)["reason"] == "0123456789"


def test_error_data_lists_every_bad_field_without_echoing_input():
    with pytest.raises(MCPError) as e:
        dispatch("issue_refund", {"customer_id": "SECRET-INPUT", "amount": -1, "reason": "x"})
    fields = {d["field"] for d in e.value.data}
    assert fields == {"customer_id", "amount", "reason"}
    assert "SECRET-INPUT" not in json.dumps(e.value.data)


def test_unknown_tool():
    with pytest.raises(MCPError) as e:
        dispatch("delete_everything", {})
    assert e.value.code == INVALID_PARAMS
