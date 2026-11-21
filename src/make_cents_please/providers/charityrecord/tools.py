"""Charity Record tools: read exports, reconcile with YNAB, and write import files.

Charity Record has no API. These tools read the CSV files the user exports from Charity
Record into a local folder, and write new import CSVs into that folder for the user to
upload. Nothing here contacts Charity Record.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ..ynab import formatting as fmt
from ..ynab.client import YnabClient
from ..ynab.confirm import ConfirmationStore, preview_response
from ..ynab.formatting import DEFAULT_LIMIT, MAX_LIMIT, InputError
from ..ynab.tools import MERGE_TRANSACTIONS, PlanId, YnabData, _guard
from .files import IMPORT_PREFIX, Folder, import_stem
from .formats import HEADERS, TYPE_LABELS, Donation, Record, money, number, read_csv, to_csv

LOCAL_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

Limit = Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Max rows to return.")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip (use next_offset).")]
TypeName = Annotated[
    str | None,
    Field(description="items, cash, mileage, stock, vehicle, digital_asset or art."),
]
Files = Annotated[
    list[str] | None,
    Field(description="Export files to read (from charityrecord_list_files). Default: all."),
]
Token = Annotated[
    str | None,
    Field(
        description="Leave empty on the first call to get a preview. Only after the user "
        "approves it, repeat the call with the same arguments and this token."
    ),
]


def _norm(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def _names_overlap(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a in b or b in a)


class Exports:
    """Reads every export CSV in the folder (generated import files are skipped)."""

    def __init__(self, folder: Folder) -> None:
        self.folder = folder

    def export_files(self) -> list[str]:
        return [
            self.folder.relative(p)
            for p in self.folder.csv_files()
            if not p.name.startswith(IMPORT_PREFIX)
        ]

    def records(self, files: list[str] | None = None) -> list[Record]:
        names = files or self.export_files()
        typed: list[Record] = []
        overall: list[Record] = []
        for name in names:
            kind, recs = read_csv(self.folder.read(name), name)
            (overall if kind == "overall" else typed).extend(recs)
        # Like Charity Record, prefer type-specific files over the summary file: drop
        # Overall rows for any (type, year) that a type-specific file already covers.
        covered = {(r.type, r.year) for r in typed}
        return typed + [r for r in overall if (r.type, r.year) not in covered]


def _sort_key(r: Record) -> tuple[date, str]:
    # "Various" (recurring) donations sort at the end of their year.
    return (r.day or date(r.year or 1, 12, 31), r.charity.casefold())


def _filter(
    records: list[Record],
    year: int | None,
    charity: str | None,
    kind: str | None,
    since: date | None,
    until: date | None,
) -> list[Record]:
    needle = _norm(charity)
    out = []
    for r in records:
        if year and r.year != year:
            continue
        if kind and r.type != kind:
            continue
        if needle and needle not in _norm(r.charity):
            continue
        # Recurring donations dated "Various" can't be placed inside a date range.
        if (since or until) and r.day is None:
            continue
        if since and r.day < since:
            continue
        if until and r.day > until:
            continue
        out.append(r)
    out.sort(key=_sort_key, reverse=True)
    return out


def register(server: MCPServer, folder: Folder, client: YnabClient | None) -> None:
    exports = Exports(folder)
    store = ConfirmationStore()
    d = YnabData(client) if client else None

    def tool(annotations: ToolAnnotations):
        def decorator(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
            server.tool(annotations=annotations)(_guard(fn))
            return fn

        return decorator

    def check_kind(kind: str | None) -> str | None:
        if kind and kind not in HEADERS:
            raise InputError(f"type must be one of: {', '.join(HEADERS)}.")
        return kind

    # ------------------------------------------------------------ reading

    @tool(LOCAL_READ)
    def charityrecord_list_files() -> dict[str, Any]:
        """List the CSV files in the Charity Record folder: exports the user downloaded
        from Charity Record and import files generated here. Shows each file's detected
        type, years and donation count.
        """
        rows = []
        for path in folder.csv_files():
            name = folder.relative(path)
            entry: dict[str, Any] = {
                "file": name,
                "generated_import": path.name.startswith(IMPORT_PREFIX),
            }
            try:
                kind, recs = read_csv(folder.read(name), name)
                entry.update(
                    type=kind,
                    donations=len(recs),
                    years=sorted({r.year for r in recs if r.year}),
                )
            except InputError as exc:
                entry["error"] = str(exc)
            rows.append(entry)
        return {"folder": str(folder.root), "files": rows}

    @tool(LOCAL_READ)
    def charityrecord_list_donations(
        year: Annotated[int | None, Field(description="Tax year, e.g. 2025.")] = None,
        charity: Annotated[str | None, Field(description="Text to find in the charity.")] = None,
        type: TypeName = None,
        since_date: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
        until_date: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
        files: Files = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List donations recorded in Charity Record exports (newest first). Rows with the
        same charity, address and date are one donation, as in Charity Record. Includes
        totals for everything that matched.
        """
        records = _filter(
            exports.records(files),
            year,
            charity,
            check_kind(type),
            fmt.parse_date(since_date, "since_date"),
            fmt.parse_date(until_date, "until_date"),
        )
        total = sum((r.value for r in records), Decimal("0"))
        miles = sum((r.miles for r in records), Decimal("0"))
        return fmt.page(
            [r.as_row() for r in records],
            limit,
            offset,
            totals=fmt.compact({"value": money(total), "miles": number(miles) if miles else None}),
        )

    @tool(LOCAL_READ)
    def charityrecord_summarize(
        year: Annotated[int | None, Field(description="Tax year; omit for all years.")] = None,
        files: Files = None,
    ) -> dict[str, Any]:
        """Summarize Charity Record exports: totals by year, by donation type and by
        charity (largest first).
        """
        records = _filter(exports.records(files), year, None, None, None, None)
        by_year: dict[int, Decimal] = defaultdict(Decimal)
        by_type: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {"value": Decimal("0"), "count": Decimal("0"), "miles": Decimal("0")}
        )
        by_charity: dict[str, Decimal] = defaultdict(Decimal)
        for r in records:
            by_year[r.year or 0] += r.value
            t = by_type[r.type]
            t["value"] += r.value
            t["count"] += 1
            t["miles"] += r.miles
            by_charity[r.charity] += r.value
        return {
            "donations": len(records),
            "total": money(sum(by_year.values(), Decimal("0"))),
            "by_year": {(str(y) if y else "unknown"): money(v) for y, v in sorted(by_year.items())},
            "by_type": {
                k: fmt.compact(
                    {
                        "value": money(v["value"]),
                        "donations": int(v["count"]),
                        "miles": number(v["miles"]) if v["miles"] else None,
                    }
                )
                for k, v in sorted(by_type.items())
            },
            "by_charity": [
                {"charity": name, "value": money(v)}
                for name, v in sorted(by_charity.items(), key=lambda kv: kv[1], reverse=True)
            ][:25],
        }

    # ------------------------------------------------------------ YNAB

    def ynab_donations(
        plan_id: str | None,
        since: date,
        until: date,
        categories: list[str] | None,
        payees: list[str] | None,
    ) -> list[dict[str, Any]]:
        if client is None or d is None:
            raise InputError("YNAB is not available in this server.")
        if not categories and not payees:
            raise InputError("Give category_names and/or payee_names to find donations.")
        plan = client.plan(plan_id)
        data, _ = client.cached(
            ("transactions", plan, "all", None, since.isoformat(), until.isoformat(), None),
            lambda k: client.call(
                client.transactions_api.get_transactions_without_preload_content,
                plan_id=plan,
                since_date=since,
                until_date=until,
                type=None,
                last_knowledge_of_server=k,
            ),
            MERGE_TRANSACTIONS,
        )
        accounts, _ = d.accounts(plan)
        account_types = {a["id"]: a.get("type") for a in accounts}
        cat_needles = [_norm(c) for c in categories or []]
        payee_needles = [_norm(p) for p in payees or []]

        def wanted(category: str | None, payee: str | None) -> bool:
            cat, pay = _norm(category), _norm(payee)
            return any(n and n in cat for n in cat_needles) or any(
                n and n in pay for n in payee_needles
            )

        found = []
        for t in fmt.live(data.get("transactions")):
            day = t.get("date") or ""
            if not since.isoformat() <= day <= until.isoformat():
                continue
            lines = fmt.live(t.get("subtransactions")) or [t]
            for line in lines:
                payee = line.get("payee_name") or t.get("payee_name")
                amount = line.get("amount") or 0
                if amount >= 0 or not wanted(line.get("category_name"), payee):
                    continue
                kind = account_types.get(t.get("account_id"))
                found.append(
                    fmt.compact(
                        {
                            "type": "cash",
                            "charity": payee or "Unknown payee",
                            "date": day,
                            "amount": float(Decimal(-amount) / 1000),
                            "payment_type": {"creditCard": "Credit Card", "cash": "Cash"}.get(
                                kind or "", "Other"
                            ),
                            "description": line.get("memo") or t.get("memo"),
                            "source_id": line.get("id") or t.get("id"),
                        }
                    )
                )
        found.sort(key=lambda x: x["date"])
        return found

    # These two need YNAB data, so they exist only when a YNAB token is configured.
    ynab_tool = tool(LOCAL_READ) if client is not None else (lambda fn: fn)

    @ynab_tool
    def charityrecord_find_ynab_donations(
        since_date: Annotated[str, Field(description="YYYY-MM-DD, e.g. 2025-01-01.")],
        until_date: Annotated[str, Field(description="YYYY-MM-DD, e.g. 2025-12-31.")],
        category_names: Annotated[
            list[str] | None,
            Field(description="YNAB category names (or parts) that hold donations."),
        ] = None,
        payee_names: Annotated[
            list[str] | None, Field(description="Payee names (or parts) that are charities.")
        ] = None,
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Find donation transactions in YNAB (outflows in the given categories or to the
        given payees) and return them as cash donation entries ready for
        charityrecord_create_import_files or charityrecord_reconcile.
        """
        since = fmt.parse_date(since_date, "since_date")
        until = fmt.parse_date(until_date, "until_date")
        found = ynab_donations(plan_id, since, until, category_names, payee_names)
        total = sum((Decimal(str(x["amount"])) for x in found), Decimal("0"))
        return {"count": len(found), "total": money(total), "donations": found}

    @ynab_tool
    def charityrecord_reconcile(
        since_date: Annotated[str, Field(description="YYYY-MM-DD.")],
        until_date: Annotated[str, Field(description="YYYY-MM-DD.")],
        category_names: Annotated[
            list[str] | None, Field(description="YNAB categories that hold donations.")
        ] = None,
        payee_names: Annotated[
            list[str] | None, Field(description="YNAB payees that are charities.")
        ] = None,
        date_tolerance_days: Annotated[int, Field(ge=0, le=31)] = 3,
        files: Files = None,
        plan_id: PlanId = None,
    ) -> dict[str, Any]:
        """Compare cash donations in YNAB with Charity Record exports for a date range.
        Reports donations in YNAB but missing from Charity Record, donations only in
        Charity Record, and likely matches whose amounts differ.
        """
        since = fmt.parse_date(since_date, "since_date")
        until = fmt.parse_date(until_date, "until_date")
        ynab_rows = ynab_donations(plan_id, since, until, category_names, payee_names)
        cash = [r for r in exports.records(files) if r.type == "cash"]
        cr = [r for r in cash if r.day is not None and since <= r.day <= until]
        # Recurring donations exported with the date "Various", in the years of the range.
        recurring = [
            r for r in cash if r.day is None and r.year and since.year <= r.year <= until.year
        ]
        tolerance = timedelta(days=date_tolerance_days)
        unmatched_cr = list(cr)
        matched, missing, mismatched = [], [], []
        for y in ynab_rows:
            y_day = date.fromisoformat(y["date"])
            y_amount = Decimal(str(y["amount"])).quantize(Decimal("0.01"))
            near = [r for r in unmatched_cr if abs(r.day - y_day) <= tolerance]
            exact = [r for r in near if r.value == y_amount]
            exact.sort(
                key=lambda r: (not _names_overlap(r.charity, y["charity"]), abs(r.day - y_day))
            )
            if exact:
                unmatched_cr.remove(exact[0])
                matched.append({"ynab": y, "charityrecord": exact[0].as_row()})
                continue
            named = [r for r in near if _names_overlap(r.charity, y["charity"])]
            if named:
                unmatched_cr.remove(named[0])
                mismatched.append(
                    {
                        "ynab": y,
                        "charityrecord": named[0].as_row(),
                        "difference": money(y_amount - named[0].value),
                    }
                )
                continue
            missing.append(y)
        # A YNAB payment to a charity that has a recurring ("Various") entry is probably
        # part of it rather than missing; amounts can't be compared payment by payment.
        possibly_recurring = [
            y
            for y in missing
            if any(
                r.year == int(y["date"][:4]) and _names_overlap(r.charity, y["charity"])
                for r in recurring
            )
        ]
        missing = [y for y in missing if y not in possibly_recurring]
        return {
            "range": f"{since.isoformat()} to {until.isoformat()}",
            "matched": len(matched),
            "missing_in_charityrecord": missing,
            "possibly_covered_by_recurring": possibly_recurring,
            "recurring_in_charityrecord": [r.as_row() for r in recurring],
            "only_in_charityrecord": [r.as_row() for r in unmatched_cr],
            "amount_mismatches": mismatched,
            "hint": "Pass missing_in_charityrecord entries to charityrecord_create_import_files "
            "to add them.",
        }

    # ------------------------------------------------------------ writing

    @tool(LOCAL_WRITE)
    def charityrecord_create_import_files(
        donations: Annotated[
            list[Donation],
            Field(
                min_length=1,
                max_length=500,
                description="Donations of any type: items, cash, mileage, stock, vehicle, "
                "digital_asset or art. Use values the user gave; never estimate them.",
            ),
        ],
        include_duplicates: Annotated[
            bool,
            Field(
                description="Also write donations that look already recorded in an export "
                "(Charity Record adds imported rows, so these would be recorded twice)."
            ),
        ] = False,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        """Write Charity Record import CSVs (one per donation type) into the Charity Record
        folder, from donations described in conversation (a mileage trip, a list of items
        given to a thrift store, a check to a charity...) or found in YNAB. The user then
        uploads the files at Charity Record. Mileage is recorded as miles (value = miles x
        rate, default 0.14 per mile, plus other costs).

        Two steps: call without confirmation_token to preview the exact rows, show the
        preview to the user, and only after they approve call again with the same
        arguments and the token.
        """
        existing = {(r.type, _norm(r.charity), r.day, r.value) for r in exports.records(None)}
        kept, duplicates = [], []
        for i, dn in enumerate(donations):
            key = (dn.type, _norm(dn.charity), dn.day, dn.value().quantize(Decimal("0.01")))
            if key in existing and not include_duplicates:
                duplicates.append(
                    {
                        "index": i,
                        "type": dn.type,
                        "charity": dn.charity,
                        "date": dn.date,
                        "value": money(dn.value()),
                    }
                )
            else:
                kept.append(dn)
        if not kept:
            raise InputError(
                "Every donation already appears in your Charity Record exports, so there "
                "is nothing to write. Pass include_duplicates=true to write them anyway."
            )
        by_type: dict[str, list[list[str]]] = defaultdict(list)
        totals: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {"value": Decimal("0"), "miles": Decimal("0"), "donations": Decimal("0")}
        )
        # Charity Record's importer merges rows with the same charity, address and date
        # into one donation, so separate same-day gifts to one charity would be combined.
        same_key: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
        for dn in kept:
            same_key[(dn.type, _norm(dn.charity), _norm(dn.charity_address), dn.date)].append(dn)
        warnings = [
            f"{len(group)} {TYPE_LABELS[group[0].type].lower()} donations to "
            f"{group[0].charity} on {group[0].date} share a charity, address and date; "
            "Charity Record's importer combines them into one donation. Enter them "
            "separately in Charity Record if they should stay separate."
            for group in same_key.values()
            if len(group) > 1
        ]
        for dn in kept:
            by_type[dn.type].extend(dn.rows())
            totals[dn.type]["value"] += dn.value()
            totals[dn.type]["donations"] += 1
            if dn.type == "mileage":
                totals[dn.type]["miles"] += Decimal(str(dn.miles))
        files = {kind: to_csv(HEADERS[kind], rows) for kind, rows in by_type.items()}
        preview = {
            "files": [
                {
                    "type": TYPE_LABELS[kind],
                    "header": HEADERS[kind],
                    "rows": by_type[kind],
                }
                for kind in by_type
            ],
            "totals": {
                kind: fmt.compact(
                    {
                        "donations": int(t["donations"]),
                        "value": money(t["value"]),
                        "miles": number(t["miles"]) if t["miles"] else None,
                    }
                )
                for kind, t in totals.items()
            },
            "duplicates_skipped": duplicates or None,
            "warnings": warnings or None,
            "folder": str(folder.root),
            "file_names": f"{IMPORT_PREFIX}<date-time>-<type>.csv",
        }
        grand = sum((t["value"] for t in totals.values()), Decimal("0"))
        summary = (
            f"Write {len(kept)} donation(s) totalling {money(grand)} to "
            f"{len(files)} Charity Record import file(s)"
        )
        request = {"files": files}
        if not confirmation_token:
            return preview_response(
                summary,
                fmt.compact(preview),
                store.issue("charityrecord_create_import_files", request),
            )
        store.redeem(confirmation_token, "charityrecord_create_import_files", request)
        now = datetime.now(UTC).astimezone()
        written = [folder.write_new(import_stem(kind, now), text) for kind, text in files.items()]
        return {
            "status": "done",
            "summary": summary,
            "files": written,
            "folder": str(folder.root),
            "next_step": "Upload these files in Charity Record's import page, review its "
            "import preview, then import.",
        }
