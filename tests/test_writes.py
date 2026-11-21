"""Write tools: gating, two-step confirmation and the exact requests sent to YNAB."""

from __future__ import annotations

import pytest
import urllib3

from make_cents_please.providers.ynab.confirm import (
    TOKEN_TTL_SECONDS,
    ConfirmationError,
    ConfirmationStore,
)

from .fakes import (
    CHECKING,
    DINING,
    GROCERIES,
    GROUP_FOOD,
    PAYEE_MARKET,
    PLAN,
    SCHED_RENT,
    TXN_GROCERY,
)

PLAN_ARGS = {"plan_id": PLAN, "plan_name": "Test Plan"}


@pytest.fixture
def allow_writes() -> bool:
    return True


@pytest.fixture
def call(call):
    """Like the shared fixture, but write tools get the test plan's id and name by default."""

    async def _call(name, /, **arguments):
        if not name.startswith(("ynab_list", "ynab_get")):
            arguments = {**PLAN_ARGS, **arguments}
        return await call(name, **arguments)

    return _call


async def preview_then_confirm(call, fake, tool, /, **args):
    """Run the two-step flow; assert the preview sent no writes. Returns (preview, result)."""
    writes_before = len(fake.writes())
    is_error, preview = await call(tool, **args)
    assert not is_error, preview
    assert preview["status"] == "confirmation_required"
    assert len(fake.writes()) == writes_before
    is_error, result = await call(tool, **args, confirmation_token=preview["confirmation_token"])
    assert not is_error, result
    assert result["status"] == "done"
    return preview, result


# ---------------------------------------------------------------- gating


async def test_write_tools_absent_by_default(fake):
    from make_cents_please.config import load_config
    from make_cents_please.server import build_server

    server = build_server(load_config({"YNAB_ACCESS_TOKEN": "x"}))
    tools = await server.list_tools()
    assert len(tools) == 21
    assert all(t.annotations.read_only_hint for t in tools)


async def test_write_tools_present_when_enabled(server):
    tools = await server.list_tools()
    writes = [t for t in tools if not t.annotations.read_only_hint]
    assert len(tools) == 37 and len(writes) == 16
    for t in writes:
        assert "confirmation_token" in t.input_schema["properties"]
        assert "Two steps" in t.description
    deletes = {t.name: t.annotations.destructive_hint for t in writes}
    assert deletes["ynab_delete_transaction"] is True
    assert deletes["ynab_create_payee"] is False


def test_allow_writes_must_be_boolean():
    from make_cents_please.config import ConfigError, load_config

    with pytest.raises(ConfigError):
        load_config({"YNAB_ACCESS_TOKEN": "x", "YNAB_ALLOW_WRITES": "maybe"})


# ---------------------------------------------------------------- confirmation


async def test_confirmation_token_is_single_use(call, fake):
    args = {"name": "New Payee"}
    _, preview = await call("ynab_create_payee", **args)
    token = preview["confirmation_token"]
    assert (await call("ynab_create_payee", **args, confirmation_token=token))[0] is False
    is_error, msg = await call("ynab_create_payee", **args, confirmation_token=token)
    assert is_error and "already used" in msg
    assert len(fake.writes()) == 1


async def test_changed_arguments_are_rejected(call, fake):
    _, preview = await call("ynab_create_payee", name="Original")
    is_error, msg = await call(
        "ynab_create_payee", name="Different", confirmation_token=preview["confirmation_token"]
    )
    assert is_error and "differ" in msg
    assert fake.writes() == []


async def test_token_is_bound_to_its_tool(call, fake):
    _, preview = await call("ynab_create_payee", name="Same Name")
    is_error, msg = await call(
        "ynab_create_category_group",
        name="Same Name",
        confirmation_token=preview["confirmation_token"],
    )
    assert is_error and "differ" in msg
    assert fake.writes() == []


