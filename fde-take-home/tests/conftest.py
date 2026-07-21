from datetime import date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SCHEMA = pa.schema(
    [
        ("account_id", pa.string()),
        ("account_name", pa.string()),
        ("account_region", pa.string()),
        ("month", pa.date32()),
        ("status", pa.string()),
        ("renewal_date", pa.date32()),
        ("account_owner", pa.string()),
        ("arr", pa.int64()),
        ("updated_at", pa.timestamp("ns")),
    ]
)


def _row(account_id, account_name, account_region, month, status, renewal_date, account_owner, arr, updated_at):
    return dict(
        account_id=account_id,
        account_name=account_name,
        account_region=account_region,
        month=month,
        status=status,
        renewal_date=renewal_date,
        account_owner=account_owner,
        arr=arr,
        updated_at=updated_at,
    )


# Fixture dataset for a target month of 2026-01-01:
#   acc1 - AMER, 3 consecutive At Risk months (Nov, Dec, Jan); a stale duplicate
#          row at the target month (earlier updated_at, wrong status) that must lose
#   acc2 - null region -> unknown_region routing failure
#   acc3 - AMER, below the ARR threshold (500 < 10000) -> excluded
#   acc4 - EMEA, Healthy in Dec then At Risk in Jan -> duration resets to 1
#   acc5 - APAC, Healthy in the target month -> not a candidate at all
#   acc6 - AMER, At Risk in Jan with no prior month on record -> duration 1
SAMPLE_ROWS = [
    _row("acc1", "Account One", "AMER", date(2025, 11, 1), "At Risk", date(2026, 3, 1), "owner1@example.com", 50000, datetime(2025, 11, 5)),
    _row("acc1", "Account One", "AMER", date(2025, 12, 1), "At Risk", date(2026, 3, 1), "owner1@example.com", 50000, datetime(2025, 12, 5)),
    _row("acc1", "Account One", "AMER", date(2026, 1, 1), "At Risk", date(2026, 6, 1), "owner1@example.com", 50000, datetime(2026, 1, 6)),
    _row("acc1", "Account One", "AMER", date(2026, 1, 1), "Healthy", date(2026, 6, 1), "owner1@example.com", 50000, datetime(2026, 1, 2)),
    _row("acc2", "Account Two", None, date(2026, 1, 1), "At Risk", None, None, 20000, datetime(2026, 1, 3)),
    _row("acc3", "Account Three", "AMER", date(2026, 1, 1), "At Risk", date(2026, 5, 1), "owner3@example.com", 500, datetime(2026, 1, 4)),
    _row("acc4", "Account Four", "EMEA", date(2025, 12, 1), "Healthy", date(2026, 4, 1), "owner4@example.com", 30000, datetime(2025, 12, 3)),
    _row("acc4", "Account Four", "EMEA", date(2026, 1, 1), "At Risk", date(2026, 4, 1), "owner4@example.com", 30000, datetime(2026, 1, 5)),
    _row("acc5", "Account Five", "APAC", date(2026, 1, 1), "Healthy", date(2026, 5, 1), "owner5@example.com", 40000, datetime(2026, 1, 2)),
    _row("acc6", "Account Six", "AMER", date(2026, 1, 1), "At Risk", None, None, 40000, datetime(2026, 1, 6)),
]


@pytest.fixture
def sample_parquet_path(tmp_path):
    table = pa.Table.from_pylist(SAMPLE_ROWS, schema=SCHEMA)
    path = tmp_path / "sample.parquet"
    pq.write_table(table, path)
    return path
