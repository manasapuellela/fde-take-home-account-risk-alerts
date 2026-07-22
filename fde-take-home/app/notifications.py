"""Aggregated unknown-region notification.

Documented stub/logging mechanism: in production this would be a real email
sender (e.g. SES, SendGrid) invoked with the same signature. Here it logs a
warning and appends a JSON record to a local file so the notification is
inspectable and testable without a real email dependency.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import List

from app.risk_logic import RiskAlert

logger = logging.getLogger(__name__)


def send_unknown_region_summary(
    alerts: List[RiskAlert],
    *,
    support_email: str,
    log_path: str,
    run_id: str,
    month,
) -> None:
    """Send one aggregated notice for accounts whose alert failed with
    reason "unknown_region" in this run. No-op if alerts is empty.
    """
    if not alerts:
        return

    month_str = month.isoformat() if hasattr(month, "isoformat") else month
    record = {
        "to": support_email,
        "subject": f"Risk alert routing failure: unknown region ({month_str})",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "month": month_str,
        "accounts": [
            {
                "account_id": a.account_id,
                "account_name": a.account_name,
                "account_region": a.account_region,
            }
            for a in alerts
        ],
    }

    logger.warning(
        "Unknown-region alert summary for run %s: %d account(s) could not be routed",
        run_id,
        len(alerts),
    )

    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
