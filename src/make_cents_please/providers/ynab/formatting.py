"""Turn raw YNAB JSON into compact, model-friendly rows."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


class InputError(ValueError):
    """Bad tool input; the message tells the model how to fix it."""


# ------------------------------------------------------------------ amounts


def amount(milliunits: int | None) -> str | None:
    """Milliunits (1/1000 of a currency unit) -> decimal string, e.g. -12340 -> "-12.34"."""
    if milliunits is None:
        return None
    value = Decimal(milliunits) / Decimal(1000)
    return f"{value:.3f}" if milliunits % 10 else f"{value:.2f}"


def to_milliunits(value: float | int | str | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 1000).to_integral_value())
    except Exception as exc:
        raise InputError(f"{field} must be a number like 25 or 25.50.") from exc


# ------------------------------------------------------------------ dates


def parse_date(value: str | None, field: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(
            f"{field} must be an ISO date YYYY-MM-DD, e.g. 2026-01-31 (got {value!r})."
        ) from exc


def parse_month(value: str | None, field: str = "month") -> date | None:
    """Accept "current", "YYYY-MM" or "YYYY-MM-DD"; return the first day of that month."""
    if value is None or value == "":
        return None
    if value.strip().lower() == "current":
        today = datetime.now(UTC).date()
        return today.replace(day=1)
    match = _MONTH_RE.match(value.strip())
    if not match:
        raise InputError(f"{field} must be 'current', 'YYYY-MM' or 'YYYY-MM-01' (got {value!r}).")
    try:
        return date(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as exc:
        raise InputError(f"{field} {value!r} is not a valid month.") from exc


# ------------------------------------------------------------------ pagination


def check_limit(limit: int, offset: int) -> None:
    if limit < 1 or limit > MAX_LIMIT:
        raise InputError(f"limit must be between 1 and {MAX_LIMIT}.")
    if offset < 0:
        raise InputError("offset must be 0 or greater.")


def page(items: list[dict[str, Any]], limit: int, offset: int, **extra: Any) -> dict[str, Any]:
    check_limit(limit, offset)
    window = items[offset : offset + limit]
    result: dict[str, Any] = {"count": len(window), "total": len(items)}
    if offset + limit < len(items):
        result["next_offset"] = offset + limit
    result.update({k: v for k, v in extra.items() if v is not None})
    result["items"] = window
    return result


def compact(row: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so rows stay short."""
    return {k: v for k, v in row.items() if v not in (None, "", [], {})}


def live(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [i for i in (items or []) if not i.get("deleted")]


# ------------------------------------------------------------------ entity shapers


def plan_row(p: dict[str, Any]) -> dict[str, Any]:
    currency = p.get("currency_format") or {}
    return compact(
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "last_modified_on": p.get("last_modified_on"),
            "first_month": p.get("first_month"),
            "last_month": p.get("last_month"),
            "currency": currency.get("iso_code"),
            "accounts": [account_row(a) for a in live(p.get("accounts"))] or None,
        }
    )


def account_row(a: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "type": a.get("type"),
            "on_budget": a.get("on_budget"),
            "closed": a.get("closed") or None,
            "balance": amount(a.get("balance")),
            "cleared_balance": amount(a.get("cleared_balance")),
            "uncleared_balance": amount(a.get("uncleared_balance")),
            "note": a.get("note"),
            "last_reconciled_at": a.get("last_reconciled_at"),
            "direct_import_in_error": a.get("direct_import_in_error") or None,
        }
    )


def category_row(c: dict[str, Any], group_name: str | None = None) -> dict[str, Any]:
    return compact(
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "group": group_name or c.get("category_group_name"),
            "budgeted": amount(c.get("budgeted")),
            "activity": amount(c.get("activity")),
            "balance": amount(c.get("balance")),
            "hidden": c.get("hidden") or None,
            "goal_type": c.get("goal_type"),
            "goal_target": amount(c.get("goal_target")) if c.get("goal_type") else None,
            "goal_target_month": c.get("goal_target_month") or c.get("goal_target_date"),
            "goal_percentage_complete": c.get("goal_percentage_complete"),
            "goal_under_funded": amount(c.get("goal_under_funded")) if c.get("goal_type") else None,
            "note": c.get("note"),
        }
    )


