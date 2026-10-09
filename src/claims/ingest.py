import csv
import random
import time
import hashlib
import logging
from dataclasses import dataclass
from botocore.exceptions import ClientError
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

from claims.repository import DuplicateBillError, DuplicateClaimError
from claims.validation import validate_claim

REQUIRED_COLUMNS = ("claim_id" , "claimant" , "category" , "amount" , "bill_id" , "date")

class MalformedCsvError(Exception):
    """The file as a whole cannot be read (empty, or a column is missing)."""

@dataclass(frozen=True)
class CsvRow:
    number: int
    data: dict[str, str]
    problem: str | None = None

@dataclass(frozen=True)
class IngestSummary:
    accepted: int
    rejected: int
    skipped: int
    failed: int

    @property
    def total(self) -> int:
        return self.accepted + self.rejected + self.skipped + self.failed


def parse_csv(text: str) -> list[CsvRow]:
    text = text.removeprefix("\ufeff")            # Excel's byte-order mark
    if not text.strip():
        raise MalformedCsvError("file is empty")

    reader = csv.reader(text.splitlines())
    header = None
    for cells in reader:                          # first non-blank line is the header
        if any(c.strip() for c in cells):
            header = [c.strip().lower() for c in cells]
            break
    if header is None:
        raise MalformedCsvError("file is empty")

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise MalformedCsvError(f"missing column(s): {', '.join(missing)}")

    rows: list[CsvRow] = []
    for cells in reader:                          # reader carries on after the header
        if not any(c.strip() for c in cells):     # blank line, skip
            continue
        cells = [c.strip() for c in cells]
        problem = None
        if len(cells) > len(header):
            problem = f"row has {len(cells) - len(header)} extra cell(s)"
        values = dict(zip(header, cells))         # zip stops at the shorter list
        data = {c: values.get(c, "") for c in REQUIRED_COLUMNS}
        rows.append(CsvRow(len(rows) + 1, data, problem))
    return rows

THROTTLE_CODES = frozenset({
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
})

def retry_on_throttle(func, *, attempts=5, base_delay=0.1, sleep=None):
    """Call func(); retry with exponential backoff + jitter only on throttling."""
    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            return func()
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in THROTTLE_CODES or attempt == attempts - 1:
                raise
            sleep(base_delay * 2**attempt + random.uniform(0, base_delay))

logger = logging.getLogger(__name__)


def _generated_claim_id(source: str, number: int) -> str:
    """Stable ID for rows without one: same file + same row -> same ID."""
    return "AUTO-" + hashlib.sha1(f"{source}:{number}".encode()).hexdigest()[:12]


def _safe_amount(text: str) -> Decimal:
    """Amount to store on a rejected row; junk becomes 0 (the reason says why)."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        return Decimal("0")
    return value if value.is_finite() else Decimal("0")


def process_rows(rows, repo, *, category_limits, today, now, source,
                 max_workers=4, attempts=5, sleep=None):
    """Validate and store every row; returns counts. Never raises for one bad row."""

    def call(func):
        return retry_on_throttle(func, attempts=attempts, sleep=sleep)

    def reject(row, reason):
        data = row.data
        claim_id = data.get("claim_id") or _generated_claim_id(source, row.number)
        call(lambda: repo.create_rejected(
            claim_id, data.get("claimant", ""), data.get("category", ""),
            _safe_amount(data.get("amount", "")), data.get("bill_id", ""), reason, now,
        ))

    def handle(row):
        try:
            if row.problem:
                reject(row, row.problem)
                return "rejected"
            result = validate_claim(row.data, category_limits=category_limits, today=today)
            if not result.is_valid:
                reject(row, result.reason)
                return "rejected"
            try:
                call(lambda: repo.create_claim(result.claim, now))
                return "accepted"
            except DuplicateBillError:
                reject(row, "bill_id: duplicate bill already claimed")
                return "rejected"
        except DuplicateClaimError:
            return "skipped"
        except Exception:
            logger.exception("row %s of %s failed", row.number, source)
            return "failed"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = Counter(pool.map(handle, rows))

    return IngestSummary(
        accepted=outcomes["accepted"], rejected=outcomes["rejected"],
        skipped=outcomes["skipped"], failed=outcomes["failed"],
    )