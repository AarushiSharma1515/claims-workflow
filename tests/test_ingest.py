"""Tests for CSV ingestion logic (no Lambda, no S3: that is the handler's job).

Contract under test (write src/claims/ingest.py to satisfy it):

    class MalformedCsvError(Exception)

    @dataclass(frozen=True) class CsvRow:  number: int; data: dict[str, str]; problem: str | None

    parse_csv(text: str) -> list[CsvRow]
    retry_on_throttle(func, *, attempts=5, base_delay=0.1, sleep=time.sleep)
    process_rows(rows, repo, *, category_limits, today, now, source,
                 max_workers=4, attempts=5, sleep=time.sleep) -> IngestSummary

    IngestSummary(accepted, rejected, skipped, failed)  # all ints, plus .total
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from claims.ingest import (
    CsvRow,
    IngestSummary,
    MalformedCsvError,
    parse_csv,
    process_rows,
    retry_on_throttle,
)
from claims.repository import ClaimRepository
from claims.states import ClaimStatus
from claims.validation import DEFAULT_CATEGORY_LIMITS

HEADER = "claim_id,claimant,category,amount,bill_id,date"
TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
SOURCE = "claims/batch-1.csv"


def good(n: int, bill: str | None = None) -> str:
    return f"C-{n},Asha Rao,travel,100.00,{bill or f'B-{n}'},2026-10-01"


def run(rows, repo, **kw):
    kw.setdefault("category_limits", DEFAULT_CATEGORY_LIMITS)
    kw.setdefault("today", TODAY)
    kw.setdefault("now", NOW)
    kw.setdefault("source", SOURCE)
    kw.setdefault("sleep", lambda _s: None)
    return process_rows(rows, repo, **kw)


def csv_rows(*lines: str) -> list[CsvRow]:
    return parse_csv("\n".join([HEADER, *lines]))


@pytest.fixture
def repo(table) -> ClaimRepository:
    return ClaimRepository(table)


def throttle() -> ClientError:
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "PutItem")


class Flaky:
    """Wraps a real repo; the first ``fail_times`` create_claim calls raise ``exc``."""

    def __init__(self, inner, exc, fail_times):
        self._inner, self._exc, self.left, self.calls = inner, exc, fail_times, 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_claim(self, claim, now):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise self._exc
        return self._inner.create_claim(claim, now)


# --- parse_csv --------------------------------------------------------------


def test_parse_csv_returns_numbered_rows_with_trimmed_values():
    rows = parse_csv(f"{HEADER}\n C-1 , Asha Rao ,travel,100,B-1,2026-10-01\nC-2,Ben,food,50,B-2,2026-10-02\n")
    assert [r.number for r in rows] == [1, 2]
    assert rows[0].data["claim_id"] == "C-1"
    assert rows[0].data["claimant"] == "Asha Rao"
    assert rows[0].problem is None


def test_parse_csv_headers_are_case_and_space_insensitive():
    rows = parse_csv(" Claim_ID , CLAIMANT,Category,Amount,Bill_ID,Date\nC-1,A,travel,1,B,2026-10-01")
    assert rows[0].data["claim_id"] == "C-1"


def test_parse_csv_handles_a_byte_order_mark():
    rows = parse_csv("﻿" + HEADER + "\n" + good(1))
    assert rows[0].data["claim_id"] == "C-1"


def test_parse_csv_skips_blank_lines():
    rows = parse_csv(f"{HEADER}\n\n{good(1)}\n\n\n{good(2)}\n")
    assert len(rows) == 2


def test_parse_csv_header_only_gives_no_rows():
    assert parse_csv(HEADER + "\n") == []


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_parse_csv_rejects_an_empty_file(text):
    with pytest.raises(MalformedCsvError):
        parse_csv(text)


def test_parse_csv_rejects_a_missing_column_and_names_it():
    with pytest.raises(MalformedCsvError, match="bill_id"):
        parse_csv("claim_id,claimant,category,amount,date\nC-1,A,travel,1,2026-10-01")


def test_parse_csv_ignores_unknown_extra_columns():
    rows = parse_csv(HEADER + ",notes\n" + good(1) + ",hello")
    assert rows[0].problem is None
    assert "notes" not in rows[0].data


def test_parse_csv_flags_a_row_with_too_many_cells():
    rows = parse_csv(f"{HEADER}\n{good(1)},surplus,cells")
    assert rows[0].problem is not None
    assert "extra" in rows[0].problem.lower()


def test_parse_csv_short_row_is_left_for_validation_to_flag():
    rows = parse_csv(f"{HEADER}\nC-1,Asha")
    assert rows[0].problem is None
    assert not rows[0].data.get("amount")


def test_parse_csv_handles_quoted_commas():
    rows = parse_csv(f'{HEADER}\nC-1,"Rao, Asha",travel,10,B-1,2026-10-01')
    assert rows[0].data["claimant"] == "Rao, Asha"


# --- retry_on_throttle ------------------------------------------------------


def test_retry_returns_the_result_without_sleeping_when_it_works():
    sleeps = []
    assert retry_on_throttle(lambda: 42, sleep=sleeps.append) == 42
    assert sleeps == []


def test_retry_retries_throttling_then_succeeds():
    state = {"n": 0}

    def func():
        state["n"] += 1
        if state["n"] < 3:
            raise throttle()
        return "ok"

    sleeps = []
    assert retry_on_throttle(func, attempts=5, sleep=sleeps.append) == "ok"
    assert state["n"] == 3
    assert len(sleeps) == 2
    assert sleeps[1] >= sleeps[0]  # backs off


def test_retry_gives_up_after_the_attempt_limit_and_reraises():
    state = {"n": 0}

    def func():
        state["n"] += 1
        raise throttle()

    with pytest.raises(ClientError):
        retry_on_throttle(func, attempts=3, sleep=lambda _s: None)
    assert state["n"] == 3


def test_retry_does_not_retry_other_errors():
    state = {"n": 0}

    def func():
        state["n"] += 1
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutItem")

    with pytest.raises(ClientError):
        retry_on_throttle(func, attempts=5, sleep=lambda _s: None)
    assert state["n"] == 1


def test_retry_does_not_retry_non_aws_errors():
    state = {"n": 0}

    def func():
        state["n"] += 1
        raise ValueError("bug")

    with pytest.raises(ValueError):
        retry_on_throttle(func, sleep=lambda _s: None)
    assert state["n"] == 1


# --- process_rows -----------------------------------------------------------


def test_valid_row_is_stored_as_submitted(repo):
    summary = run(csv_rows(good(1)), repo)
    assert summary == IngestSummary(accepted=1, rejected=0, skipped=0, failed=0)
    item = repo.get("C-1")
    assert item["status"] == "SUBMITTED"
    assert item["amount"] == Decimal("100.00")


def test_invalid_row_is_stored_as_rejected_with_a_reason(repo):
    summary = run(csv_rows("C-1,Asha,travel,-5,B-1,2026-10-01"), repo)
    assert summary.rejected == 1 and summary.accepted == 0
    item = repo.get("C-1")
    assert item["status"] == "REJECTED"
    assert "amount" in item["reason"]


def test_row_without_a_claim_id_is_still_recorded_under_a_generated_id(repo):
    summary = run(csv_rows(",Asha,travel,10,B-1,2026-10-01"), repo)
    assert summary.rejected == 1
    rejected = repo.list_by_status(ClaimStatus.REJECTED)
    assert len(rejected) == 1
    assert "claim_id" in rejected[0]["reason"]


def test_generated_ids_are_stable_so_reprocessing_does_not_duplicate(repo):
    rows = csv_rows(",Asha,travel,10,B-1,2026-10-01")
    run(rows, repo)
    again = run(rows, repo)
    assert again.skipped == 1 and again.rejected == 0
    assert len(repo.list_by_status(ClaimStatus.REJECTED)) == 1


def test_reprocessing_the_same_file_skips_existing_claims(repo):
    rows = csv_rows(good(1), good(2))
    run(rows, repo)
    again = run(rows, repo)
    assert again == IngestSummary(accepted=0, rejected=0, skipped=2, failed=0)


def test_existing_claim_is_never_overwritten(repo):
    run(csv_rows(good(1)), repo)
    run(csv_rows("C-1,Ben,food,999,B-NEW,2026-10-02"), repo)
    assert repo.get("C-1")["claimant"] == "Asha Rao"


def test_duplicate_bill_inside_one_file_accepts_exactly_one(repo):
    summary = run(csv_rows(good(1, "SAME"), good(2, "SAME")), repo)
    assert summary.accepted == 1 and summary.rejected == 1
    losers = repo.list_by_status(ClaimStatus.REJECTED)
    assert len(losers) == 1
    assert "duplicate" in losers[0]["reason"].lower()


def test_duplicate_bill_across_files_is_rejected(repo):
    run(csv_rows(good(1, "SAME")), repo)
    summary = run(csv_rows(good(2, "SAME")), repo, source="claims/batch-2.csv")
    assert summary.rejected == 1
    assert repo.get("C-2")["status"] == "REJECTED"


def test_problem_rows_are_rejected_with_the_parse_problem(repo):
    rows = parse_csv(f"{HEADER}\n{good(1)},surplus")
    summary = run(rows, repo)
    assert summary.rejected == 1
    assert "extra" in repo.list_by_status(ClaimStatus.REJECTED)[0]["reason"].lower()


def test_mixed_file_summary_counts(repo):
    rows = csv_rows(
        good(1),
        good(2),
        "C-3,Asha,travel,-1,B-3,2026-10-01",
        "C-4,Asha,snacks,10,B-4,2026-10-01",
        "C-5,Asha,travel,10,B-5,2099-01-01",
    )
    summary = run(rows, repo)
    assert summary == IngestSummary(accepted=2, rejected=3, skipped=0, failed=0)
    assert summary.total == 5


def test_one_bad_row_never_stops_the_rest(repo):
    class Boom:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def create_claim(self, claim, now):
            if claim.claim_id == "C-2":
                raise RuntimeError("unexpected")
            return self._inner.create_claim(claim, now)

    summary = run(csv_rows(good(1), good(2), good(3)), Boom(repo))
    assert summary.accepted == 2 and summary.failed == 1
    assert repo.get("C-1") and repo.get("C-3") and repo.get("C-2") is None


def test_throttling_is_retried_and_the_row_still_lands(repo):
    flaky = Flaky(repo, throttle(), fail_times=2)
    summary = run(csv_rows(good(1)), flaky)
    assert summary.accepted == 1 and summary.failed == 0
    assert flaky.calls == 3


def test_persistent_throttling_counts_as_failed_not_a_crash(repo):
    flaky = Flaky(repo, throttle(), fail_times=99)
    summary = run(csv_rows(good(1), good(2)), flaky, attempts=3)
    assert summary.failed == 2 and summary.accepted == 0


def test_large_file_is_processed_with_a_thread_pool(repo):
    """Uses several worker threads and still lands every row.

    moto's in-memory database is not thread-safe under heavy parallel writes
    (real DynamoDB is), so a lock serialises the actual writes while the
    sleep outside the lock lets the pool genuinely run in parallel.
    """
    import threading
    import time

    lock = threading.Lock()
    threads: set[int] = set()

    class Locked:
        def __getattr__(self, name):
            return getattr(repo, name)

        def create_claim(self, claim, now):
            threads.add(threading.get_ident())
            time.sleep(0.002)
            with lock:
                return repo.create_claim(claim, now)

    rows = csv_rows(*[good(n) for n in range(1, 201)])
    summary = run(rows, Locked(), max_workers=8)
    assert summary == IngestSummary(accepted=200, rejected=0, skipped=0, failed=0)
    assert len(repo.list_by_status(ClaimStatus.SUBMITTED)) == 200
    assert len(threads) > 1


def test_empty_row_list_is_a_no_op(repo):
    assert run([], repo) == IngestSummary(0, 0, 0, 0)
