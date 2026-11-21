"""Read-only YNAB tools exposed over MCP (write tools live in write_tools.py)."""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import formatting as fmt
from .client import YnabClient, YnabError, merge_collections
from .confirm import ConfirmationError
from .formatting import DEFAULT_LIMIT, MAX_LIMIT, InputError

DEFAULT_TRANSACTION_DAYS = 30

PlanId = Annotated[
    str | None,
    Field(
        description="Plan (budget) id from ynab_list_plans. Omit to use the configured "
        "default plan.",
    ),
]
Limit = Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Max rows to return.")]
Offset = Annotated[
    int, Field(ge=0, description="Rows to skip (use next_offset from a prior call).")
]
Month = Annotated[str, Field(description="Month as 'current', 'YYYY-MM' or 'YYYY-MM-01'.")]

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

MERGE_ACCOUNTS = merge_collections({"accounts": None})
MERGE_CATEGORIES = merge_collections({"category_groups": {"categories": "categories"}})
MERGE_MONTHS = merge_collections({"months": None})
MERGE_PAYEES = merge_collections({"payees": None})
MERGE_TRANSACTIONS = merge_collections({"transactions": None})
MERGE_SCHEDULED = merge_collections({"scheduled_transactions": None})
MERGE_PLAN = merge_collections(
    {
        "accounts": None,
        "payees": None,
        "payee_locations": None,
        "category_groups": None,
        "categories": None,
        "months": {"categories": "categories"},
        "transactions": None,
        "subtransactions": None,
        "scheduled_transactions": None,
        "scheduled_subtransactions": None,
    },
    root="plan",
)