async def test_made_up_token_is_rejected(call, fake):
    is_error, msg = await call("ynab_create_payee", name="X", confirmation_token="deadbeef")
    assert is_error and "unknown" in msg
    assert fake.writes() == []


def test_token_expiry():
    now = [0.0]
    store = ConfirmationStore(clock=lambda: now[0])
    token = store.issue("tool", {"a": 1})
    now[0] = TOKEN_TTL_SECONDS + 1
    with pytest.raises(ConfirmationError, match="expired"):
        store.redeem(token, "tool", {"a": 1})


def test_fingerprint_ignores_key_order():
    store = ConfirmationStore()
    token = store.issue("tool", {"a": 1, "b": 2})
    store.redeem(token, "tool", {"b": 2, "a": 1})


async def test_preview_includes_instructions(call):
    _, preview = await call("ynab_create_payee", name="X")
    assert "explicitly agrees" in preview["instructions"]
    assert preview["expires_in_seconds"] == TOKEN_TTL_SECONDS


# ---------------------------------------------------------------- transactions


async def test_create_transaction(call, fake):
    preview, result = await preview_then_confirm(
        call,
        fake,
        "ynab_create_transactions",
        transactions=[
            {
                "account_id": CHECKING,
                "account_name": "Everyday Checking",
                "date": "2026-10-09",
                "amount": -23.45,
                "payee_name": "Bakery",
                "category_id": GROCERIES,
                "category_name": "groceries",
                "memo": "bread",
            }
        ],
    )
    row = preview["preview"]["transactions"][0]
    assert row == {
        "date": "2026-10-09",
        "amount": "-23.45",
        "account": "Everyday Checking",
        "payee": "Bakery",
        "category": "Groceries",
        "memo": "bread",
        "cleared": "uncleared",
        "approved": True,
    }
    write = fake.writes()[0]
    assert write.method == "POST" and write.path.endswith("/transactions")
    assert write.body == {
        "transactions": [
            {
                "account_id": CHECKING,
                "date": "2026-10-09",
                "amount": -23450,
                "payee_name": "Bakery",
                "category_id": GROCERIES,
                "memo": "bread",
                "cleared": "uncleared",
                "approved": True,
            }
        ]
    }
    assert result["created"][0]["amount"] == "-23.45"


async def test_create_split_transaction(call, fake):
    await preview_then_confirm(
        call,
        fake,
        "ynab_create_transactions",
        transactions=[
            {
                "account_id": CHECKING,
                "account_name": "Everyday Checking",
                "date": "2026-10-09",
                "amount": -50,
                "payee_id": PAYEE_MARKET,
                "payee_name": "Corner Market",
                "splits": [
                    {"amount": -30, "category_id": GROCERIES, "category_name": "Groceries"},
                    {
                        "amount": -20,
                        "category_id": DINING,
                        "category_name": "Dining Out",
                        "memo": "lunch",
                    },
                ],
            }
        ],
    )
    sent = fake.writes()[0].body["transactions"][0]
    assert sent["payee_id"] == PAYEE_MARKET and "payee_name" not in sent
    subs = sent["subtransactions"]
    assert subs == [
        {"amount": -30000, "category_id": GROCERIES},
        {"amount": -20000, "category_id": DINING, "memo": "lunch"},
    ]


async def test_split_amounts_must_match(call, fake):
    is_error, msg = await call(
        "ynab_create_transactions",
        transactions=[
            {
                "account_id": CHECKING,
                "account_name": "Everyday Checking",
                "date": "2026-10-09",
                "amount": -50,
                "splits": [{"amount": -30, "category_id": GROCERIES, "category_name": "Groceries"}],
            }
        ],
    )
    assert is_error and "must match" in msg


async def test_unknown_ids_rejected_in_preview(call, fake):
    is_error, msg = await call(
        "ynab_create_transactions",
        transactions=[
            {
                "account_id": "aaaaaaaa-0000-0000-0000-00000000ffff",
                "account_name": "Nope",
                "date": "2026-10-09",
                "amount": -1,
            }
        ],
    )
    assert is_error and "not found" in msg and "ynab_list_accounts" in msg
    assert fake.writes() == []


