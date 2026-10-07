"""Claim lifecycle state machine.

Pure Python: no AWS imports. The DynamoDB layer uses ``allowed_sources`` to
build conditional writes, so this module is the single source of truth for
which transitions are legal.

    SUBMITTED -> APPROVED | REJECTED
    APPROVED  -> PAID
    REJECTED, PAID are terminal.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ClaimStatus(StrEnum):
    """Lifecycle states of a reimbursement claim."""

    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PAID = "PAID"


class InvalidTransitionError(Exception):
    """Raised when a status change is not allowed by the state machine."""

    def __init__(self, current: ClaimStatus, target: ClaimStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Illegal transition: {current.value} -> {target.value}")


# Read-only so no caller can accidentally mutate the rules at runtime.
TRANSITIONS: Mapping[ClaimStatus, frozenset[ClaimStatus]] = MappingProxyType(
    {
        ClaimStatus.SUBMITTED: frozenset({ClaimStatus.APPROVED, ClaimStatus.REJECTED}),
        ClaimStatus.APPROVED: frozenset({ClaimStatus.PAID}),
        ClaimStatus.REJECTED: frozenset(),
        ClaimStatus.PAID: frozenset(),
    }
)


def parse_status(value: str) -> ClaimStatus:
    """Convert user-supplied text (e.g. a query-string filter) to a status.

    Case-insensitive; surrounding whitespace is ignored.

    Raises:
        ValueError: if ``value`` is not a known status.
    """
    try:
        return ClaimStatus(value.strip().upper())
    except ValueError:
        valid = ", ".join(s.value for s in ClaimStatus)
        raise ValueError(f"Unknown status {value!r}; expected one of: {valid}") from None


def allowed_next(status: ClaimStatus) -> frozenset[ClaimStatus]:
    """Return the states reachable in one step from ``status``."""
    return TRANSITIONS[status]


def allowed_sources(target: ClaimStatus) -> frozenset[ClaimStatus]:
    """Return the states from which ``target`` may be entered.

    The persistence layer turns this into a conditional write, e.g.
    ``status IN (:sources)``, so the database itself rejects illegal moves.
    """
    return frozenset(src for src, nxt in TRANSITIONS.items() if target in nxt)


def can_transition(current: ClaimStatus, target: ClaimStatus) -> bool:
    """Return True if moving from ``current`` to ``target`` is legal."""
    return target in TRANSITIONS[current]


def ensure_transition(current: ClaimStatus, target: ClaimStatus) -> None:
    """Raise ``InvalidTransitionError`` unless the transition is legal."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


def is_terminal(status: ClaimStatus) -> bool:
    """Return True if no further transitions are possible from ``status``."""
    return not TRANSITIONS[status]
