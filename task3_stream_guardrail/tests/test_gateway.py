import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from task3_stream_guardrail import mock_llm
from task3_stream_guardrail.gateway import create_app, redact_stream, sse_events


def _content(sse_text: str) -> str:
    out = []
    for line in sse_text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            ev = json.loads(line[6:])
            for c in ev.get("choices", []):
                out.append((c.get("delta") or {}).get("content") or "")
    return "".join(out)


async def test_sse_parser_handles_lines_split_across_chunks():
    raw = b'data: {"a": 1}\n\ndata: {"b"' + b': 2}\n\n' + b"data: [DONE]\n\n"

    async def chunks():
        for i in range(0, len(raw), 3):
            yield raw[i:i + 3]

    assert [e async for e in sse_events(chunks())] == ['{"a": 1}', '{"b": 2}', "[DONE]"]


async def test_first_token_is_not_delayed_by_a_slow_upstream():
    async def slow_events():
        yield json.dumps({"choices": [{"delta": {"content": "Hello world, "}}]})
        await asyncio.sleep(1.0)  # model is slow after the first token
        yield json.dumps({"choices": [{"delta": {"content": "bye x@y.com "}}]})
        yield "[DONE]"

    start = time.perf_counter()
    gen = redact_stream(slow_events())
    first = await gen.__anext__()
    assert time.perf_counter() - start < 0.2
    assert "Hello world, " in first
    rest = "".join([e async for e in gen])
    assert _content(first + rest) == "Hello world, bye [REDACTED] "


async def test_gateway_end_to_end_in_process():
    app = create_app(upstream_url="http://llm/v1/chat/completions", transport=httpx.ASGITransport(app=mock_llm.app))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        r = await c.post("/v1/chat/completions", json={"messages": [], "mock_delay_s": 0})
    assert r.status_code == 200
    text = _content(r.text)
    assert text == (
        "Sure. The customer is Jane Doe, email [REDACTED], SSN [REDACTED], and the card on file is "
        "[REDACTED]. Her backup contact is [REDACTED]. Anything else?"
    )
    assert r.text.rstrip().endswith("data: [DONE]")


async def test_upstream_down_gives_clean_error():
    def boom(request):
        raise httpx.ConnectError("refused 10.1.2.3")

    app = create_app(upstream_url="http://llm/x", transport=httpx.MockTransport(boom))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        r = await c.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 502
    assert "10.1.2.3" not in r.text


# ---------- real sockets: prove streaming, not buffering ----------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    return server


@pytest.fixture
def live_servers():
    llm_port, gw_port = _free_port(), _free_port()
    s1 = _serve(mock_llm.app, llm_port)
    s2 = _serve(create_app(upstream_url=f"http://127.0.0.1:{llm_port}/v1/chat/completions"), gw_port)
    yield f"http://127.0.0.1:{gw_port}"
    s1.should_exit = s2.should_exit = True


def test_live_stream_time_to_first_token(live_servers):
    # Mock sends ~40 chunks with 50 ms between them (about 2 s total).
    start = time.perf_counter()
    first_at = None
    body = ""
    with httpx.stream("POST", f"{live_servers}/v1/chat/completions",
                      json={"messages": [], "mock_delay_s": 0.05}, timeout=30) as r:
        for chunk in r.iter_text():
            if first_at is None and _content(chunk):
                first_at = time.perf_counter() - start
            body += chunk
    total = time.perf_counter() - start
    assert first_at is not None and first_at < 0.5, first_at
    assert total > 1.0  # the upstream really was slow, so the early first token proves streaming
    assert "jane.doe" not in body and "4111" not in body and "123-45-6789" not in body
