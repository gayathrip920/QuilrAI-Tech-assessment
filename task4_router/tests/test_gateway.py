import json
import time

import pytest

MSG = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}


def auth(key="key-a"):
    return {"Authorization": f"Bearer {key}"}


def assert_clean_error(r, status, type_):
    assert r.status_code == status
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"type", "message", "request_id"}
    assert body["error"]["type"] == type_
    text = r.text.lower()
    for leak in ["traceback", "org-123", "secret", "primary internal", "http://", "127.0.0.1", "exception"]:
        assert leak not in text


async def test_primary_ok(make_gateway, providers):
    app, c = await make_gateway()
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "primary"
    assert r.json()["choices"][0]["message"]["content"] == "hello from primary"
    assert providers[1].state.calls == 0


@pytest.mark.parametrize("behavior", ["429", "500"])
async def test_failover_on_429_and_5xx(make_gateway, providers, behavior):
    primary, backup = providers
    primary.state.behavior = behavior
    app, c = await make_gateway()
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "backup"
    assert primary.state.calls == 1 and backup.state.calls == 1


async def test_failover_on_timeout_and_primary_is_cancelled(make_gateway, providers):
    primary, backup = providers
    primary.state.behavior = "slow"  # sleeps 5 s
    app, c = await make_gateway(primary_timeout_s=0.3)
    start = time.perf_counter()
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    elapsed = time.perf_counter() - start
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "backup"
    assert 0.3 <= elapsed < 1.5
    assert primary.state.cancelled == 1  # the slow call did not keep running


async def test_default_timeout_is_3000ms(make_gateway, providers):
    primary, _ = providers
    primary.state.behavior = "slow"
    app, c = await make_gateway(primary_timeout_s=3.0)
    start = time.perf_counter()
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    elapsed = time.perf_counter() - start
    assert r.headers["x-gateway-provider"] == "backup"
    assert 2.9 <= elapsed < 4.0


async def test_both_down_gives_sanitized_503(make_gateway, providers):
    for p in providers:
        p.state.behavior = "500"
    app, c = await make_gateway()
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert_clean_error(r, 503, "upstream_unavailable")
    # The failed request gave its tokens back.
    assert await app.state.limiter.used("a") == 0


async def test_provider_400_is_not_retried_and_not_leaked(make_gateway, providers):
    primary, backup = providers
    primary.state.behavior = "400"
    app, c = await make_gateway()
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert_clean_error(r, 400, "invalid_request")
    assert backup.state.calls == 0
    assert "bad request detail" not in r.text


async def test_rate_limit_blocks_before_calling_any_provider(make_gateway, providers, clock):
    app, c = await make_gateway(tenants={"key-a": {"tenant": "a", "tokens_per_minute": 200}})
    # Estimate per call is ~ len(messages json)/4 + 50 = ~63. The real usage (20) is
    # written back after each call, so the window holds the true numbers.
    ok = 0
    for _ in range(20):
        r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
        if r.status_code == 200:
            ok += 1
        else:
            assert_clean_error(r, 429, "rate_limit_exceeded")
            assert int(r.headers["retry-after"]) >= 1
            break
    calls_before = providers[0].state.calls
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert r.status_code == 429
    assert providers[0].state.calls == calls_before  # blocked request never reached a provider
    assert ok > 1
    clock.t += 61
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert r.status_code == 200  # window slid


async def test_actual_usage_replaces_estimate(make_gateway):
    app, c = await make_gateway()
    await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert await app.state.limiter.used("a") == 20  # mock reports total_tokens=20


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "key-a"}])
async def test_bad_api_key(make_gateway, providers, headers):
    app, c = await make_gateway()
    r = await c.post("/v1/chat/completions", json=MSG, headers=headers)
    assert_clean_error(r, 401, "authentication_error")
    assert providers[0].state.calls == 0


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"messages": "hi"}'])
async def test_bad_body(make_gateway, body):
    app, c = await make_gateway()
    r = await c.post("/v1/chat/completions", content=body, headers={**auth(), "content-type": "application/json"})
    assert_clean_error(r, 400, "invalid_request")


async def test_unexpected_crash_is_sanitized(make_gateway, monkeypatch):
    app, c = await make_gateway()

    async def explode(payload):
        raise RuntimeError("db password=hunter2 at /srv/app/secret.py line 9")

    monkeypatch.setattr(app.state.router, "route", explode)
    r = await c.post("/v1/chat/completions", json=MSG, headers=auth())
    assert_clean_error(r, 500, "internal_error")
    assert "hunter2" not in r.text
    assert await app.state.limiter.used("a") == 0
