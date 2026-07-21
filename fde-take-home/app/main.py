from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import config, data, db, notifications, risk_logic, slack_client, storage
from app.risk_logic import RiskAlert

app = FastAPI(title="Risk Alert Service")

ALERT_TYPE = "at_risk"


class RunRequest(BaseModel):
    source_uri: str
    month: str  # YYYY-MM-01
    dry_run: bool = False


def _parse_month(month_str: str) -> date:
    try:
        parsed = date.fromisoformat(month_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid month: {month_str!r}") from exc
    if parsed.day != 1:
        raise HTTPException(
            status_code=400, detail=f"month must be the first day of a month, got {month_str!r}"
        )
    return parsed


def _alert_summary(
    alert: RiskAlert, channel: Optional[str], status: str, reason: Optional[str] = None
) -> Dict[str, Any]:
    return {
        "account_id": alert.account_id,
        "account_name": alert.account_name,
        "account_region": alert.account_region,
        "channel": channel,
        "status": status,
        "reason": reason,
        "duration_months": alert.duration_months,
        "risk_start_month": alert.risk_start_month.isoformat(),
        "arr": alert.arr,
    }


def _run_pipeline(
    source_uri: str,
    month: date,
    dry_run: bool,
    run_id: str,
    settings: config.Settings,
    engine,
    counts: Dict[str, int],
) -> tuple[Dict[str, int], List[Dict[str, Any]]]:
    dataset = storage.open_uri(source_uri)
    load_result = data.load_at_risk_candidates(dataset, month, settings.arr_threshold)
    alerts = risk_logic.compute_alerts(load_result, month)

    counts["rows_scanned"] = load_result.rows_scanned
    counts["duplicates_found"] = load_result.duplicates_found
    unknown_region_alerts: List[RiskAlert] = []
    summaries: List[Dict[str, Any]] = []

    for alert in alerts:
        channel = slack_client.route_channel(alert.account_region, settings.region_channel_map)

        existing = db.get_existing_outcome(engine, alert.account_id, month, ALERT_TYPE)
        if existing is not None and existing["status"] == "sent":
            counts["skipped_replay"] += 1
            summaries.append(_alert_summary(alert, channel, "skipped_replay"))
            if not dry_run:
                db.record_run_result(
                    engine,
                    run_id=run_id,
                    account_id=alert.account_id,
                    month=month,
                    alert_type=ALERT_TYPE,
                    channel=channel,
                    status="skipped_replay",
                    reason="already_sent",
                )
            continue

        if channel is None:
            counts["failed_deliveries"] += 1
            unknown_region_alerts.append(alert)
            summaries.append(_alert_summary(alert, None, "failed", reason="unknown_region"))
            if not dry_run:
                db.upsert_alert_outcome(
                    engine,
                    run_id=run_id,
                    account_id=alert.account_id,
                    month=month,
                    alert_type=ALERT_TYPE,
                    channel=None,
                    status="failed",
                    reason="unknown_region",
                )
            continue

        if dry_run:
            counts["alerts_sent"] += 1
            summaries.append(_alert_summary(alert, channel, "sent", reason="dry_run"))
            continue

        if not db.claim_alert(
            engine,
            run_id=run_id,
            account_id=alert.account_id,
            month=month,
            alert_type=ALERT_TYPE,
            channel=channel,
            claim_timeout_seconds=settings.claim_timeout_seconds,
        ):
            # A concurrent run may have claimed the key since our initial
            # lookup. Treat both a newly-sent row and an in-flight pending row
            # as a replay skip; most importantly, never send twice.
            counts["skipped_replay"] += 1
            latest = db.get_existing_outcome(engine, alert.account_id, month, ALERT_TYPE)
            reason = "already_sent" if latest and latest["status"] == "sent" else "delivery_in_progress"
            summaries.append(_alert_summary(alert, channel, "skipped_replay", reason=reason))
            db.record_run_result(
                engine,
                run_id=run_id,
                account_id=alert.account_id,
                month=month,
                alert_type=ALERT_TYPE,
                channel=channel,
                status="skipped_replay",
                reason=reason,
            )
            continue

        # A claim has been reserved as "pending" at this point, and
        # claim_alert only ever reclaims rows left "failed" -- so any
        # exception here (not just Slack HTTP errors, which send_alert
        # already handles) MUST resolve to a "failed" DeliveryResult rather
        # than propagate, or the claim is stuck "pending" forever and the
        # account can never be alerted again.
        try:
            payload = slack_client.format_alert_message(alert, settings.details_base_url)
            result = slack_client.send_alert(channel, payload, settings)
        except Exception as exc:  # noqa: BLE001 - see comment above
            result = slack_client.DeliveryResult(status="failed", error=str(exc))

        if result.status == "sent":
            counts["alerts_sent"] += 1
            summaries.append(_alert_summary(alert, channel, "sent"))
            db.upsert_alert_outcome(
                engine,
                run_id=run_id,
                account_id=alert.account_id,
                month=month,
                alert_type=ALERT_TYPE,
                channel=channel,
                status="sent",
                sent_at=datetime.now(timezone.utc),
            )
        else:
            counts["failed_deliveries"] += 1
            summaries.append(_alert_summary(alert, channel, "failed", reason=result.error))
            db.upsert_alert_outcome(
                engine,
                run_id=run_id,
                account_id=alert.account_id,
                month=month,
                alert_type=ALERT_TYPE,
                channel=channel,
                status="failed",
                error=result.error,
            )

    if not dry_run and unknown_region_alerts:
        notifications.send_unknown_region_summary(
            unknown_region_alerts,
            support_email=settings.support_email,
            log_path=settings.support_notification_log,
            run_id=run_id,
            month=month,
        )

    return counts, summaries


def _execute(req: RunRequest, run_id: str, dry_run: bool):
    settings = config.get_settings()
    engine = db.get_engine(settings.sqlite_path)
    month = _parse_month(req.month)

    db.create_run(engine, run_id=run_id, source_uri=req.source_uri, month=month, dry_run=dry_run)

    counts = dict(
        rows_scanned=0,
        duplicates_found=0,
        alerts_sent=0,
        skipped_replay=0,
        failed_deliveries=0,
    )
    try:
        counts, summaries = _run_pipeline(
            req.source_uri, month, dry_run, run_id, settings, engine, counts
        )
        db.finalize_run(engine, run_id, status="succeeded", **counts)
        return counts, summaries, None
    except Exception as exc:  # noqa: BLE001 - convert any pipeline failure into a failed run record
        db.finalize_run(
            engine,
            run_id,
            status="failed",
            **counts,
            error=str(exc),
        )
        return None, None, exc


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/runs")
def create_run(req: RunRequest):
    run_id = str(uuid.uuid4())
    # Errors are recorded on the run row (status="failed") rather than raised,
    # since /runs must always complete and return a run_id per spec.
    _execute(req, run_id, dry_run=req.dry_run)
    return {"run_id": run_id}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    settings = config.get_settings()
    engine = db.get_engine(settings.sqlite_path)
    run = db.get_run(engine, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    outcomes = db.list_alert_outcomes(engine, run_id)
    sample_alerts = [o for o in outcomes if o["status"] in {"sent", "skipped_replay"}][:5]
    sample_errors = [o for o in outcomes if o["status"] == "failed"][:5]

    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "source_uri": run["source_uri"],
        "month": run["month"],
        "dry_run": run["dry_run"],
        "counts": {
            "rows_scanned": run["rows_scanned"],
            "duplicates_found": run["duplicates_found"],
            "alerts_sent": run["alerts_sent"],
            "skipped_replay": run["skipped_replay"],
            "failed_deliveries": run["failed_deliveries"],
        },
        "sample_alerts": sample_alerts,
        "sample_errors": sample_errors,
        "error": run["error"],
    }


@app.post("/preview")
def preview(req: RunRequest):
    run_id = str(uuid.uuid4())
    counts, summaries, exc = _execute(req, run_id, dry_run=True)
    if exc is not None:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"month": req.month, "run_id": run_id, "counts": counts, "alerts": summaries}
