"""Tests for the DynamoDB access layer (run against moto, no real AWS).

Contract under test (write src/claims/repository.py to satisfy it):

    ClaimRepository(table)
      .create_claim(claim: Claim, now: datetime) -> None
      .create_rejected(claim_id, claimant, category, amount, bill_id, reason, now) -> None
      .get(claim_id) -> dict | None
      .bill_exists(bill_id) -> bool
      .transition(claim_id, target: ClaimStatus, now: datetime) -> dict
      .list_by_status(status: ClaimStatus) -> list[dict]

    Exceptions: DuplicateClaimError, DuplicateBillError, ClaimNotFoundError
    (InvalidTransitionError comes from claims.states).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from claims.repository import (
    ClaimNotFoundError,
    ClaimRepository,
    DuplicateBillError,
    DuplicateClaimError,
)
from claims.states import ClaimStatus, InvalidTransitionError
from claims.validation import Claim

S = ClaimStatus
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def make_claim(claim_id: str = "C-1", bill_id: str = "BILL-1", **overrides) -> Claim:
    fields = dict(
        claim_id=claim_id,
        claimant="Asha Rao",
        category="travel",
        amount=Decimal("1200.50"),
        bill_id=bill_id,
        claim_date=date(2026, 9, 30),
    )
    fields.update(overrides)
    return Claim(**fields)


@pytest.fixture
def repo(table) -> ClaimRepository:
    return ClaimRepository(table)


def put_in_state(repo: ClaimRepository, state: ClaimStatus, claim_id: str = "C-1") -> None:
    """Create a claim and walk it to ``state`` along legal transitions."""
    if state is S.REJECTED:
        repo.create_claim(make_claim(claim_id, bill_id=f"B-{claim_id}"), T0)
        repo.transition(claim_id, S.REJECTED, T0)
        return
    repo.create_claim(make_claim(claim_id, bill_id=f"B-{claim_id}"), T0)
    if state in (S.APPROVED, S.PAID):
        repo.transition(claim_id, S.APPROVED, T0)
    if state is S.PAID:
        repo.transition(claim_id, S.PAID, T0)


# --- create_claim -----------------------------------------------------------


def test_create_claim_stores_a_submitted_claim(repo):
    repo.create_claim(make_claim(), T0)
    item = repo.get("C-1")
    assert item["status"] == "SUBMITTED"
    assert item["claimant"] == "Asha Rao"
    assert item["category"] == "travel"
    assert item["amount"] == Decimal("1200.50")
    assert item["bill_id"] == "BILL-1"
    assert item["created_at"] == T0.isoformat()
    assert item["status_updated_at"] == T0.isoformat()


def test_create_claim_refuses_an_existing_claim_id_and_keeps_the_original(repo):
    repo.create_claim(make_claim(amount=Decimal("100")), T0)
    with pytest.raises(DuplicateClaimError):
        repo.create_claim(make_claim(bill_id="OTHER", amount=Decimal("999")), T0)
    assert repo.get("C-1")["amount"] == Decimal("100")


def test_create_claim_refuses_a_reused_bill_id(repo):
    repo.create_claim(make_claim("C-1", "BILL-1"), T0)
    with pytest.raises(DuplicateBillError):
        repo.create_claim(make_claim("C-2", "BILL-1"), T0)


def test_failed_duplicate_bill_leaves_no_half_written_claim(repo):
    """The claim row and the bill reservation are written atomically."""
    repo.create_claim(make_claim("C-1", "BILL-1"), T0)
    with pytest.raises(DuplicateBillError):
        repo.create_claim(make_claim("C-2", "BILL-1"), T0)
    assert repo.get("C-2") is None


def test_failed_duplicate_claim_id_does_not_reserve_the_new_bill(repo):
    repo.create_claim(make_claim("C-1", "BILL-1"), T0)
    with pytest.raises(DuplicateClaimError):
        repo.create_claim(make_claim("C-1", "BILL-2"), T0)
    assert not repo.bill_exists("BILL-2")


def test_same_bill_race_lets_exactly_one_claim_in(repo):
    def attempt(i: int) -> str:
        try:
            repo.create_claim(make_claim(f"C-{i}", "BILL-RACE"), T0)
            return "ok"
        except DuplicateBillError:
            return "dup"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("ok") == 1
    assert results.count("dup") == 7


# --- bill_exists / get ------------------------------------------------------


def test_bill_exists(repo):
    assert not repo.bill_exists("BILL-1")
    repo.create_claim(make_claim(), T0)
    assert repo.bill_exists("BILL-1")


def test_get_returns_none_for_unknown_claim(repo):
    assert repo.get("NOPE") is None


# --- create_rejected --------------------------------------------------------


def test_create_rejected_stores_the_reason(repo):
    repo.create_rejected("C-9", "Ben", "food", Decimal("99999"), "B-9", "amount: too high", T0)
    item = repo.get("C-9")
    assert item["status"] == "REJECTED"
    assert item["reason"] == "amount: too high"
    assert item["status_updated_at"] == T0.isoformat()


def test_rejected_claim_does_not_reserve_its_bill_id(repo):
    repo.create_rejected("C-9", "Ben", "food", Decimal("5"), "B-9", "bad date", T0)
    assert not repo.bill_exists("B-9")
    repo.create_claim(make_claim("C-10", "B-9"), T0)  # must not raise


def test_create_rejected_refuses_an_existing_claim_id(repo):
    repo.create_claim(make_claim(), T0)
    with pytest.raises(DuplicateClaimError):
        repo.create_rejected("C-1", "Ben", "food", Decimal("5"), "X", "r", T0)
    assert repo.get("C-1")["status"] == "SUBMITTED"


# --- transitions ------------------------------------------------------------

LEGAL = [(S.SUBMITTED, S.APPROVED), (S.SUBMITTED, S.REJECTED), (S.APPROVED, S.PAID)]
ILLEGAL = [(a, b) for a in S for b in S if (a, b) not in LEGAL]


@pytest.mark.parametrize(("current", "target"), LEGAL)
def test_legal_transition_updates_status_and_timestamp(repo, current, target):
    put_in_state(repo, current)
    later = T0 + timedelta(days=2)
    updated = repo.transition("C-1", target, later)
    assert updated["status"] == target.value
    assert updated["status_updated_at"] == later.isoformat()
    assert updated["created_at"] == T0.isoformat()  # never changes
    assert repo.get("C-1")["status"] == target.value


@pytest.mark.parametrize(("current", "target"), ILLEGAL)
def test_illegal_transition_is_refused_and_changes_nothing(repo, current, target):
    put_in_state(repo, current)
    before = repo.get("C-1")
    with pytest.raises(InvalidTransitionError) as exc:
        repo.transition("C-1", target, T0 + timedelta(days=1))
    assert exc.value.current == current  # reports the REAL current state
    assert exc.value.target == target
    assert repo.get("C-1") == before


def test_transition_on_unknown_claim_raises_not_found(repo):
    with pytest.raises(ClaimNotFoundError):
        repo.transition("NOPE", S.APPROVED, T0)


def test_transition_does_not_create_a_missing_claim(repo):
    with pytest.raises(ClaimNotFoundError):
        repo.transition("NOPE", S.APPROVED, T0)
    assert repo.get("NOPE") is None


def test_cannot_pay_twice_sequentially(repo):
    put_in_state(repo, S.APPROVED)
    repo.transition("C-1", S.PAID, T0)
    with pytest.raises(InvalidTransitionError):
        repo.transition("C-1", S.PAID, T0)


def test_cannot_skip_approval(repo):
    repo.create_claim(make_claim(), T0)
    with pytest.raises(InvalidTransitionError):
        repo.transition("C-1", S.PAID, T0)


# --- concurrency: the point of conditional writes ---------------------------


def test_concurrent_payments_succeed_exactly_once(repo):
    put_in_state(repo, S.APPROVED)

    def pay(_: int) -> str:
        try:
            repo.transition("C-1", S.PAID, T0)
            return "paid"
        except InvalidTransitionError:
            return "refused"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(pay, range(10)))
    assert results.count("paid") == 1
    assert results.count("refused") == 9
    assert repo.get("C-1")["status"] == "PAID"


def test_concurrent_approve_and_reject_have_one_winner(repo):
    repo.create_claim(make_claim(), T0)

    def act(target: ClaimStatus) -> str:
        try:
            repo.transition("C-1", target, T0)
            return target.value
        except InvalidTransitionError:
            return "refused"

    targets = [S.APPROVED, S.REJECTED] * 5
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(act, targets))
    winners = [r for r in results if r != "refused"]
    assert len(winners) == 1
    assert repo.get("C-1")["status"] == winners[0]


# --- list_by_status (GSI) ---------------------------------------------------


def test_list_by_status_returns_only_that_state(repo):
    put_in_state(repo, S.SUBMITTED, "C-1")
    put_in_state(repo, S.APPROVED, "C-2")
    put_in_state(repo, S.PAID, "C-3")
    assert [c["claim_id"] for c in repo.list_by_status(S.SUBMITTED)] == ["C-1"]
    assert [c["claim_id"] for c in repo.list_by_status(S.APPROVED)] == ["C-2"]
    assert [c["claim_id"] for c in repo.list_by_status(S.PAID)] == ["C-3"]


def test_list_by_status_is_empty_when_nothing_matches(repo):
    assert repo.list_by_status(S.PAID) == []


def test_list_by_status_is_oldest_first(repo):
    repo.create_claim(make_claim("C-NEW", "B-NEW"), T0 + timedelta(days=5))
    repo.create_claim(make_claim("C-OLD", "B-OLD"), T0)
    repo.create_claim(make_claim("C-MID", "B-MID"), T0 + timedelta(days=2))
    ids = [c["claim_id"] for c in repo.list_by_status(S.SUBMITTED)]
    assert ids == ["C-OLD", "C-MID", "C-NEW"]


def test_bill_reservations_never_appear_in_listings(repo):
    repo.create_claim(make_claim(), T0)
    for status in S:
        for item in repo.list_by_status(status):
            assert not item["claim_id"].startswith("BILL#")


def test_moving_state_moves_the_claim_between_listings(repo):
    repo.create_claim(make_claim(), T0)
    repo.transition("C-1", S.APPROVED, T0)
    assert repo.list_by_status(S.SUBMITTED) == []
    assert [c["claim_id"] for c in repo.list_by_status(S.APPROVED)] == ["C-1"]
