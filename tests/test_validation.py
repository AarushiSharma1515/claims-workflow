"""Tests for claim validation rules."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from claims.validation import (
    DEFAULT_CATEGORY_LIMITS,
    REQUIRED_FIELDS,
    normalize_bill_id,
    today_in_timezone,
    validate_claim,
)

TODAY = date(2026, 10, 7)


@pytest.fixture
def row() -> dict[str, str]:
    return {
        "claim_id": "C-001",
        "claimant": "Asha Rao",
        "category": "travel",
        "amount": "1200.50",
        "bill_id": "BILL-77",
        "date": "2026-10-01",
    }


def check(row, **kwargs):
    kwargs.setdefault("today", TODAY)
    return validate_claim(row, **kwargs)


def errors_by_field(result) -> dict[str, str]:
    return {e.field: e.message for e in result.errors}


# --- happy path -----------------------------------------------------------


def test_valid_claim_is_normalised(row):
    row.update(category="  Travel ", bill_id=" bill-77 ", claimant="  Asha Rao ")
    result = check(row)
    assert result.is_valid
    assert result.errors == ()
    assert result.reason == ""
    claim = result.claim
    assert claim.claim_id == "C-001"
    assert claim.claimant == "Asha Rao"
    assert claim.category == "travel"
    assert claim.amount == Decimal("1200.50")
    assert claim.bill_id == "BILL-77"
    assert claim.claim_date == date(2026, 10, 1)


def test_amount_is_quantised_to_two_places(row):
    row["amount"] = "500"
    assert str(check(row).claim.amount) == "500.00"


def test_numeric_json_values_are_accepted(row):
    row["amount"] = 250  # int from a JSON body
    assert check(row).claim.amount == Decimal("250.00")
    row["amount"] = 250.5  # float from a JSON body
    assert check(row).claim.amount == Decimal("250.50")


def test_valid_result_has_claim_and_invalid_result_does_not(row):
    assert check(row).claim is not None
    row["amount"] = "-1"
    bad = check(row)
    assert bad.claim is None
    assert not bad.is_valid


# --- required fields --------------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_field_is_reported(row, field):
    del row[field]
    result = check(row)
    assert not result.is_valid
    assert errors_by_field(result)[field] == "is required"


@pytest.mark.parametrize("blank", ["", "   ", None])
@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_blank_field_is_reported(row, field, blank):
    row[field] = blank
    assert errors_by_field(check(row))[field] == "is required"


def test_all_errors_are_collected_together():
    result = check({})
    assert {e.field for e in result.errors} == set(REQUIRED_FIELDS)
    assert "claim_id: is required" in result.reason
    assert "; " in result.reason


def test_required_error_is_not_doubled_with_format_errors(row):
    row["amount"] = ""
    result = check(row)
    assert [e for e in result.errors if e.field == "amount"] == [
        e for e in result.errors if e.message == "is required"
    ]
    assert len([e for e in result.errors if e.field == "amount"]) == 1


# --- claim_id ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a/b", "has space", "x" * 65, "é-1", "a?b"])
def test_claim_id_must_be_url_safe(row, bad):
    row["claim_id"] = bad
    assert "claim_id" in errors_by_field(check(row))


@pytest.mark.parametrize("good", ["1", "C_1-b", "x" * 64])
def test_claim_id_accepts_url_safe_values(row, good):
    row["claim_id"] = good
    assert check(row).is_valid


# --- amount -----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "0.00", "-5", "-0.01"])
def test_amount_must_be_positive(row, bad):
    row["amount"] = bad
    assert errors_by_field(check(row))["amount"] == "must be greater than 0"


@pytest.mark.parametrize("bad", ["abc", "NaN", "inf", "1e3", "1,000", "1_000", "12.", ".5", "$5"])
def test_amount_must_be_a_plain_number(row, bad):
    row["amount"] = bad
    assert "plain number" in errors_by_field(check(row))["amount"]


def test_amount_rejects_more_than_two_decimals(row):
    row["amount"] = "10.123"
    assert "2 decimal" in errors_by_field(check(row))["amount"]


def test_amount_allows_trailing_zero_decimals(row):
    row["amount"] = "10.100"
    assert check(row).claim.amount == Decimal("10.10")


def test_amount_at_the_limit_is_allowed(row):
    row["amount"] = str(DEFAULT_CATEGORY_LIMITS["travel"])
    assert check(row).is_valid


def test_amount_over_the_limit_is_rejected(row):
    row["amount"] = "5000.01"
    message = errors_by_field(check(row))["amount"]
    assert "exceeds" in message
    assert "travel" in message
    assert "5000.00" in message


def test_limits_are_per_category(row):
    row.update(category="misc", amount="1500")
    assert "exceeds" in errors_by_field(check(row))["amount"]
    row.update(category="equipment")
    assert check(row).is_valid


def test_custom_limits_override_defaults(row):
    limits = {"travel": Decimal("100")}
    row["amount"] = "100.01"
    assert "exceeds" in errors_by_field(check(row, category_limits=limits))["amount"]


def test_limit_is_not_checked_when_category_is_unknown(row):
    row.update(category="nonsense", amount="999999")
    assert set(errors_by_field(check(row))) == {"category"}


# --- category ---------------------------------------------------------------


def test_unknown_category_lists_allowed_ones(row):
    row["category"] = "snacks"
    message = errors_by_field(check(row))["category"]
    assert "snacks" in message
    assert "travel" in message


def test_category_is_case_insensitive(row):
    row["category"] = "FOOD"
    assert check(row).claim.category == "food"


def test_custom_limit_keys_are_case_insensitive(row):
    result = check(row, category_limits={"Travel": Decimal("5000")})
    assert result.is_valid


# --- date -------------------------------------------------------------------


def test_today_is_allowed(row):
    row["date"] = "2026-10-07"
    assert check(row).is_valid


def test_future_date_is_rejected(row):
    row["date"] = "2026-10-08"
    assert errors_by_field(check(row))["date"] == "must not be in the future"


@pytest.mark.parametrize(
    "bad", ["07/10/2026", "2026-1-5", "20261007", "2026-02-30", "2026-13-01", "yesterday"]
)
def test_malformed_or_impossible_dates_are_rejected(row, bad):
    row["date"] = bad
    assert "YYYY-MM-DD" in errors_by_field(check(row))["date"]


def test_today_defaults_to_the_current_date(row):
    row["date"] = "2999-01-01"
    result = validate_claim(row)
    assert errors_by_field(result)["date"] == "must not be in the future"
    row["date"] = "2000-01-01"
    assert validate_claim(row).is_valid


def test_today_in_timezone_handles_the_ist_date_boundary():
    # 18:56 UTC on 6 Oct is already 00:26 on 7 Oct in India.
    moment = datetime(2026, 10, 6, 18, 56, tzinfo=timezone.utc)
    assert today_in_timezone("Asia/Kolkata", now=moment) == date(2026, 10, 7)
    assert today_in_timezone("UTC", now=moment) == date(2026, 10, 6)


def test_today_in_timezone_defaults_to_now():
    assert isinstance(today_in_timezone("UTC"), date)


# --- duplicate bill_id ------------------------------------------------------


def test_duplicate_bill_id_is_rejected(row):
    result = check(row, existing_bill_ids={"BILL-77"})
    assert "duplicate" in errors_by_field(result)["bill_id"]


def test_duplicate_detection_ignores_case_and_whitespace(row):
    row["bill_id"] = "  bill-77 "
    result = check(row, existing_bill_ids={normalize_bill_id("Bill-77")})
    assert "duplicate" in errors_by_field(result)["bill_id"]


def test_unseen_bill_id_is_accepted(row):
    assert check(row, existing_bill_ids={"OTHER"}).is_valid


def test_duplicate_detection_accepts_any_collection(row):
    assert not check(row, existing_bill_ids=["BILL-77"]).is_valid
    assert not check(row, existing_bill_ids=("BILL-77",)).is_valid
    assert check(row, existing_bill_ids=()).is_valid


def test_duplicate_error_is_added_alongside_other_errors(row):
    row["amount"] = "-1"
    fields = set(errors_by_field(check(row, existing_bill_ids={"BILL-77"})))
    assert fields == {"amount", "bill_id"}


# --- config -----------------------------------------------------------------


def test_default_limits_are_read_only():
    with pytest.raises(TypeError):
        DEFAULT_CATEGORY_LIMITS["travel"] = Decimal("1")  # type: ignore[index]


def test_normalize_bill_id():
    assert normalize_bill_id("  ab-1 ") == "AB-1"
