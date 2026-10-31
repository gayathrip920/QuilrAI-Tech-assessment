"""Mock model providers for demos and tests.

Run two copies:
    uvicorn task4_router.mock_providers:primary --port 9201
    uvicorn task4_router.mock_providers:backup  --port 9202

Control the primary's behavior per request with the "mock_behavior" field:
    "ok" (default), "429", "slow" (sleeps 5 s), "500", "400"
"""
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def make_provider(name: str) -> FastAPI:
    app = FastAPI(title=f"mock-{name}")
    app.state.calls = 0
    app.state.cancelled = 0
    app.state.behavior = None  # tests can force a behavior for every call

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        app.state.calls += 1
        body = await request.json()
        behavior = app.state.behavior or body.get("mock_behavior", "ok")
        if name == "backup" and app.state.behavior is None:
            behavior = "ok"
        if behavior == "429":
            return JSONResponse({"error": {"message": f"{name} internal quota detail: org-123"}}, status_code=429)
        if behavior == "500":
            return JSONResponse({"error": {"message": "Traceback (most recent call last): secret stack"}}, status_code=500)
        if behavior == "400":
            return JSONResponse({"error": {"message": "bad request detail"}}, status_code=400)
        if behavior == "slow":
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                app.state.cancelled += 1
                raise
        return {
            "id": f"cmpl-{name}",
            "object": "chat.completion",
            "model": f"{name}-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"hello from {name}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }

    return app


primary = make_provider("primary")
backup = make_provider("backup")
