"""Call the primary model provider, fall back to the backup if it fails.

Failover happens when the primary:
  * returns HTTP 429 (rate limited), or
  * does not finish within PRIMARY_TIMEOUT_S (3000 ms by default), or
  * cannot be reached, or returns a 5xx.

The 3 s limit is one total deadline (asyncio.wait_for), not httpx's
per-phase timeouts, so a provider that trickles bytes slowly still gets cut
off at 3 s. When the deadline hits, the primary request is cancelled, so
it does not keep running in the background.
"""
import asyncio
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("task4.router")

PRIMARY_TIMEOUT_S = 3.0


@dataclass
class Provider:
    name: str
    url: str
    api_key: str = ""


@dataclass
class RouteResult:
    provider: str
    status: int
    body: dict


class AllProvidersFailed(Exception):
    """Raised with a short, safe reason. Never carries upstream text."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ClientRequestRejected(Exception):
    """The provider said the request itself is bad (4xx other than 429)."""

    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class _Retryable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FallbackRouter:
    def __init__(self, primary: Provider, backup: Provider, client: httpx.AsyncClient,
                 primary_timeout_s: float = PRIMARY_TIMEOUT_S, backup_timeout_s: float = 30.0):
        self.primary = primary
        self.backup = backup
        self.client = client
        self.primary_timeout_s = primary_timeout_s
        self.backup_timeout_s = backup_timeout_s

    async def _call(self, p: Provider, payload: dict) -> RouteResult:
        headers = {"content-type": "application/json"}
        if p.api_key:
            headers["authorization"] = f"Bearer {p.api_key}"
        try:
            r = await self.client.post(p.url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise _Retryable(f"{p.name}: {type(e).__name__}") from None
        if r.status_code == 429:
            raise _Retryable(f"{p.name}: 429")
        if r.status_code >= 500:
            raise _Retryable(f"{p.name}: {r.status_code}")
        if r.status_code >= 400:
            raise ClientRequestRejected(r.status_code)
        try:
            body = r.json()
        except ValueError:
            raise _Retryable(f"{p.name}: bad json") from None
        return RouteResult(p.name, r.status_code, body)

    async def route(self, payload: dict) -> RouteResult:
        try:
            return await asyncio.wait_for(self._call(self.primary, payload), timeout=self.primary_timeout_s)
        except asyncio.TimeoutError:
            log.warning("primary timed out after %.0f ms, failing over", self.primary_timeout_s * 1000)
        except _Retryable as e:
            log.warning("primary failed (%s), failing over", e.reason)

        try:
            return await asyncio.wait_for(self._call(self.backup, payload), timeout=self.backup_timeout_s)
        except asyncio.TimeoutError:
            log.error("backup timed out")
            raise AllProvidersFailed("timeout") from None
        except _Retryable as e:
            log.error("backup failed (%s)", e.reason)
            raise AllProvidersFailed("unavailable") from None