def _guard(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Convert expected failures into MCP tool errors the model can act on."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except (YnabError, InputError, ConfirmationError) as exc:
            raise ToolError(str(exc)) from None

    return wrapper


def _with_warning(result: dict[str, Any], *warnings: str | None) -> dict[str, Any]:
    notes = [w for w in warnings if w]
    if notes:
        result = {"warning": " ".join(dict.fromkeys(notes)), **result}
    return result


def _contains(haystack: str | None, needle: str) -> bool:
    return bool(haystack) and needle in haystack.lower()


class YnabData:
    """Cached data access shared by the tools."""

    def __init__(self, client: YnabClient) -> None:
        self.c = client

    def accounts(self, plan: str) -> tuple[list[dict[str, Any]], str | None]:
        c = self.c
        data, warn = c.cached(
            ("accounts", plan),
            lambda k: c.call(
                c.accounts_api.get_accounts_without_preload_content,
                plan_id=plan,
                last_knowledge_of_server=k,
            ),
            MERGE_ACCOUNTS,
        )
        return fmt.live(data.get("accounts")), warn

    def category_groups(self, plan: str) -> tuple[list[dict[str, Any]], str | None]:
        c = self.c
        data, warn = c.cached(
            ("categories", plan),
            lambda k: c.call(
                c.categories_api.get_categories_without_preload_content,
                plan_id=plan,
                last_knowledge_of_server=k,
            ),
            MERGE_CATEGORIES,
        )
        return fmt.live(data.get("category_groups")), warn

    def category_names(self, plan: str) -> tuple[dict[str, str], str | None]:
        groups, warn = self.category_groups(plan)
        names = {
            cat["id"]: cat.get("name", "") for g in groups for cat in g.get("categories") or []
        }
        return names, warn

    def month(self, plan: str, month: date) -> tuple[dict[str, Any], str | None]:
        c = self.c
        data, warn = c.cached(
            ("month", plan, month.isoformat()),
            lambda _k: c.call(
                c.months_api.get_plan_month_without_preload_content, plan_id=plan, month=month
            ),
        )
        return data.get("month") or {}, warn

    def months(self, plan: str) -> tuple[list[dict[str, Any]], str | None]:
        c = self.c
        data, warn = c.cached(
            ("months", plan),
            lambda k: c.call(
                c.months_api.get_plan_months_without_preload_content,
                plan_id=plan,
                last_knowledge_of_server=k,
            ),
            MERGE_MONTHS,
        )
        return fmt.live(data.get("months")), warn

    def payees(self, plan: str) -> tuple[list[dict[str, Any]], str | None]:
        c = self.c
        data, warn = c.cached(
            ("payees", plan),
            lambda k: c.call(
                c.payees_api.get_payees_without_preload_content,
                plan_id=plan,
                last_knowledge_of_server=k,
            ),
            MERGE_PAYEES,
        )
        return fmt.live(data.get("payees")), warn

    def scheduled(self, plan: str) -> tuple[list[dict[str, Any]], str | None]:
        c = self.c
        data, warn = c.cached(
            ("scheduled", plan),
            lambda k: c.call(
                c.scheduled_api.get_scheduled_transactions_without_preload_content,
                plan_id=plan,
                last_knowledge_of_server=k,
            ),
            MERGE_SCHEDULED,
        )
        return fmt.live(data.get("scheduled_transactions")), warn

    def single(self, key: tuple[Any, ...], method: Callable[..., Any], field: str, **kwargs: Any):
        c = self.c
        data, warn = c.cached(key, lambda _k: c.call(method, **kwargs))
        return data.get(field) or {}, warn


def register(server: MCPServer, client: YnabClient) -> None:
    """Register every YNAB read tool on ``server``."""
    d = YnabData(client)
    c = client

    def tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        server.tool(annotations=READ_ONLY)(_guard(fn))
        return fn

    # ------------------------------------------------------------ user / plans

    @tool
    def ynab_get_user() -> dict[str, Any]:
        """Get the id of the YNAB user that owns the access token. Rarely needed."""
        data, warn = c.cached(
            ("user",), lambda _k: c.call(c.user_api.get_user_without_preload_content)
        )
        return _with_warning({"user": data.get("user")}, warn)

    @tool
    def ynab_list_plans(
        include_accounts: Annotated[
            bool, Field(description="Also list each plan's accounts (larger output).")
        ] = False,
    ) -> dict[str, Any]:
        """List the user's YNAB plans (budgets) with ids, currency and month range.

        Call this first if you need a plan_id; most tools default to the configured plan.
        """
        data, warn = c.cached(
            ("plans", include_accounts),
            lambda _k: c.call(
                c.plans_api.get_plans_without_preload_content, include_accounts=include_accounts
            ),
        )
        plans = [fmt.plan_row(p) for p in data.get("plans") or []]
        default = (data.get("default_plan") or {}).get("id")
        return _with_warning(
            fmt.compact(
                {"default_plan_id": default, "configured_plan": c.default_plan_id, "plans": plans}
            ),
            warn,
        )

    @tool
    def ynab_get_plan_settings(plan_id: PlanId = None) -> dict[str, Any]:
        """Get a plan's currency and date format settings."""
        plan = c.plan(plan_id)
        settings, warn = d.single(
            ("settings", plan),
            c.plans_api.get_plan_settings_by_id_without_preload_content,
            "settings",
            plan_id=plan,
        )
        return _with_warning({"plan_id": plan, "settings": settings}, warn)

    @tool
    def ynab_get_plan_overview(plan_id: PlanId = None) -> dict[str, Any]:
        """Summarize a whole plan: account balances, net worth and how many categories,
        payees and transactions it has. Uses YNAB's full plan export (refreshed by delta),
        but returns only totals and counts. For details use the list_* tools.
        """
        plan = c.plan(plan_id)
        data, warn = c.cached(
            ("plan", plan),
            lambda k: c.call(
                c.plans_api.get_plan_by_id_without_preload_content,
                plan_id=plan,
                last_knowledge_of_server=k,
            ),
            MERGE_PLAN,
        )
        p = data.get("plan") or {}
        accounts = [a for a in fmt.live(p.get("accounts")) if not a.get("closed")]
        on_budget = sum(a.get("balance") or 0 for a in accounts if a.get("on_budget"))
        net_worth = sum(a.get("balance") or 0 for a in accounts)
        counts = {
            name: len(fmt.live(p.get(name)))
            for name in (
                "accounts",
                "category_groups",
                "categories",
                "payees",
                "months",
                "transactions",
                "scheduled_transactions",
            )
        }
        return _with_warning(
            fmt.compact(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "currency": (p.get("currency_format") or {}).get("iso_code"),
                    "first_month": p.get("first_month"),
                    "last_month": p.get("last_month"),
                    "last_modified_on": p.get("last_modified_on"),
                    "open_accounts": len(accounts),
                    "on_budget_balance": fmt.amount(on_budget),
                    "net_worth": fmt.amount(net_worth),
                    "counts": counts,
                }
            ),
            warn,
        )

    # ------------------------------------------------------------ accounts

    @tool
    def ynab_list_accounts(
        plan_id: PlanId = None,
        on_budget: Annotated[
            bool | None, Field(description="True = budget accounts only, False = tracking only.")
        ] = None,
        include_closed: Annotated[bool, Field(description="Include closed accounts.")] = False,
        account_type: Annotated[
            str | None,
            Field(description="Filter by type, e.g. checking, savings, creditCard, mortgage."),
        ] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List accounts with current, cleared and uncleared balances. Use this to find
        account_id values for other tools.
        """
        plan = c.plan(plan_id)
        accounts, warn = d.accounts(plan)
        rows = [
            fmt.account_row(a)
            for a in accounts
            if (include_closed or not a.get("closed"))
            and (on_budget is None or bool(a.get("on_budget")) == on_budget)
            and (not account_type or (a.get("type") or "").lower() == account_type.lower())
        ]
        rows.sort(key=lambda r: (r.get("closed", False), r.get("name", "").lower()))
        return _with_warning(fmt.page(rows, limit, offset), warn)

    @tool
    def ynab_get_account(
        account_id: Annotated[str, Field(description="Account id from ynab_list_accounts.")],
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Get one account's details and balances."""
        plan = c.plan(plan_id)
        account, warn = d.single(
            ("account", plan, account_id),
            c.accounts_api.get_account_by_id_without_preload_content,
            "account",
            plan_id=plan,
            account_id=account_id,
        )
        return _with_warning({"account": fmt.account_row(account)}, warn)

    # ------------------------------------------------------------ categories

    @tool
    def ynab_list_categories(
        plan_id: PlanId = None,
        month: Annotated[
            str | None,
            Field(
                description="Show assigned/activity/available for this month ('current', "
                "'YYYY-MM'). Omit for the current month."
            ),
        ] = None,
        group: Annotated[
            str | None, Field(description="Only categories whose group name contains this text.")
        ] = None,
        name_contains: Annotated[
            str | None, Field(description="Only categories whose name contains this text.")
        ] = None,
        only_overspent: Annotated[
            bool, Field(description="Only categories with a negative available balance.")
        ] = False,
        only_underfunded: Annotated[
            bool, Field(description="Only categories whose goal/target is underfunded.")
        ] = False,
        include_hidden: Annotated[bool, Field(description="Include hidden categories.")] = False,
        limit: Limit = 100,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List budget categories with assigned (budgeted), activity and available (balance)
        amounts and goal info. Use this to find category_id values and to check spending
        against the budget.
        """
        plan = c.plan(plan_id)
        rows: list[dict[str, Any]] = []
        warns: list[str | None] = []
        if month:
            m, warn = d.month(plan, fmt.parse_month(month))
            warns.append(warn)
            group_names: dict[str, str] = {}
            if any(not cat.get("category_group_name") for cat in m.get("categories") or []):
                groups, warn = d.category_groups(plan)
                warns.append(warn)
                group_names = {g["id"]: g.get("name", "") for g in groups}
            source = [
                (
                    cat,
                    cat.get("category_group_name") or group_names.get(cat.get("category_group_id")),
                )
                for cat in fmt.live(m.get("categories"))
            ]
        else:
            groups, warn = d.category_groups(plan)
            warns.append(warn)
            source = [
                (cat, g.get("name"))
                for g in groups
                if include_hidden or not (g.get("hidden") or g.get("internal"))
                for cat in fmt.live(g.get("categories"))
            ]

        g_needle = (group or "").lower()
        n_needle = (name_contains or "").lower()
        for cat, group_name in source:
            if cat.get("internal") or (not include_hidden and cat.get("hidden")):
                continue
            if g_needle and not _contains(group_name, g_needle):
                continue
            if n_needle and not _contains(cat.get("name"), n_needle):
                continue
            if only_overspent and (cat.get("balance") or 0) >= 0:
                continue
            if only_underfunded and (cat.get("goal_under_funded") or 0) <= 0:
                continue
            rows.append(fmt.category_row(cat, group_name))
        return _with_warning(fmt.page(rows, limit, offset, month=month), *warns)

    @tool
    def ynab_get_category(
        category_id: Annotated[str, Field(description="Category id from ynab_list_categories.")],
        month: Annotated[
            str | None,
            Field(description="Month for the amounts ('current', 'YYYY-MM'). Omit for current."),
        ] = None,
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Get one category's assigned, activity, available and goal details, optionally for
        a specific month.
        """
        plan = c.plan(plan_id)
        if month:
            m = fmt.parse_month(month)
            cat, warn = d.single(
                ("month_category", plan, m.isoformat(), category_id),
                c.categories_api.get_month_category_by_id_without_preload_content,
                "category",
                plan_id=plan,
                month=m,
                category_id=category_id,
            )
        else:
            cat, warn = d.single(
                ("category", plan, category_id),
                c.categories_api.get_category_by_id_without_preload_content,
                "category",
                plan_id=plan,
                category_id=category_id,
            )
        return _with_warning({"category": fmt.category_row(cat)}, warn)

    # ------------------------------------------------------------ months

    @tool
    def ynab_list_months(
        plan_id: PlanId = None,
        since_month: Annotated[
            str | None, Field(description="Earliest month to include ('YYYY-MM').")
        ] = None,
        until_month: Annotated[
            str | None, Field(description="Latest month to include ('YYYY-MM').")
        ] = None,
        limit: Limit = 12,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List budget months (newest first) with income, assigned, activity, Ready to Assign
        and age of money. Good for month-over-month trends.
        """
        plan = c.plan(plan_id)
        start = fmt.parse_month(since_month, "since_month")
        end = fmt.parse_month(until_month, "until_month")
        months, warn = d.months(plan)
        rows = [
            fmt.month_row(m)
            for m in months
            if (not start or m.get("month", "") >= start.isoformat())
            and (not end or m.get("month", "") <= end.isoformat())
        ]
        rows.sort(key=lambda r: r.get("month", ""), reverse=True)
        return _with_warning(fmt.page(rows, limit, offset), warn)

    @tool
    def ynab_get_month(
        month: Month = "current",
        plan_id: PlanId = None,
        group: Annotated[
            str | None, Field(description="Only categories whose group name contains this text.")
        ] = None,
        only_overspent: Annotated[
            bool, Field(description="Only categories with a negative available balance.")
        ] = False,
        include_zero: Annotated[
            bool,
            Field(description="Include categories with nothing assigned, spent or available."),
        ] = False,
        include_categories: Annotated[
            bool, Field(description="Set False to return only the month totals.")
        ] = True,
    ) -> dict[str, Any]:
        """Get one budget month: totals (income, assigned, activity, Ready to Assign, age of
        money) plus per-category assigned/activity/available for that month.
        """
        plan = c.plan(plan_id)
        m, warn = d.month(plan, fmt.parse_month(month))
        result: dict[str, Any] = {"month": fmt.month_row(m)}
        if include_categories:
            g_needle = (group or "").lower()
            cats = []
            for cat in fmt.live(m.get("categories")):
                if cat.get("hidden") or cat.get("internal"):
                    continue
                if g_needle and not _contains(cat.get("category_group_name"), g_needle):
                    continue
                if only_overspent and (cat.get("balance") or 0) >= 0:
                    continue
                if not include_zero and not any(
                    cat.get(k) for k in ("budgeted", "activity", "balance")
                ):
                    continue
                cats.append(fmt.category_row(cat))
            result["categories"] = cats
        return _with_warning(result, warn)

    # ------------------------------------------------------------ payees

    @tool
    def ynab_list_payees(
        plan_id: PlanId = None,
        name_contains: Annotated[
            str | None, Field(description="Only payees whose name contains this text.")
        ] = None,
        include_transfers: Annotated[
            bool, Field(description="Include 'Transfer : <account>' payees.")
        ] = False,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List payees (merchants, people, transfer targets). Use this to find payee_id values."""
        plan = c.plan(plan_id)
        payees, warn = d.payees(plan)
        needle = (name_contains or "").lower()
        rows = [
            fmt.payee_row(p)
            for p in payees
            if (include_transfers or not p.get("transfer_account_id"))
            and (not needle or _contains(p.get("name"), needle))
        ]
        rows.sort(key=lambda r: r.get("name", "").lower())
        return _with_warning(fmt.page(rows, limit, offset), warn)

    @tool
    def ynab_get_payee(
        payee_id: Annotated[str, Field(description="Payee id from ynab_list_payees.")],
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Get one payee."""
        plan = c.plan(plan_id)
        payee, warn = d.single(
            ("payee", plan, payee_id),
            c.payees_api.get_payee_by_id_without_preload_content,
            "payee",
            plan_id=plan,
            payee_id=payee_id,
        )
        return _with_warning({"payee": fmt.payee_row(payee)}, warn)

    @tool
    def ynab_list_payee_locations(
        plan_id: PlanId = None,
        payee_id: Annotated[
            str | None, Field(description="Only locations for this payee (from ynab_list_payees).")
        ] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List GPS locations recorded for payees by the YNAB mobile app."""
        plan = c.plan(plan_id)
        if payee_id:
            data, warn = c.cached(
                ("payee_locations", plan, payee_id),
                lambda _k: c.call(
                    c.payee_locations_api.get_payee_locations_by_payee_without_preload_content,
                    plan_id=plan,
                    payee_id=payee_id,
                ),
            )
        else:
            data, warn = c.cached(
                ("payee_locations", plan),
                lambda _k: c.call(
                    c.payee_locations_api.get_payee_locations_without_preload_content,
                    plan_id=plan,
                ),
            )
        rows = [fmt.payee_location_row(x) for x in fmt.live(data.get("payee_locations"))]
        return _with_warning(fmt.page(rows, limit, offset), warn)

    @tool
    def ynab_get_payee_location(
        payee_location_id: Annotated[
            str, Field(description="Payee location id from ynab_list_payee_locations.")
        ],
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Get one payee location."""
        plan = c.plan(plan_id)
        loc, warn = d.single(
            ("payee_location", plan, payee_location_id),
            c.payee_locations_api.get_payee_location_by_id_without_preload_content,
            "payee_location",
            plan_id=plan,
            payee_location_id=payee_location_id,
        )
        return _with_warning({"payee_location": fmt.payee_location_row(loc)}, warn)

    # ------------------------------------------------------------ transactions

    @tool
    def ynab_list_transactions(
        plan_id: PlanId = None,
        since_date: Annotated[
            str | None,
            Field(
                description="Earliest date (YYYY-MM-DD). Defaults to 30 days ago unless month "
                "is given."
            ),
        ] = None,
        until_date: Annotated[str | None, Field(description="Latest date (YYYY-MM-DD).")] = None,
        month: Annotated[
            str | None,
            Field(description="Restrict to one month ('current', 'YYYY-MM'); sets the dates."),
        ] = None,
        account_id: Annotated[
            str | None, Field(description="Only this account (id from ynab_list_accounts).")
        ] = None,
        category_id: Annotated[
            str | None, Field(description="Only this category (id from ynab_list_categories).")
        ] = None,
        payee_id: Annotated[
            str | None, Field(description="Only this payee (id from ynab_list_payees).")
        ] = None,
        status: Annotated[
            Literal["uncategorized", "unapproved"] | None,
            Field(description="Only uncategorized or only unapproved transactions."),
        ] = None,
        cleared: Annotated[
            Literal["cleared", "uncleared", "reconciled"] | None,
            Field(description="Filter by cleared status."),
        ] = None,
        direction: Annotated[
            Literal["inflow", "outflow"] | None,
            Field(description="Only money in (inflow) or money out (outflow)."),
        ] = None,
        min_amount: Annotated[
            float | None,
            Field(description="Minimum absolute amount in currency units, e.g. 100 = $100."),
        ] = None,
        max_amount: Annotated[
            float | None, Field(description="Maximum absolute amount in currency units.")
        ] = None,
        text: Annotated[
            str | None,
            Field(description="Case-insensitive text to find in payee, memo or category."),
        ] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Search transactions (newest first) with filters for dates, account, category,
        payee, amount and text. Returns a page of compact rows plus inflow/outflow totals for
        all matches. Split transactions show their splits; when filtering by category or
        payee, matching split lines are returned individually.
        """
        plan = c.plan(plan_id)
        since = fmt.parse_date(since_date, "since_date")
        until = fmt.parse_date(until_date, "until_date")
        month_start = fmt.parse_month(month)
        if month_start:
            next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            since = max(since, month_start) if since else month_start
            month_end = next_month - timedelta(days=1)
            until = min(until, month_end) if until else month_end
        if since is None:
            since = datetime.now(UTC).date() - timedelta(days=DEFAULT_TRANSACTION_DAYS)
        if until and until < since:
            raise InputError("until_date is before since_date.")
        min_milli = fmt.to_milliunits(min_amount, "min_amount")
        max_milli = fmt.to_milliunits(max_amount, "max_amount")

        api = c.transactions_api
        common = {"plan_id": plan, "since_date": since, "until_date": until, "type": status}
        if account_id:
            scope = ("account", account_id)
            method = api.get_transactions_by_account_without_preload_content
            common["account_id"] = account_id
        elif category_id:
            scope = ("category", category_id)
            method = api.get_transactions_by_category_without_preload_content
            common["category_id"] = category_id
        elif payee_id:
            scope = ("payee", payee_id)
            method = api.get_transactions_by_payee_without_preload_content
            common["payee_id"] = payee_id
        elif month_start and not since_date and not until_date:
            scope = ("month", month_start.isoformat())
            method = api.get_transactions_by_month_without_preload_content
            common = {"plan_id": plan, "month": month_start.isoformat(), "type": status}
        else:
            scope = ("all", None)
            method = api.get_transactions_without_preload_content

        key = ("transactions", plan, *scope, since.isoformat(), str(until), status)
        data, warn = c.cached(
            key,
            lambda k: c.call(method, last_knowledge_of_server=k, **common),
            MERGE_TRANSACTIONS,
        )

        def matches_id(t: dict[str, Any], field: str, value: str | None) -> bool:
            if not value:
                return True
            return t.get(field) == value or any(
                s.get(field) == value for s in fmt.live(t.get("subtransactions"))
            )

        needle = (text or "").lower()
        matched = []
        for t in fmt.live(data.get("transactions")):
            day = t.get("date") or ""
            if day < since.isoformat() or (until and day > until.isoformat()):
                continue
            if not (
                matches_id(t, "account_id", account_id)
                and matches_id(t, "category_id", category_id)
                and matches_id(t, "payee_id", payee_id)
            ):
                continue
            amt = t.get("amount") or 0
            if cleared and t.get("cleared") != cleared:
                continue
            if direction == "inflow" and amt <= 0 or direction == "outflow" and amt >= 0:
                continue
            if min_milli is not None and abs(amt) < abs(min_milli):
                continue
            if max_milli is not None and abs(amt) > abs(max_milli):
                continue
            if needle:
                fields = [
                    t.get(f) for f in ("payee_name", "memo", "category_name", "import_payee_name")
                ]
                for s in fmt.live(t.get("subtransactions")):
                    fields += [s.get("payee_name"), s.get("memo"), s.get("category_name")]
                if not any(_contains(f, needle) for f in fields):
                    continue
            matched.append(t)

        matched.sort(key=lambda t: (t.get("date") or "", t.get("id") or ""), reverse=True)
        inflow = sum(t["amount"] for t in matched if (t.get("amount") or 0) > 0)
        outflow = sum(t["amount"] for t in matched if (t.get("amount") or 0) < 0)
        rows = [fmt.transaction_row(t) for t in matched]
        result = fmt.page(
            rows,
            limit,
            offset,
            since_date=since.isoformat(),
            until_date=until.isoformat() if until else None,
            totals={
                "inflow": fmt.amount(inflow),
                "outflow": fmt.amount(outflow),
                "net": fmt.amount(inflow + outflow),
            },
        )
        return _with_warning(result, warn)

    @tool
    def ynab_get_transaction(
        transaction_id: Annotated[
            str, Field(description="Transaction id from ynab_list_transactions.")
        ],
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Get one transaction in full, including ids, import details and splits."""
        plan = c.plan(plan_id)
        txn, warn = d.single(
            ("transaction", plan, transaction_id),
            c.transactions_api.get_transaction_by_id_without_preload_content,
            "transaction",
            plan_id=plan,
            transaction_id=transaction_id,
        )
        return _with_warning({"transaction": fmt.transaction_detail_row(txn)}, warn)

    # ------------------------------------------------------------ scheduled

    @tool
    def ynab_list_scheduled_transactions(
        plan_id: PlanId = None,
        account_id: Annotated[str | None, Field(description="Only this account.")] = None,
        category_id: Annotated[str | None, Field(description="Only this category.")] = None,
        due_before: Annotated[
            str | None,
            Field(description="Only those whose next date is on/before this date (YYYY-MM-DD)."),
        ] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List scheduled (recurring or future) transactions, soonest first. Useful for
        upcoming bills.
        """
        plan = c.plan(plan_id)
        due = fmt.parse_date(due_before, "due_before")
        items, warn = d.scheduled(plan)
        rows = [
            s
            for s in items
            if (not account_id or s.get("account_id") == account_id)
            and (
                not category_id
                or s.get("category_id") == category_id
                or any(x.get("category_id") == category_id for x in s.get("subtransactions") or [])
            )
            and (not due or (s.get("date_next") or "") <= due.isoformat())
        ]
        rows.sort(key=lambda s: s.get("date_next") or "")
        return _with_warning(fmt.page([fmt.scheduled_row(s) for s in rows], limit, offset), warn)

    @tool
    def ynab_get_scheduled_transaction(
        scheduled_transaction_id: Annotated[
            str, Field(description="Id from ynab_list_scheduled_transactions.")
        ],
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Get one scheduled transaction."""
        plan = c.plan(plan_id)
        item, warn = d.single(
            ("scheduled_transaction", plan, scheduled_transaction_id),
            c.scheduled_api.get_scheduled_transaction_by_id_without_preload_content,
            "scheduled_transaction",
            plan_id=plan,
            scheduled_transaction_id=scheduled_transaction_id,
        )
        return _with_warning({"scheduled_transaction": fmt.scheduled_row(item)}, warn)

    # ------------------------------------------------------------ money movements

    @tool
    def ynab_list_money_movements(
        plan_id: PlanId = None,
        month: Annotated[
            str | None, Field(description="Only this month ('current', 'YYYY-MM').")
        ] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List money moved between categories (assigning or reassigning funds), newest
        first, with category names.
        """
        plan = c.plan(plan_id)
        api = c.money_movements_api
        m = fmt.parse_month(month)
        if m:
            data, warn = c.cached(
                ("money_movements", plan, m.isoformat()),
                lambda _k: c.call(
                    api.get_money_movements_by_month_without_preload_content, plan_id=plan, month=m
                ),
            )
        else:
            data, warn = c.cached(
                ("money_movements", plan),
                lambda _k: c.call(api.get_money_movements_without_preload_content, plan_id=plan),
            )
        names, warn2 = d.category_names(plan)
        items = fmt.live(data.get("money_movements"))
        items.sort(key=lambda x: x.get("moved_at") or x.get("month") or "", reverse=True)
        rows = [fmt.money_movement_row(x, names) for x in items]
        return _with_warning(fmt.page(rows, limit, offset), warn, warn2)

    @tool
    def ynab_list_money_movement_groups(
        plan_id: PlanId = None,
        month: Annotated[
            str | None, Field(description="Only this month ('current', 'YYYY-MM').")
        ] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List groups of money movements (one group per assign/move action), newest first."""
        plan = c.plan(plan_id)
        api = c.money_movements_api
        m = fmt.parse_month(month)
        if m:
            data, warn = c.cached(
                ("money_movement_groups", plan, m.isoformat()),
                lambda _k: c.call(
                    api.get_money_movement_groups_by_month_without_preload_content,
                    plan_id=plan,
                    month=m,
                ),
            )
        else:
            data, warn = c.cached(
                ("money_movement_groups", plan),
                lambda _k: c.call(
                    api.get_money_movement_groups_without_preload_content, plan_id=plan
                ),
            )
        items = fmt.live(data.get("money_movement_groups"))
        items.sort(key=lambda x: x.get("group_created_at") or "", reverse=True)
        rows = [fmt.money_movement_group_row(x) for x in items]
        return _with_warning(fmt.page(rows, limit, offset), warn)

    # ------------------------------------------------------------ local status

    @tool
    def ynab_get_api_status() -> dict[str, Any]:
        """Show how many of the 200 hourly YNAB API requests have been used and cache
        settings. Makes no API call.
        """
        return c.status()
