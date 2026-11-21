"""YNAB tools that create, change or delete data.

Registered only when YNAB_ALLOW_WRITES=true. Every tool works in two steps: a call without
``confirmation_token`` validates the request and returns a preview plus a one-time token
(nothing is sent to YNAB except reads used to build the preview); calling again with the
same arguments and that token performs the write. See confirm.py.

Every id argument has a matching ``*_name`` argument (plan_name, account_name,
category_name, ...). Clients such as Claude Desktop show raw arguments in their approval
prompt, so the names let the user see what is being changed. The server checks every name
against its id and refuses the call on a mismatch, so the prompt cannot be misleading.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import formatting as fmt
from .client import YnabClient, YnabError, raw_operation
from .confirm import ConfirmationStore, preview_response
from .formatting import InputError
from .tools import YnabData, _guard

CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
CHANGE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

MAX_BATCH = 50
# Longest category or category group name YNAB accepts
# ("name is too long (maximum is 50 characters)").
MAX_CATEGORY_NAME = 50

Token = Annotated[
    str | None,
    Field(
        description="Leave empty on the first call to get a preview. Only after the user "
        "approves that preview, repeat the call with the same arguments and this token."
    ),
]
PlanId = Annotated[
    str,
    Field(description="Plan (budget) id from ynab_list_plans. Required for every write."),
]
PlanName = Annotated[
    str,
    Field(
        description="Name of that plan as ynab_list_plans shows it. Checked against plan_id "
        "and shown to the user when approving."
    ),
]
NAME_HELP = "Its name as the list tools show it; required with the id and checked against it."
Cleared = Literal["cleared", "uncleared", "reconciled"]
FlagColor = Literal["red", "orange", "yellow", "green", "blue", "purple", "none"]
Frequency = Literal[
    "never",
    "daily",
    "weekly",
    "everyOtherWeek",
    "twiceAMonth",
    "every4Weeks",
    "monthly",
    "everyOtherMonth",
    "every3Months",
    "every4Months",
    "twiceAYear",
    "yearly",
    "everyOtherYear",
]
AccountType = Literal["checking", "savings", "cash", "creditCard", "otherAsset", "otherLiability"]
AMOUNT_HELP = "Amount in currency units; negative for money out (spending), positive for money in."

GoalTarget = Annotated[
    float | None,
    Field(description="Target amount in currency units; sets or changes the category goal."),
]
GoalDate = Annotated[str | None, Field(description="Goal target date, YYYY-MM-DD.")]
GoalWhole = Annotated[
    bool | None,
    Field(description="For NEED goals: true = 'Set aside another', false = 'Refill up to'."),
]
GoalFreq = Annotated[
    Literal["monthly", "weekly", "yearly"] | None,
    Field(description="Repeat the target at this frequency (needs goal_target)."),
]


class Split(BaseModel):
    """One line of a split transaction."""

    amount: float = Field(description=AMOUNT_HELP)
    category_id: str | None = Field(None, description="Category id for this line.")
    category_name: str | None = Field(None, description=NAME_HELP)
    payee_id: str | None = Field(None, description="Payee id for this line.")
    payee_name: str | None = Field(
        None,
        description="Payee name. With payee_id it must match; alone, YNAB matches or "
        "creates the payee.",
    )
    memo: str | None = Field(None, description="Memo for this line.")


class NewTransaction(BaseModel):
    account_id: str = Field(description="Account id from ynab_list_accounts.")
    account_name: str = Field(description=NAME_HELP)
    date: str = Field(description="Transaction date, YYYY-MM-DD (not in the future).")
    amount: float = Field(description=AMOUNT_HELP)
    payee_id: str | None = Field(None, description="Existing payee id from ynab_list_payees.")
    payee_name: str | None = Field(
        None,
        description="Payee name. With payee_id it must match; alone, YNAB matches or "
        "creates the payee.",
    )
    category_id: str | None = Field(
        None, description="Category id from ynab_list_categories. Omit for splits."
    )
    category_name: str | None = Field(None, description=NAME_HELP)
    memo: str | None = Field(None, description="Memo, up to 500 characters.")
    cleared: Cleared = Field("uncleared", description="Cleared status.")
    approved: bool = Field(True, description="Whether the transaction is approved.")
    flag_color: FlagColor | None = Field(None, description="Flag color.")
    splits: list[Split] | None = Field(
        None, description="Split lines; their amounts must add up to amount."
    )


class TransactionChanges(BaseModel):
    """Fields to change on an existing transaction. Omitted fields stay as they are."""

    id: str = Field(description="Transaction id from ynab_list_transactions.")
    account_id: str | None = Field(None, description="Move to this account.")
    account_name: str | None = Field(None, description=NAME_HELP)
    date: str | None = Field(None, description="New date, YYYY-MM-DD.")
    amount: float | None = Field(None, description=AMOUNT_HELP)
    payee_id: str | None = Field(None, description="New payee id.")
    payee_name: str | None = Field(
        None,
        description="New payee name. With payee_id it must match; alone, YNAB matches "
        "or creates the payee.",
    )
    category_id: str | None = Field(None, description="New category id.")
    category_name: str | None = Field(None, description=NAME_HELP)
    memo: str | None = Field(None, description="New memo (empty string clears it).")
    cleared: Cleared | None = Field(None, description="New cleared status.")
    approved: bool | None = Field(None, description="Approve (true) or unapprove (false).")
    flag_color: FlagColor | None = Field(None, description="New flag color; 'none' clears it.")


def _flag(value: str | None) -> str | None:
    return None if value is None else ("" if value == "none" else value)


def _date(value: str | None, field: str) -> str | None:
    parsed = fmt.parse_date(value, field)
    return parsed.isoformat() if parsed else None


def _require_text(value: str | None, field: str, max_len: int = 200) -> str:
    text = (value or "").strip()
    if not text:
        raise InputError(f"{field} must not be empty.")
    if len(text) > max_len:
        raise InputError(f"{field} must be at most {max_len} characters.")
    return text


def _long_name_message(name: str, kind: str) -> str:
    return (
        f"The {kind}'s current name is {len(name)} characters. YNAB allows at most "
        f"{MAX_CATEGORY_NAME} and appears to check the existing name on every update, even "
        f"when the name is not being changed. Rename the {kind} to {MAX_CATEGORY_NAME} "
        "characters or fewer (pass new_name in the same call, or rename it in the YNAB app) "
        "and try again."
    )


def _clean(body: dict[str, Any]) -> dict[str, Any]:
    """Drop fields that were not provided (None). Explicit clears are handled by callers."""
    return {k: v for k, v in body.items() if v is not None}


# Unicode categories ignored when comparing names: symbols (emoji, arrows, currency signs),
# formatting characters (zero-width joiners) and unassigned/private-use code points.
_IGNORED_CATEGORIES = {"So", "Sk", "Sm", "Sc", "Cf", "Co", "Cn"}


def _norm(text: str | None) -> str:
    """Normalize a name for comparison: case, spacing, emoji and symbols don't matter.

    YNAB names often carry emoji ("Weekend Treats 🍦"), which the assistant may leave out.
    Letters, digits and punctuation must still match.
    """
    text = unicodedata.normalize("NFKC", text or "")
    kept = "".join(
        ch
        for ch in text
        if unicodedata.category(ch) not in _IGNORED_CATEGORIES
        and not 0xFE00 <= ord(ch) <= 0xFE0F  # emoji variation selectors
    )
    stripped = " ".join(kept.split()).casefold()
    # A name made only of symbols must then match exactly (ignoring case and spacing).
    return stripped or " ".join(text.split()).casefold()


MIN_PREFIX = 3


def _names_match(label: str | None, actual: str | None) -> bool:
    """True when ``label`` is the real name or the start of it.

    Lenient on purpose: case, spacing, emoji and symbols are ignored, and a shortened name
    is accepted if it is how the real name begins and ends on a word boundary (so
    "Home Maintenance" matches "Home Maintenance, Repairs, and House Things", but
    "Home Maint" does not). The id is what decides what changes; the name is for display.
    """
    short, full = _norm(label), _norm(actual)
    if not short:
        return False
    if short == full:
        return True
    if len(short) < MIN_PREFIX or not full.startswith(short):
        return False
    return not full[len(short)].isalnum()


class Names:
    """Resolves ids to names for previews, using cached reads. Also checks ids exist and
    that the ``*_name`` labels sent alongside them match."""

    def __init__(self, data: YnabData, plan: str) -> None:
        self._d = data
        self._plan = plan
        self._maps: dict[str, dict[str, str]] = {}

    def _map(self, kind: str) -> dict[str, str]:
        if kind not in self._maps:
            if kind == "account":
                items, _ = self._d.accounts(self._plan)
                self._maps[kind] = {a["id"]: a.get("name", "") for a in items}
            elif kind == "category":
                self._maps[kind], _ = self._d.category_names(self._plan)
            elif kind == "group":
                groups, _ = self._d.category_groups(self._plan)
                self._maps[kind] = {g["id"]: g.get("name", "") for g in groups}
            else:
                items, _ = self._d.payees(self._plan)
                self._maps[kind] = {p["id"]: p.get("name", "") for p in items}
        return self._maps[kind]

    def name(self, kind: str, id_: str | None, field: str) -> str | None:
        if not id_:
            return None
        found = self._map(kind).get(id_)
        if found is None:
            lookup = {
                "account": "ynab_list_accounts",
                "category": "ynab_list_categories",
                "group": "ynab_list_categories",
                "payee": "ynab_list_payees",
            }[kind]
            raise InputError(f"{field} {id_!r} was not found in this plan; use {lookup}.")
        return found

    def check(
        self, kind: str, id_: str | None, label: str | None, id_field: str, label_field: str
    ) -> str | None:
        """Return the name for ``id_`` after checking ``label`` matches it."""
        if not id_:
            if label is not None:
                raise InputError(f"{label_field} was given without {id_field}.")
            return None
        actual = self.name(kind, id_, id_field)
        if label is None:
            raise InputError(
                f"{label_field} is required with {id_field} so the user approves a name, not "
                f"an id. For this id it is {actual!r}."
            )
        if not _names_match(label, actual):
            raise InputError(
                f"{label_field} {label!r} does not match {id_field}, which is {actual!r}. "
                "Make sure the id is the one you meant."
            )
        return actual

    def payee(
        self, id_: str | None, label: str | None, id_field: str, label_field: str
    ) -> tuple[str | None, str | None]:
        """Payee rules: an id needs a matching name; a name alone is a new or matched payee.

        Returns the (payee_id, payee_name) pair to send: never both.
        """
        if id_:
            self.check("payee", id_, label, id_field, label_field)
            return id_, None
        return None, (label or "").strip() or None


def register_write_tools(server: MCPServer, client: YnabClient) -> None:
    """Register every YNAB write tool on ``server``."""
    d = YnabData(client)
    c = client
    store = ConfirmationStore()

    def tool(annotations: ToolAnnotations):
        def decorator(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
            server.tool(annotations=annotations)(_guard(fn))
            return fn

        return decorator

    def verify_plan(plan_id: str, plan_name: str) -> tuple[str, str]:
        """Writes never use the default plan: the id must be real and its name must match."""
        plan_id = plan_id.strip()
        if plan_id in ("last-used", "default"):
            raise InputError(
                "Writes need the plan's real id and name, not 'last-used' or 'default'. "
                "Call ynab_list_plans to get them."
            )
        data, _ = c.cached(
            ("plans", False),
            lambda _k: c.call(
                c.plans_api.get_plans_without_preload_content, include_accounts=False
            ),
        )
        plans = {p["id"]: p.get("name", "") for p in data.get("plans") or []}
        actual = plans.get(plan_id)
        if actual is None:
            raise InputError(f"plan_id {plan_id!r} was not found; call ynab_list_plans.")
        if not _names_match(plan_name, actual):
            raise InputError(
                f"plan_name {plan_name!r} does not match plan_id, which is {actual!r}. "
                "Make sure the id is the one you meant."
            )
        return plan_id, actual

    def two_step(
        tool_name: str,
        token: str | None,
        request: dict[str, Any],
        summary: str,
        details: dict[str, Any],
        execute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if not token:
            return preview_response(summary, details, store.issue(tool_name, request))
        store.redeem(token, tool_name, request)
        return {"status": "done", "summary": summary, **execute()}

    # ------------------------------------------------------------ transactions

    def transaction_body(t: NewTransaction, idx: str, names: Names) -> dict[str, Any]:
        amount = fmt.to_milliunits(t.amount, f"{idx}amount")
        names.check(
            "account", t.account_id, t.account_name, f"{idx}account_id", f"{idx}account_name"
        )
        names.check(
            "category", t.category_id, t.category_name, f"{idx}category_id", f"{idx}category_name"
        )
        payee_id, payee_name = names.payee(
            t.payee_id, t.payee_name, f"{idx}payee_id", f"{idx}payee_name"
        )
        body = _clean(
            {
                "account_id": t.account_id,
                "date": _date(t.date, f"{idx}date"),
                "amount": amount,
                "payee_id": payee_id,
                "payee_name": payee_name,
                "category_id": t.category_id,
                "memo": t.memo,
                "cleared": t.cleared,
                "approved": t.approved,
                "flag_color": _flag(t.flag_color),
            }
        )
        if t.splits:
            if t.category_id:
                raise InputError(f"{idx}category_id must be omitted when splits are given.")
            subs = []
            for j, sp in enumerate(t.splits):
                at = f"{idx}splits[{j}]."
                names.check(
                    "category",
                    sp.category_id,
                    sp.category_name,
                    f"{at}category_id",
                    f"{at}category_name",
                )
                sub_payee_id, sub_payee_name = names.payee(
                    sp.payee_id, sp.payee_name, f"{at}payee_id", f"{at}payee_name"
                )
                subs.append(
                    _clean(
                        {
                            "amount": fmt.to_milliunits(sp.amount, f"{at}amount"),
                            "category_id": sp.category_id,
                            "payee_id": sub_payee_id,
                            "payee_name": sub_payee_name,
                            "memo": sp.memo,
                        }
                    )
                )
            if sum(s["amount"] for s in subs) != amount:
                raise InputError(
                    f"{idx}split amounts add up to {fmt.amount(sum(s['amount'] for s in subs))} "
                    f"but amount is {fmt.amount(amount)}; they must match."
                )
            body["subtransactions"] = subs
        return body

    def transaction_preview(body: dict[str, Any], names: Names, idx: str) -> dict[str, Any]:
        return fmt.compact(
            {
                "date": body.get("date"),
                "amount": fmt.amount(body.get("amount")),
                "account": names.name("account", body.get("account_id"), f"{idx}account_id"),
                "payee": names.name("payee", body.get("payee_id"), f"{idx}payee_id")
                or body.get("payee_name"),
                "category": names.name("category", body.get("category_id"), f"{idx}category_id"),
                "memo": body.get("memo"),
                "cleared": body.get("cleared"),
                "approved": body.get("approved"),
                "flag_color": body.get("flag_color"),
                "splits": [
                    fmt.compact(
                        {
                            "amount": fmt.amount(s["amount"]),
                            "category": names.name(
                                "category", s.get("category_id"), f"{idx}splits.category_id"
                            ),
                            "payee": names.name("payee", s.get("payee_id"), f"{idx}splits.payee_id")
                            or s.get("payee_name"),
                            "memo": s.get("memo"),
                        }
                    )
                    for s in body.get("subtransactions", [])
                ]
                or None,
            }
        )

    @tool(CREATE)
    def ynab_create_transactions(
        plan_id: PlanId,
        plan_name: PlanName,
        transactions: Annotated[
            list[NewTransaction],
            Field(min_length=1, max_length=MAX_BATCH, description="Transactions to create."),
        ],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Create one or more transactions (including split transactions).

        Two steps: call without confirmation_token to get a preview, show it to the user,
        and only after they approve call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        many = len(transactions) > 1
        names = Names(d, plan)
        bodies = [
            transaction_body(t, f"transactions[{i}]." if many else "", names)
            for i, t in enumerate(transactions)
        ]
        previews = [
            transaction_preview(b, names, f"transactions[{i}]." if many else "")
            for i, b in enumerate(bodies)
        ]
        total = sum(b["amount"] for b in bodies)
        summary = (
            f"Create {len(bodies)} transaction{'s' if many else ''} totalling {fmt.amount(total)}"
        )

        def execute() -> dict[str, Any]:
            data = c.call(
                c.transactions_api.create_transaction_without_preload_content,
                write=True,
                plan_id=plan,
                data={"transactions": bodies},
            )
            created = data.get("transactions") or (
                [data["transaction"]] if data.get("transaction") else []
            )
            return fmt.compact(
                {
                    "created": [fmt.transaction_row(t) for t in created],
                    "duplicate_import_ids": data.get("duplicate_import_ids"),
                }
            )

        return two_step(
            "ynab_create_transactions",
            confirmation_token,
            {"plan": plan, "transactions": bodies},
            summary,
            {"plan": plan_label, "transactions": previews},
            execute,
        )

    def changes_body(ch: TransactionChanges, idx: str, names: Names) -> dict[str, Any]:
        names.check(
            "account", ch.account_id, ch.account_name, f"{idx}account_id", f"{idx}account_name"
        )
        names.check(
            "category",
            ch.category_id,
            ch.category_name,
            f"{idx}category_id",
            f"{idx}category_name",
        )
        payee_id, payee_name = names.payee(
            ch.payee_id, ch.payee_name, f"{idx}payee_id", f"{idx}payee_name"
        )
        body = _clean(
            {
                "account_id": ch.account_id,
                "date": _date(ch.date, f"{idx}date"),
                "amount": fmt.to_milliunits(ch.amount, f"{idx}amount"),
                "payee_id": payee_id,
                "payee_name": payee_name,
                "category_id": ch.category_id,
                "memo": ch.memo,
                "cleared": ch.cleared,
                "approved": ch.approved,
                "flag_color": _flag(ch.flag_color),
            }
        )
        if not body:
            raise InputError(f"{idx}no fields to change were given.")
        return body

    def changes_preview(body: dict[str, Any], names: Names, idx: str) -> dict[str, Any]:
        out = dict(body)
        if "amount" in out:
            out["amount"] = fmt.amount(out["amount"])
        for key, kind in (("account_id", "account"), ("category_id", "category")):
            if key in out:
                out[kind] = names.name(kind, out.pop(key), f"{idx}{key}")
        if "payee_id" in out:
            out["payee"] = names.name("payee", out.pop("payee_id"), f"{idx}payee_id")
        if "payee_name" in out:
            out["payee"] = out.pop("payee_name")
        return out

    def current_transaction(plan: str, transaction_id: str) -> dict[str, Any]:
        txn, _ = d.single(
            ("transaction", plan, transaction_id),
            c.transactions_api.get_transaction_by_id_without_preload_content,
            "transaction",
            plan_id=plan,
            transaction_id=transaction_id,
        )
        return txn

    @tool(CHANGE)
    def ynab_update_transaction(
        plan_id: PlanId,
        plan_name: PlanName,
        changes: Annotated[
            TransactionChanges, Field(description="Transaction id and the fields to change.")
        ],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Change fields of one existing transaction (date, amount, payee, category, memo,
        cleared, approved, flag, account). Fields you omit are kept. Splits cannot be edited.

        Two steps: call without confirmation_token to preview the before/after, show it to
        the user, and only after they approve call again with the same arguments and token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        names = Names(d, plan)
        body = changes_body(changes, "", names)
        current = current_transaction(plan, changes.id)
        is_split = bool(fmt.live(current.get("subtransactions")))
        if is_split and ({"amount", "category_id"} & body.keys()):
            raise InputError(
                "This is a split transaction; its amount and categories cannot be changed "
                "through the API. Change them in the YNAB app, or delete and recreate it."
            )
        before = fmt.transaction_row(current)
        after = changes_preview(body, names, "")
        # PUT replaces the transaction, so send the current values for everything not changed.
        full = _clean(
            {
                "account_id": current.get("account_id"),
                "date": current.get("date"),
                "amount": current.get("amount"),
                "payee_id": current.get("payee_id"),
                "category_id": current.get("category_id"),
                "memo": current.get("memo"),
                "cleared": current.get("cleared"),
                "approved": current.get("approved"),
                "flag_color": current.get("flag_color"),
            }
        )
        if is_split:
            full.pop("category_id", None)  # the parent of a split has no real category
        if "payee_name" in body:
            full.pop("payee_id", None)
        full.update(body)

        def execute() -> dict[str, Any]:
            data = c.call(
                c.transactions_api.update_transaction_without_preload_content,
                write=True,
                plan_id=plan,
                transaction_id=changes.id,
                data={"transaction": full},
            )
            return {"transaction": fmt.transaction_row(data.get("transaction") or {})}

        return two_step(
            "ynab_update_transaction",
            confirmation_token,
            {"plan": plan, "id": changes.id, "transaction": full},
            f"Update transaction on {before.get('date')} ({before.get('payee', 'no payee')}, "
            f"{before.get('amount')})",
            {"plan": plan_label, "before": before, "changes": after},
            execute,
        )

    @tool(CHANGE)
    def ynab_update_transactions(
        plan_id: PlanId,
        plan_name: PlanName,
        updates: Annotated[
            list[TransactionChanges],
            Field(
                min_length=1,
                max_length=MAX_BATCH,
                description="Each item has a transaction id and the fields to change.",
            ),
        ],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Change several existing transactions in one request, e.g. approve or recategorize
        a batch. Only the fields given for each transaction are changed.

        Two steps: call without confirmation_token to preview, show it to the user, and only
        after they approve call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        names = Names(d, plan)
        bodies = []
        previews = []
        seen: set[str] = set()
        for i, ch in enumerate(updates):
            if ch.id in seen:
                raise InputError(f"Transaction {ch.id} appears more than once in updates.")
            seen.add(ch.id)
            body = changes_body(ch, f"updates[{i}].", names)
            bodies.append({"id": ch.id, **body})
            previews.append(
                {"id": ch.id, "changes": changes_preview(body, names, f"updates[{i}].")}
            )

        def execute() -> dict[str, Any]:
            data = c.call(
                c.transactions_api.update_transactions_without_preload_content,
                write=True,
                plan_id=plan,
                data={"transactions": bodies},
            )
            return {"updated": [fmt.transaction_row(t) for t in data.get("transactions") or []]}

        return two_step(
            "ynab_update_transactions",
            confirmation_token,
            {"plan": plan, "transactions": bodies},
            f"Update {len(bodies)} transaction{'s' if len(bodies) > 1 else ''}",
            {"plan": plan_label, "updates": previews},
            execute,
        )

    @tool(CHANGE)
    def ynab_delete_transaction(
        plan_id: PlanId,
        plan_name: PlanName,
        transaction_id: Annotated[
            str, Field(description="Transaction id from ynab_list_transactions.")
        ],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Delete one transaction.

        Two steps: call without confirmation_token to see exactly what will be deleted, show
        it to the user, and only after they approve call again with the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        row = fmt.transaction_row(current_transaction(plan, transaction_id))

        def execute() -> dict[str, Any]:
            data = c.call(
                c.transactions_api.delete_transaction_without_preload_content,
                write=True,
                plan_id=plan,
                transaction_id=transaction_id,
            )
            return {"deleted": fmt.transaction_row(data.get("transaction") or {})}

        return two_step(
            "ynab_delete_transaction",
            confirmation_token,
            {"plan": plan, "id": transaction_id},
            f"Delete transaction on {row.get('date')} ({row.get('payee', 'no payee')}, "
            f"{row.get('amount')})",
            {"plan": plan_label, "transaction": row},
            execute,
        )

    @tool(CREATE)
    def ynab_import_transactions(
        plan_id: PlanId,
        plan_name: PlanName,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Ask YNAB to import new transactions from all linked (direct import) accounts,
        like pressing Import in the app.

        Two steps: call without confirmation_token, confirm with the user, then call again
        with the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)

        def execute() -> dict[str, Any]:
            data = c.call(
                c.transactions_api.import_transactions_without_preload_content,
                write=True,
                plan_id=plan,
            )
            ids = data.get("transaction_ids") or []
            return {"imported_count": len(ids), "transaction_ids": ids}

        return two_step(
            "ynab_import_transactions",
            confirmation_token,
            {"plan": plan},
            "Import new transactions from linked accounts",
            {"plan": plan_label, "effect": "New transactions from linked accounts are added."},
            execute,
        )

    # ------------------------------------------------------------ scheduled

    def current_scheduled(plan: str, scheduled_id: str) -> dict[str, Any]:
        item, _ = d.single(
            ("scheduled_transaction", plan, scheduled_id),
            c.scheduled_api.get_scheduled_transaction_by_id_without_preload_content,
            "scheduled_transaction",
            plan_id=plan,
            scheduled_transaction_id=scheduled_id,
        )
        return item

    def scheduled_preview(body: dict[str, Any], names: Names) -> dict[str, Any]:
        return fmt.compact(
            {
                "date": body.get("date"),
                "frequency": body.get("frequency"),
                "amount": fmt.amount(body.get("amount")),
                "account": names.name("account", body.get("account_id"), "account_id"),
                "payee": names.name("payee", body.get("payee_id"), "payee_id")
                or body.get("payee_name"),
                "category": names.name("category", body.get("category_id"), "category_id"),
                "memo": body.get("memo"),
                "flag_color": body.get("flag_color"),
            }
        )

    @tool(CREATE)
    def ynab_create_scheduled_transaction(
        plan_id: PlanId,
        plan_name: PlanName,
        account_id: Annotated[str, Field(description="Account id from ynab_list_accounts.")],
        account_name: Annotated[str, Field(description=NAME_HELP)],
        date: Annotated[
            str, Field(description="First date, YYYY-MM-DD; today or later (max 5 years ahead).")
        ],
        amount: Annotated[float, Field(description=AMOUNT_HELP)],
        frequency: Annotated[Frequency, Field(description="How often it repeats.")] = "never",
        payee_id: Annotated[str | None, Field(description="Existing payee id.")] = None,
        payee_name: Annotated[
            str | None,
            Field(description="Payee name. With payee_id it must match; alone, a new payee."),
        ] = None,
        category_id: Annotated[str | None, Field(description="Category id.")] = None,
        category_name: Annotated[str | None, Field(description=NAME_HELP)] = None,
        memo: Annotated[str | None, Field(description="Memo.")] = None,
        flag_color: Annotated[FlagColor | None, Field(description="Flag color.")] = None,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Create a scheduled (future or recurring) transaction, e.g. a monthly bill.

        Two steps: call without confirmation_token to preview, show it to the user, and only
        after they approve call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        names = Names(d, plan)
        names.check("account", account_id, account_name, "account_id", "account_name")
        names.check("category", category_id, category_name, "category_id", "category_name")
        payee_id, payee_name = names.payee(payee_id, payee_name, "payee_id", "payee_name")
        body = _clean(
            {
                "account_id": account_id,
                "date": _date(date, "date"),
                "amount": fmt.to_milliunits(amount, "amount"),
                "frequency": frequency,
                "payee_id": payee_id,
                "payee_name": payee_name,
                "category_id": category_id,
                "memo": memo,
                "flag_color": _flag(flag_color),
            }
        )
        preview = scheduled_preview(body, names)

        def execute() -> dict[str, Any]:
            data = c.call(
                c.scheduled_api.create_scheduled_transaction_without_preload_content,
                write=True,
                plan_id=plan,
                data={"scheduled_transaction": body},
            )
            return {
                "scheduled_transaction": fmt.scheduled_row(data.get("scheduled_transaction") or {})
            }

        return two_step(
            "ynab_create_scheduled_transaction",
            confirmation_token,
            {"plan": plan, "scheduled_transaction": body},
            f"Create {frequency} scheduled transaction of {preview.get('amount')} "
            f"starting {preview.get('date')}",
            {"plan": plan_label, "scheduled_transaction": preview},
            execute,
        )

    @tool(CHANGE)
    def ynab_update_scheduled_transaction(
        plan_id: PlanId,
        plan_name: PlanName,
        scheduled_transaction_id: Annotated[
            str, Field(description="Id from ynab_list_scheduled_transactions.")
        ],
        account_id: Annotated[str | None, Field(description="New account id.")] = None,
        account_name: Annotated[str | None, Field(description=NAME_HELP)] = None,
        date: Annotated[str | None, Field(description="New next date, YYYY-MM-DD.")] = None,
        amount: Annotated[float | None, Field(description=AMOUNT_HELP)] = None,
        frequency: Annotated[Frequency | None, Field(description="New frequency.")] = None,
        payee_id: Annotated[str | None, Field(description="New payee id.")] = None,
        payee_name: Annotated[
            str | None,
            Field(description="Payee name. With payee_id it must match; alone, a new payee."),
        ] = None,
        category_id: Annotated[str | None, Field(description="New category id.")] = None,
        category_name: Annotated[str | None, Field(description=NAME_HELP)] = None,
        memo: Annotated[str | None, Field(description="New memo.")] = None,
        flag_color: Annotated[
            FlagColor | None, Field(description="New flag color; 'none' clears it.")
        ] = None,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Change a scheduled transaction. Fields you omit are kept.

        Two steps: call without confirmation_token to preview the before/after, show it to
        the user, and only after they approve call again with the same arguments and token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        names = Names(d, plan)
        names.check("account", account_id, account_name, "account_id", "account_name")
        names.check("category", category_id, category_name, "category_id", "category_name")
        payee_id, payee_name = names.payee(payee_id, payee_name, "payee_id", "payee_name")
        changes = _clean(
            {
                "account_id": account_id,
                "date": _date(date, "date"),
                "amount": fmt.to_milliunits(amount, "amount"),
                "frequency": frequency,
                "payee_id": payee_id,
                "payee_name": payee_name,
                "category_id": category_id,
                "memo": memo,
                "flag_color": _flag(flag_color),
            }
        )
        if not changes:
            raise InputError("No fields to change were given.")
        current = current_scheduled(plan, scheduled_transaction_id)
        full = _clean(
            {
                "account_id": current.get("account_id"),
                "date": current.get("date_next"),
                "amount": current.get("amount"),
                "frequency": current.get("frequency"),
                "payee_id": current.get("payee_id"),
                "category_id": current.get("category_id"),
                "memo": current.get("memo"),
                "flag_color": current.get("flag_color"),
            }
        )
        if "payee_name" in changes:
            full.pop("payee_id", None)
        full.update(changes)
        before = fmt.scheduled_row(current)
        after = scheduled_preview(full, names)

        def execute() -> dict[str, Any]:
            data = c.call(
                c.scheduled_api.update_scheduled_transaction_without_preload_content,
                write=True,
                plan_id=plan,
                scheduled_transaction_id=scheduled_transaction_id,
                put_scheduled_transaction_wrapper={"scheduled_transaction": full},
            )
            return {
                "scheduled_transaction": fmt.scheduled_row(data.get("scheduled_transaction") or {})
            }

        return two_step(
            "ynab_update_scheduled_transaction",
            confirmation_token,
            {"plan": plan, "id": scheduled_transaction_id, "scheduled_transaction": full},
            f"Update scheduled transaction ({before.get('payee', 'no payee')}, "
            f"{before.get('amount')}, {before.get('frequency')})",
            {"plan": plan_label, "before": before, "after": after},
            execute,
        )

    @tool(CHANGE)
    def ynab_delete_scheduled_transaction(
        plan_id: PlanId,
        plan_name: PlanName,
        scheduled_transaction_id: Annotated[
            str, Field(description="Id from ynab_list_scheduled_transactions.")
        ],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Delete a scheduled transaction.

        Two steps: call without confirmation_token to see what will be deleted, show it to
        the user, and only after they approve call again with the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        row = fmt.scheduled_row(current_scheduled(plan, scheduled_transaction_id))

        def execute() -> dict[str, Any]:
            data = c.call(
                c.scheduled_api.delete_scheduled_transaction_without_preload_content,
                write=True,
                plan_id=plan,
                scheduled_transaction_id=scheduled_transaction_id,
            )
            return {"deleted": fmt.scheduled_row(data.get("scheduled_transaction") or {})}

        return two_step(
            "ynab_delete_scheduled_transaction",
            confirmation_token,
            {"plan": plan, "id": scheduled_transaction_id},
            f"Delete scheduled transaction ({row.get('payee', 'no payee')}, "
            f"{row.get('amount')}, {row.get('frequency')})",
            {"plan": plan_label, "scheduled_transaction": row},
            execute,
        )

    # ------------------------------------------------------------ budgeting

    @tool(CHANGE)
    def ynab_set_category_assigned(
        plan_id: PlanId,
        plan_name: PlanName,
        category_id: Annotated[str, Field(description="Category id from ynab_list_categories.")],
        category_name: Annotated[str, Field(description=NAME_HELP)],
        assigned: Annotated[
            float,
            Field(
                description="New total assigned (budgeted) amount for the month in currency "
                "units. This replaces the current assigned amount; it is not added to it."
            ),
        ],
        month: Annotated[
            str, Field(description="Month: 'current', 'YYYY-MM' or 'YYYY-MM-01'.")
        ] = "current",
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Set how much is assigned (budgeted) to a category in a month. To move money
        between categories, lower one and raise the other (two calls).

        Two steps: call without confirmation_token to preview the before/after, show it to
        the user, and only after they approve call again with the same arguments and token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        m = fmt.parse_month(month)
        new_amount = fmt.to_milliunits(assigned, "assigned")
        Names(d, plan).check("category", category_id, category_name, "category_id", "category_name")
        current, _ = d.single(
            ("month_category", plan, m.isoformat(), category_id),
            c.categories_api.get_month_category_by_id_without_preload_content,
            "category",
            plan_id=plan,
            month=m,
            category_id=category_id,
        )
        old = current.get("budgeted") or 0
        delta = new_amount - old

        def execute() -> dict[str, Any]:
            data = c.call(
                c.categories_api.update_month_category_without_preload_content,
                write=True,
                plan_id=plan,
                month=m,
                category_id=category_id,
                data={"category": {"budgeted": new_amount}},
            )
            return {"category": fmt.category_row(data.get("category") or {})}

        return two_step(
            "ynab_set_category_assigned",
            confirmation_token,
            {"plan": plan, "month": m.isoformat(), "id": category_id, "budgeted": new_amount},
            f"Set assigned for {current.get('name')} in {m.isoformat()[:7]} from "
            f"{fmt.amount(old)} to {fmt.amount(new_amount)}",
            {
                "plan": plan_label,
                "category": current.get("name"),
                "month": m.isoformat(),
                "assigned_before": fmt.amount(old),
                "assigned_after": fmt.amount(new_amount),
                "available_before": fmt.amount(current.get("balance")),
                "available_after": fmt.amount((current.get("balance") or 0) + delta),
                "ready_to_assign_change": fmt.amount(-delta),
            },
            execute,
        )

    def goal_fields(
        goal_target: float | None,
        goal_target_date: str | None,
        goal_needs_whole_amount: bool | None,
        goal_frequency: str | None,
    ) -> dict[str, Any]:
        if goal_frequency and goal_target_date:
            raise InputError("goal_frequency cannot be combined with goal_target_date.")
        if goal_frequency and goal_target is None:
            raise InputError("goal_frequency requires goal_target.")
        return _clean(
            {
                "goal_target": fmt.to_milliunits(goal_target, "goal_target"),
                "goal_target_date": _date(goal_target_date, "goal_target_date"),
                "goal_needs_whole_amount": goal_needs_whole_amount,
                "goal_frequency": goal_frequency,
            }
        )

    def category_preview(body: dict[str, Any], names: Names) -> dict[str, Any]:
        out = dict(body)
        if "category_group_id" in out:
            out["group"] = names.name("group", out.pop("category_group_id"), "category_group_id")
        if out.get("goal_target") is not None:
            out["goal_target"] = fmt.amount(out["goal_target"])
        return out

    @tool(CREATE)
    def ynab_create_category(
        plan_id: PlanId,
        plan_name: PlanName,
        name: Annotated[str, Field(description="Category name.")],
        category_group_id: Annotated[
            str, Field(description="Category group id (from ynab_list_categories group ids).")
        ],
        category_group_name: Annotated[str, Field(description=NAME_HELP)],
        note: Annotated[str | None, Field(description="Category note.")] = None,
        goal_target: GoalTarget = None,
        goal_target_date: GoalDate = None,
        goal_needs_whole_amount: GoalWhole = None,
        goal_frequency: GoalFreq = None,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Create a budget category in an existing category group, optionally with a goal.

        Two steps: call without confirmation_token to preview, show it to the user, and only
        after they approve call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        names = Names(d, plan)
        names.check(
            "group",
            category_group_id,
            category_group_name,
            "category_group_id",
            "category_group_name",
        )
        body = {
            "name": _require_text(name, "name", MAX_CATEGORY_NAME),
            "category_group_id": category_group_id,
            **_clean({"note": note}),
            **goal_fields(goal_target, goal_target_date, goal_needs_whole_amount, goal_frequency),
        }
        preview = category_preview(body, names)

        def execute() -> dict[str, Any]:
            data = c.call(
                c.categories_api.create_category_without_preload_content,
                write=True,
                plan_id=plan,
                data={"category": body},
            )
            return {"category": fmt.category_row(data.get("category") or {})}

        return two_step(
            "ynab_create_category",
            confirmation_token,
            {"plan": plan, "category": body},
            f"Create category '{body['name']}' in {preview.get('group')}",
            {"plan": plan_label, "category": preview},
            execute,
        )

    @tool(CHANGE)
    def ynab_update_category(
        plan_id: PlanId,
        plan_name: PlanName,
        category_id: Annotated[str, Field(description="Category id from ynab_list_categories.")],
        category_name: Annotated[
            str, Field(description="The category's current name; checked against category_id.")
        ],
        new_name: Annotated[str | None, Field(description="Rename the category to this.")] = None,
        note: Annotated[str | None, Field(description="New note (empty string clears it).")] = None,
        category_group_id: Annotated[
            str | None, Field(description="Move to this category group.")
        ] = None,
        category_group_name: Annotated[str | None, Field(description=NAME_HELP)] = None,
        goal_target: GoalTarget = None,
        goal_target_date: GoalDate = None,
        goal_needs_whole_amount: GoalWhole = None,
        goal_frequency: GoalFreq = None,
        remove_goal: Annotated[
            bool, Field(description="Remove the category's goal/target entirely.")
        ] = False,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Rename a category, change its note or group, or set/change/remove its goal.
        Fields you omit are kept. To change the assigned amount use
        ynab_set_category_assigned instead.

        Two steps: call without confirmation_token to preview, show it to the user, and only
        after they approve call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        if remove_goal and (goal_target is not None or goal_target_date or goal_frequency):
            raise InputError("Use either remove_goal or new goal fields, not both.")
        names = Names(d, plan)
        names.check("category", category_id, category_name, "category_id", "category_name")
        names.check(
            "group",
            category_group_id,
            category_group_name,
            "category_group_id",
            "category_group_name",
        )
        body = {
            **_clean(
                {
                    "name": _require_text(new_name, "new_name", MAX_CATEGORY_NAME)
                    if new_name is not None
                    else None,
                    "note": note,
                    "category_group_id": category_group_id,
                }
            ),
            **goal_fields(goal_target, goal_target_date, goal_needs_whole_amount, goal_frequency),
        }
        if remove_goal:
            body["goal_target"] = None  # YNAB removes the goal when goal_target is null.
        if not body:
            raise InputError("No fields to change were given.")
        current, _ = d.single(
            ("category", plan, category_id),
            c.categories_api.get_category_by_id_without_preload_content,
            "category",
            plan_id=plan,
            category_id=category_id,
        )
        changes = category_preview(body, names)
        if remove_goal:
            changes["goal_target"] = "removed"

        current_name = current.get("name") or ""
        long_name_note = None
        if "name" not in body and len(current_name) > MAX_CATEGORY_NAME:
            long_name_note = _long_name_message(current_name, "category")
            changes["warning"] = long_name_note

        def execute() -> dict[str, Any]:
            try:
                data = c.call(
                    raw_operation(c.categories_api, "update_category"),
                    write=True,
                    plan_id=plan,
                    category_id=category_id,
                    data={"category": body},
                )
            except YnabError as exc:
                if long_name_note and "too long" in str(exc):
                    raise YnabError(f"{exc} {long_name_note}") from None
                raise
            return {"category": fmt.category_row(data.get("category") or {})}

        return two_step(
            "ynab_update_category",
            confirmation_token,
            {"plan": plan, "id": category_id, "category": body},
            f"Update category '{current.get('name')}'",
            {"plan": plan_label, "before": fmt.category_row(current), "changes": changes},
            execute,
        )

    @tool(CREATE)
    def ynab_create_category_group(
        plan_id: PlanId,
        plan_name: PlanName,
        name: Annotated[str, Field(description="Category group name.")],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Create a category group.

        Two steps: call without confirmation_token to preview, confirm with the user, then
        call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        body = {"name": _require_text(name, "name", MAX_CATEGORY_NAME)}

        def execute() -> dict[str, Any]:
            data = c.call(
                c.categories_api.create_category_group_without_preload_content,
                write=True,
                plan_id=plan,
                data={"category_group": body},
            )
            group = data.get("category_group") or {}
            return {
                "category_group": fmt.compact({"id": group.get("id"), "name": group.get("name")})
            }

        return two_step(
            "ynab_create_category_group",
            confirmation_token,
            {"plan": plan, "category_group": body},
            f"Create category group '{body['name']}'",
            {"plan": plan_label, "category_group": body},
            execute,
        )

    @tool(CHANGE)
    def ynab_update_category_group(
        plan_id: PlanId,
        plan_name: PlanName,
        category_group_id: Annotated[str, Field(description="Category group id.")],
        category_group_name: Annotated[
            str, Field(description="The group's current name; checked against the id.")
        ],
        new_name: Annotated[str, Field(description="Rename the group to this.")],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Rename a category group.

        Two steps: call without confirmation_token to preview, confirm with the user, then
        call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        body = {"name": _require_text(new_name, "new_name", MAX_CATEGORY_NAME)}
        old = Names(d, plan).check(
            "group",
            category_group_id,
            category_group_name,
            "category_group_id",
            "category_group_name",
        )

        def execute() -> dict[str, Any]:
            data = c.call(
                c.categories_api.update_category_group_without_preload_content,
                write=True,
                plan_id=plan,
                category_group_id=category_group_id,
                data={"category_group": body},
            )
            group = data.get("category_group") or {}
            return {
                "category_group": fmt.compact({"id": group.get("id"), "name": group.get("name")})
            }

        return two_step(
            "ynab_update_category_group",
            confirmation_token,
            {"plan": plan, "id": category_group_id, "category_group": body},
            f"Rename category group '{old}' to '{body['name']}'",
            {"plan": plan_label, "name_before": old, "name_after": body["name"]},
            execute,
        )

    # ------------------------------------------------------------ payees / accounts

    @tool(CREATE)
    def ynab_create_payee(
        plan_id: PlanId,
        plan_name: PlanName,
        name: Annotated[str, Field(description="Payee name.")],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Create a payee. (Creating a transaction with payee_name also creates payees.)

        Two steps: call without confirmation_token to preview, confirm with the user, then
        call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        body = {"name": _require_text(name, "name", 500)}

        def execute() -> dict[str, Any]:
            data = c.call(
                c.payees_api.create_payee_without_preload_content,
                write=True,
                plan_id=plan,
                data={"payee": body},
            )
            return {"payee": fmt.payee_row(data.get("payee") or {})}

        return two_step(
            "ynab_create_payee",
            confirmation_token,
            {"plan": plan, "payee": body},
            f"Create payee '{body['name']}'",
            {"plan": plan_label, "payee": body},
            execute,
        )

    @tool(CHANGE)
    def ynab_update_payee(
        plan_id: PlanId,
        plan_name: PlanName,
        payee_id: Annotated[str, Field(description="Payee id from ynab_list_payees.")],
        payee_name: Annotated[
            str, Field(description="The payee's current name; checked against payee_id.")
        ],
        new_name: Annotated[str, Field(description="Rename the payee to this.")],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Rename a payee. This changes the payee on all of its existing transactions.

        Two steps: call without confirmation_token to preview, confirm with the user, then
        call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        body = {"name": _require_text(new_name, "new_name", 500)}
        old = Names(d, plan).check("payee", payee_id, payee_name, "payee_id", "payee_name")

        def execute() -> dict[str, Any]:
            data = c.call(
                c.payees_api.update_payee_without_preload_content,
                write=True,
                plan_id=plan,
                payee_id=payee_id,
                data={"payee": body},
            )
            return {"payee": fmt.payee_row(data.get("payee") or {})}

        return two_step(
            "ynab_update_payee",
            confirmation_token,
            {"plan": plan, "id": payee_id, "payee": body},
            f"Rename payee '{old}' to '{body['name']}'",
            {"plan": plan_label, "name_before": old, "name_after": body["name"]},
            execute,
        )

    @tool(CREATE)
    def ynab_create_account(
        plan_id: PlanId,
        plan_name: PlanName,
        name: Annotated[str, Field(description="Account name.")],
        type: Annotated[AccountType, Field(description="Account type.")],
        balance: Annotated[
            float,
            Field(description="Starting balance in currency units (negative for debts)."),
        ],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Create an unlinked (manual) account with a starting balance.

        Two steps: call without confirmation_token to preview, confirm with the user, then
        call again with the same arguments and the token.
        """
        plan, plan_label = verify_plan(plan_id, plan_name)
        body = {
            "name": _require_text(name, "name"),
            "type": type,
            "balance": fmt.to_milliunits(balance, "balance"),
        }

        def execute() -> dict[str, Any]:
            data = c.call(
                c.accounts_api.create_account_without_preload_content,
                write=True,
                plan_id=plan,
                data={"account": body},
            )
            return {"account": fmt.account_row(data.get("account") or {})}

        return two_step(
            "ynab_create_account",
            confirmation_token,
            {"plan": plan, "account": body},
            f"Create {type} account '{body['name']}' with balance {fmt.amount(body['balance'])}",
            {"plan": plan_label, "account": {**body, "balance": fmt.amount(body["balance"])}},
            execute,
        )
