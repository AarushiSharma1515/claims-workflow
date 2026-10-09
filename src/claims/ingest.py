import csv
from dataclasses import dataclass
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


def retry_on_throttle(func, *, attempts=5, base_delay=0.1, sleep=None):
    raise NotImplementedError


def process_rows(rows, repo, *, category_limits, today, now, source,
                 max_workers=4, attempts=5, sleep=None):
    raise NotImplementedError