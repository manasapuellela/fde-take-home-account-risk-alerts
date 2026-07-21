# Tests for app.data: dedup by latest updated_at, duplicate counting
from datetime import date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import storage
from app.data import AccountMonthRow, dedupe_rows, load_at_risk_candidates


def _row(account_id, month, status, updated_at, arr=10000):
    return AccountMonthRow(
        account_id=account_id,
        account_name=f"Name {account_id}",
        account_region="AMER",
        month=month,
        status=status,
        renewal_date=None,
        account_owner=None,
        arr=arr,
        updated_at=updated_at,
    )


def test_dedupe_rows_keeps_latest_updated_at_and_counts_duplicates():
    older = _row("a1", date(2026, 1, 1), "Healthy", datetime(2026, 1, 2))
    newer = _row("a1", date(2026, 1, 1), "At Risk", datetime(2026, 1, 5))
    other = _row("a2", date(2026, 1, 1), "At Risk", datetime(2026, 1, 5))

    deduped, dup_count = dedupe_rows([older, newer, other])

    assert dup_count == 1
    by_id = {r.account_id: r for r in deduped}
    assert by_id["a1"].status == "At Risk"
    assert by_id["a1"].updated_at == datetime(2026, 1, 5)
    assert "a2" in by_id


def test_dedupe_rows_no_duplicates_reports_zero():
    rows = [_row("a1", date(2026, 1, 1), "At Risk", datetime(2026, 1, 5))]
    deduped, dup_count = dedupe_rows(rows)
    assert dup_count == 0
    assert len(deduped) == 1


def test_load_at_risk_candidates_filters_and_computes_history(sample_parquet_path):
    dataset = storage.open_uri(sample_parquet_path.as_uri())
    result = load_at_risk_candidates(dataset, date(2026, 1, 1), arr_threshold=10000)

    candidate_ids = {r.account_id for r in result.target_month_rows}
    # acc3 excluded by ARR threshold; acc5 excluded (Healthy in target month)
    assert candidate_ids == {"acc1", "acc2", "acc4", "acc6"}

    acc1 = next(r for r in result.target_month_rows if r.account_id == "acc1")
    assert acc1.status == "At Risk"  # the stale Healthy duplicate must lose

    assert result.duplicates_found >= 1
    assert len(result.history_by_account["acc1"]) == 3  # Nov, Dec, Jan
    assert [r.month for r in result.history_by_account["acc1"]] == [
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
    ]


def test_load_at_risk_candidates_arr_threshold_zero_includes_low_arr(sample_parquet_path):
    dataset = storage.open_uri(sample_parquet_path.as_uri())
    result = load_at_risk_candidates(dataset, date(2026, 1, 1), arr_threshold=0)
    candidate_ids = {r.account_id for r in result.target_month_rows}
    assert "acc3" in candidate_ids


def test_optional_account_owner_column_may_be_absent(tmp_path):
    table = pa.Table.from_pylist(
        [
            {
                "account_id": "a1",
                "account_name": "Account One",
                "account_region": "AMER",
                "month": date(2026, 1, 1),
                "status": "At Risk",
                "renewal_date": None,
                "arr": 10000,
                "updated_at": datetime(2026, 1, 2),
            }
        ]
    )
    path = tmp_path / "without_owner.parquet"
    pq.write_table(table, path)
    result = load_at_risk_candidates(storage.open_uri(path.as_uri()), date(2026, 1, 1), 0)
    assert result.target_month_rows[0].account_owner is None


def test_missing_required_column_has_clear_error(tmp_path):
    table = pa.Table.from_pylist(
        [{"account_id": "a1", "month": date(2026, 1, 1)}]
    )
    path = tmp_path / "bad_schema.parquet"
    pq.write_table(table, path)
    with pytest.raises(ValueError, match="missing required column"):
        load_at_risk_candidates(storage.open_uri(path.as_uri()), date(2026, 1, 1), 0)
