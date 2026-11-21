"""Charity Record CSV formats: typed donation models, the import writer and the export reader.

Column names and allowed values follow Charity Record's Import Format Guide
(https://charityrecord.com/help/import-format-guide/). Headers must match exactly, because
Charity Record detects the file type from the header row.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..ynab.formatting import InputError

# IRS standard mileage rate for charitable driving, fixed by statute at 14 cents per mile
# (26 U.S.C. 170(i)); the guide's own example uses 0.14.
DEFAULT_MILEAGE_RATE = Decimal("0.14")

BASE = ["Charity", "Charity Address", "Donation Date"]

HEADERS: dict[str, list[str]] = {
    "items": BASE
    + [
        "Item Description",
        "Quality",
        "Quantity",
        "Fair Market Value per Unit in $",
        "Donation Value in $",
        "FMV Notes",
        "Custom Category",
        "Custom Subcategory",
        "Donation Notes",
    ],
    "cash": BASE
    + [
        "Payment Type",
        # Spelled this way in Charity Record's own cash export (seen in a real 2026
        # export); the guide spells it "Additional". Exports are re-importable, so the
        # export spelling is the one known to be accepted.
        "Addtional Payment Details",
        "Additional Description",
        "Receipt",
        "Donation Value in $",
        "Recurring Frequency",
        "Donation Notes",
    ],
    "mileage": BASE
    + [
        "Miles Driven",
        "IRS Rate per Mile in $",
        "Value of Miles Driven in $",
        "Other transportation costs in $",
        "Description of other transportation costs",
        "Donation Value in $",
        "Recurring Frequency",
        "Donation Notes",
    ],
    "stock": BASE
    + [
        "Stock Symbol",
        "Full Name of Stock",
        "Original Cost Adjusted Basis in $",
        "Original Purchase Date",
        "Fair Market Value on Date Donated in $",
        "Donation Value in $",
        "# Shares",
        "Publicly Traded",
        "How Acquired",
        "FMV Method",
        "Appraisal on File",
        "Donation Notes",
    ],
    "vehicle": BASE
    + [
        "Vehicle Type",
        "Year",
        "Make",
        "Model",
        "VIN",
        "Odometer",
        "Date Acquired",
        "How Acquired",
        "Cost Basis in $",
        "FMV in $",
        "FMV Method",
        "Received Form 1098-C",
        "Vehicle Details",
        "Donation Notes",
    ],
    "digital_asset": BASE
    + [
        "Asset Symbol",
        "Asset Name",
        "Quantity",
        "Total Basis in $",
        "Total FMV in $",
        "Date Acquired",
        "How Acquired",
        "FMV Source",
        "Transaction Details",
        "Appraisal on File",
        "Donation Notes",
    ],
    "art": BASE
    + [
        "Item Type",
        "Description",
        "Artist/Creator",
        "Physical Condition",
        "Date Acquired",
        "How Acquired",
        "Cost Basis in $",
        "FMV in $",
        "FMV Method",
        "Appraisal on File",
        "Appraiser Name",
        "Appraisal Date",
        "Photo on File",
        "Donation Notes",
    ],
}

# Alternative spellings accepted when reading, mapped to the names used above.
HEADER_ALIASES = {"Additional Payment Details": "Addtional Payment Details"}


def canonical_header(header: list[str]) -> list[str]:
    return [HEADER_ALIASES.get(h.strip(), h.strip()) for h in header]


# Number of leading columns that make up the "basic" layout, for types that have one.
BASIC_LENGTH = {"items": 8, "cash": 8, "mileage": 9, "stock": 9}

OVERALL_HEADER = [
    "Charity",
    "Charity Address",
    "Date",
    "Donation Type",
    "IRS Donation Type Classification",
    "Description",
    "Donation Value in $",
]
# Donation Type values in the Overall CSV. A real export uses "Item" and "Money" where the
# guide lists "Items" and "Cash", so singular and plural forms are all accepted.
OVERALL_TYPES = {
    "item": "items",
    "items": "items",
    "stocks": "stock",
    "vehicles": "vehicle",
    "digital assets": "digital_asset",
    "digital_asset": "digital_asset",
    "cash": "cash",
    "money": "cash",
    "mileage": "mileage",
    "stock": "stock",
    "vehicle": "vehicle",
    "digital asset": "digital_asset",
    "art": "art",
}

TYPE_LABELS = {
    "items": "Items",
    "cash": "Cash",
    "mileage": "Mileage",
    "stock": "Stock",
    "vehicle": "Vehicle",
    "digital_asset": "Digital Asset",
    "art": "Art",
}

# ------------------------------------------------------------------ value helpers


def money(value: Decimal | float | int | str | None) -> str:
    if value is None or value == "":
        return ""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def number(value: Decimal | float | int | None) -> str:
    """Quantities and miles: no trailing zeros, no exponent."""
    if value is None:
        return ""
    text = format(Decimal(str(value)).normalize(), "f")
    return text


def long_date(d: date) -> str:
    """The guide's own example style: "October 7, 2025"."""
    return f"{d:%B} {d.day}, {d.year}"


