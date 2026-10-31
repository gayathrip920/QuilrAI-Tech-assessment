# QuilrAI Technical Assessment

My solution to the four tasks in the QuilrAI AI Field Developer assessment.
Everything is in Python, with tests for each task.

> **License:** this code is shared only so QuilrAI can evaluate my application.
> Any other use is not allowed. See [LICENSE](LICENSE).

| Task | Folder | What it does |
|---|---|---|
| 1 | [`task1_mcp_server`](task1_mcp_server) | MCP server over stdio with a customer lookup tool and a refund tool |
| 2 | [`task2_mcp_gateway`](task2_mcp_gateway) | Reverse proxy that blocks `admin_*` tool calls for non-admin users |
| 3 | [`task3_stream_guardrail`](task3_stream_guardrail) | LLM gateway that redacts emails, SSNs and card numbers while streaming |
| 4 | [`task4_router`](task4_router) | Per-tenant token rate limiter on SQLite, with failover to a backup provider |

## Setup

Python 3.10 or newer.

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run all tests (129 tests, about 15 seconds):

```bash
python -m pytest -q
```

---

## Task 1: Custom MCP server

Built with the official MCP Python SDK (`mcp`), using the low-level `Server` class over stdio.

**Tools**

* `lookup_customer(customer_id)`: `customer_id` must match `CUST-` plus 6 digits, for example `CUST-000001`.
* `issue_refund(customer_id, amount, reason)`: `amount` must be greater than 0, at most 10,000, with at most 2 decimal places. `reason` must be 10 to 500 characters, not counting leading or trailing spaces.

**Run it**

```bash
python -m task1_mcp_server.server
```

To try it in an MCP client such as Claude Desktop or MCP Inspector, set the command to
`python -m task1_mcp_server.server`, with the working directory set to this repo.

**How it works**

* **stdout is only for JSON-RPC.** Logging is set up to write to stderr before anything
  else runs. The SDK also moves `sys.stdout` away from the protocol stream, so a stray
  `print()` cannot break the protocol. A test starts the real process and checks that
  every stdout line is valid JSON-RPC and that the logs show up on stderr.
* **Validation with Pydantic** in strict mode with `extra="forbid"`. That means no silent
  type changes (`"10"` is not accepted as `10`, `true` is not accepted as `1`) and unknown
  fields are rejected. `inf`, `NaN` and amounts with more than 2 decimals are rejected too.
* **Two kinds of errors:**
  * Bad input or an unknown tool is a **protocol error**: JSON-RPC `-32602 Invalid params`.
    The `data` field lists each bad field and why, without echoing the input back.
  * A valid request that fails for a business reason (customer not found) is a normal
    tool result with `isError: true`. This follows the MCP spec, so the model can read
    the reason and react.

---

## Task 2: MCP security gateway

A FastAPI reverse proxy that sits between an agent and an MCP server (`POST /mcp`).

**Run it**

```bash
# terminal 1: mock MCP server
uvicorn task2_mcp_gateway.mock_mcp_server:app --port 9001
# terminal 2: gateway
uvicorn task2_mcp_gateway.gateway:app --port 9000
# terminal 3: make tokens and call it
python -m task2_mcp_gateway.tokens user
python -m task2_mcp_gateway.tokens admin
curl -s localhost:9000/mcp -H "Authorization: Bearer <token>" -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_delete_user","arguments":{}}}'
```

Settings: `DOWNSTREAM_MCP_URL` (default `http://127.0.0.1:9001/mcp`) and
`GATEWAY_JWT_SECRET` (default `dev-secret-change-me`, change it).

**Rules**

1. The Bearer token is a signed JWT (HS256). The gateway checks the signature and expiry,
   then reads the `role` claim. A missing or bad token gets HTTP 401.
2. `tools/list` and all other methods are forwarded without changes.
3. `tools/call` for a tool whose name starts with `admin_` needs `role == "admin"`.
   Anyone else gets `-32001 Unauthorized Tool Call` and **the downstream server is never
   contacted**. Tests check the mock server's request log to prove this.

**Security details**

* **The role cannot be faked.** The signing algorithm is fixed to HS256, so an `alg: none`
  token is rejected, and signatures are compared in constant time.
* **The prefix check is normalized.** `ADMIN_x`, ` admin_x` and full-width Unicode
  lookalikes are all treated as admin tools.
* **What we check is what we forward.** The gateway forwards the JSON it parsed, not the
  raw bytes. It also rejects duplicate keys. Without that, a body like
  `{"name":"get_weather","name":"admin_delete_user"}` could be read one way by the gateway
  and another way by the server.
* **Batches are rejected.** The MCP spec (2025-06-18) removed JSON-RPC batching, and a
  batch is an easy place to hide an admin call.
* The client's `Authorization` header is not passed downstream. Downstream errors come back
  as a generic `-32603` with no hostnames or details.

In production I would verify tokens from the identity provider (RS256 with JWKS) and read
the admin tool list from config, not only from the name prefix.

---

## Task 3: Streaming PII guardrail

