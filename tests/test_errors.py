"""API failures become short, actionable tool errors."""

from __future__ import annotations

import urllib3

from .conftest import TOKEN
from .fakes import CHECKING


async def test_401_explains_token_problem(call, fake):
    fake.respond(
        r"/accounts$",
        401,
        {"error": {"id": "401", "name": "unauthorized", "detail": "Unauthorized"}},
    )
    is_error, msg = await call("ynab_list_accounts")
    assert is_error
    assert "YNAB_ACCESS_TOKEN" in msg
    assert TOKEN not in msg


async def test_404_points_to_list_tools(call, fake):
    is_error, msg = await call(
        "ynab_get_account", account_id="aaaaaaaa-0000-0000-0000-00000000ffff"
    )
    assert is_error
    assert "list_accounts" in msg and "Resource not found" in msg


async def test_400_includes_detail(call, fake):
    fake.respond(
        r"/transactions$",
        400,
        {"error": {"id": "400", "name": "bad_request", "detail": "since_date is invalid"}},
    )
    is_error, msg = await call("ynab_list_transactions", since_date="2026-10-01")
    assert is_error and "since_date is invalid" in msg


async def test_500_suggests_retry(call, fake):
    fake.respond(r"/payees$", 503, {})
    is_error, msg = await call("ynab_list_payees")
    assert is_error and "try again" in msg


async def test_network_error(call, fake):
    fake.raise_exc = urllib3.exceptions.NewConnectionError(None, "boom")
    is_error, msg = await call("ynab_list_payees")
    assert is_error and "network" in msg


async def test_invalid_uuid_rejected_before_request(call, fake):
    is_error, msg = await call("ynab_get_account", account_id="not-an-id")
    assert is_error and "account_id" in msg
    assert fake.calls == []


async def test_bad_dates_and_months(call, fake):
    is_error, msg = await call("ynab_list_transactions", since_date="10/01/2026")
    assert is_error and "YYYY-MM-DD" in msg
    is_error, msg = await call("ynab_get_month", month="October")
    assert is_error and "YYYY-MM" in msg
    is_error, msg = await call(
        "ynab_list_transactions", since_date="2026-10-05", until_date="2026-10-01"
    )
    assert is_error and "before" in msg
    assert fake.calls == []


async def test_limit_out_of_range(call):
    is_error, _ = await call("ynab_list_accounts", limit=0)
    assert is_error


async def test_error_detail_is_redacted(call, fake):
    fake.respond(r"/accounts$", 400, {"error": {"detail": f"bad token {TOKEN}"}})
    is_error, msg = await call("ynab_list_accounts")
    assert is_error and TOKEN not in msg


async def test_get_account_ok_after_errors(ok):
    assert (await ok("ynab_get_account", account_id=CHECKING))["account"]["type"] == "checking"
