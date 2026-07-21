"""Alert formatting, region->channel routing, and retrying Slack delivery."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Optional
from urllib.parse import quote

import requests

from app.config import Settings
from app.risk_logic import RiskAlert

def route_channel(account_region: Optional[str], region_channel_map: Dict[str, str]) -> Optional[str]:
    """Return the configured channel for a region, or None (no default channel)."""
    if not account_region:
        return None
    return region_channel_map.get(account_region)


def format_alert_message(alert: RiskAlert, details_base_url: str) -> dict:
    renewal = alert.renewal_date.isoformat() if alert.renewal_date else "Unknown"
    lines = [
        f"\U0001F6A9 At Risk: {alert.account_name} ({alert.account_id})",
        f"Region: {alert.account_region or 'Unknown'}",
        f"At Risk for: {alert.duration_months} months (since {alert.risk_start_month.isoformat()})",
        f"ARR: {alert.arr}",
        f"Renewal date: {renewal}",
    ]
    if alert.account_owner:
        lines.append(f"Owner: {alert.account_owner}")
    lines.append(f"Details: {details_base_url.rstrip('/')}/{quote(alert.account_id, safe='')}")
    return {"text": "\n".join(lines)}


@dataclass
class DeliveryResult:
    status: str  # "sent" or "failed"
    attempts: int = 0
    error: Optional[str] = None


def _target_url(channel: str, settings: Settings) -> str:
    if settings.slack_webhook_base_url:
        return f"{settings.slack_webhook_base_url.rstrip('/')}/{quote(channel, safe='')}"
    if settings.slack_webhook_url:
        return settings.slack_webhook_url
    raise ValueError("Neither SLACK_WEBHOOK_BASE_URL nor SLACK_WEBHOOK_URL is configured")


def send_alert(channel: str, payload: dict, settings: Settings) -> DeliveryResult:
    """POST payload to the routed channel, retrying on 429/5xx with exponential backoff.

    Honors the Retry-After header when present; otherwise backs off by
    retry_base_delay_seconds * retry_backoff_factor ** (attempt - 1).
    """
    url = _target_url(channel, settings)
    delay = settings.retry_base_delay_seconds
    last_error: Optional[str] = None
    attempt = 0

    for attempt in range(1, settings.retry_max_attempts + 1):
        try:
            resp = requests.post(url, json=payload, timeout=settings.http_timeout_seconds)
        except requests.RequestException as exc:
            last_error = str(exc)
            resp = None

        if resp is not None and 200 <= resp.status_code < 300:
            return DeliveryResult(status="sent", attempts=attempt)

        if resp is not None:
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
        else:
            retryable = True  # network/connection error: worth a retry

        if not retryable or attempt == settings.retry_max_attempts:
            break

        retry_after = resp.headers.get("Retry-After") if resp is not None else None
        wait = _retry_after_seconds(retry_after) if retry_after else None
        wait = delay if wait is None else wait
        wait = min(max(wait, 0.0), settings.retry_max_delay_seconds)
        time.sleep(wait)
        delay = min(delay * settings.retry_backoff_factor, settings.retry_max_delay_seconds)

    return DeliveryResult(status="failed", attempts=attempt, error=last_error)


def _retry_after_seconds(value: str) -> Optional[float]:
    """Parse Retry-After seconds or HTTP-date; malformed values fall back."""
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