def month_row(m: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "month": m.get("month"),
            "income": amount(m.get("income")),
            "budgeted": amount(m.get("budgeted")),
            "activity": amount(m.get("activity")),
            "to_be_budgeted": amount(m.get("to_be_budgeted")),
            "age_of_money": m.get("age_of_money"),
            "note": m.get("note"),
        }
    )


def payee_row(p: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "transfer_account_id": p.get("transfer_account_id"),
        }
    )


def payee_location_row(loc: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": loc.get("id"),
            "payee_id": loc.get("payee_id"),
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
        }
    )


def _sub_row(s: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "amount": amount(s.get("amount")),
            "category": s.get("category_name"),
            "category_id": s.get("category_id"),
            "payee": s.get("payee_name"),
            "memo": s.get("memo"),
            "transfer_account_id": s.get("transfer_account_id"),
        }
    )


def transaction_row(t: dict[str, Any]) -> dict[str, Any]:
    subs = [_sub_row(s) for s in live(t.get("subtransactions"))]
    return compact(
        {
            "id": t.get("id"),
            "date": t.get("date"),
            "amount": amount(t.get("amount")),
            "payee": t.get("payee_name"),
            "category": t.get("category_name"),
            "account": t.get("account_name"),
            "memo": t.get("memo"),
            "cleared": t.get("cleared"),
            "approved": t.get("approved"),
            "flag": t.get("flag_name") or t.get("flag_color"),
            "transfer_account_id": t.get("transfer_account_id"),
            # Present on "hybrid" results (by category / by payee) for split lines.
            "parent_transaction_id": t.get("parent_transaction_id"),
            "splits": subs or None,
        }
    )


def transaction_detail_row(t: dict[str, Any]) -> dict[str, Any]:
    row = transaction_row(t)
    row.update(
        compact(
            {
                "account_id": t.get("account_id"),
                "payee_id": t.get("payee_id"),
                "category_id": t.get("category_id"),
                "import_payee_name": t.get("import_payee_name"),
                "matched_transaction_id": t.get("matched_transaction_id"),
                "debt_transaction_type": t.get("debt_transaction_type"),
            }
        )
    )
    return row


def scheduled_row(s: dict[str, Any]) -> dict[str, Any]:
    subs = [_sub_row(x) for x in live(s.get("subtransactions"))]
    return compact(
        {
            "id": s.get("id"),
            "date_next": s.get("date_next"),
            "date_first": s.get("date_first"),
            "frequency": s.get("frequency"),
            "amount": amount(s.get("amount")),
            "payee": s.get("payee_name"),
            "category": s.get("category_name"),
            "account": s.get("account_name"),
            "memo": s.get("memo"),
            "flag": s.get("flag_name") or s.get("flag_color"),
            "transfer_account_id": s.get("transfer_account_id"),
            "splits": subs or None,
        }
    )


def money_movement_row(m: dict[str, Any], category_names: dict[str, str]) -> dict[str, Any]:
    from_id, to_id = m.get("from_category_id"), m.get("to_category_id")
    return compact(
        {
            "id": m.get("id"),
            "month": m.get("month"),
            "moved_at": m.get("moved_at"),
            "amount": amount(m.get("amount")),
            "from_category": category_names.get(from_id, from_id) if from_id else None,
            "to_category": category_names.get(to_id, to_id) if to_id else None,
            "note": m.get("note"),
            "group_id": m.get("money_movement_group_id"),
        }
    )


def money_movement_group_row(g: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": g.get("id"),
            "month": g.get("month"),
            "created_at": g.get("group_created_at"),
            "note": g.get("note"),
        }
    )
