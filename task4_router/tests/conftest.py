import httpx
import pytest

from task4_router.app import create_app
from task4_router.mock_providers import make_provider
from task4_router.router import Provider


class HostRouter(httpx.AsyncBaseTransport):
    """Send each request to an in-process ASGI app chosen by host name."""

    def __init__(self, apps: dict):
        self.transports = {host: httpx.ASGITransport(app=a) for host, a in apps.items()}

    async def handle_async_request(self, request):
        return await self.transports[request.url.host].handle_async_request(request)


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def providers():
    return make_provider("primary"), make_provider("backup")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def make_gateway(tmp_path, providers, clock):
    primary_app, backup_app = providers
    opened = []

    async def _make(primary_timeout_s=0.3, tenants=None):
        app = create_app(
            db_path=str(tmp_path / "limits.db"),
            primary=Provider("primary", "http://primary/v1/chat/completions"),
            backup=Provider("backup", "http://backup/v1/chat/completions"),
            tenants=tenants or {"key-a": {"tenant": "a", "tokens_per_minute": 1000},
                                "key-b": {"tenant": "b", "tokens_per_minute": 1000}},
            transport=HostRouter({"primary": primary_app, "backup": backup_app}),
            primary_timeout_s=primary_timeout_s,
            clock=clock,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()  # opens the SQLite file and the HTTP client
        # raise_app_exceptions=False: behave like a real server and return the 500 response
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://gw")
        opened.append((lifespan, client))
        return app, client

    yield _make
    for lifespan, client in opened:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)