async def test_update_transaction_sends_full_record(call, fake):
    preview, result = await preview_then_confirm(
        call,
        fake,
        "ynab_update_transaction",
        changes={
            "id": TXN_GROCERY,
            "category_id": DINING,
            "category_name": "Dining Out",
            "flag_color": "red",
        },
    )
    assert preview["preview"]["before"]["category"] == "Groceries"
    assert preview["preview"]["changes"] == {"category": "Dining Out", "flag_color": "red"}
    write = fake.writes()[0]
    assert write.method == "PUT" and write.path.endswith(f"/transactions/{TXN_GROCERY}")
    sent = write.body["transaction"]
    assert sent["category_id"] == DINING and sent["flag_color"] == "red"
    assert sent["amount"] == -125500 and sent["memo"] == "weekly shop"  # unchanged values kept
    assert result["transaction"]["id"] == TXN_GROCERY


async def test_update_transactions_sends_only_changes(call, fake):
    await preview_then_confirm(
        call,
        fake,
        "ynab_update_transactions",
        updates=[{"id": TXN_GROCERY, "approved": True, "memo": ""}],
    )
    write = fake.writes()[0]
    assert write.method == "PATCH"
    assert write.body == {"transactions": [{"id": TXN_GROCERY, "memo": "", "approved": True}]}


async def test_split_transaction_amount_cannot_change(call, fake):
    from .fakes import TXN_SPLIT

    is_error, msg = await call("ynab_update_transaction", changes={"id": TXN_SPLIT, "amount": -10})
    assert is_error and "split transaction" in msg
    await preview_then_confirm(
        call, fake, "ynab_update_transaction", changes={"id": TXN_SPLIT, "memo": "groceries run"}
    )
    sent = fake.writes()[0].body["transaction"]
    assert sent["memo"] == "groceries run" and "category_id" not in sent


async def test_update_needs_some_change(call):
    is_error, msg = await call("ynab_update_transaction", changes={"id": TXN_GROCERY})
    assert is_error and "no fields" in msg


async def test_delete_transaction(call, fake):
    preview, result = await preview_then_confirm(
        call, fake, "ynab_delete_transaction", transaction_id=TXN_GROCERY
    )
    assert preview["preview"]["transaction"]["amount"] == "-125.50"
    assert "Corner Market" in preview["summary"]
    assert fake.writes()[0].method == "DELETE"


async def test_import_transactions(call, fake):
    _, result = await preview_then_confirm(call, fake, "ynab_import_transactions")
    assert result["imported_count"] == 1
    assert fake.writes()[0].path.endswith("/transactions/import")


# ---------------------------------------------------------------- scheduled


async def test_create_scheduled_transaction(call, fake):
    preview, _ = await preview_then_confirm(
        call,
        fake,
        "ynab_create_scheduled_transaction",
        account_id=CHECKING,
        account_name="Everyday Checking",
        date="2026-11-01",
        amount=-15.99,
        frequency="monthly",
        payee_name="Streaming Co",
    )
    assert preview["preview"]["scheduled_transaction"]["account"] == "Everyday Checking"
    assert fake.writes()[0].body == {
        "scheduled_transaction": {
            "account_id": CHECKING,
            "date": "2026-11-01",
            "amount": -15990,
            "frequency": "monthly",
            "payee_name": "Streaming Co",
        }
    }


async def test_update_scheduled_transaction(call, fake):
    preview, _ = await preview_then_confirm(
        call,
        fake,
        "ynab_update_scheduled_transaction",
        scheduled_transaction_id=SCHED_RENT,
        amount=-1250,
    )
    assert preview["preview"]["before"]["amount"] == "-1200.00"
    assert preview["preview"]["after"]["amount"] == "-1250.00"
    write = fake.writes()[0]
    assert write.method == "PUT"
    sent = write.body["scheduled_transaction"]
    assert sent["amount"] == -1250000 and sent["date"] == "2026-11-02"
    assert sent["frequency"] == "monthly"


