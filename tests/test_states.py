"""Tests for the claim state machine."""

from __future__ import annotations

import pytest

from claims.states import (
    TRANSITIONS,
    ClaimStatus,
    InvalidTransitionError,
    allowed_next,
    allowed_sources,
    can_transition,
    ensure_transition,
    is_terminal,
    parse_status,
)

S = ClaimStatus
LEGAL = {
    (S.SUBMITTED, S.APPROVED),
    (S.SUBMITTED, S.REJECTED),
    (S.APPROVED, S.PAID),
}
ALL_PAIRS = [(a, b) for a in S for b in S]


@pytest.mark.parametrize(("current", "target"), ALL_PAIRS)
def test_can_transition_matches_the_rules_for_every_pair(current, target):
    assert can_transition(current, target) is ((current, target) in LEGAL)


@pytest.mark.parametrize(("current", "target"), sorted(LEGAL))
def test_ensure_transition_allows_legal_moves(current, target):
    ensure_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"), [p for p in ALL_PAIRS if p not in LEGAL]
)
def test_ensure_transition_rejects_every_illegal_move(current, target):
    with pytest.raises(InvalidTransitionError) as exc:
        ensure_transition(current, target)
    assert exc.value.current == current
    assert exc.value.target == target
    assert current.value in str(exc.value)
    assert target.value in str(exc.value)


def test_cannot_pay_twice():
    with pytest.raises(InvalidTransitionError):
        ensure_transition(S.PAID, S.PAID)


def test_cannot_skip_approval():
    assert not can_transition(S.SUBMITTED, S.PAID)


def test_cannot_resurrect_rejected_claim():
    for target in S:
        assert not can_transition(S.REJECTED, target)


def test_allowed_next():
    assert allowed_next(S.SUBMITTED) == {S.APPROVED, S.REJECTED}
    assert allowed_next(S.APPROVED) == {S.PAID}
    assert allowed_next(S.PAID) == frozenset()


def test_allowed_sources_is_the_inverse_of_allowed_next():
    assert allowed_sources(S.APPROVED) == {S.SUBMITTED}
    assert allowed_sources(S.REJECTED) == {S.SUBMITTED}
    assert allowed_sources(S.PAID) == {S.APPROVED}
    assert allowed_sources(S.SUBMITTED) == frozenset()
    for target in S:
        for source in S:
            assert (source in allowed_sources(target)) == can_transition(source, target)


@pytest.mark.parametrize(
    ("status", "terminal"),
    [(S.SUBMITTED, False), (S.APPROVED, False), (S.REJECTED, True), (S.PAID, True)],
)
def test_is_terminal(status, terminal):
    assert is_terminal(status) is terminal


def test_every_status_has_an_entry_in_the_table():
    assert set(TRANSITIONS) == set(S)


def test_transition_table_is_read_only():
    with pytest.raises(TypeError):
        TRANSITIONS[S.PAID] = frozenset({S.SUBMITTED})  # type: ignore[index]


@pytest.mark.parametrize("text", ["PAID", "paid", "  Paid  "])
def test_parse_status_is_case_and_whitespace_insensitive(text):
    assert parse_status(text) is S.PAID


@pytest.mark.parametrize("text", ["", "ESCALATED", "done"])
def test_parse_status_rejects_unknown_values(text):
    with pytest.raises(ValueError, match="Unknown status"):
        parse_status(text)


def test_status_serialises_as_plain_string():
    assert str(S.APPROVED) == "APPROVED"
