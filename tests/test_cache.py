"""In-memory caching and server_knowledge delta merging."""

from __future__ import annotations

from make_cents_please.providers.ynab.cache import merge_entities

from .fakes import ACCOUNTS, CATEGORY_GROUPS, DINING, GROCERIES, TRANSACTIONS


def test_merge_entities_replaces_adds_and_removes_deleted():
    old = [{"id": "a", "v": 1}, {"id": "b", "v": 1}]
    new = [{"id": "a", "v": 2}, {"id": "b", "deleted": True}, {"id": "c", "v": 1}]
    assert merge_entities(old, new) == [{"id": "a", "v": 2}, {"id": "c", "v": 1}]


def test_merge_entities_nested_children():
    old = [{"id": "g", "categories": [{"id": "x", "v": 1}, {"id": "y", "v": 1}]}]
    new = [{"id": "g", "categories": [{"id": "y", "v": 2}]}]
    merged = merge_entities(old, new, {"categories": "categories"})
    assert merged[0]["categories"] == [{"id": "x", "v": 1}, {"id": "y", "v": 2}]


def test_merge_entities_months_keyed_by_month():
    old = [{"month": "2026-09-01", "v": 1}]
    new = [{"month": "2026-09-01", "v": 2}, {"month": "2026-10-01", "v": 1}]
    assert [m["v"] for m in merge_entities(old, new)] == [2, 1]


async def test_fresh_cache_avoids_requests(ok, fake):
    await ok("ynab_list_accounts")
    await ok("ynab_list_accounts", include_closed=True)
    await ok("ynab_get_api_status")
    assert fake.paths() == ["/plans/last-used/accounts"]


async def test_stale_cache_uses_delta_and_merges(ok, fake, clock):
    await ok("ynab_list_accounts")
    assert "last_knowledge_of_server" not in fake.calls[-1].query

    clock.advance(301)
    changed = {**ACCOUNTS[0], "balance": 999_000}
    deleted = {**ACCOUNTS[1], "deleted": True}
    fake.set_data("/accounts$", {"accounts": [changed, deleted], "server_knowledge": 11})
    out = await ok("ynab_list_accounts")

    assert fake.calls[-1].query["last_knowledge_of_server"] == ["10"]
    by_name = {a["name"]: a for a in out["items"]}
    assert by_name["Everyday Checking"]["balance"] == "999.00"
    assert "Rainy Day Savings" not in by_name
    assert "Test Rewards Card" in by_name  # unchanged entity kept from cache

    clock.advance(301)
    fake.set_data("/accounts$", {"accounts": [], "server_knowledge": 11})
    await ok("ynab_list_accounts")
    assert fake.calls[-1].query["last_knowledge_of_server"] == ["11"]


async def test_category_delta_merges_nested_categories(ok, fake, clock):
    await ok("ynab_list_categories")
    clock.advance(301)
    food = CATEGORY_GROUPS[2]
    dining = next(c for c in food["categories"] if c["id"] == DINING)
    fake.set_data(
        "/categories$",
        {
            "category_groups": [
                {**food, "categories": [{**dining, "balance": 5_000}]},
            ],
            "server_knowledge": 21,
        },
    )
    out = await ok("ynab_list_categories", group="food")
    rows = {c["name"]: c for c in out["items"]}
    assert rows["Dining Out"]["balance"] == "5.00"
    assert rows["Groceries"]["balance"] == "274.50"  # untouched sibling survives the merge
    assert any(c["id"] == GROCERIES for c in out["items"])


async def test_transaction_delta_removes_deleted(ok, fake, clock):
    out = await ok("ynab_list_transactions", since_date="2026-10-01")
    assert out["total"] == 4
    clock.advance(301)
    fake.set_data(
        "/transactions$",
        lambda q: {"transactions": [{**TRANSACTIONS[0], "deleted": True}], "server_knowledge": 101},
    )
    out = await ok("ynab_list_transactions", since_date="2026-10-01")
    assert fake.calls[-1].query["last_knowledge_of_server"] == ["100"]
    assert out["total"] == 3


async def test_plan_export_delta(ok, fake, clock):
    await ok("ynab_get_plan_overview")
    clock.advance(301)
    fake.set_data(
        ")$",  # the full plan export route
        {
            "plan": {
                "id": "x",
                "name": "Test Plan",
                "accounts": [{**ACCOUNTS[1], "deleted": True}],
            },
            "server_knowledge": 501,
        },
    )
    out = await ok("ynab_get_plan_overview")
    assert fake.calls[-1].query["last_knowledge_of_server"] == ["500"]
    assert out["counts"]["accounts"] == 3
    assert out["counts"]["transactions"] == 4


def test_cache_evicts_oldest_entries():
    from make_cents_please.providers.ynab.cache import ResponseCache

    cache = ResponseCache(ttl_seconds=60, clock=lambda: 0.0, max_entries=2)
    cache.put("a", {})
    cache.put("b", {})
    cache.put("a", {})
    cache.put("c", {})
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("c") is not None
