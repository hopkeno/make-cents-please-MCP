"""A fake YNAB HTTP layer plus synthetic fixture data.

All names, ids and amounts below are invented. No real budget data belongs in this repo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

PLAN = "11111111-1111-1111-1111-111111111111"
CHECKING = "aaaaaaaa-0000-0000-0000-000000000001"
SAVINGS = "aaaaaaaa-0000-0000-0000-000000000002"
CARD = "aaaaaaaa-0000-0000-0000-000000000003"
OLD = "aaaaaaaa-0000-0000-0000-000000000004"
GROCERIES = "cccccccc-0000-0000-0000-000000000001"
DINING = "cccccccc-0000-0000-0000-000000000002"
RENT = "cccccccc-0000-0000-0000-000000000003"
HIDDEN_CAT = "cccccccc-0000-0000-0000-000000000004"
RTA = "cccccccc-0000-0000-0000-000000000099"
GROUP_BILLS = "9999aaaa-0000-0000-0000-000000000001"
GROUP_FOOD = "9999aaaa-0000-0000-0000-000000000002"
GROUP_INTERNAL = "9999aaaa-0000-0000-0000-000000000099"
PAYEE_MARKET = "bbbbbbbb-0000-0000-0000-000000000001"
PAYEE_LANDLORD = "bbbbbbbb-0000-0000-0000-000000000002"
PAYEE_EMPLOYER = "bbbbbbbb-0000-0000-0000-000000000003"
PAYEE_TRANSFER = "bbbbbbbb-0000-0000-0000-000000000004"
TXN_GROCERY = "dddddddd-0000-0000-0000-000000000001"
TXN_RENT = "dddddddd-0000-0000-0000-000000000002"
TXN_PAY = "dddddddd-0000-0000-0000-000000000003"
TXN_SPLIT = "dddddddd-0000-0000-0000-000000000004"
SCHED_RENT = "eeeeeeee-0000-0000-0000-000000000001"
SCHED_GYM = "eeeeeeee-0000-0000-0000-000000000002"
LOCATION = "ffffffff-0000-0000-0000-000000000001"

CURRENCY = {
    "iso_code": "USD",
    "example_format": "123,456.78",
    "decimal_digits": 2,
    "decimal_separator": ".",
    "symbol_first": True,
    "group_separator": ",",
    "currency_symbol": "$",
    "display_symbol": True,
}


def account(id_, name, type_, balance, on_budget=True, closed=False, deleted=False):
    return {
        "id": id_,
        "name": name,
        "type": type_,
        "on_budget": on_budget,
        "closed": closed,
        "note": None,
        "balance": balance,
        "cleared_balance": balance,
        "uncleared_balance": 0,
        "transfer_payee_id": None,
        "direct_import_linked": False,
        "direct_import_in_error": False,
        "deleted": deleted,
    }


ACCOUNTS = [
    account(CHECKING, "Everyday Checking", "checking", 1_523_450),
    account(SAVINGS, "Rainy Day Savings", "savings", 5_000_000),
    account(CARD, "Test Rewards Card", "creditCard", -250_120),
    account(OLD, "Old Account", "checking", 0, closed=True),
]


def category(id_, name, group_id, group_name, budgeted, activity, balance, **extra):
    cat = {
        "id": id_,
        "category_group_id": group_id,
        "category_group_name": group_name,
        "name": name,
        "hidden": False,
        "internal": False,
        "note": None,
        "budgeted": budgeted,
        "activity": activity,
        "balance": balance,
        "goal_type": None,
        "goal_target": None,
        "goal_under_funded": None,
        "deleted": False,
    }
    cat.update(extra)
    return cat


CATEGORY_GROUPS = [
    {
        "id": GROUP_INTERNAL,
        "name": "Internal Master Category",
        "hidden": False,
        "internal": True,
        "deleted": False,
        "categories": [
            category(
                RTA,
                "Inflow: Ready to Assign",
                GROUP_INTERNAL,
                "Internal Master Category",
                0,
                3_000_000,
                1_000_000,
                internal=True,
            ),
        ],
    },
    {
        "id": GROUP_BILLS,
        "name": "Bills",
        "hidden": False,
        "internal": False,
        "deleted": False,
        "categories": [
            category(
                RENT,
                "Rent",
                GROUP_BILLS,
                "Bills",
                1_200_000,
                -1_200_000,
                0,
                goal_type="NEED",
                goal_target=1_200_000,
                goal_under_funded=0,
            ),
        ],
    },
    {
        "id": GROUP_FOOD,
        "name": "Food",
        "hidden": False,
        "internal": False,
        "deleted": False,
        "categories": [
            category(
                GROCERIES,
                "Groceries",
                GROUP_FOOD,
                "Food",
                400_000,
                -125_500,
                274_500,
                goal_type="MF",
                goal_target=500_000,
                goal_under_funded=100_000,
            ),
            category(DINING, "Dining Out", GROUP_FOOD, "Food", 100_000, -142_250, -42_250),
            category(HIDDEN_CAT, "Old Hobby", GROUP_FOOD, "Food", 0, 0, 0, hidden=True),
        ],
    },
]


def all_categories():
    return [c for g in CATEGORY_GROUPS for c in g["categories"]]


MONTHS = [
    {
        "month": "2026-08-01",
        "note": None,
        "income": 3_000_000,
        "budgeted": 2_800_000,
        "activity": -2_600_000,
        "to_be_budgeted": 200_000,
        "age_of_money": 30,
        "deleted": False,
    },
    {
        "month": "2026-09-01",
        "note": None,
        "income": 3_000_000,
        "budgeted": 2_900_000,
        "activity": -2_700_000,
        "to_be_budgeted": 100_000,
        "age_of_money": 32,
        "deleted": False,
    },
    {
        "month": "2026-10-01",
        "note": "Test note",
        "income": 3_000_000,
        "budgeted": 1_700_000,
        "activity": -1_467_750,
        "to_be_budgeted": 1_000_000,
        "age_of_money": 33,
        "deleted": False,
    },
]

PAYEES = [
    {"id": PAYEE_MARKET, "name": "Corner Market", "transfer_account_id": None, "deleted": False},
    {
        "id": PAYEE_LANDLORD,
        "name": "Example Properties",
        "transfer_account_id": None,
        "deleted": False,
    },
    {"id": PAYEE_EMPLOYER, "name": "Acme Payroll", "transfer_account_id": None, "deleted": False},
    {
        "id": PAYEE_TRANSFER,
        "name": "Transfer : Rainy Day Savings",
        "transfer_account_id": SAVINGS,
        "deleted": False,
    },
]

PAYEE_LOCATIONS = [
    {
        "id": LOCATION,
        "payee_id": PAYEE_MARKET,
        "latitude": "0.0",
        "longitude": "0.0",
        "deleted": False,
    },
]


def txn(
    id_,
    date,
    amount,
    payee_id,
    payee_name,
    category_id,
    category_name,
    memo=None,
    cleared="cleared",
    subtransactions=None,
    deleted=False,
):
    return {
        "id": id_,
        "date": date,
        "amount": amount,
        "memo": memo,
        "cleared": cleared,
        "approved": True,
        "flag_color": None,
        "flag_name": None,
        "account_id": CHECKING,
        "account_name": "Everyday Checking",
        "payee_id": payee_id,
        "payee_name": payee_name,
        "category_id": category_id,
        "category_name": category_name,
        "transfer_account_id": None,
        "transfer_transaction_id": None,
        "matched_transaction_id": None,
        "import_id": None,
        "import_payee_name": None,
        "import_payee_name_original": None,
        "debt_transaction_type": None,
        "deleted": deleted,
        "subtransactions": subtransactions or [],
    }


TRANSACTIONS = [
    txn(
        TXN_PAY,
        "2026-10-01",
        3_000_000,
        PAYEE_EMPLOYER,
        "Acme Payroll",
        RTA,
        "Inflow: Ready to Assign",
    ),
    txn(TXN_RENT, "2026-10-02", -1_200_000, PAYEE_LANDLORD, "Example Properties", RENT, "Rent"),
    txn(
        TXN_GROCERY,
        "2026-10-05",
        -125_500,
        PAYEE_MARKET,
        "Corner Market",
        GROCERIES,
        "Groceries",
        memo="weekly shop",
        cleared="uncleared",
    ),
    txn(
        TXN_SPLIT,
        "2026-10-06",
        -142_250,
        PAYEE_MARKET,
        "Corner Market",
        None,
        "Split",
        subtransactions=[
            {
                "id": "dddddddd-0000-0000-0000-0000000000a1",
                "transaction_id": TXN_SPLIT,
                "amount": -100_000,
                "memo": "dinner",
                "payee_id": None,
                "payee_name": None,
                "category_id": DINING,
                "category_name": "Dining Out",
                "transfer_account_id": None,
                "transfer_transaction_id": None,
                "deleted": False,
            },
            {
                "id": "dddddddd-0000-0000-0000-0000000000a2",
                "transaction_id": TXN_SPLIT,
                "amount": -42_250,
                "memo": "snacks",
                "payee_id": None,
                "payee_name": None,
                "category_id": DINING,
                "category_name": "Dining Out",
                "transfer_account_id": None,
                "transfer_transaction_id": None,
                "deleted": False,
            },
        ],
    ),
]

SCHEDULED = [
    {
        "id": SCHED_RENT,
        "date_first": "2026-01-02",
        "date_next": "2026-11-02",
        "frequency": "monthly",
        "amount": -1_200_000,
        "memo": None,
        "flag_color": None,
        "flag_name": None,
        "account_id": CHECKING,
        "account_name": "Everyday Checking",
        "payee_id": PAYEE_LANDLORD,
        "payee_name": "Example Properties",
        "category_id": RENT,
        "category_name": "Rent",
        "transfer_account_id": None,
        "deleted": False,
        "subtransactions": [],
    },
    {
        "id": SCHED_GYM,
        "date_first": "2026-01-15",
        "date_next": "2026-10-15",
        "frequency": "monthly",
        "amount": -40_000,
        "memo": "gym",
        "flag_color": None,
        "flag_name": None,
        "account_id": CARD,
        "account_name": "Test Rewards Card",
        "payee_id": None,
        "payee_name": "Test Gym",
        "category_id": None,
        "category_name": None,
        "transfer_account_id": None,
        "deleted": False,
        "subtransactions": [],
    },
]

MONEY_MOVEMENTS = [
    {
        "id": "mm-1",
        "month": "2026-10-01",
        "moved_at": "2026-10-03T12:00:00Z",
        "note": None,
        "money_movement_group_id": "mmg-1",
        "performed_by_user_id": "user-1",
        "from_category_id": GROCERIES,
        "to_category_id": DINING,
        "amount": 50_000,
    },
]
MONEY_MOVEMENT_GROUPS = [
    {
        "id": "mmg-1",
        "group_created_at": "2026-10-03T12:00:00Z",
        "month": "2026-10-01",
        "note": "cover dining",
        "performed_by_user_id": "user-1",
    },
]


def plan_summary(with_accounts=False):
    p = {
        "id": PLAN,
        "name": "Test Plan",
        "last_modified_on": "2026-10-06T00:00:00Z",
        "first_month": "2026-01-01",
        "last_month": "2026-10-01",
        "date_format": {"format": "MM/DD/YYYY"},
        "currency_format": CURRENCY,
    }
    if with_accounts:
        p["accounts"] = ACCOUNTS
    return p


def month_detail(month):
    m = next(x for x in MONTHS if x["month"] == month)
    cats = [c for c in all_categories()]
    return {**m, "categories": cats}


def default_routes() -> dict[str, Any]:
    """Map of path regex -> JSON ``data`` object (or a callable(query) -> data)."""
    p = rf"/plans/{PLAN}|/plans/last-used"

    def txns_for(pred):
        return lambda q: {
            "transactions": [t for t in TRANSACTIONS if pred(t)],
            "server_knowledge": 100,
        }

    return {
        r"/user$": {"user": {"id": "user-1"}},
        r"/plans$": lambda q: {
            "plans": [plan_summary(q.get("include_accounts") == ["true"])],
            "default_plan": None,
        },
        rf"({p})/settings$": {
            "settings": {"date_format": {"format": "MM/DD/YYYY"}, "currency_format": CURRENCY}
        },
        rf"({p})/accounts$": {"accounts": ACCOUNTS, "server_knowledge": 10},
        rf"({p})/accounts/{CHECKING}$": {"account": ACCOUNTS[0]},
        rf"({p})/categories$": {"category_groups": CATEGORY_GROUPS, "server_knowledge": 20},
        rf"({p})/categories/{GROCERIES}$": {"category": CATEGORY_GROUPS[2]["categories"][0]},
        rf"({p})/months/2026-09-01/categories/{GROCERIES}$": {
            "category": {**CATEGORY_GROUPS[2]["categories"][0], "budgeted": 350_000}
        },
        rf"({p})/months$": {"months": MONTHS, "server_knowledge": 30},
        rf"({p})/payees$": {"payees": PAYEES, "server_knowledge": 40},
        rf"({p})/payees/{PAYEE_MARKET}$": {"payee": PAYEES[0]},
        rf"({p})/payee_locations$": {"payee_locations": PAYEE_LOCATIONS},
        rf"({p})/payee_locations/{LOCATION}$": {"payee_location": PAYEE_LOCATIONS[0]},
        rf"({p})/payees/{PAYEE_MARKET}/payee_locations$": {"payee_locations": PAYEE_LOCATIONS},
        rf"({p})/transactions$": txns_for(lambda t: True),
        rf"({p})/transactions/{TXN_GROCERY}$": {
            "transaction": TRANSACTIONS[2],
            "server_knowledge": 100,
        },
        rf"({p})/transactions/{TXN_SPLIT}$": {
            "transaction": TRANSACTIONS[3],
            "server_knowledge": 100,
        },
        rf"({p})/accounts/{CHECKING}/transactions$": txns_for(
            lambda t: t["account_id"] == CHECKING
        ),
        rf"({p})/months/2026-10-01/transactions$": txns_for(lambda t: t["date"] >= "2026-10-01"),
        rf"({p})/categories/{DINING}/transactions$": lambda q: {
            "transactions": [
                {
                    **{k: v for k, v in TRANSACTIONS[3].items() if k != "subtransactions"},
                    "id": s["id"],
                    "amount": s["amount"],
                    "memo": s["memo"],
                    "category_id": DINING,
                    "category_name": "Dining Out",
                    "type": "subtransaction",
                    "parent_transaction_id": TXN_SPLIT,
                }
                for s in TRANSACTIONS[3]["subtransactions"]
            ],
            "server_knowledge": 100,
        },
        rf"({p})/payees/{PAYEE_MARKET}/transactions$": lambda q: {
            "transactions": [
                {
                    **{k: v for k, v in t.items() if k != "subtransactions"},
                    "type": "transaction",
                    "parent_transaction_id": None,
                }
                for t in TRANSACTIONS
                if t["payee_id"] == PAYEE_MARKET
            ],
            "server_knowledge": 100,
        },
        rf"({p})/scheduled_transactions$": {
            "scheduled_transactions": SCHEDULED,
            "server_knowledge": 50,
        },
        rf"({p})/scheduled_transactions/{SCHED_RENT}$": {"scheduled_transaction": SCHEDULED[0]},
        rf"({p})/money_movements$": {"money_movements": MONEY_MOVEMENTS, "server_knowledge": 1},
        rf"({p})/months/2026-10-01/money_movements$": {
            "money_movements": MONEY_MOVEMENTS,
            "server_knowledge": 1,
        },
        rf"({p})/money_movement_groups$": {
            "money_movement_groups": MONEY_MOVEMENT_GROUPS,
            "server_knowledge": 1,
        },
        rf"({p})/months/2026-10-01/money_movement_groups$": {
            "money_movement_groups": MONEY_MOVEMENT_GROUPS,
            "server_knowledge": 1,
        },
        rf"({p})$": {
            "plan": {
                **plan_summary(),
                "accounts": ACCOUNTS,
                "payees": PAYEES,
                "payee_locations": PAYEE_LOCATIONS,
                "category_groups": [
                    {k: v for k, v in g.items() if k != "categories"} for g in CATEGORY_GROUPS
                ],
                "categories": all_categories(),
                "months": [month_detail(m["month"]) for m in MONTHS],
                "transactions": [
                    {k: v for k, v in t.items() if k != "subtransactions"} for t in TRANSACTIONS
                ],
                "subtransactions": TRANSACTIONS[3]["subtransactions"],
                "scheduled_transactions": SCHEDULED,
                "scheduled_subtransactions": [],
            },
            "server_knowledge": 500,
        },
    }


@dataclass
class FakeResponse:
    status: int
    data: bytes
    headers: dict[str, str] = field(default_factory=dict)
    reason: str = "OK"

    def getheaders(self):
        return self.headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any = None


class FakeYnab:
    """Routes SDK HTTP requests to fixture data and records every call."""

    def __init__(self) -> None:
        self.routes = default_routes()
        self.calls: list[Call] = []
        # Optional overrides: list of (path regex, status, body dict, headers).
        self.overrides: list[tuple[str, int, dict[str, Any], dict[str, str]]] = []
        self.rate_header = "1/200"
        self.raise_exc: Exception | None = None

    def respond(self, path_regex: str, status: int, body: dict[str, Any], headers=None) -> None:
        self.overrides.insert(0, (path_regex, status, body, headers or {}))

    def set_data(self, suffix: str, data: Any) -> None:
        """Replace the data for the route whose pattern ends with ``suffix``."""
        for key in list(self.routes):
            if key.endswith(suffix):
                self.routes[key] = data
                return
        raise KeyError(suffix)

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]

    def paths(self) -> list[str]:
        return [c.path for c in self.calls]

    def handle(self, method, url, headers=None, body=None, post_params=None, _request_timeout=None):
        parsed = urlparse(url)
        path = parsed.path.removeprefix("/v1")
        query = parse_qs(parsed.query)
        self.calls.append(Call(method, path, query, dict(headers or {}), body))
        if self.raise_exc is not None:
            raise self.raise_exc

        def make(status, payload, extra_headers=None):
            hdrs = {"X-Rate-Limit": self.rate_header, **(extra_headers or {})}
            from ynab.rest import RESTResponse

            return RESTResponse(FakeResponse(status, json.dumps(payload).encode(), hdrs))

        for regex, status, payload, hdrs in self.overrides:
            if re.search(regex, path):
                return make(status, payload, hdrs)

        if method != "GET":
            return make(200, {"data": fake_write(method, path, body or {})})

        cat_match = re.search(r"/months/(2026-\d\d-01)/categories/([^/]+)$", path)
        if cat_match and not any(re.fullmatch(r, path) for r in self.routes):
            found = [c for c in all_categories() if c["id"] == cat_match.group(2)]
            if found:
                return make(200, {"data": {"category": found[0]}})

        month_match = re.search(r"/months/(2026-\d\d-01)$", path)
        if month_match:
            return make(200, {"data": {"month": month_detail(month_match.group(1))}})

        for regex, data in self.routes.items():
            if re.fullmatch(regex, path):
                payload = data(query) if callable(data) else data
                return make(200, {"data": payload})
        return make(
            404,
            {
                "error": {
                    "id": "404.2",
                    "name": "resource_not_found",
                    "detail": "Resource not found",
                }
            },
        )


def fake_write(method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Echo-style responses for write endpoints, shaped like YNAB's."""
    tail = re.sub(r"^/plans/[^/]+", "", path)
    new_id = "00000000-0000-0000-0000-00000000000f"
    if tail == "/transactions" and method == "POST":
        created = [{"id": new_id, **t} for t in body.get("transactions", [])]
        return {
            "transaction_ids": [t["id"] for t in created],
            "transactions": created,
            "server_knowledge": 1,
        }
    if tail == "/transactions" and method == "PATCH":
        return {
            "transaction_ids": [t["id"] for t in body["transactions"]],
            "transactions": body["transactions"],
            "server_knowledge": 1,
        }
    if tail == "/transactions/import":
        return {"transaction_ids": [new_id]}
    m = re.fullmatch(r"/transactions/([^/]+)", tail)
    if m:
        existing = next((t for t in TRANSACTIONS if t["id"] == m.group(1)), {"id": m.group(1)})
        if method == "DELETE":
            return {"transaction": {**existing, "deleted": True}, "server_knowledge": 1}
        return {"transaction": {**existing, **body["transaction"]}, "server_knowledge": 1}
    if tail == "/scheduled_transactions":
        return {"scheduled_transaction": {"id": new_id, **body["scheduled_transaction"]}}
    m = re.fullmatch(r"/scheduled_transactions/([^/]+)", tail)
    if m:
        existing = next((s for s in SCHEDULED if s["id"] == m.group(1)), {"id": m.group(1)})
        if method == "DELETE":
            return {"scheduled_transaction": {**existing, "deleted": True}}
        return {"scheduled_transaction": {**existing, **body["scheduled_transaction"]}}
    m = re.fullmatch(r"/months/[^/]+/categories/([^/]+)", tail)
    if m:
        existing = next(c for c in all_categories() if c["id"] == m.group(1))
        return {"category": {**existing, **body["category"]}, "server_knowledge": 1}
    if tail == "/categories":
        return {"category": {"id": new_id, **body["category"]}, "server_knowledge": 1}
    m = re.fullmatch(r"/categories/([^/]+)", tail)
    if m:
        existing = next(c for c in all_categories() if c["id"] == m.group(1))
        return {"category": {**existing, **body["category"]}, "server_knowledge": 1}
    if tail == "/category_groups":
        return {"category_group": {"id": new_id, **body["category_group"]}}
    m = re.fullmatch(r"/category_groups/([^/]+)", tail)
    if m:
        return {"category_group": {"id": m.group(1), **body["category_group"]}}
    if tail == "/payees":
        return {"payee": {"id": new_id, **body["payee"]}}
    m = re.fullmatch(r"/payees/([^/]+)", tail)
    if m:
        return {"payee": {"id": m.group(1), **body["payee"]}}
    if tail == "/accounts":
        return {"account": {"id": new_id, **body["account"]}}
    raise AssertionError(f"unexpected write {method} {path}")
