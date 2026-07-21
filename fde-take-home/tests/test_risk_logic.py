# Tests for app.risk_logic: duration_months / risk_start_month edge cases, ARR threshold
from datetime import date

from app.data import AccountMonthRow, LoadResult
from app.risk_logic import compute_alerts, compute_duration


def _row(account_id, month, status, arr=10000):
    return AccountMonthRow(
        account_id=account_id,
        account_name=f"Name {account_id}",
        account_region="AMER",
        month=month,
        status=status,
        renewal_date=None,
        account_owner=None,
        arr=arr,
        updated_at=None,
    )


def test_spec_example_gap_resets_duration_to_one():
    # 2025-10 At Risk, 2025-11 At Risk, 2025-12 Healthy, 2026-01 At Risk -> duration 1
    history = [
        _row("a1", date(2025, 10, 1), "At Risk"),
        _row("a1", date(2025, 11, 1), "At Risk"),
        _row("a1", date(2025, 12, 1), "Healthy"),
        _row("a1", date(2026, 1, 1), "At Risk"),
    ]
    duration, risk_start = compute_duration(history, date(2026, 1, 1))
    assert duration == 1
    assert risk_start == date(2026, 1, 1)


def test_continuous_months_counts_full_streak():
    history = [
        _row("a1", date(2025, 11, 1), "At Risk"),
        _row("a1", date(2025, 12, 1), "At Risk"),
        _row("a1", date(2026, 1, 1), "At Risk"),
    ]
    duration, risk_start = compute_duration(history, date(2026, 1, 1))
    assert duration == 3
    assert risk_start == date(2025, 11, 1)


def test_missing_month_breaks_streak():
    history = [
        _row("a1", date(2025, 11, 1), "At Risk"),
        # 2025-12 missing entirely
        _row("a1", date(2026, 1, 1), "At Risk"),
    ]
    duration, risk_start = compute_duration(history, date(2026, 1, 1))
    assert duration == 1
    assert risk_start == date(2026, 1, 1)


def test_no_prior_history_defaults_to_one():
    history = [_row("a1", date(2026, 1, 1), "At Risk")]
    duration, risk_start = compute_duration(history, date(2026, 1, 1))
    assert duration == 1
    assert risk_start == date(2026, 1, 1)


def test_year_boundary_is_handled():
    history = [
        _row("a1", date(2025, 12, 1), "At Risk"),
        _row("a1", date(2026, 1, 1), "At Risk"),
    ]
    duration, risk_start = compute_duration(history, date(2026, 1, 1))
    assert duration == 2
    assert risk_start == date(2025, 12, 1)


def test_compute_alerts_wires_duration_per_account():
    target = date(2026, 1, 1)
    row_dec = _row("a1", date(2025, 12, 1), "At Risk")
    row_jan = _row("a1", target, "At Risk")
    load_result = LoadResult(
        target_month_rows=[row_jan],
        history_by_account={"a1": [row_dec, row_jan]},
        rows_scanned=2,
        duplicates_found=0,
    )
    alerts = compute_alerts(load_result, target)
    assert len(alerts) == 1
    assert alerts[0].duration_months == 2
    assert alerts[0].risk_start_month == date(2025, 12, 1)
