"""Shared pytest fixtures: a fake (moto) DynamoDB 'Claims' table.

The schema below is the contract your SAM template must match in Stage 3:

* partition key  claim_id (S)
* GSI "status-index": partition key status (S), sort key status_updated_at (S)
"""

from __future__ import annotations

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


@pytest.fixture
def table(aws_env):
    """An empty Claims table inside a moto sandbox."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
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