async def test_delete_scheduled_transaction(call, fake):
    await preview_then_confirm(
        call, fake, "ynab_delete_scheduled_transaction", scheduled_transaction_id=SCHED_RENT
    )
    assert fake.writes()[0].method == "DELETE"


# ---------------------------------------------------------------- budgeting


async def test_set_category_assigned(call, fake):
    preview, result = await preview_then_confirm(
        call,
        fake,
        "ynab_set_category_assigned",
        category_id=GROCERIES,
        category_name="Groceries",
        assigned=450,
        month="2026-10",
    )
    p = preview["preview"]
    assert p["assigned_before"] == "400.00" and p["assigned_after"] == "450.00"
    assert p["available_after"] == "324.50"
    assert p["ready_to_assign_change"] == "-50.00"
    write = fake.writes()[0]
    assert write.method == "PATCH"
    assert write.path.endswith(f"/months/2026-10-01/categories/{GROCERIES}")
    assert write.body == {"category": {"budgeted": 450000}}
    assert result["category"]["budgeted"] == "450.00"


async def test_create_category_omits_unset_goal_fields(call, fake):
    preview, _ = await preview_then_confirm(
        call,
        fake,
        "ynab_create_category",
        name="Gifts",
        category_group_id=GROUP_FOOD,
        category_group_name="Food",
    )
    assert preview["preview"]["category"]["group"] == "Food"
    assert fake.writes()[0].body == {"category": {"name": "Gifts", "category_group_id": GROUP_FOOD}}


async def test_update_category_rename_keeps_goal(call, fake):
    await preview_then_confirm(
        call,
        fake,
        "ynab_update_category",
        category_id=GROCERIES,
        category_name="Groceries",
        new_name="Food & Groceries",
    )
    assert fake.writes()[0].body == {"category": {"name": "Food & Groceries"}}


async def test_update_category_remove_goal_sends_null(call, fake):
    preview, _ = await preview_then_confirm(
        call,
        fake,
        "ynab_update_category",
        category_id=GROCERIES,
        category_name="Groceries",
        remove_goal=True,
    )
    assert preview["preview"]["changes"]["goal_target"] == "removed"
    assert fake.writes()[0].body == {"category": {"goal_target": None}}  # explicit null kept


async def test_update_category_goal(call, fake):
    await preview_then_confirm(
        call,
        fake,
        "ynab_update_category",
        category_id=GROCERIES,
        category_name="Groceries",
        goal_target=600,
        goal_frequency="monthly",
    )
    assert fake.writes()[0].body == {
        "category": {"goal_target": 600000, "goal_frequency": "monthly"}
    }


async def test_goal_frequency_needs_target(call):
    is_error, msg = await call(
        "ynab_update_category",
        category_id=GROCERIES,
        category_name="Groceries",
        goal_frequency="weekly",
    )
    assert is_error and "requires goal_target" in msg


async def test_category_groups(call, fake):
    await preview_then_confirm(call, fake, "ynab_create_category_group", name="Travel")
    preview, _ = await preview_then_confirm(
        call,
        fake,
        "ynab_update_category_group",
        category_group_id=GROUP_FOOD,
        category_group_name="Food",
        new_name="Eating",
    )
    assert preview["preview"] == {
        "plan": "Test Plan",
        "name_before": "Food",
        "name_after": "Eating",
    }
    assert [w.method for w in fake.writes()] == ["POST", "PATCH"]


# ---------------------------------------------------------------- payees / accounts


async def test_payees(call, fake):
    await preview_then_confirm(call, fake, "ynab_create_payee", name="Hardware Store")
    preview, _ = await preview_then_confirm(
        call,
        fake,
        "ynab_update_payee",
        payee_id=PAYEE_MARKET,
        payee_name="Corner Market",
        new_name="Corner Grocer",
    )
    assert "Corner Market" in preview["summary"]
    assert fake.writes()[1].body == {"payee": {"name": "Corner Grocer"}}