An OpenAI-compatible gateway (`POST /v1/chat/completions`, streaming) that forwards to an
LLM and redacts emails, SSNs and credit card numbers as `[REDACTED]` while the answer
streams back.

**Run it**

```bash
uvicorn task3_stream_guardrail.mock_llm:app --port 9101
uvicorn task3_stream_guardrail.gateway:app --port 9100
curl -N localhost:9100/v1/chat/completions -H "content-type: application/json" -d '{"messages":[]}'
```

To use a real provider, set `UPSTREAM_LLM_URL` to any OpenAI-compatible
`/v1/chat/completions` URL and `UPSTREAM_API_KEY` to its key.

**The hard part: PII split across chunks**

The model may send `"email jo"` and then `"hn@example.com"`. Redacting each chunk alone
misses it, and waiting for the full answer ruins time to first token.

The redactor (`redactor.py`) keeps a small buffer and **only holds back the end of the text
that could still turn into a match**:

* Emails never contain spaces, so only the trailing run of email characters can still grow
  into an email.
* Card numbers and SSNs are only digits, spaces and dashes, so only the trailing run of
  those (starting with a digit) can still grow into one.

In normal text that tail is just the last partial word, so text flows out almost right away.
The regexes run over the whole buffer, so word boundaries are checked correctly, but only
matches that end before the cut point are replaced.

**Memory:** the held tail is capped at 254 characters (the longest valid email), so memory
stays flat no matter how long the answer is. The SSE line parser is also chunk-safe, since
a network chunk can cut a `data:` line in half.

**Fewer false positives:** card numbers must pass the Luhn check, and SSNs skip number
ranges that are never issued (000, 666, 900-999). Phone numbers and dates are left alone.

**Tests** include every 2-way split of a sample text, character-by-character feeding, 500
random chunkings (all must equal one-shot redaction), a check that raw PII never appears
in the output even halfway through, a 1 MB input with no spaces to show memory stays
bounded, and a real-socket test showing the first token arrives in under 0.5 s while the
mock model takes about 2 s in total.

---

## Task 4: Rate limiting and fallback router

An OpenAI-compatible gateway (`POST /v1/chat/completions`) with per-tenant token limits
and automatic failover.

**Run it**

```bash
uvicorn task4_router.mock_providers:primary --port 9201
uvicorn task4_router.mock_providers:backup --port 9202
uvicorn task4_router.app:create_app --factory --port 9200
curl -s localhost:9200/v1/chat/completions -H "Authorization: Bearer key-tenant-a" \
  -H "content-type: application/json" -d '{"messages":[{"role":"user","content":"hi"}],"mock_behavior":"slow"}'
```

`mock_behavior` makes the mock primary return `429`, `500`, `400`, or `slow` (sleeps 5 s).
The response header `x-gateway-provider` shows which provider answered.
Demo keys: `key-tenant-a` (50,000 tokens/min) and `key-tenant-b` (5,000 tokens/min).
Settings: `RATE_LIMIT_DB`, `PRIMARY_URL`, `BACKUP_URL`, `PRIMARY_API_KEY`, `BACKUP_API_KEY`.

**Rate limiter** (`limiter.py`)

* **Sliding window log** in an on-disk SQLite file: one row per request
  `(tenant, timestamp, tokens)`. Usage is the sum of the tenant's rows from the last 60
  seconds. Unlike a fixed per-minute counter, there is no burst at the minute boundary.
* **Token-aware:** before calling a model we reserve an estimate (prompt length / 4 plus
  `max_tokens`). After the call, the row is updated to the provider's real
  `usage.total_tokens`. If every provider fails, the reservation is released.
* **No races:** the "sum, then insert" step runs in one `BEGIN IMMEDIATE` transaction, so
  two requests can never both see room and both get in. This holds across several gateway
  processes sharing the file. Inside one process an `asyncio.Lock` makes requests take turns
  on the shared connection. A test fires 200 requests at once at a budget that fits 50,
  and exactly 50 get through.
* **Eviction:** old rows for a tenant are deleted inside the same transaction, and
  `evict_all()` cleans up tenants that went quiet.
* Over the limit returns 429 with a `Retry-After` header set to when enough old usage
  will have expired.

**Failover** (`router.py`)

* The primary has **one total deadline of 3000 ms** (`asyncio.wait_for`). httpx timeouts
  are per step (connect, read and so on), so a provider that sends bytes slowly could run
  far past 3 s without this. When the deadline hits, the primary call is cancelled, not left
  running (a test checks this).
* Failover happens on 429, timeout, connection errors and 5xx. A 4xx such as 400 is not
  retried, because the backup would reject the same bad request.

**Errors**

Every error has the same shape and never includes upstream messages, hostnames or stack traces:

```json
{"error": {"type": "rate_limit_exceeded", "message": "Token rate limit exceeded for this API key", "request_id": "..."}}
```

A catch-all handler turns any unexpected exception into a generic 500. The full details
go to the server log with the same `request_id`, so problems can still be traced.

In production I would store API keys hashed, use a real tokenizer for the estimate, and
move the limiter to Redis if many gateway nodes had to share it.
