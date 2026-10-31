import asyncio

import pytest

from task4_router.limiter import SlidingWindowLimiter
from task4_router.tests.conftest import FakeClock


@pytest.fixture
async def limiter(tmp_path):
    clock = FakeClock()
    lim = SlidingWindowLimiter(str(tmp_path / "t.db"), window_s=60, clock=clock)
    await lim.open()
    lim.test_clock = clock
    yield lim
    await lim.close()


async def test_allows_until_limit_then_blocks(limiter):
    assert (await limiter.reserve("a", 30_000, 50_000)).allowed
    assert (await limiter.reserve("a", 20_000, 50_000)).allowed  # exactly at the limit is fine
    d = await limiter.reserve("a", 1, 50_000)
    assert not d.allowed and d.used == 50_000


async def test_tenants_are_isolated(limiter):
    assert (await limiter.reserve("a", 50_000, 50_000)).allowed
    assert (await limiter.reserve("b", 50_000, 50_000)).allowed


async def test_window_slides(limiter):
    c = limiter.test_clock
    await limiter.reserve("a", 30_000, 50_000)
    c.t += 30
    await limiter.reserve("a", 20_000, 50_000)
    assert not (await limiter.reserve("a", 10_000, 50_000)).allowed
    c.t += 30.001  # the first 30k is now older than 60 s
    assert (await limiter.reserve("a", 10_000, 50_000)).allowed
    assert await limiter.used("a") == 30_000


async def test_retry_after_points_at_when_room_opens(limiter):
    c = limiter.test_clock
    await limiter.reserve("a", 40_000, 50_000)
    c.t += 20
    await limiter.reserve("a", 10_000, 50_000)
    d = await limiter.reserve("a", 5_000, 50_000)
    assert not d.allowed
    assert d.retry_after_s == pytest.approx(40.0)  # first row expires 40 s from now


async def test_request_bigger_than_limit_never_fits(limiter):
    d = await limiter.reserve("a", 60_000, 50_000)
    assert not d.allowed and d.retry_after_s == 60


async def test_expired_rows_are_evicted_from_disk(limiter):
    c = limiter.test_clock
    for _ in range(10):
        await limiter.reserve("a", 100, 50_000)
    c.t += 61
    await limiter.reserve("a", 100, 50_000)  # triggers per-tenant eviction
    async with limiter._db.execute("SELECT COUNT(*) FROM token_usage WHERE tenant='a'") as cur:
        assert (await cur.fetchone())[0] == 1
    await limiter.reserve("b", 100, 50_000)
    c.t += 61
    assert await limiter.evict_all() == 2


async def test_reconcile_and_release(limiter):
    d = await limiter.reserve("a", 1_000, 50_000)
    await limiter.reconcile(d.reservation_id, 37)
    assert await limiter.used("a") == 37
    await limiter.release(d.reservation_id)
    assert await limiter.used("a") == 0


async def test_concurrent_requests_cannot_overshoot(limiter):
    # 200 requests of 1,000 tokens race for a 50,000 budget: exactly 50 may win.
    results = await asyncio.gather(*[limiter.reserve("a", 1_000, 50_000) for _ in range(200)])
    assert sum(r.allowed for r in results) == 50
    assert await limiter.used("a") == 50_000


async def test_two_processes_sharing_the_file_cannot_overshoot(tmp_path):
    # Two separate connections stand in for two gateway workers.
    clock = FakeClock()
    path = str(tmp_path / "shared.db")
    l1, l2 = SlidingWindowLimiter(path, clock=clock), SlidingWindowLimiter(path, clock=clock)
    await l1.open()
    await l2.open()
    try:
        results = await asyncio.gather(*[(l1 if i % 2 else l2).reserve("a", 1_000, 10_000) for i in range(60)])
        assert sum(r.allowed for r in results) == 10
    finally:
        await l1.close()
        await l2.close()


async def test_state_survives_restart(tmp_path):
    clock = FakeClock()
    path = str(tmp_path / "p.db")
    lim = SlidingWindowLimiter(path, clock=clock)
    await lim.open()
    await lim.reserve("a", 45_000, 50_000)
    await lim.close()
    lim2 = SlidingWindowLimiter(path, clock=clock)
    await lim2.open()
    assert not (await lim2.reserve("a", 10_000, 50_000)).allowed
    await lim2.close()
