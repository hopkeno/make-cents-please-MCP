"""Every tool, called through a real in-process MCP client against mocked YNAB responses."""

from __future__ import annotations

from .fakes import (
    CHECKING,
    DINING,
    GROCERIES,
    LOCATION,
    PAYEE_MARKET,
    PLAN,
    SCHED_RENT,
    TXN_GROCERY,
    TXN_SPLIT,
)


async def test_all_tools_are_read_only(server):
    tools = await server.list_tools()
    assert len(tools) == 21
    for tool in tools:
        assert tool.name.startswith("ynab_")
        assert tool.annotations.read_only_hint is True
        assert tool.description


async def test_get_user(ok):
    assert (await ok("ynab_get_user"))["user"] == {"id": "user-1"}


async def test_list_plans(ok, fake):
    out = await ok("ynab_list_plans")
    assert out["plans"][0]["id"] == PLAN
    assert out["plans"][0]["currency"] == "USD"
    assert "accounts" not in out["plans"][0]
    out = await ok("ynab_list_plans", include_accounts=True)
    assert len(out["plans"][0]["accounts"]) == 4
    assert fake.calls[-1].query == {"include_accounts": ["true"]}


async def test_default_plan_is_last_used_and_explicit_plan_is_used(ok, fake):
    await ok("ynab_get_plan_settings")
    assert fake.calls[-1].path == "/plans/last-used/settings"
    out = await ok("ynab_get_plan_settings", plan_id=PLAN)
    assert fake.calls[-1].path == f"/plans/{PLAN}/settings"
    assert out["settings"]["currency_format"]["iso_code"] == "USD"


async def test_plan_overview_is_compact(ok):
    out = await ok("ynab_get_plan_overview")
    assert out["name"] == "Test Plan"
    assert out["open_accounts"] == 3
    assert out["on_budget_balance"] == "6273.33"
    assert out["counts"]["transactions"] == 4
    assert "transactions" not in out


async def test_list_accounts_filters(ok):
    out = await ok("ynab_list_accounts")
    assert [a["name"] for a in out["items"]] == [
        "Everyday Checking",
        "Rainy Day Savings",
        "Test Rewards Card",
    ]
    assert out["items"][2]["balance"] == "-250.12"
    out = await ok("ynab_list_accounts", include_closed=True)
    assert out["total"] == 4
    out = await ok("ynab_list_accounts", account_type="creditcard")
    assert [a["name"] for a in out["items"]] == ["Test Rewards Card"]
    out = await ok("ynab_list_accounts", limit=1, offset=1)
    assert out["count"] == 1 and out["next_offset"] == 2


async def test_get_account(ok):
    out = await ok("ynab_get_account", account_id=CHECKING)
    assert out["account"]["name"] == "Everyday Checking"


async def test_list_categories_excludes_hidden_and_internal(ok):
    out = await ok("ynab_list_categories")
    names = [c["name"] for c in out["items"]]
    assert names == ["Rent", "Groceries", "Dining Out"]
    out = await ok("ynab_list_categories", include_hidden=True)
    assert "Old Hobby" in [c["name"] for c in out["items"]]


async def test_list_categories_filters(ok):
    out = await ok("ynab_list_categories", group="food")
    assert [c["name"] for c in out["items"]] == ["Groceries", "Dining Out"]
    out = await ok("ynab_list_categories", only_overspent=True)
    assert [c["name"] for c in out["items"]] == ["Dining Out"]
    assert out["items"][0]["balance"] == "-42.25"
    out = await ok("ynab_list_categories", only_underfunded=True)
    assert [c["name"] for c in out["items"]] == ["Groceries"]
    assert out["items"][0]["goal_under_funded"] == "100.00"
    out = await ok("ynab_list_categories", name_contains="RENT")
    assert [c["name"] for c in out["items"]] == ["Rent"]


