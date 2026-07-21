"""SQLite persistence with replay-safe canonical state and per-run audit results.

Idempotency contract: alert_outcomes has UNIQUE(account_id, month, alert_type).
Delivery callers atomically reserve work with claim_alert():
  - status == "sent"   -> do not resend; caller records this as skipped_replay
                          and must NOT call upsert_alert_outcome (row stays as-is)
  - status == "failed" -> a later run may atomically claim and retry it
  - no existing row     -> the caller claims and attempts delivery

run_alert_results stores immutable per-run history, so canonical state changes
do not erase old run samples and skipped replays remain inspectable.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("source_uri", String, nullable=False),
    Column("month", String, nullable=False),
    Column("dry_run", Boolean, nullable=False),
    Column("status", String, nullable=False),  # running / succeeded / failed
    Column("started_at", DateTime, nullable=False),
    Column("completed_at", DateTime, nullable=True),
    Column("rows_scanned", Integer, nullable=False, default=0),
    Column("duplicates_found", Integer, nullable=False, default=0),
    Column("alerts_sent", Integer, nullable=False, default=0),
    Column("skipped_replay", Integer, nullable=False, default=0),
    Column("failed_deliveries", Integer, nullable=False, default=0),
    Column("error", String, nullable=True),
)

alert_outcomes = Table(
    "alert_outcomes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False),
    Column("account_id", String, nullable=False),
    Column("month", String, nullable=False),
    Column("alert_type", String, nullable=False),
    Column("channel", String, nullable=True),
    Column("status", String, nullable=False),  # sent / failed / skipped_replay
    Column("reason", String, nullable=True),
    Column("sent_at", DateTime, nullable=True),
    Column("error", String, nullable=True),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("account_id", "month", "alert_type", name="uq_alert_outcome_key"),
)

# Immutable per-run audit records.  alert_outcomes remains the canonical
# replay/idempotency state; this table prevents a retry from erasing the
# previous run's samples and records skipped replays as real run results.
run_alert_results = Table(
    "run_alert_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False),
    Column("account_id", String, nullable=False),
    Column("month", String, nullable=False),
    Column("alert_type", String, nullable=False),
    Column("channel", String, nullable=True),
    Column("status", String, nullable=False),
    Column("reason", String, nullable=True),
    Column("sent_at", DateTime, nullable=True),
    Column("error", String, nullable=True),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint(
        "run_id", "account_id", "month", "alert_type", name="uq_run_alert_result_key"
    ),
)


def _month_str(month) -> str:
    return month.isoformat() if isinstance(month, date) else month


def get_engine(sqlite_path: str) -> Engine:
    engine = create_engine(f"sqlite:///{sqlite_path}", future=True)
    metadata.create_all(engine)
    return engine


def create_run(engine: Engine, *, run_id: str, source_uri: str, month, dry_run: bool) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(runs).values(
                run_id=run_id,
                source_uri=source_uri,
                month=_month_str(month),
                dry_run=dry_run,
                status="running",
                started_at=datetime.now(timezone.utc),
                rows_scanned=0,
                duplicates_found=0,
                alerts_sent=0,
                skipped_replay=0,
                failed_deliveries=0,
            )
        )


def finalize_run(
    engine: Engine,
    run_id: str,
    *,
    status: str,
    rows_scanned: int,
    duplicates_found: int,
    alerts_sent: int,
    skipped_replay: int,
    failed_deliveries: int,
    error: Optional[str] = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(runs)
            .where(runs.c.run_id == run_id)
            .values(
                status=status,
                completed_at=datetime.now(timezone.utc),
                rows_scanned=rows_scanned,
                duplicates_found=duplicates_found,
                alerts_sent=alerts_sent,
                skipped_replay=skipped_replay,
                failed_deliveries=failed_deliveries,
                error=error,
            )
        )


def get_run(engine: Engine, run_id: str) -> Optional[Dict[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
        return dict(row) if row else None


def get_existing_outcome(
    engine: Engine, account_id: str, month, alert_type: str
) -> Optional[Dict[str, Any]]:
    month_str = _month_str(month)
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(alert_outcomes).where(
                    (alert_outcomes.c.account_id == account_id)
                    & (alert_outcomes.c.month == month_str)
                    & (alert_outcomes.c.alert_type == alert_type)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


def claim_alert(
    engine: Engine,
    *,
    run_id: str,
    account_id: str,
    month,
    alert_type: str,
    channel: str,
    claim_timeout_seconds: float = 300.0,
) -> bool:
    """Atomically claim a new or previously-failed alert for delivery.

    Returns False when the alert is already sent or another run currently
    holds a pending claim.  This closes the check-then-send race between
    concurrent runs; SQLite serializes the short claim transaction.
    """
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=claim_timeout_seconds)
    month_str = _month_str(month)
    values = dict(
        run_id=run_id,
        account_id=account_id,
        month=month_str,
        alert_type=alert_type,
        channel=channel,
        status="pending",
        reason=None,
        sent_at=None,
        error=None,
        updated_at=now,
    )
    with engine.begin() as conn:
        inserted = conn.execute(
            sqlite_insert(alert_outcomes)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["account_id", "month", "alert_type"])
        )
        if inserted.rowcount == 1:
            return True

        retried = conn.execute(
            update(alert_outcomes)
            .where(
                (alert_outcomes.c.account_id == account_id)
                & (alert_outcomes.c.month == month_str)
                & (alert_outcomes.c.alert_type == alert_type)
                & (
                    (alert_outcomes.c.status == "failed")
                    | (
                        (alert_outcomes.c.status == "pending")
                        & (alert_outcomes.c.updated_at < stale_before)
                    )
                )
            )
            .values(**{k: v for k, v in values.items() if k not in {"account_id", "month", "alert_type"}})
        )
        return retried.rowcount == 1


def record_run_result(
    engine: Engine,
    *,
    run_id: str,
    account_id: str,
    month,
    alert_type: str,
    channel: Optional[str],
    status: str,
    reason: Optional[str] = None,
    sent_at: Optional[datetime] = None,
    error: Optional[str] = None,
) -> None:
    values = dict(
        run_id=run_id,
        account_id=account_id,
        month=_month_str(month),
        alert_type=alert_type,
        channel=channel,
        status=status,
        reason=reason,
        sent_at=sent_at,
        error=error,
        updated_at=datetime.now(timezone.utc),
    )
    with engine.begin() as conn:
        conn.execute(
            sqlite_insert(run_alert_results)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["run_id", "account_id", "month", "alert_type"],
                set_={k: v for k, v in values.items() if k != "run_id"},
            )
        )


def upsert_alert_outcome(
    engine: Engine,
    *,
    run_id: str,
    account_id: str,
    month,
    alert_type: str,
    channel: Optional[str],
    status: str,
    reason: Optional[str] = None,
    sent_at: Optional[datetime] = None,
    error: Optional[str] = None,
) -> None:
    values = dict(
        run_id=run_id,
        account_id=account_id,
        month=_month_str(month),
        alert_type=alert_type,
        channel=channel,
        status=status,
        reason=reason,
        sent_at=sent_at,
        error=error,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = sqlite_insert(alert_outcomes).values(**values)
    update_cols = {k: v for k, v in values.items() if k not in ("account_id", "month", "alert_type")}
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id", "month", "alert_type"],
        set_=update_cols,
        # A successful canonical delivery is terminal. A concurrent or stale
        # failure may still be recorded in run_alert_results, but it must not
        # make a future replay send an already-delivered alert again.
        where=(alert_outcomes.c.status != "sent") | (stmt.excluded.status == "sent"),
    )
    with engine.begin() as conn:
        conn.execute(stmt)
    record_run_result(
        engine,
        run_id=run_id,
        account_id=account_id,
        month=month,
        alert_type=alert_type,
        channel=channel,
        status=status,
        reason=reason,
        sent_at=sent_at,
        error=error,
    )


def list_alert_outcomes(
    engine: Engine, run_id: str, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        stmt = select(run_alert_results).where(run_alert_results.c.run_id == run_id).order_by(
            run_alert_results.c.id
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]