def yes_no(value: bool | None, upper: bool = False) -> str:
    if value is None:
        return ""
    text = "Yes" if value else "No"
    return text.upper() if upper else text


DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y")


def parse_any_date(text: str | None) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_money(text: str | None) -> Decimal | None:
    cleaned = (text or "").replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


# ------------------------------------------------------------------ donation models

Positive = Annotated[float, Field(gt=0)]
NonNegative = Annotated[float, Field(ge=0)]
YesNo = Annotated[bool | None, Field(description="True = Yes, false = No.")]
HowAcquiredBasic = Literal["Purchased", "Gift", "Inheritance"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charity: str = Field(min_length=1, description="Charity name.")
    charity_address: str | None = Field(None, description="Charity address, if known.")
    date: str = Field(description="Donation date, YYYY-MM-DD (not in the future).")
    notes: str | None = Field(None, description="Donation notes (stored once per donation).")

    @field_validator("charity")
    @classmethod
    def _strip_charity(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        try:
            d = date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("must be YYYY-MM-DD") from exc
        if d > datetime.now(UTC).date():
            raise ValueError("must not be in the future")
        return v

    @property
    def day(self) -> date:
        return date.fromisoformat(self.date)

    def _base_cells(self) -> list[str]:
        return [self.charity, self.charity_address or "", long_date(self.day)]


class ItemLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, description="What the item is.")
    quantity: Annotated[int, Field(ge=1)] = Field(1, description="How many identical items.")
    quality: Literal["High", "Medium", "Other"] = Field(description="High, Medium or Other.")
    value_per_unit: NonNegative = Field(
        description="Fair market value per item in dollars, as the user states it."
    )
    fmv_notes: str | None = Field(None, description="How the value was determined.")
    custom_category: str | None = None
    custom_subcategory: str | None = None


class ItemsDonation(_Base):
    """Non-cash goods, e.g. clothes or household items given to a thrift store."""

    type: Literal["items"]
    items: list[ItemLine] = Field(min_length=1, description="One entry per kind of item.")

    def value(self) -> Decimal:
        return sum((Decimal(str(i.value_per_unit)) * i.quantity for i in self.items), Decimal("0"))

    def rows(self) -> list[list[str]]:
        out = []
        for n, item in enumerate(self.items):
            unit = Decimal(str(item.value_per_unit))
            out.append(
                self._base_cells()
                + [
                    item.description,
                    item.quality,
                    str(item.quantity),
                    money(unit),
                    money(unit * item.quantity),
                    item.fmv_notes or "",
                    item.custom_category or "",
                    item.custom_subcategory or "",
                    (self.notes or "") if n == 0 else "",
                ]
            )
        return out


class CashDonation(_Base):
    """Money given by cash, check, card or payroll deduction."""

    type: Literal["cash"]
    amount: Positive = Field(description="Amount in dollars.")
    payment_type: Literal[
        "Cash", "Check", "Credit Card", "Payroll Deduction", "Debit Card", "Other"
    ] = "Other"
    payment_details: str | None = Field(None, description="E.g. check number.")
    description: str | None = None
    receipt: YesNo = None
    recurring_frequency: Annotated[int | None, Field(ge=1, le=365)] = Field(
        None, description="Times per year if recurring (12 monthly, 52 weekly, 4 quarterly)."
    )
    source_id: str | None = Field(
        None, description="Optional reference such as a YNAB transaction id; not written."
    )

    def value(self) -> Decimal:
        return Decimal(str(self.amount))

    def rows(self) -> list[list[str]]:
        return [
            self._base_cells()
            + [
                self.payment_type,
                self.payment_details or "",
                self.description or "",
                yes_no(self.receipt),
                money(self.amount),
                str(self.recurring_frequency or ""),
                self.notes or "",
            ]
        ]


class MileageDonation(_Base):
    """Driving done for a charity, recorded as miles (value = miles x rate + other costs)."""

    type: Literal["mileage"]
    miles: Positive = Field(description="Miles driven.")
    rate_per_mile: Positive = Field(
        float(DEFAULT_MILEAGE_RATE), description="IRS charitable rate; defaults to 0.14."
    )
    start_location: str | None = None
    destination: str | None = None
    purpose: str | None = Field(None, description="What the trip was for.")
    other_costs: NonNegative = Field(0, description="Parking, tolls and similar, in dollars.")
    other_costs_description: str | None = None
    recurring_frequency: Annotated[int | None, Field(ge=1, le=365)] = None

    def mileage_value(self) -> Decimal:
        return (Decimal(str(self.miles)) * Decimal(str(self.rate_per_mile))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def value(self) -> Decimal:
        return self.mileage_value() + Decimal(str(self.other_costs))

    def trip_notes(self) -> str:
        parts = []
        if self.start_location or self.destination:
            parts.append(f"Drove from {self.start_location or '?'} to {self.destination or '?'}")
        if self.purpose:
            parts.append(self.purpose)
        if self.notes:
            parts.append(self.notes)
        return ". ".join(parts)

    def rows(self) -> list[list[str]]:
        return [
            self._base_cells()
            + [
                number(self.miles),
                money(self.rate_per_mile),
                money(self.mileage_value()),
                money(self.other_costs),
                self.other_costs_description or "",
                money(self.value()),
                str(self.recurring_frequency or ""),
                self.trip_notes(),
            ]
        ]


class StockDonation(_Base):
    type: Literal["stock"]
    symbol: str = Field(min_length=1)
    name: str | None = Field(None, description="Full name of the stock.")
    cost_basis: NonNegative = Field(description="Original cost / adjusted basis in dollars.")
    purchase_date: str = Field(description="YYYY-MM-DD, or 'Various' for grouped lots.")
    fair_market_value: Positive = Field(description="Fair market value on the date donated.")
    shares: Positive | None = None
    publicly_traded: bool | None = None
    how_acquired: (
        Literal["Purchased", "Gift", "Inheritance", "Stock option exercise", "RSU/Restricted stock"]
        | None
    ) = None
    fmv_method: (
        Literal[
            "Stock exchange closing price",
            "Average of high/low",
            "Qualified appraisal (for private stock)",
        ]
        | None
    ) = None
    appraisal_on_file: YesNo = None

    @field_validator("purchase_date")
    @classmethod
    def _purchase(cls, v: str) -> str:
        if v.strip().lower() == "various":
            return "Various"
        date.fromisoformat(v)
        return v

    def value(self) -> Decimal:
        return Decimal(str(self.fair_market_value))

    def rows(self) -> list[list[str]]:
        purchased = (
            self.purchase_date
            if self.purchase_date == "Various"
            else long_date(date.fromisoformat(self.purchase_date))
        )
        return [
            self._base_cells()
            + [
                self.symbol,
                self.name or "",
                money(self.cost_basis),
                purchased,
                money(self.fair_market_value),
                money(self.fair_market_value),
                number(self.shares),
                yes_no(self.publicly_traded, upper=True),
                self.how_acquired or "",
                self.fmv_method or "",
                yes_no(self.appraisal_on_file),
                self.notes or "",
            ]
        ]


def _optional_long_date(v: str | None) -> str:
    return long_date(date.fromisoformat(v)) if v else ""


class VehicleDonation(_Base):
    type: Literal["vehicle"]
    make: str = Field(min_length=1)
    model: str = Field(min_length=1)
    fmv: Positive = Field(description="Fair market value in dollars.")
    vehicle_type: (
        Literal["Car/Truck", "Motorcycle", "Boat", "RV/Camper", "Airplane", "Other"] | None
    ) = None
    year: Annotated[int | None, Field(ge=1886, le=2100)] = None
    vin: str | None = Field(None, description="17-character VIN.")
    odometer: Annotated[int | None, Field(ge=0)] = None
    date_acquired: str | None = Field(None, description="YYYY-MM-DD.")
    how_acquired: HowAcquiredBasic | None = None
    cost_basis: NonNegative | None = None
    fmv_method: (
        Literal[
            "Form 1098-C (sale price from charity)",
            "Qualified appraisal",
            "Kelley Blue Book / NADA",
            "Comparable sales",
        ]
        | None
    ) = None
    received_1098c: YesNo = None
    details: str | None = None

    @field_validator("vin")
    @classmethod
    def _vin(cls, v: str | None) -> str | None:
        if v is not None and len(v.strip()) != 17:
            raise ValueError("must be 17 characters")
        return v.strip().upper() if v else v

    @field_validator("date_acquired")
    @classmethod
    def _acq(cls, v: str | None) -> str | None:
        if v:
            date.fromisoformat(v)
        return v

    def value(self) -> Decimal:
        return Decimal(str(self.fmv))

    def rows(self) -> list[list[str]]:
        return [
            self._base_cells()
            + [
                self.vehicle_type or "",
                str(self.year or ""),
                self.make,
                self.model,
                self.vin or "",
                str(self.odometer) if self.odometer is not None else "",
                _optional_long_date(self.date_acquired),
                self.how_acquired or "",
                money(self.cost_basis),
                money(self.fmv),
                self.fmv_method or "",
                yes_no(self.received_1098c),
                self.details or "",
                self.notes or "",
            ]
        ]


class DigitalAssetDonation(_Base):
    type: Literal["digital_asset"]
    symbol: str | None = None
    name: str | None = None
    quantity: Positive = Field(description="Units donated; decimals allowed.")
    total_fmv: Positive = Field(description="Total fair market value in dollars.")
    total_basis: NonNegative | None = None
    date_acquired: str | None = Field(None, description="YYYY-MM-DD.")
    how_acquired: (
        Literal[
            "Purchased",
            "Mining rewards",
            "Staking rewards",
            "Airdrop",
            "Hard fork",
            "Gift",
            "Inheritance",
            "Other",
        ]
        | None
    ) = None
    fmv_source: str | None = None
    transaction_details: str | None = None
    appraisal_on_file: YesNo = None

    @model_validator(mode="after")
    def _symbol_or_name(self) -> DigitalAssetDonation:
        if not (self.symbol or self.name):
            raise ValueError("symbol or name is required")
        if self.date_acquired:
            date.fromisoformat(self.date_acquired)
        return self

    def value(self) -> Decimal:
        return Decimal(str(self.total_fmv))

    def rows(self) -> list[list[str]]:
        return [
            self._base_cells()
            + [
                self.symbol or "",
                self.name or "",
                number(self.quantity),
                money(self.total_basis),
                money(self.total_fmv),
                _optional_long_date(self.date_acquired),
                self.how_acquired or "",
                self.fmv_source or "",
                self.transaction_details or "",
                yes_no(self.appraisal_on_file),
                self.notes or "",
            ]
        ]


class ArtDonation(_Base):
    type: Literal["art"]
    description: str = Field(min_length=1)
    fmv: Positive = Field(description="Fair market value in dollars.")
    item_type: (
        Literal[
            "Painting",
            "Sculpture",
            "Antique",
            "Jewelry",
            "Coins/Stamps",
            "Sports Memorabilia/Collectibles",
            "Antique Furniture",
            "Other",
        ]
        | None
    ) = None
    artist: str | None = None
    condition: Literal["Excellent", "Good", "Fair", "Poor"] | None = None
    date_acquired: str | None = Field(None, description="YYYY-MM-DD.")
    how_acquired: Literal["Purchased", "Gift", "Inheritance", "Created by me"] | None = None
    cost_basis: NonNegative | None = None
    fmv_method: (
        Literal[
            "Qualified appraisal",
            "Auction records/comparable sales",
            "Art dealer quote",
            "Catalog/price guide",
        ]
        | None
    ) = None
    appraisal_on_file: YesNo = None
    appraiser_name: str | None = None
    appraisal_date: str | None = Field(None, description="YYYY-MM-DD.")
    photo_on_file: YesNo = None

    @field_validator("date_acquired", "appraisal_date")
    @classmethod
    def _dates(cls, v: str | None) -> str | None:
        if v:
            date.fromisoformat(v)
        return v

    def value(self) -> Decimal:
        return Decimal(str(self.fmv))

    def rows(self) -> list[list[str]]:
        return [
            self._base_cells()
            + [
                self.item_type or "",
                self.description,
                self.artist or "",
                self.condition or "",
                _optional_long_date(self.date_acquired),
                self.how_acquired or "",
                money(self.cost_basis),
                money(self.fmv),
                self.fmv_method or "",
                yes_no(self.appraisal_on_file),
                self.appraiser_name or "",
                _optional_long_date(self.appraisal_date),
                yes_no(self.photo_on_file),
                self.notes or "",
            ]
        ]


Donation = Annotated[
    ItemsDonation
    | CashDonation
    | MileageDonation
    | StockDonation
    | VehicleDonation
    | DigitalAssetDonation
    | ArtDonation,
    Field(discriminator="type"),
]


def to_csv(header: list[str], rows: Iterable[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


# ------------------------------------------------------------------ export reader


@dataclass
class Record:
    """One donation. Items rows with the same charity, address and date form one
    donation; every other export row is a donation of its own.

    Recurring donations are exported with the date "Various" and a Recurring Frequency;
    those have ``day=None`` and take their ``year`` from the export file name.
    """

    type: str
    charity: str
    address: str
    day: date | None
    year: int | None = None
    recurring_frequency: int | None = None
    value: Decimal = Decimal("0")
    miles: Decimal = Decimal("0")
    lines: int = 0
    descriptions: list[str] = field(default_factory=list)
    notes: str = ""
    file: str = ""

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "date": self.day.isoformat() if self.day else "Various",
            "type": self.type,
            "charity": self.charity,
            "value": money(self.value),
        }
        if self.day is None and self.year:
            row["year"] = self.year
        if self.recurring_frequency:
            row["recurring_times_per_year"] = self.recurring_frequency
        if self.address:
            row["charity_address"] = self.address
        if self.type == "mileage":
            row["miles"] = number(self.miles)
        if self.lines > 1:
            row["lines"] = self.lines
        if self.descriptions:
            row["description"] = "; ".join(self.descriptions[:5]) + (
                " ..." if len(self.descriptions) > 5 else ""
            )
        if self.notes:
            row["notes"] = self.notes
        row["file"] = self.file
        return row


def detect_type(header: list[str]) -> str:
    """Return the donation type for a header row, 'overall', or raise InputError."""
    clean = canonical_header(header)
    if clean == OVERALL_HEADER:
        return "overall"
    for kind, full in HEADERS.items():
        if clean == full:
            return kind
        basic = BASIC_LENGTH.get(kind)
        if basic and clean == full[:basic]:
            return kind
    # Tolerate extra trailing columns on a known layout.
    for kind, full in HEADERS.items():
        basic = BASIC_LENGTH.get(kind, len(full))
        if clean[:basic] == full[:basic]:
            return kind
    raise InputError(
        "Unrecognized Charity Record CSV header. Found: "
        + ", ".join(repr(h) for h in clean[:20])
        + ". Expected one of the Charity Record import/export layouts."
    )


VALUE_COLUMN = {
    "items": "Donation Value in $",
    "cash": "Donation Value in $",
    "mileage": "Donation Value in $",
    "stock": "Donation Value in $",
    "vehicle": "FMV in $",
    "digital_asset": "Total FMV in $",
    "art": "FMV in $",
}
DESCRIPTION_COLUMN = {
    "items": "Item Description",
    "cash": "Additional Description",
    "stock": "Stock Symbol",
    "vehicle": "Model",
    "digital_asset": "Asset Symbol",
    "art": "Description",
}


_YEAR_IN_NAME = re.compile(r"(?:^|[_\-/ ])((?:19|20)\d{2})(?=[_\-. ]|$)")


def year_from_name(file_name: str) -> int | None:
    """Charity Record names exports like CharityRecord_2026_cash_20261031_191403.csv."""
    match = _YEAR_IN_NAME.search(file_name.rsplit("/", 1)[-1])
    return int(match.group(1)) if match else None


def read_csv(text: str, file_name: str) -> tuple[str, list[Record]]:
    """Parse one Charity Record CSV into grouped donation records."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise InputError(f"{file_name} is empty.") from exc
    kind = detect_type(header)
    names = canonical_header(header)
    file_year = year_from_name(file_name)
    groups: dict[tuple[Any, ...], Record] = {}
    for line_no, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        row = dict(zip(names, (c.strip() for c in cells), strict=False))
        raw_date = row.get("Date" if kind == "overall" else "Donation Date") or ""
        day = parse_any_date(raw_date)
        various = day is None and raw_date.strip().lower() == "various"
        if day is None and not various:
            raise InputError(f"{file_name} line {line_no}: unreadable donation date {raw_date!r}.")
        if kind == "overall":
            row_type = OVERALL_TYPES.get((row.get("Donation Type") or "").strip().lower())
            if row_type is None:
                raise InputError(
                    f"{file_name} line {line_no}: unknown Donation Type "
                    f"{row.get('Donation Type')!r}."
                )
            value = parse_money(row.get("Donation Value in $"))
            description = row.get("Description") or ""
        else:
            row_type = kind
            value = parse_money(row.get(VALUE_COLUMN[kind]))
            description = row.get(DESCRIPTION_COLUMN.get(kind, ""), "") or ""
        charity = row.get("Charity") or ""
        address = row.get("Charity Address") or ""
        # Only an items file spreads one donation over several rows (one per item), so
        # only items rows with the same charity, address and date are grouped. Every
        # other row, including each Overall summary row, is its own donation: a real
        # export has two separate cash gifts to one charity on the same day.
        if kind == "items" and day is not None:
            key: tuple[Any, ...] = (row_type, charity.casefold(), address.casefold(), day)
        else:
            key = ("row", line_no)
        rec = groups.get(key)
        if rec is None:
            frequency = (row.get("Recurring Frequency") or "").strip()
            rec = groups[key] = Record(
                row_type,
                charity,
                address,
                day,
                year=day.year if day else file_year,
                recurring_frequency=int(frequency) if frequency.isdigit() else None,
                file=file_name,
            )
        rec.value += value or Decimal("0")
        rec.lines += 1
        if row_type == "mileage" and kind == "mileage":
            rec.miles += parse_money(row.get("Miles Driven")) or Decimal("0")
        if description:
            rec.descriptions.append(description)
        if not rec.notes and row.get("Donation Notes"):
            rec.notes = row["Donation Notes"]
    return kind, list(groups.values())