async def test_list_categories_for_month_uses_month_endpoint(ok, fake):
    out = await ok("ynab_list_categories", month="2026-09")
    assert fake.calls[-1].path == "/plans/last-used/months/2026-09-01"
    assert out["month"] == "2026-09"
    assert {c["group"] for c in out["items"]} == {"Bills", "Food"}


async def test_get_category(ok, fake):
    out = await ok("ynab_get_category", category_id=GROCERIES)
    assert out["category"]["budgeted"] == "400.00"
    out = await ok("ynab_get_category", category_id=GROCERIES, month="2026-09")
    assert fake.calls[-1].path.endswith(f"/months/2026-09-01/categories/{GROCERIES}")
    assert out["category"]["budgeted"] == "350.00"


async def test_list_months(ok):
    out = await ok("ynab_list_months")
    assert [m["month"] for m in out["items"]] == ["2026-10-01", "2026-09-01", "2026-08-01"]
    out = await ok("ynab_list_months", since_month="2026-09", until_month="2026-09")
    assert [m["month"] for m in out["items"]] == ["2026-09-01"]
    assert out["items"][0]["to_be_budgeted"] == "100.00"


async def test_get_month(ok):
    out = await ok("ynab_get_month", month="2026-10")
    assert out["month"]["note"] == "Test note"
    names = [c["name"] for c in out["categories"]]
    assert "Old Hobby" not in names  # hidden
    assert "Inflow: Ready to Assign" not in names  # internal
    out = await ok("ynab_get_month", month="2026-10", only_overspent=True)
    assert [c["name"] for c in out["categories"]] == ["Dining Out"]
    out = await ok("ynab_get_month", month="2026-10", include_categories=False)
    assert "categories" not in out


async def test_list_payees(ok):
    out = await ok("ynab_list_payees")
    assert [p["name"] for p in out["items"]] == [
        "Acme Payroll",
        "Corner Market",
        "Example Properties",
    ]
    out = await ok("ynab_list_payees", include_transfers=True, name_contains="transfer")
    assert out["items"][0]["transfer_account_id"]


async def test_get_payee(ok):
    assert (await ok("ynab_get_payee", payee_id=PAYEE_MARKET))["payee"]["name"] == "Corner Market"


async def test_payee_locations(ok, fake):
    out = await ok("ynab_list_payee_locations")
    assert out["items"][0]["id"] == LOCATION
    await ok("ynab_list_payee_locations", payee_id=PAYEE_MARKET)
    assert fake.calls[-1].path.endswith(f"/payees/{PAYEE_MARKET}/payee_locations")
    out = await ok("ynab_get_payee_location", payee_location_id=LOCATION)
    assert out["payee_location"]["payee_id"] == PAYEE_MARKET


async def test_list_transactions_defaults_and_totals(ok, fake):
    out = await ok("ynab_list_transactions", since_date="2026-10-01")
    assert fake.calls[-1].path == "/plans/last-used/transactions"
    assert fake.calls[-1].query["since_date"] == ["2026-10-01"]
    assert [t["date"] for t in out["items"]] == [
        "2026-10-06",
        "2026-10-05",
        "2026-10-02",
        "2026-10-01",
    ]
    assert out["totals"] == {"inflow": "3000.00", "outflow": "-1467.75", "net": "1532.25"}
    split = out["items"][0]
    assert split["id"] == TXN_SPLIT
    assert [s["amount"] for s in split["splits"]] == ["-100.00", "-42.25"]


async def test_list_transactions_defaults_to_recent_window(ok, fake):
    out = await ok("ynab_list_transactions")
    assert "since_date" in fake.calls[-1].query
    assert out["since_date"] == fake.calls[-1].query["since_date"][0]


