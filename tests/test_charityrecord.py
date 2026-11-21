"""Charity Record provider: formats, file handling, export reading, reconcile and imports.

All CSV content here is synthetic; column headers follow Charity Record's import guide.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from make_cents_please.providers.charityrecord.files import Folder
from make_cents_please.providers.charityrecord.formats import (
    HEADERS,
    OVERALL_HEADER,
    Donation,
    detect_type,
    long_date,
    read_csv,
    to_csv,
)
from make_cents_please.providers.ynab.formatting import InputError

DONATION = TypeAdapter(Donation)


def q(*cells: str) -> str:
    return ",".join(f'"{c}"' for c in cells)


ITEMS_EXPORT = "\r\n".join(
    [
        ",".join(HEADERS["items"][:8]),
        q("Example Thrift", "1 Test St", "October 7, 2025", "Shirt", "High", "2", "8.00", "16.00"),
        q(
            "Example Thrift",
            "1 Test St",
            "October 7, 2025",
            "Lamp",
            "Medium",
            "1",
            "12.00",
            "12.00",
        ),
        q("Book Charity", "", "March 3, 2025", "Books", "Other", "10", "1.00", "10.00"),
    ]
)
CASH_EXPORT = "\r\n".join(
    [
        ",".join(HEADERS["cash"]),
        q(
            "Example Properties",
            "",
            "October 1, 2026",
            "Check",
            "#101",
            "",
            "Yes",
            "1200.00",
            "",
            "",
        ),
        q("Food Bank", "", "September 20, 2026", "Credit Card", "", "", "Yes", "50.00", "", ""),
    ]
)
MILEAGE_EXPORT = "\r\n".join(
    [
        ",".join(HEADERS["mileage"][:9]),
        q("Food Bank", "", "May 5, 2025", "26", "0.14", "3.64", "2.00", "Parking", "5.64"),
    ]
)
OVERALL_EXPORT = "\r\n".join(
    [
        ",".join(OVERALL_HEADER),
        q(
            "Example Thrift",
            "1 Test St",
            "October 7, 2025",
            "Items",
            "Non-Cash Donation",
            "Clothes",
            "28.00",
        ),
        q("Old Charity", "", "June 1, 2024", "Money", "Monetary Gift", "Gift", "75.00"),
    ]
)


@pytest.fixture
def crdir(tmp_path):
    return tmp_path


@pytest.fixture
def charityrecord_dir(crdir):
    return crdir


def write(crdir, name, text):
    path = crdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def parse(text):
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------- writer


def test_long_date_format():
    assert long_date(date(2025, 10, 7)) == "October 7, 2025"


def test_items_rows_one_per_item_with_notes_once():
    d = DONATION.validate_python(
        {
            "type": "items",
            "charity": "Example Thrift",
            "charity_address": "1 Test St",
            "date": "2025-10-07",
            "notes": "Fall clean-out",
            "items": [
                {"description": "Shirt", "quantity": 3, "quality": "High", "value_per_unit": 5},
                {"description": "Lamp", "quality": "Medium", "value_per_unit": 12},
            ],
        }
    )
    rows = parse(to_csv(HEADERS["items"], d.rows()))
    assert rows[0] == HEADERS["items"]
    assert rows[1] == [
        "Example Thrift",
        "1 Test St",
        "October 7, 2025",
        "Shirt",
        "High",
        "3",
        "5.00",
        "15.00",
        "",
        "",
        "",
        "Fall clean-out",
    ]
    assert rows[2][3:8] == ["Lamp", "Medium", "1", "12.00", "12.00"]
    assert rows[2][-1] == ""  # notes stored once per donation
    assert d.value() == Decimal("27")


def test_csv_quotes_every_field():
    d = DONATION.validate_python(
        {"type": "cash", "charity": "Food Bank", "date": "2025-01-02", "amount": 50}
    )
    text = to_csv(HEADERS["cash"], d.rows())
    assert text.splitlines()[1].startswith('"Food Bank","","January 2, 2025","Other"')


def test_mileage_records_miles_rate_and_values():
    d = DONATION.validate_python(
        {
            "type": "mileage",
            "charity": "Food Bank",
            "date": "2025-05-05",
            "miles": 26,
            "start_location": "Home",
            "destination": "Food Bank warehouse",
            "purpose": "Delivered meals",
            "other_costs": 2,
            "other_costs_description": "Parking",
        }
    )
    row = d.rows()[0]
    named = dict(zip(HEADERS["mileage"], row, strict=True))
    assert named["Miles Driven"] == "26"
    assert named["IRS Rate per Mile in $"] == "0.14"
    assert named["Value of Miles Driven in $"] == "3.64"
    assert named["Other transportation costs in $"] == "2.00"
    assert named["Donation Value in $"] == "5.64"
    assert named["Donation Notes"] == ("Drove from Home to Food Bank warehouse. Delivered meals")


def test_cash_recurring_and_receipt():
    d = DONATION.validate_python(
        {
            "type": "cash",
            "charity": "Church",
            "date": "2025-01-05",
            "amount": 100,
            "payment_type": "Payroll Deduction",
            "receipt": True,
            "recurring_frequency": 12,
        }
    )
    named = dict(zip(HEADERS["cash"], d.rows()[0], strict=True))
    assert named["Receipt"] == "Yes" and named["Recurring Frequency"] == "12"
    assert named["Donation Value in $"] == "100.00"


def test_minimum_vehicle_digital_asset_and_art():
    v = DONATION.validate_python(
        {
            "type": "vehicle",
            "charity": "Car Charity",
            "date": "2025-02-01",
            "make": "Ford",
            "model": "Focus",
            "fmv": 1500,
        }
    )
    assert dict(zip(HEADERS["vehicle"], v.rows()[0], strict=True))["FMV in $"] == "1500.00"
    c = DONATION.validate_python(
        {
            "type": "digital_asset",
            "charity": "X",
            "date": "2025-02-01",
            "symbol": "BTC",
            "quantity": 0.015,
            "total_fmv": 900,
        }
    )
    assert dict(zip(HEADERS["digital_asset"], c.rows()[0], strict=True))["Quantity"] == "0.015"
    a = DONATION.validate_python(
        {
            "type": "art",
            "charity": "Museum",
            "date": "2025-02-01",
            "description": "Oil painting",
            "fmv": 400,
            "condition": "Good",
            "appraisal_on_file": False,
        }
    )
    named = dict(zip(HEADERS["art"], a.rows()[0], strict=True))
    assert named["Physical Condition"] == "Good" and named["Appraisal on File"] == "No"


def test_stock_various_and_publicly_traded():
    s = DONATION.validate_python(
        {
            "type": "stock",
            "charity": "Uni",
            "date": "2025-12-01",
            "symbol": "ABC",
            "cost_basis": 100,
            "purchase_date": "various",
            "fair_market_value": 900,
            "shares": 10,
            "publicly_traded": True,
        }
    )
    named = dict(zip(HEADERS["stock"], s.rows()[0], strict=True))
    assert named["Original Purchase Date"] == "Various"
    assert named["Publicly Traded"] == "YES" and named["# Shares"] == "10"


@pytest.mark.parametrize(
    "payload, field",
    [
        (
            {
                "type": "items",
                "charity": "X",
                "date": "2025-01-01",
                "items": [{"description": "Shirt", "quality": "Great", "value_per_unit": 1}],
            },
            "quality",
        ),
        ({"type": "mileage", "charity": "X", "date": "2025-01-01"}, "miles"),
        (
            {
                "type": "vehicle",
                "charity": "X",
                "date": "2025-01-01",
                "make": "Ford",
                "model": "F",
                "fmv": 1,
                "vin": "SHORT",
            },
            "vin",
        ),
        (
            {
                "type": "digital_asset",
                "charity": "X",
                "date": "2025-01-01",
                "quantity": 1,
                "total_fmv": 1,
            },
            "symbol or name",
        ),
        ({"type": "cash", "charity": "X", "date": "2025-01-01", "amount": 0}, "amount"),
        (
            {
                "type": "cash",
                "charity": "X",
                "date": (date.today() + timedelta(days=2)).isoformat(),
                "amount": 5,
            },
            "future",
        ),
        (
            {
                "type": "items",
                "charity": "X",
                "date": "2025-01-01",
                "items": [{"description": "Shirt", "quality": "High"}],
            },
            "value_per_unit",
        ),
    ],
)
def test_validation_errors_name_the_field(payload, field):
    with pytest.raises(ValidationError) as exc:
        DONATION.validate_python(payload)
    assert field in str(exc.value)


# ---------------------------------------------------------------- reader


def test_detect_type_basic_extended_overall_and_unknown():
    assert detect_type(HEADERS["items"][:8]) == "items"
    assert detect_type(HEADERS["items"]) == "items"
    assert detect_type(HEADERS["vehicle"]) == "vehicle"
    assert detect_type(OVERALL_HEADER) == "overall"
    with pytest.raises(InputError, match="Unrecognized"):
        detect_type(["Name", "Amount"])


def test_cash_header_accepts_both_spellings():
    # Charity Record's real cash export spells it "Addtional"; the guide says "Additional".
    export_header = HEADERS["cash"]
    assert "Addtional Payment Details" in export_header
    guide_header = [
        "Additional Payment Details" if h == "Addtional Payment Details" else h
        for h in export_header
    ]
    assert detect_type(export_header) == "cash"
    assert detect_type(guide_header) == "cash"
    assert detect_type(guide_header[:8]) == "cash"


def test_read_groups_rows_into_donations():
    kind, recs = read_csv(ITEMS_EXPORT, "items.csv")
    assert kind == "items"
    thrift = next(r for r in recs if r.charity == "Example Thrift")
    assert thrift.lines == 2 and thrift.value == Decimal("28.00")
    assert len(recs) == 2


def test_same_day_cash_gifts_stay_separate():
    # A real export has two cash gifts to one charity on the same day: two donations.
    text = "\r\n".join(
        [
            q(*HEADERS["cash"]),
            q("Food Bank", "PO Box 1", "May 1, 2026", "Check", "", "", "Yes", "50", "", ""),
            q("Food Bank", "PO Box 1", "May 1, 2026", "Cash", "", "", "No", "20", "", ""),
        ]
    )
    _, recs = read_csv(text, "cash.csv")
    assert sorted(r.value for r in recs) == [Decimal("20"), Decimal("50")]
    overall = "\r\n".join(
        [
            q(*OVERALL_HEADER),
            q("Food Bank", "PO Box 1", "May 1, 2026", "Money", "Cash Donation", "", "50"),
            q("Food Bank", "PO Box 1", "May 1, 2026", "Money", "Cash Donation", "", "20"),
        ]
    )
    _, recs = read_csv(overall, "overall.csv")
    assert len(recs) == 2


def test_read_mileage_and_bad_date():
    _, recs = read_csv(MILEAGE_EXPORT, "m.csv")
    assert recs[0].miles == Decimal("26") and recs[0].value == Decimal("5.64")
    bad = ",".join(HEADERS["cash"][:8]) + "\n" + q("X", "", "someday", "Cash", "", "", "", "1")
    with pytest.raises(InputError, match="line 2"):
        read_csv(bad, "bad.csv")


# Shaped like a real Charity Record export (synthetic values): UTF-8 BOM, CRLF, every
# field quoted, recurring donations dated "Various", and singular "Item" / "Money" types.
REAL_CASH = "\ufeff" + "\r\n".join(
    [
        q(*HEADERS["cash"]),
        q("Food Bank", "PO Box 1", "March 14, 2026", "Check", "", "", "Yes", "250", "", ""),
        q(
            "Food Bank",
            "PO Box 1",
            "Various",
            "Credit Card",
            "",
            "",
            "Yes",
            "120",
            "3",
            "Monthly gift - 1/26",
        ),
    ]
)
REAL_OVERALL = "\ufeff" + "\r\n".join(
    [
        q(*OVERALL_HEADER),
        q("Food Bank", "PO Box 1", "March 14, 2026", "Money", "Cash Donation", "", "250"),
        q("Food Bank", "PO Box 1", "Various", "Money", "Cash Donation", "", "120"),
        q("Thrift", "", "July 4, 2026", "Item", "Non-Cash Donation", "Lamp", "12.00"),
    ]
)


def test_real_export_shapes():
    kind, recs = read_csv(REAL_CASH, "2026/CharityRecord_2026_cash_20261031_191403.csv")
    assert kind == "cash"
    recurring = next(r for r in recs if r.day is None)
    assert recurring.year == 2026 and recurring.recurring_frequency == 3
    assert recurring.value == Decimal("120")
    assert recurring.as_row()["date"] == "Various"
    kind, recs = read_csv(REAL_OVERALL, "CharityRecord_2026_overall_summary_x.csv")
    assert kind == "overall"
    assert sorted(r.type for r in recs) == ["cash", "cash", "items"]


def test_year_from_name():
    from make_cents_please.providers.charityrecord.formats import year_from_name

    assert year_from_name("CharityRecord_2026_cash_20261031_191403.csv") == 2026
    assert year_from_name("dir/CharityRecord_2025_all/x_2025_items.csv") == 2025
    assert year_from_name("my-donations.csv") is None


@pytest.mark.parametrize("kind", list(HEADERS))
def test_written_import_files_read_back(kind):
    """Every import file the server writes is recognized by its own reader."""
    samples = {
        "items": {"items": [{"description": "Shirt", "quality": "High", "value_per_unit": 4}]},
        "cash": {"amount": 20},
        "mileage": {"miles": 10},
        "stock": {
            "symbol": "ABC",
            "cost_basis": 1,
            "purchase_date": "2020-01-01",
            "fair_market_value": 50,
        },
        "vehicle": {"make": "Ford", "model": "F", "fmv": 900},
        "digital_asset": {"symbol": "BTC", "quantity": 0.1, "total_fmv": 60},
        "art": {"description": "Print", "fmv": 30},
    }
    d = DONATION.validate_python(
        {"type": kind, "charity": "C", "date": "2025-06-01", **samples[kind]}
    )
    text = to_csv(HEADERS[kind], d.rows())
    read_kind, recs = read_csv(text, f"charityrecord-import-20250601-120000-{kind}.csv")
    assert read_kind == kind
    assert recs[0].value == d.value().quantize(Decimal("0.01"))


# ---------------------------------------------------------------- files


def test_folder_refuses_escapes(tmp_path):
    folder = Folder(tmp_path)
    with pytest.raises(InputError, match="outside"):
        folder.resolve("../secret.csv")
    with pytest.raises(InputError, match="outside"):
        folder.resolve("/etc/passwd")


def test_folder_never_overwrites(tmp_path):
    folder = Folder(tmp_path)
    assert folder.write_new("a", "one") == "a.csv"
    assert folder.write_new("a", "two") == "a-2.csv"
    assert (tmp_path / "a.csv").read_text() == "one"


def test_folder_size_cap(tmp_path):
    (tmp_path / "big.csv").write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(InputError, match="10 MB"):
        Folder(tmp_path).read("big.csv")


def test_config_rejects_missing_folder():
    from make_cents_please.config import ConfigError, load_config

    with pytest.raises(ConfigError, match="CHARITYRECORD_DIR"):
        load_config({"YNAB_ACCESS_TOKEN": "x", "CHARITYRECORD_DIR": "/no/such/folder"})


# ---------------------------------------------------------------- tools


async def test_tools_registered_only_with_folder(server):
    names = {t.name for t in await server.list_tools()}
    assert "charityrecord_create_import_files" in names


async def test_tools_absent_without_folder(fake):
    from make_cents_please.config import load_config
    from make_cents_please.server import build_server

    server = build_server(load_config({"YNAB_ACCESS_TOKEN": "x"}))
    assert not any(t.name.startswith("charityrecord") for t in await server.list_tools())


async def test_list_files_and_donations(ok, crdir):
    write(crdir, "2025/items.csv", ITEMS_EXPORT)
    write(crdir, "2025/mileage.csv", MILEAGE_EXPORT)
    write(crdir, "notes.csv", "Name,Amount\nx,1\n")
    files = (await ok("charityrecord_list_files"))["files"]
    by_name = {f["file"]: f for f in files}
    assert by_name["2025/items.csv"]["type"] == "items"
    assert "Unrecognized" in by_name["notes.csv"]["error"]

    out = await ok(
        "charityrecord_list_donations", year=2025, files=["2025/items.csv", "2025/mileage.csv"]
    )
    assert out["total"] == 3
    assert out["totals"] == {"value": "43.64", "miles": "26"}
    assert out["items"][0]["charity"] == "Example Thrift"  # newest first
    out = await ok("charityrecord_list_donations", type="mileage", files=["2025/mileage.csv"])
    assert out["items"][0]["miles"] == "26"


async def test_overall_rows_ignored_when_type_file_covers_them(ok, crdir):
    write(crdir, "items.csv", ITEMS_EXPORT)
    write(crdir, "overall.csv", OVERALL_EXPORT)
    out = await ok("charityrecord_summarize")
    # 2025 items come from items.csv only (not double counted); 2024 cash from overall.
    assert out["by_year"] == {"2024": "75.00", "2025": "38.00"}
    assert out["by_type"]["cash"]["value"] == "75.00"


async def test_find_ynab_donations(ok, fake):
    out = await ok(
        "charityrecord_find_ynab_donations",
        since_date="2026-10-01",
        until_date="2026-10-31",
        payee_names=["example properties"],
    )
    assert out["count"] == 1
    entry = out["donations"][0]
    assert entry == {
        "type": "cash",
        "charity": "Example Properties",
        "date": "2026-10-02",
        "amount": 1200.0,
        "payment_type": "Other",
        "source_id": entry["source_id"],
    }


async def test_find_ynab_donations_split_lines_by_category(ok, fake):
    out = await ok(
        "charityrecord_find_ynab_donations",
        since_date="2026-10-01",
        until_date="2026-10-31",
        category_names=["dining"],
    )
    assert sorted(d["amount"] for d in out["donations"]) == [42.25, 100.0]


async def test_find_ynab_donations_needs_a_filter(call, fake):
    is_error, msg = await call(
        "charityrecord_find_ynab_donations", since_date="2026-10-01", until_date="2026-10-31"
    )
    assert is_error and "category_names" in msg


async def test_reconcile(ok, fake, crdir):
    write(crdir, "cash.csv", CASH_EXPORT)
    out = await ok(
        "charityrecord_reconcile",
        since_date="2026-09-01",
        until_date="2026-10-31",
        payee_names=["Example Properties", "Corner Market"],
    )
    # Rent (Oct 2 in YNAB, Oct 1 in Charity Record) matches within the 3-day tolerance.
    assert out["matched"] == 1
    # Corner Market purchases are in YNAB only (the split counts as two lines).
    assert sorted(m["amount"] for m in out["missing_in_charityrecord"]) == [42.25, 100.0, 125.5]
    assert [r["charity"] for r in out["only_in_charityrecord"]] == ["Food Bank"]


async def test_reconcile_amount_mismatch(ok, fake, crdir):
    write(crdir, "cash.csv", CASH_EXPORT.replace('"1200.00"', '"1100.00"'))
    out = await ok(
        "charityrecord_reconcile",
        since_date="2026-09-01",
        until_date="2026-10-31",
        payee_names=["Example Properties"],
    )
    assert out["amount_mismatches"][0]["difference"] == "100.00"
    assert out["missing_in_charityrecord"] == []


async def test_recurring_entries_in_summary_and_reconcile(ok, fake, crdir):
    write(crdir, "CharityRecord_2026_cash_x.csv", REAL_CASH)
    write(crdir, "CharityRecord_2026_overall_summary_x.csv", REAL_OVERALL)
    summary = await ok("charityrecord_summarize")
    # Cash comes from the cash file only; items from the overall file (no items file).
    assert summary["by_year"] == {"2026": "382.00"}
    assert summary["by_type"]["cash"]["donations"] == 2
    out = await ok("charityrecord_list_donations", year=2026, type="cash")
    assert [r["date"] for r in out["items"]] == ["Various", "2026-03-14"]

    rec = await ok(
        "charityrecord_reconcile",
        since_date="2026-10-01",
        until_date="2026-10-31",
        payee_names=["Corner Market"],
    )
    assert rec["recurring_in_charityrecord"][0]["recurring_times_per_year"] == 3
    assert rec["possibly_covered_by_recurring"] == []  # Corner Market has no recurring entry


async def test_reconcile_payment_covered_by_recurring_entry(ok, fake, crdir):
    recurring = "\r\n".join(
        [
            q(*HEADERS["cash"]),
            q("Example Properties", "", "Various", "Check", "", "", "Yes", "14400", "12", ""),
        ]
    )
    write(crdir, "CharityRecord_2026_cash_x.csv", recurring)
    rec = await ok(
        "charityrecord_reconcile",
        since_date="2026-10-01",
        until_date="2026-10-31",
        payee_names=["Example Properties"],
    )
    assert rec["missing_in_charityrecord"] == []
    assert rec["possibly_covered_by_recurring"][0]["charity"] == "Example Properties"


# ---------------------------------------------------------------- import files

MILEAGE = {
    "type": "mileage",
    "charity": "Charity X",
    "date": "2025-10-12",
    "miles": 26,
    "start_location": "Home",
    "destination": "Shelter",
}
ITEMS = {
    "type": "items",
    "charity": "Example Thrift",
    "date": "2025-10-20",
    "items": [
        {"description": "Shirt", "quantity": 3, "quality": "High", "value_per_unit": 5},
        {"description": "Lamp", "quality": "Medium", "value_per_unit": 12},
    ],
}


async def test_create_import_files_preview_then_confirm(call, crdir):
    args = {"donations": [MILEAGE, ITEMS]}
    is_error, preview = await call("charityrecord_create_import_files", **args)
    assert not is_error, preview
    assert preview["status"] == "confirmation_required"
    assert list(crdir.iterdir()) == []  # nothing written by the preview
    p = preview["preview"]
    assert p["totals"]["mileage"] == {"donations": 1, "value": "3.64", "miles": "26"}
    assert p["totals"]["items"]["value"] == "27.00"
    assert {f["type"] for f in p["files"]} == {"Mileage", "Items"}

    is_error, done = await call(
        "charityrecord_create_import_files",
        **args,
        confirmation_token=preview["confirmation_token"],
    )
    assert not is_error, done
    assert len(done["files"]) == 2
    mileage_file = next(f for f in done["files"] if f.endswith("-mileage.csv"))
    rows = parse((crdir / mileage_file).read_text(encoding="utf-8"))
    assert rows[0] == HEADERS["mileage"]
    assert rows[1][:9] == [
        "Charity X",
        "",
        "October 12, 2025",
        "26",
        "0.14",
        "3.64",
        "0.00",
        "",
        "3.64",
    ]
    items_file = next(f for f in done["files"] if f.endswith("-items.csv"))
    assert len(parse((crdir / items_file).read_text(encoding="utf-8"))) == 3


async def test_create_import_files_token_bound_to_rows(call, crdir):
    _, preview = await call("charityrecord_create_import_files", donations=[MILEAGE])
    changed = {**MILEAGE, "miles": 30}
    is_error, msg = await call(
        "charityrecord_create_import_files",
        donations=[changed],
        confirmation_token=preview["confirmation_token"],
    )
    assert is_error and "differ" in msg
    assert list(crdir.iterdir()) == []


async def test_create_import_files_skips_duplicates(call, crdir):
    write(crdir, "items.csv", ITEMS_EXPORT)
    dup = {
        "type": "items",
        "charity": "example thrift",
        "date": "2025-10-07",
        "items": [
            {"description": "Shirt", "quantity": 2, "quality": "High", "value_per_unit": 8},
            {"description": "Lamp", "quality": "Medium", "value_per_unit": 12},
        ],
    }
    _, preview = await call("charityrecord_create_import_files", donations=[dup, MILEAGE])
    assert preview["preview"]["duplicates_skipped"][0]["index"] == 0
    assert set(preview["preview"]["totals"]) == {"mileage"}
    is_error, msg = await call("charityrecord_create_import_files", donations=[dup])
    assert is_error and "include_duplicates" in msg
    _, preview = await call(
        "charityrecord_create_import_files", donations=[dup], include_duplicates=True
    )
    assert preview["status"] == "confirmation_required"


async def test_create_import_files_warns_when_import_would_merge(call, crdir):
    gift = {"type": "cash", "charity": "Food Bank", "date": "2025-05-01", "amount": 20}
    _, preview = await call(
        "charityrecord_create_import_files", donations=[gift, {**gift, "amount": 50}]
    )
    warning = preview["preview"]["warnings"][0]
    assert "2 cash donations to Food Bank on 2025-05-01" in warning
    _, preview = await call("charityrecord_create_import_files", donations=[gift])
    assert "warnings" not in preview["preview"]


async def test_create_import_files_rejects_bad_entry(call, crdir):
    bad = {**MILEAGE}
    del bad["miles"]
    is_error, msg = await call("charityrecord_create_import_files", donations=[bad])
    assert is_error and "miles" in msg
