"""Scale-aware data loading and (account_id, month) deduplication.

Two-pass read against the pyarrow Dataset returned by app.storage.open_uri:
  1. all rows for the target month (status filter is deliberately NOT pushed
     down here, since duplicate resolution -- latest updated_at wins -- must
     happen before status is evaluated), deduped, then narrowed to At Risk +
     ARR-eligible candidates
  2. remaining history (months before the target) for just those candidate
     accounts, needed to compute continuous at-risk duration

Duplicate counting is scoped to the rows actually scanned by this design
(not the full dataset), consistent with minimizing scanning/memory use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pyarrow.compute as pc
import pyarrow.dataset as ds

REQUIRED_COLUMNS = [
    "account_id",
    "account_name",
    "account_region",
    "month",
    "status",
    "renewal_date",
    "arr",
    "updated_at",
]
OPTIONAL_COLUMNS = ["account_owner"]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass
class AccountMonthRow:
    account_id: str
    account_name: str
    account_region: Optional[str]
    month: date
    status: str
    renewal_date: Optional[date]
    account_owner: Optional[str]
    arr: int
    updated_at: datetime


@dataclass
class LoadResult:
    # Candidate accounts At Risk in the target month, ARR-filtered, deduped.
    target_month_rows: List[AccountMonthRow]
    # Full deduped month history per candidate account_id, sorted ascending by month.
    history_by_account: Dict[str, List[AccountMonthRow]]
    rows_scanned: int
    duplicates_found: int


def _validate_schema(dataset: ds.Dataset) -> List[str]:
    missing = sorted(set(REQUIRED_COLUMNS) - set(dataset.schema.names))
    if missing:
        raise ValueError(f"Parquet schema is missing required column(s): {', '.join(missing)}")
    return [name for name in ALL_COLUMNS if name in dataset.schema.names]


def _table_to_rows(table) -> List[AccountMonthRow]:
    cols = {name: table.column(name).to_pylist() for name in table.column_names}
    cols.setdefault("account_owner", [None] * table.num_rows)
    rows = []
    for i in range(table.num_rows):
        arr_value = cols["arr"][i]
        required_values = {
            name: cols[name][i]
            for name in ("account_id", "account_name", "month", "status", "updated_at")
        }
        missing_values = [name for name, value in required_values.items() if value is None]
        if missing_values:
            raise ValueError(
                f"Row {i} has null required value(s): {', '.join(sorted(missing_values))}"
            )
        month_value = cols["month"][i]
        if not isinstance(month_value, date) or month_value.day != 1:
            raise ValueError(f"Row {i} has invalid first-of-month value: {month_value!r}")
        rows.append(
            AccountMonthRow(
                account_id=cols["account_id"][i],
                account_name=cols["account_name"][i],
                account_region=cols["account_region"][i],
                month=month_value,
                status=cols["status"][i],
                renewal_date=cols["renewal_date"][i],
                account_owner=cols["account_owner"][i],
                arr=arr_value if arr_value is not None else 0,
                updated_at=cols["updated_at"][i],
            )
        )
    return rows


def dedupe_rows(rows: List[AccountMonthRow]) -> Tuple[List[AccountMonthRow], int]:
    """Keep the row with max updated_at per (account_id, month).

    Returns (deduped_rows, duplicate_count) where duplicate_count is the
    number of extra rows beyond one-per-key that were discarded.
    """
    best: Dict[Tuple[str, date], AccountMonthRow] = {}
    duplicate_count = 0
    for row in rows:
        key = (row.account_id, row.month)
        existing = best.get(key)
        if existing is None:
            best[key] = row
        else:
            duplicate_count += 1
            if row.updated_at > existing.updated_at:
                best[key] = row
    return list(best.values()), duplicate_count


def load_at_risk_candidates(
    dataset: ds.Dataset,
    target_month: date,
    arr_threshold: int,
) -> LoadResult:
    projected_columns = _validate_schema(dataset)
    # Deliberately not pushing "status == At Risk" down here: duplicate
    # resolution (latest updated_at wins) must happen before status is
    # evaluated, since the winning row could differ in status from a
    # discarded one for the same (account_id, month).
    pass1_filter = pc.field("month") == target_month
    pass1_table = dataset.to_table(columns=projected_columns, filter=pass1_filter)
    target_month_all, duplicates_found = dedupe_rows(_table_to_rows(pass1_table))
    rows_scanned = pass1_table.num_rows

    candidates = [
        r for r in target_month_all if r.status == "At Risk" and r.arr >= arr_threshold
    ]
    candidates.sort(key=lambda row: row.account_id)
    candidate_ids = sorted({r.account_id for r in candidates})

    history_by_account: Dict[str, List[AccountMonthRow]] = {
        row.account_id: [row] for row in candidates
    }

    if candidate_ids:
        pass2_filter = pc.field("account_id").isin(candidate_ids) & (
            pc.field("month") < target_month
        )
        pass2_table = dataset.to_table(columns=projected_columns, filter=pass2_filter)
        deduped_history, dup2 = dedupe_rows(_table_to_rows(pass2_table))
        rows_scanned += pass2_table.num_rows
        duplicates_found += dup2
        for row in deduped_history:
            history_by_account.setdefault(row.account_id, []).append(row)

    for acct_rows in history_by_account.values():
        acct_rows.sort(key=lambda r: r.month)

    return LoadResult(
        target_month_rows=candidates,
        history_by_account=history_by_account,
        rows_scanned=rows_scanned,
        duplicates_found=duplicates_found,
    )