async def test_list_transactions_client_side_filters(ok):
    base = {"since_date": "2026-10-01"}
    out = await ok("ynab_list_transactions", **base, direction="inflow")
    assert [t["payee"] for t in out["items"]] == ["Acme Payroll"]
    out = await ok("ynab_list_transactions", **base, min_amount=1000)
    assert {t["payee"] for t in out["items"]} == {"Acme Payroll", "Example Properties"}
    out = await ok("ynab_list_transactions", **base, max_amount=130)
    assert [t["id"] for t in out["items"]] == [TXN_GROCERY]
    out = await ok("ynab_list_transactions", **base, text="snacks")
    assert [t["id"] for t in out["items"]] == [TXN_SPLIT]
    out = await ok("ynab_list_transactions", **base, cleared="uncleared")
    assert [t["id"] for t in out["items"]] == [TXN_GROCERY]
    out = await ok("ynab_list_transactions", **base, until_date="2026-10-02")
    assert len(out["items"]) == 2


async def test_list_transactions_routes_to_scoped_endpoints(ok, fake):
    await ok("ynab_list_transactions", account_id=CHECKING, since_date="2026-10-01")
    assert fake.calls[-1].path.endswith(f"/accounts/{CHECKING}/transactions")

    out = await ok("ynab_list_transactions", category_id=DINING, since_date="2026-10-01")
    assert fake.calls[-1].path.endswith(f"/categories/{DINING}/transactions")
    assert sorted(t["amount"] for t in out["items"]) == ["-100.00", "-42.25"]
    assert all(t["parent_transaction_id"] == TXN_SPLIT for t in out["items"])

    await ok("ynab_list_transactions", payee_id=PAYEE_MARKET, since_date="2026-10-01")
    assert fake.calls[-1].path.endswith(f"/payees/{PAYEE_MARKET}/transactions")

    out = await ok("ynab_list_transactions", month="2026-10")
    assert fake.calls[-1].path.endswith("/months/2026-10-01/transactions")
    assert out["since_date"] == "2026-10-01" and out["until_date"] == "2026-10-31"


async def test_list_transactions_status_passed_to_api(ok, fake):
    await ok("ynab_list_transactions", status="unapproved", since_date="2026-10-01")
    assert fake.calls[-1].query["type"] == ["unapproved"]


async def test_get_transaction(ok):
    out = await ok("ynab_get_transaction", transaction_id=TXN_GROCERY)
    t = out["transaction"]
    assert t["amount"] == "-125.50"
    assert t["account_id"] == CHECKING
    assert t["memo"] == "weekly shop"


async def test_scheduled_transactions(ok):
    out = await ok("ynab_list_scheduled_transactions")
    assert [s["date_next"] for s in out["items"]] == ["2026-10-15", "2026-11-02"]
    out = await ok("ynab_list_scheduled_transactions", due_before="2026-10-31")
    assert len(out["items"]) == 1
    out = await ok("ynab_list_scheduled_transactions", account_id=CHECKING)
    assert [s["id"] for s in out["items"]] == [SCHED_RENT]
    out = await ok("ynab_get_scheduled_transaction", scheduled_transaction_id=SCHED_RENT)
    assert out["scheduled_transaction"]["frequency"] == "monthly"


async def test_money_movements(ok, fake):
    out = await ok("ynab_list_money_movements")
    row = out["items"][0]
    assert row["from_category"] == "Groceries" and row["to_category"] == "Dining Out"
    assert row["amount"] == "50.00"
    await ok("ynab_list_money_movements", month="2026-10")
    assert fake.calls[-1].path.endswith("/months/2026-10-01/money_movements")


async def test_money_movement_groups(ok, fake):
    out = await ok("ynab_list_money_movement_groups")
    assert out["items"][0]["note"] == "cover dining"
    await ok("ynab_list_money_movement_groups", month="2026-10")
    assert fake.calls[-1].path.endswith("/months/2026-10-01/money_movement_groups")


async def test_api_status_makes_no_request(ok, fake):
    out = await ok("ynab_get_api_status")
    assert out["requests_used_this_hour"] == 0
    assert fake.calls == []
