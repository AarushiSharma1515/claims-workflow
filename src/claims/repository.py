"""DynamoDB access layer for claims.

This is the only module that knows how claims are stored. It leans on
``states.py`` for the rules and on DynamoDB *conditions* for enforcement, so
concurrent Lambdas cannot corrupt a claim even if both pass the same checks
at the same instant.

Table layout (must match the SAM template in Stage 3, see tests/conftest.py):

* partition key ``claim_id``
* GSI ``status-index``: partition key ``status``, sort key ``status_updated_at``

Two kinds of items share the table:

* claim items      -> ``claim_id`` is the claim's id, ``status`` is set
* bill reservations -> ``claim_id`` is ``"BILL#<normalised bill id>"`` and there
  is no ``status`` attribute, so they never appear in the (sparse) status
  index. Claim ids cannot contain ``#`` (see validation.py), so the two key
  spaces never collide.

Concurrency guarantees:

* ``create_claim`` writes the claim and its bill reservation in one DynamoDB
  transaction, each guarded by ``attribute_not_exists``. Either both land or
  neither does, and two claims can never hold the same bill.
* ``transition`` is a single ``UpdateItem`` guarded by
  ``status IN (<legal source states>)``. Of N simultaneous attempts exactly
  one succeeds; the rest are refused by the database, not by a read-then-write
  check in Python.
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from claims.states import ClaimStatus, InvalidTransitionError, allowed_sources
from claims.validation import Claim, normalize_bill_id

STATUS_INDEX = "status-index"
BILL_PREFIX = "BILL#"

# DynamoDB cancels a transaction with TransactionConflict when another
# transaction touches the same item at the same moment. That is transient,
# so we retry a few times with a short jittered backoff.
_MAX_TX_ATTEMPTS = 5


class DuplicateClaimError(Exception):
    """A claim with this ``claim_id`` already exists."""

    def __init__(self, claim_id: str) -> None:
        self.claim_id = claim_id
        super().__init__(f"Claim {claim_id!r} already exists")


class DuplicateBillError(Exception):
    """Another claim has already used this bill id."""

    def __init__(self, bill_id: str) -> None:
        self.bill_id = bill_id
        super().__init__(f"Bill {bill_id!r} was already claimed")


class ClaimNotFoundError(Exception):
    """No claim with this ``claim_id`` exists."""

    def __init__(self, claim_id: str) -> None:
        self.claim_id = claim_id
        super().__init__(f"Claim {claim_id!r} not found")


def _bill_key(bill_id: str) -> str:
    return f"{BILL_PREFIX}{normalize_bill_id(bill_id)}"


def _error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


class ClaimRepository:
    """Persistence operations for claims, backed by one DynamoDB table.

    Args:
        table: a boto3 ``dynamodb.Table`` resource (tests pass a moto table,
            Lambdas pass the real one). Injecting it keeps this class free of
            any global AWS state.
        index_name: name of the status GSI.
    """

    def __init__(self, table: Any, *, index_name: str = STATUS_INDEX) -> None:
        self._table = table
        self._index_name = index_name

    # ------------------------------------------------------------------ writes

    def create_claim(self, claim: Claim, now: datetime) -> None:
        """Store a valid claim as SUBMITTED and reserve its bill id atomically.

        Raises:
            DuplicateClaimError: ``claim_id`` already exists (checked first).
            DuplicateBillError: the bill id is held by another claim.
        """
        timestamp = now.isoformat()
        bill_key = _bill_key(claim.bill_id)
        client = self._table.meta.client
        table_name = self._table.name

        # ``table.meta.client`` is the resource-level client, so plain Python
        # values (str, Decimal) are accepted and converted for us.
        claim_item = {
            "claim_id": claim.claim_id,
            "claimant": claim.claimant,
            "category": claim.category,
            "amount": claim.amount,
            "bill_id": claim.bill_id,
            "claim_date": claim.claim_date.isoformat(),
            "status": ClaimStatus.SUBMITTED.value,
            "created_at": timestamp,
            "status_updated_at": timestamp,
        }
        reservation_item = {"claim_id": bill_key, "reserved_by": claim.claim_id}

        transact_items = [
            {
                "Put": {
                    "TableName": table_name,
                    "Item": claim_item,
                    "ConditionExpression": "attribute_not_exists(claim_id)",
                }
            },
            {
                "Put": {
                    "TableName": table_name,
                    "Item": reservation_item,
                    "ConditionExpression": "attribute_not_exists(claim_id)",
                }
            },
        ]

        for attempt in range(1, _MAX_TX_ATTEMPTS + 1):
            try:
                client.transact_write_items(TransactItems=transact_items)
                return
            except ClientError as error:
                if _error_code(error) != "TransactionCanceledException":
                    raise
                codes = [r.get("Code") for r in error.response.get("CancellationReasons", [])]
                # Order matches transact_items: [claim, reservation].
                if len(codes) > 0 and codes[0] == "ConditionalCheckFailed":
                    raise DuplicateClaimError(claim.claim_id) from None
                if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                    raise DuplicateBillError(claim.bill_id) from None
                if "TransactionConflict" in codes and attempt < _MAX_TX_ATTEMPTS:
                    time.sleep(random.uniform(0.005, 0.03) * attempt)
                    continue
                raise

    def create_rejected(
        self,
        claim_id: str,
        claimant: str,
        category: str,
        amount: Decimal | str,
        bill_id: str,
        reason: str,
        now: datetime,
    ) -> None:
        """Record a claim that failed validation, so it stays visible.

        Rejected-at-intake claims do **not** reserve their bill id: the
        claimant may fix the problem and resubmit under a new ``claim_id``.

        Raises:
            DuplicateClaimError: ``claim_id`` already exists; nothing changes.
        """
        timestamp = now.isoformat()
        try:
            self._table.put_item(
                Item={
                    "claim_id": claim_id,
                    "claimant": claimant,
                    "category": category,
                    "amount": amount,
                    "bill_id": bill_id,
                    "status": ClaimStatus.REJECTED.value,
                    "reason": reason,
                    "created_at": timestamp,
                    "status_updated_at": timestamp,
                },
                ConditionExpression="attribute_not_exists(claim_id)",
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                raise DuplicateClaimError(claim_id) from None
            raise

    def transition(self, claim_id: str, target: ClaimStatus, now: datetime) -> dict[str, Any]:
        """Move a claim to ``target`` if (and only if) the state machine allows.

        The legality check runs inside DynamoDB as a condition on the current
        status, so concurrent callers cannot both succeed.

        Returns:
            The updated claim item.

        Raises:
            ClaimNotFoundError: no such claim (and none is created).
            InvalidTransitionError: the move is illegal from the claim's real
                current state, which is the one reported on the exception.
        """
        sources = sorted(s.value for s in allowed_sources(target))
        if not sources:
            # Nothing may ever move *into* ``target`` (e.g. SUBMITTED), so there
            # is no condition to build. Report the real current state.
            raise self._refusal(claim_id, target)

        placeholders = {f":src{i}": value for i, value in enumerate(sources)}
        try:
            response = self._table.update_item(
                Key={"claim_id": claim_id},
                UpdateExpression="SET #status = :target, status_updated_at = :now",
                # A missing item has no ``status`` attribute, so this condition
                # fails for it too, which also stops UpdateItem from upserting.
                ConditionExpression=f"#status IN ({', '.join(placeholders)})",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":target": target.value,
                    ":now": now.isoformat(),
                    **placeholders,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                raise self._refusal(claim_id, target) from None
            raise
        return response["Attributes"]

    # ------------------------------------------------------------------- reads

    def get(self, claim_id: str) -> dict[str, Any] | None:
        """Return the claim item, or None. Strongly consistent."""
        response = self._table.get_item(Key={"claim_id": claim_id}, ConsistentRead=True)
        return response.get("Item")

    def bill_exists(self, bill_id: str) -> bool:
        """Return True if a live claim already holds this bill id."""
        response = self._table.get_item(Key={"claim_id": _bill_key(bill_id)}, ConsistentRead=True)
        return "Item" in response

    def list_by_status(self, status: ClaimStatus) -> list[dict[str, Any]]:
        """Return every claim in ``status``, oldest ``status_updated_at`` first.

        Reads the status GSI, which is eventually consistent (a claim that
        just changed state may take a moment to move between listings).
        """
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "IndexName": self._index_name,
            "KeyConditionExpression": "#status = :status",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":status": status.value},
            "ScanIndexForward": True,
        }
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            kwargs["ExclusiveStartKey"] = last_key

    # ----------------------------------------------------------------- helpers

    def _refusal(self, claim_id: str, target: ClaimStatus) -> Exception:
        """Build the right error after a refused transition.

        Re-reads the claim (strongly consistent) so the error names the state
        the claim is *actually* in, not the state the caller assumed.
        """
        item = self.get(claim_id)
        if item is None or "status" not in item:  # absent, or a bill reservation
            return ClaimNotFoundError(claim_id)
        return InvalidTransitionError(ClaimStatus(item["status"]), target)