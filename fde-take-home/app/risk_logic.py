"""Continuous At Risk duration computation.

ARR threshold filtering happens upstream in app.data.load_at_risk_candidates
(it also determines which accounts' history is worth reading at all). This
module turns each At Risk target-month candidate plus its month history into
a RiskAlert with duration_months / risk_start_month.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple

from app.data import AccountMonthRow, LoadResult


def _prev_month(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


@dataclass
class RiskAlert:
    account_id: str
    account_name: str
    account_region: Optional[str]
    month: date
    status: str
    renewal_date: Optional[date]
    account_owner: Optional[str]
    arr: int
    duration_months: int
    risk_start_month: date


def compute_duration(history: List[AccountMonthRow], target_month: date) -> Tuple[int, date]:
    """Count continuous At Risk months ending at target_month (inclusive).

    Stops at the first status change or missing month. duration_months == 1
    when there is no prior At Risk month (including when target_month is the
    first month on record for the account).
    """
    by_month = {row.month: row for row in history}
    duration_months = 0
    risk_start_month = target_month
    cursor = target_month
    while True:
        row = by_month.get(cursor)
        if row is None or row.status != "At Risk":
            break
        duration_months += 1
        risk_start_month = cursor
        cursor = _prev_month(cursor)

    if duration_months == 0:
        # target_month itself wasn't found At Risk in history; shouldn't happen
        # given callers only pass candidates already known At Risk, but keep the
        # spec's "no prior At Risk month -> duration = 1" guarantee regardless.
        return 1, target_month

    return duration_months, risk_start_month


def compute_alerts(load_result: LoadResult, target_month: date) -> List[RiskAlert]:
    alerts = []
    for row in load_result.target_month_rows:
        history = load_result.history_by_account.get(row.account_id, [row])
        duration_months, risk_start_month = compute_duration(history, target_month)
        alerts.append(
            RiskAlert(
                account_id=row.account_id,
                account_name=row.account_name,
                account_region=row.account_region,
                month=row.month,
                status=row.status,
                renewal_date=row.renewal_date,
                account_owner=row.account_owner,
                arr=row.arr,
                duration_months=duration_months,
                risk_start_month=risk_start_month,
            )
        )
    return alerts
