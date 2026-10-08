"""Shared pytest fixtures: a fake (moto) DynamoDB 'Claims' table.

The schema below is the contract your SAM template must match in Stage 3:

* partition key  claim_id (S)
* GSI "status-index": partition key status (S), sort key status_updated_at (S)
"""

from __future__ import annotations

import threading

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "Claims"
STATUS_INDEX = "status-index"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so tests can never touch a real AWS account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")


def _serialize_api_calls(client) -> None:
    """Make each DynamoDB API call atomic under moto.

    Real DynamoDB evaluates every conditional write atomically on the server.
    moto's in-memory backend is not thread-safe, so concurrent calls can read
    the same state or corrupt its dicts, which would make the concurrency
    tests flaky for reasons unrelated to our code. A lock around each call
    restores DynamoDB's per-call atomicity. Calls from different threads still
    interleave, so a read-then-write bug in the repository would still fail.
    """
    lock = threading.Lock()

    def acquire(**_kwargs) -> None:
        lock.acquire()

    def release(**_kwargs) -> None:
        try:
            lock.release()
        except RuntimeError:  # not held (only one of the two release hooks fires)
            pass

    events = client.meta.events
    events.register("before-call.dynamodb", acquire)
    events.register("after-call.dynamodb", release)
    events.register("after-call-error.dynamodb", release)


@pytest.fixture
def table(aws_env):
    """An empty Claims table inside a moto sandbox."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        _serialize_api_calls(dynamodb.meta.client)
        yield dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "claim_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "claim_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "status_updated_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": STATUS_INDEX,
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "status_updated_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )