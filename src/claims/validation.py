"""Claim validation rules.

Pure Python: no AWS imports. Handlers pass in everything environment-specific
(category limits, "today", already-seen bill ids) so every rule is trivially
unit-testable.

Rules:
  * all required fields present and non-blank
  * claim_id is URL-safe (it appears in API paths)
  * category is known; amount is a positive number with <= 2 decimals and
    does not exceed the category limit
  * date is a real YYYY-MM-DD date and not in the future
  * bill_id has not been used by another claim

All problems are collected and returned together, keyed by field, so a form
can show every inline error at once.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

REQUIRED_FIELDS: tuple[str, ...] = (
    "claim_id",
    "claimant",
    "category",
    "amount",
    "bill_id",
    "date",
)

# Per-category maximum, in rupees. Handlers may override this with config.
DEFAULT_CATEGORY_LIMITS: Mapping[str, Decimal] = MappingProxyType(
    {
        "travel": Decimal("5000"),
        "food": Decimal("3000"),
        "printing": Decimal("2000"),
        "equipment": Decimal("10000"),
        "misc": Decimal("1000"),
    }
)

_CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AMOUNT_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class FieldError:
    """A validation problem attached to one input field."""

    field: str
    message: str


@dataclass(frozen=True)
class Claim:
    """A claim whose fields have all passed validation and been normalised."""

    claim_id: str
    claimant: str
    category: str
    amount: Decimal
    bill_id: str
    claim_date: date


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating one raw claim.

    Invariant: ``claim`` is set if and only if ``errors`` is empty.
    """

    claim: Claim | None
    errors: tuple[FieldError, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def reason(self) -> str:
        """Human-readable summary, stored on REJECTED claims."""
        return "; ".join(f"{e.field}: {e.message}" for e in self.errors)


def normalize_bill_id(bill_id: str) -> str:
    """Canonical form used for duplicate detection (trimmed, upper-case)."""
    return bill_id.strip().upper()


def today_in_timezone(tz_name: str, now: datetime | None = None) -> date:
    """Return today's date in ``tz_name`` (e.g. ``"Asia/Kolkata"``).

    Lambda runs in UTC, so shortly after midnight in India a claim dated
    "today" would look like it is in the future if we compared against UTC.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    return moment.astimezone(ZoneInfo(tz_name)).date()


def validate_claim(
    raw: Mapping[str, object],
    *,
    category_limits: Mapping[str, Decimal] = DEFAULT_CATEGORY_LIMITS,
    today: date | None = None,
    existing_bill_ids: Collection[str] = (),
) -> ValidationResult:
    """Validate one raw claim (a CSV row or JSON body).

    Args:
        raw: Field name -> value. Values may be strings or numbers.
        category_limits: Lower-case category -> maximum allowed amount.
        today: Reference date for the "not in the future" rule. Defaults to
            the server's local date; pass ``today_in_timezone(...)`` in
            handlers.
        existing_bill_ids: Bill ids already used, **already normalised** with
            ``normalize_bill_id`` (pass a ``set`` for O(1) lookups on big files).

    Returns:
        ``ValidationResult`` with either a normalised ``Claim`` or all errors.
    """
    today = today if today is not None else date.today()
    errors: list[FieldError] = []
    values = {name: _clean(raw.get(name)) for name in REQUIRED_FIELDS}

    for name in REQUIRED_FIELDS:
        if not values[name]:
            errors.append(FieldError(name, "is required"))

    claim_id = values["claim_id"]
    if claim_id and not _CLAIM_ID_RE.match(claim_id):
        errors.append(
            FieldError("claim_id", "must be 1-64 characters: letters, digits, '-' or '_'")
        )

    category = values["category"].lower()
    limit: Decimal | None = None
    if category:
        limit = {k.lower(): v for k, v in category_limits.items()}.get(category)
        if limit is None:
            allowed = ", ".join(sorted(k.lower() for k in category_limits))
            errors.append(
                FieldError("category", f"unknown category {category!r}; allowed: {allowed}")
            )

    amount = _check_amount(values["amount"], limit, category, errors)

    claim_date = _check_date(values["date"], today, errors)

    bill_id = normalize_bill_id(values["bill_id"])
    if bill_id and bill_id in existing_bill_ids:
        errors.append(FieldError("bill_id", f"duplicate: bill {bill_id} was already claimed"))

    if errors:
        return ValidationResult(claim=None, errors=tuple(errors))

    assert amount is not None and claim_date is not None  # guaranteed by no errors
    return ValidationResult(
        claim=Claim(
            claim_id=claim_id,
            claimant=values["claimant"],
            category=category,
            amount=amount.quantize(_TWO_PLACES),
            bill_id=bill_id,
            claim_date=claim_date,
        )
    )


def _clean(value: object) -> str:
    """Stringify and trim a raw value; ``None`` becomes an empty string."""
    return "" if value is None else str(value).strip()


def _check_amount(
    text: str,
    limit: Decimal | None,
    category: str,
    errors: list[FieldError],
) -> Decimal | None:
    """Validate the amount; append to ``errors`` and return None on failure."""
    if not text:
        return None  # "is required" already recorded
    if not _AMOUNT_RE.match(text):
        errors.append(FieldError("amount", "must be a plain number such as 250 or 250.50"))
        return None
    amount = Decimal(text)
    if amount <= 0:
        errors.append(FieldError("amount", "must be greater than 0"))
        return None
    if amount != amount.quantize(_TWO_PLACES):
        errors.append(FieldError("amount", "must have at most 2 decimal places"))
        return None
    if limit is not None and amount > limit:
        errors.append(
            FieldError("amount", f"exceeds the {category} limit of {limit:.2f}")
        )
        return None
    return amount


def _check_date(text: str, today: date, errors: list[FieldError]) -> date | None:
    """Validate the claim date; append to ``errors`` and return None on failure."""
    if not text:
        return None  # "is required" already recorded
    if not _DATE_RE.match(text):
        errors.append(FieldError("date", "must be a valid date in YYYY-MM-DD format"))
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        errors.append(FieldError("date", "must be a valid date in YYYY-MM-DD format"))
        return None
    if parsed > today:
        errors.append(FieldError("date", "must not be in the future"))
        return None
    return parsed
