"""Rate-limit tracking and stale-cache fallback."""

from __future__ import annotations

from make_cents_please.providers.ynab.client import RateTracker


def test_header_parsing():
    rate = RateTracker(clock=lambda: 0.0)
    rate.update_from_header("36/200")
    assert rate.used() == 36 and rate.limit() == 200
    rate.update_from_header("garbage")
    assert rate.used() == 36


def test_sliding_window():
    now = [0.0]
    rate = RateTracker(clock=lambda: now[0])
    for _ in range(3):
        rate.record()
    assert rate.used() == 3
    now[0] = 3601
    assert rate.used() == 0


async def test_header_count_reported(ok, fake):
    fake.rate_header = "42/200"
    await ok("ynab_list_accounts")
    status = await ok("ynab_get_api_status")
    assert status["requests_used_this_hour"] == 42


async def test_exhausted_without_cache_errors_without_request(call, fake, ynab_client):
    ynab_client.rate.update_from_header("200/200")
    is_error, msg = await call("ynab_list_accounts")
    assert is_error and "rate limit" in msg.lower() and "minutes" in msg
    assert fake.calls == []


async def test_near_limit_serves_stale_cache(ok, fake, clock):
    await ok("ynab_list_accounts")
    clock.advance(301)
    fake.rate_header = "185/200"
    await ok("ynab_list_payees")  # updates header-reported usage
    calls_before = len(fake.calls)
    out = await ok("ynab_list_accounts")
    assert len(fake.calls) == calls_before
    assert "cached data" in out["warning"]
    assert out["total"] == 3


async def test_429_falls_back_to_cache(ok, fake, clock):
    await ok("ynab_list_accounts")
    clock.advance(301)
    fake.respond(r"/accounts$", 429, {"error": {"id": "429", "name": "too_many_requests"}})
    out = await ok("ynab_list_accounts")
    assert "cached data" in out["warning"]


async def test_429_without_cache(call, fake):
    fake.respond(r"/accounts$", 429, {"error": {"id": "429", "name": "too_many_requests"}})
    is_error, msg = await call("ynab_list_accounts")
    assert is_error and "429" in msg