async def test_create_account(call, fake):
    preview, result = await preview_then_confirm(
        call, fake, "ynab_create_account", name="Wallet", type="cash", balance=40
    )
    assert preview["preview"]["account"]["balance"] == "40.00"
    assert fake.writes()[0].body == {
        "account": {"name": "Wallet", "type": "cash", "balance": 40000}
    }
    assert result["account"]["name"] == "Wallet"


async def test_empty_name_rejected(call):
    is_error, msg = await call("ynab_create_payee", name="   ")
    assert is_error and "must not be empty" in msg


# ---------------------------------------------------------------- name labels


async def test_plan_name_required_and_checked(call, fake):
    is_error, msg = await call("ynab_create_payee", plan_name="Other Plan", name="X")
    assert is_error and "does not match plan_id" in msg and "Test Plan" in msg
    is_error, msg = await call(
        "ynab_create_payee", plan_id="last-used", plan_name="Test Plan", name="X"
    )
    assert is_error and "ynab_list_plans" in msg
    assert fake.writes() == []


async def test_missing_label_says_what_it_should_be(call, fake):
    is_error, msg = await call(
        "ynab_set_category_assigned", category_id=GROCERIES, category_name=None, assigned=1
    )
    assert is_error  # schema requires category_name
    is_error, msg = await call(
        "ynab_update_transaction",
        changes={"id": TXN_GROCERY, "category_id": DINING},
    )
    assert is_error and "category_name is required" in msg and "'Dining Out'" in msg


async def test_mismatched_label_is_refused(call, fake):
    is_error, msg = await call(
        "ynab_set_category_assigned",
        category_id=GROCERIES,
        category_name="Dining Out",
        assigned=1,
    )
    assert is_error and "does not match category_id" in msg and "'Groceries'" in msg
    assert fake.writes() == []


async def test_label_without_id_is_refused(call):
    is_error, msg = await call(
        "ynab_update_transaction",
        changes={"id": TXN_GROCERY, "category_name": "Dining Out"},
    )
    assert is_error and "category_name was given without category_id" in msg


async def test_payee_name_must_match_payee_id(call, fake):
    is_error, msg = await call(
        "ynab_update_transaction",
        changes={"id": TXN_GROCERY, "payee_id": PAYEE_MARKET, "payee_name": "Someone Else"},
    )
    assert is_error and "does not match payee_id" in msg


def test_name_normalization():
    from make_cents_please.providers.ynab.write_tools import _norm

    assert _norm("Weekend Treats 🍦🎈🎠") == _norm("Weekend Treats")
    assert _norm("🏠 Rent") == _norm("rent")
    assert _norm("❤️ Gifts") == _norm("Gifts")  # emoji with variation selector
    assert _norm("👨‍👩‍👧 Family") == _norm("Family")  # zero-width joiner sequence
    assert _norm("Café") == _norm("CAFÉ")
    assert _norm("Weekend Treats 2") != _norm("Weekend Treats")
    assert _norm("Food & Drink") != _norm("Food Drink")  # punctuation still counts
    assert _norm("🎉") != _norm("🎂")  # symbol-only names still compared


def test_lenient_name_match():
    from make_cents_please.providers.ynab.write_tools import _names_match

    long = "Home Maintenance, Repairs, Appliances, and Other House Things 🏠"
    assert _names_match("Home Maintenance", long)
    assert _names_match("home maintenance, repairs", long)
    assert _names_match(long, long)
    assert not _names_match("Home Maint", long)  # must end on a word boundary
    assert not _names_match("Ve", long)  # too short to mean anything
    assert not _names_match("Registration", long)  # must be how the name begins
    assert not _names_match("Weekend Treats Extra", "Weekend Treats")  # longer than the real name
    assert not _names_match("", "Weekend Treats")


