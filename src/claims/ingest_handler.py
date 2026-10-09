"""S3-triggered Lambda: read an uploaded CSV and ingest its rows."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3

from claims.ingest import MalformedCsvError, parse_csv, process_rows
from claims.repository import ClaimRepository
from claims.validation import DEFAULT_CATEGORY_LIMITS, today_in_timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    repo = ClaimRepository(table)
    s3 = boto3.client("s3")
    tz = os.environ.get("TIMEZONE", "Asia/Kolkata")

    files = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        if not key.lower().endswith(".csv"):
            logger.info(json.dumps({"bucket": bucket, "key": key, "ignored": True}))
            continue

        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        try:
            rows = parse_csv(body.decode("utf-8"))
        except MalformedCsvError:
            logger.exception(json.dumps({"bucket": bucket, "key": key, "error": "malformed csv"}))
            raise                                  # lets the event reach the DLQ

        summary = process_rows(
            rows,
            repo,
            category_limits=DEFAULT_CATEGORY_LIMITS,
            today=today_in_timezone(tz),
            now=datetime.now(timezone.utc),
            source=key,
        )
        entry = {
            "key": key,
            "accepted": summary.accepted,
            "rejected": summary.rejected,
            "skipped": summary.skipped,
            "failed": summary.failed,
        }
        logger.info(json.dumps({"bucket": bucket, **entry}))
        files.append(entry)

    return {"files": files}