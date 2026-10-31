import json

import pytest
from mcp.shared.exceptions import MCPError

from task1_mcp_server.server import INVALID_PARAMS, dispatch

GOOD_REASON = "Item arrived damaged"


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("cid", ["CUST-00001", "CUST-00002"])
def test_lookup_ok(cid):
    r = dispatch("get_customer_record", {"customer_id": cid})
    assert r.is_error is False
    assert _payload(r)["customer_id"] == cid


@pytest.mark.parametrize(
    "cid",
    ["", "CUST-", "CUST-1", "CUST-0001", "CUST-000001", "cust-00001", "CUST_00001", "CUST00001", " CUST-00001", "CUST-00001 ",
     "CUST-00001\n", "CUST-0000a", "CUST-\uff10\uff10\uff10\uff10\uff11", "CUST-\u0661\u0662\u0663\u0664\u0665", 12345, None],
)
def test_lookup_bad_id(cid):
    with pytest.raises(MCPError) as e:
        dispatch("get_customer_record", {"customer_id": cid})
    assert e.value.code == INVALID_PARAMS


def test_lookup_missing_and_extra_args():
    for args in [{}, None, {"customer_id": "CUST-00001", "extra": 1}]:
        with pytest.raises(MCPError) as e:
            dispatch("get_customer_record", args)
        assert e.value.code == INVALID_PARAMS


def test_lookup_unknown_customer_is_tool_error_not_protocol_error():
    r = dispatch("get_customer_record", {"customer_id": "CUST-99999"})
    assert r.is_error is True
    assert _payload(r)["error"] == "customer_not_found"


@pytest.mark.parametrize("amount", [0.01, 1, 25.5, 99.999, 1e-6, 150000])
def test_refund_ok(amount):
    r = dispatch("trigger_refund", {"customer_id": "CUST-00001", "amount": amount, "reason": GOOD_REASON})
    assert r.is_error is False
    body = _payload(r)
    assert body["status"] == "processed" and body["amount"] == amount


@pytest.mark.parametrize(
    "amount",
    [0, 0.0, -0.0, -1, -0.01, float("inf"), float("nan"), "10", True, False, None, [5], {"v": 5}],
)
def test_refund_bad_amount(amount):
    with pytest.raises(MCPError) as e:
        dispatch("trigger_refund", {"customer_id": "CUST-00001", "amount": amount, "reason": GOOD_REASON})
    assert e.value.code == INVALID_PARAMS


@pytest.mark.parametrize("reason", ["", "short", "123456789", "   too short   ", " " * 20, "\n\t" * 10, 1234567890, None])
def test_refund_bad_reason(reason):
    with pytest.raises(MCPError) as e:
        dispatch("trigger_refund", {"customer_id": "CUST-00001", "amount": 5, "reason": reason})
    assert e.value.code == INVALID_PARAMS


def test_long_reason_ok():
    r = dispatch("trigger_refund", {"customer_id": "CUST-00001", "amount": 5, "reason": "x" * 5000})
    assert r.is_error is False


def test_refund_reason_exactly_10_ok_and_trimmed():
    r = dispatch("trigger_refund", {"customer_id": "CUST-00001", "amount": 5, "reason": "  0123456789  "})
    assert _payload(r)["reason"] == "0123456789"


def test_error_data_lists_every_bad_field_without_echoing_input():
    with pytest.raises(MCPError) as e:
        dispatch("trigger_refund", {"customer_id": "SECRET-INPUT", "amount": -1, "reason": "x"})
    fields = {d["field"] for d in e.value.data}
    assert fields == {"customer_id", "amount", "reason"}
    assert "SECRET-INPUT" not in json.dumps(e.value.data)


def test_unknown_tool():
    with pytest.raises(MCPError) as e:
        dispatch("delete_everything", {})
    assert e.value.code == INVALID_PARAMS