async def test_shortened_name_accepted(call, fake):
    await preview_then_confirm(
        call,
        fake,
        "ynab_set_category_assigned",
        category_id=DINING,
        category_name="Dining",
        assigned=1,
    )


async def test_labels_ignore_case_and_spacing(call, fake):
    await preview_then_confirm(
        call,
        fake,
        "ynab_set_category_assigned",
        plan_name="  test   plan ",
        category_id=GROCERIES,
        category_name="GROCERIES",
        assigned=1,
    )


# ---------------------------------------------------------------- long names


LONG_NAME = "Home Maintenance, Repairs, Appliances, and Other House Things"


@pytest.fixture
def long_named_category(fake):
    from .fakes import CATEGORY_GROUPS

    original = CATEGORY_GROUPS[2]["categories"][0]["name"]
    CATEGORY_GROUPS[2]["categories"][0]["name"] = LONG_NAME
    yield
    CATEGORY_GROUPS[2]["categories"][0]["name"] = original


async def test_long_existing_name_warns_in_preview(call, fake, long_named_category):
    _, preview = await call(
        "ynab_update_category",
        category_id=GROCERIES,
        category_name="Home Maintenance",
        goal_target=100,
    )
    assert "61 characters" in preview["preview"]["changes"]["warning"]


async def test_long_existing_name_error_is_explained(call, fake, long_named_category):
    args = {"category_id": GROCERIES, "category_name": "Home Maintenance", "goal_target": 100}
    _, preview = await call("ynab_update_category", **args)
    fake.respond(
        rf"/categories/{GROCERIES}$",
        400,
        {"error": {"detail": "name is too long (maximum is 50 characters)"}},
    )
    is_error, msg = await call(
        "ynab_update_category", **args, confirmation_token=preview["confirmation_token"]
    )
    assert is_error and "name is too long" in msg and "Rename the category" in msg


async def test_long_new_category_name_rejected_before_sending(call, fake):
    is_error, msg = await call(
        "ynab_create_category",
        name=LONG_NAME,
        category_group_id=GROUP_FOOD,
        category_group_name="Food",
    )
    assert is_error and "at most 50 characters" in msg
    assert fake.writes() == []


async def test_long_group_names_rejected_before_sending(call, fake):
    is_error, msg = await call("ynab_create_category_group", name=LONG_NAME)
    assert is_error and "at most 50 characters" in msg
    is_error, msg = await call(
        "ynab_update_category_group",
        category_group_id=GROUP_FOOD,
        category_group_name="Food",
        new_name=LONG_NAME,
    )
    assert is_error and "at most 50 characters" in msg
    assert fake.writes() == []


# ---------------------------------------------------------------- side effects


async def test_write_clears_cache(call, fake):
    def account_reads():
        return [c for c in fake.calls if c.method == "GET" and c.path.endswith("/accounts")]

    await call("ynab_list_accounts")
    await call("ynab_list_accounts")
    assert len(account_reads()) == 1  # second read served from cache
    await preview_then_confirm(call, fake, "ynab_create_payee", name="Cache Buster")
    await call("ynab_list_accounts")
    assert len(account_reads()) == 2  # cache was cleared by the write


async def test_write_network_error_warns_change_may_have_applied(call, fake):
    _, preview = await call("ynab_create_payee", name="Flaky")
    fake.raise_exc = urllib3.exceptions.NewConnectionError(None, "boom")
    is_error, msg = await call(
        "ynab_create_payee", name="Flaky", confirmation_token=preview["confirmation_token"]
    )
    assert is_error and "may or may not have been applied" in msg


async def test_write_api_error_is_reported(call, fake):
    fake.respond(r"/payees$", 400, {"error": {"detail": "name is invalid"}})
    _, preview = await call("ynab_create_payee", name="Bad")
    is_error, msg = await call(
        "ynab_create_payee", name="Bad", confirmation_token=preview["confirmation_token"]
    )
    assert is_error and "name is invalid" in msg
