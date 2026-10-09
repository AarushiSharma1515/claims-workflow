"""Tests for the S3-triggered ingest Lambda handler (moto S3 + moto DynamoDB).

Contract under test (write src/claims/ingest_handler.py to satisfy it):

    lambda_handler(event, context) -> {"files": [{"key", "accepted", "rejected",
                                                    "skipped", "failed"}, ...]}

* Environment: TABLE_NAME (required), TIMEZONE (optional, default "Asia/Kolkata").
* For every S3 record: take bucket + key (keys arrive URL-encoded: "+" is a space),
  read the object, ``parse_csv``, ``process_rows``, log ONE JSON summary line.
* Keys that do not end in ".csv" are ignored (not read, not counted).
* A file that cannot be parsed (MalformedCsvError) is logged and RE-RAISED so the
  async invocation fails and the event lands in the dead-letter queue.
* AWS clients are created inside the handler (never at import time), so moto works.
"""

from __future__ import annotations

import json
import logging

import boto3
import pytest

from claims.ingest import MalformedCsvError
from claims.ingest_handler import lambda_handler
from claims.states import ClaimStatus
from claims.repository import ClaimRepository

BUCKET = "claims-upload-bucket"
HEADER = "claim_id,claimant,category,amount,bill_id,date"


def row(n: int, bill: str | None = None) -> str:
    return f"C-{n},Asha Rao,travel,100.00,{bill or f'B-{n}'},2020-01-01"


@pytest.fixture
def s3(table):  # `table` keeps the moto sandbox open
    client = boto3.client("s3")
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
    )
    return client


@pytest.fixture(autouse=True)
def env(monkeypatch, table):
    monkeypatch.setenv("TABLE_NAME", table.name)
    monkeypatch.delenv("TIMEZONE", raising=False)


@pytest.fixture
def repo(table):
    return ClaimRepository(table)


def put(s3, key: str, body: str | bytes) -> None:
    data = body.encode() if isinstance(body, str) else body
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)


def event(*keys: str) -> dict:
    return {
        "Records": [
            {"s3": {"bucket": {"name": BUCKET}, "object": {"key": key}}} for key in keys
        ]
    }


def test_valid_file_is_ingested(s3, repo):
    put(s3, "claims/a.csv", "\n".join([HEADER, row(1), row(2)]))
    result = lambda_handler(event("claims/a.csv"), None)
    assert result["files"] == [
        {"key": "claims/a.csv", "accepted": 2, "rejected": 0, "skipped": 0, "failed": 0}
    ]
    assert repo.get("C-1")["status"] == "SUBMITTED"
    assert len(repo.list_by_status(ClaimStatus.SUBMITTED)) == 2


def test_mixed_file_counts_rejections(s3, repo):
    put(s3, "claims/b.csv", "\n".join([HEADER, row(1), "C-2,Asha,travel,-5,B-2,2020-01-01"]))
    entry = lambda_handler(event("claims/b.csv"), None)["files"][0]
    assert (entry["accepted"], entry["rejected"]) == (1, 1)
    assert repo.get("C-2")["status"] == "REJECTED"


def test_keys_are_url_decoded(s3, repo):
    put(s3, "claims/my file.csv", "\n".join([HEADER, row(1)]))
    result = lambda_handler(event("claims/my+file.csv"), None)
    assert result["files"][0]["accepted"] == 1


def test_several_records_are_all_processed(s3, repo):
    put(s3, "one.csv", "\n".join([HEADER, row(1)]))
    put(s3, "two.csv", "\n".join([HEADER, row(2)]))
    result = lambda_handler(event("one.csv", "two.csv"), None)
    assert [f["key"] for f in result["files"]] == ["one.csv", "two.csv"]
    assert repo.get("C-1") and repo.get("C-2")


def test_redelivered_event_is_harmless(s3, repo):
    put(s3, "claims/a.csv", "\n".join([HEADER, row(1), row(2)]))
    lambda_handler(event("claims/a.csv"), None)
    again = lambda_handler(event("claims/a.csv"), None)["files"][0]
    assert (again["accepted"], again["skipped"]) == (0, 2)


def test_non_csv_keys_are_ignored(s3, repo):
    put(s3, "notes.txt", "hello")
    result = lambda_handler(event("notes.txt"), None)
    assert result["files"] == []


def test_malformed_file_is_reraised_so_it_reaches_the_dlq(s3):
    put(s3, "bad.csv", "claim_id,claimant\nC-1,Asha")
    with pytest.raises(MalformedCsvError):
        lambda_handler(event("bad.csv"), None)


def test_empty_file_is_reraised(s3):
    put(s3, "empty.csv", "")
    with pytest.raises(MalformedCsvError):
        lambda_handler(event("empty.csv"), None)


def test_file_with_bom_is_read(s3, repo):
    put(s3, "bom.csv", b"\xef\xbb\xbf" + "\n".join([HEADER, row(1)]).encode())
    assert lambda_handler(event("bom.csv"), None)["files"][0]["accepted"] == 1


def test_one_json_summary_line_is_logged_per_file(s3, caplog):
    put(s3, "claims/a.csv", "\n".join([HEADER, row(1)]))
    with caplog.at_level(logging.INFO):
        lambda_handler(event("claims/a.csv"), None)
    summaries = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("key") == "claims/a.csv":
            summaries.append(payload)
    assert len(summaries) == 1
    assert summaries[0]["accepted"] == 1
    assert summaries[0]["bucket"] == BUCKET


def test_missing_table_name_fails_loudly(s3, monkeypatch):
    monkeypatch.delenv("TABLE_NAME")
    put(s3, "a.csv", "\n".join([HEADER, row(1)]))
    with pytest.raises(KeyError):
        lambda_handler(event("a.csv"), None)