"""Mock OpenAI-compatible LLM that streams a canned answer as SSE.

The text is cut into small random-sized pieces so PII gets split across
chunks, which is exactly the case the gateway has to handle.
"""
import asyncio
import json
import random

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

DEFAULT_TEXT = (
    "Sure. The customer is Jane Doe, email jane.doe@example.com, "
    "SSN 123-45-6789, and the card on file is 4111 1111 1111 1111. "
    "Her backup contact is ops-team+billing@corp.example.org. Anything else?"
)

app = FastAPI(title="mock-llm")


def sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    text = body.get("mock_text", DEFAULT_TEXT)
    delay = float(body.get("mock_delay_s", 0.02))
    seed = body.get("mock_seed", 7)

    async def gen():
        rng = random.Random(seed)
        i = 0
        while i < len(text):
            n = rng.randint(1, 6)
            piece = text[i:i + n]
            i += n
            yield sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
            await asyncio.sleep(delay)
        yield sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
