# Tests for app.db: unique constraint, skipped_replay, retry-on-failed behavior
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import update

from app import db


def test_get_run_missing_returns_none(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    assert db.get_run(engine, "missing") is None


def test_create_and_finalize_run_roundtrip(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    db.create_run(
        engine, run_id="run1", source_uri="file:///x.parquet", month=date(2026, 1, 1), dry_run=False
    )
    run = db.get_run(engine, "run1")
    assert run["status"] == "running"

    db.finalize_run(
        engine,
        "run1",
        status="succeeded",
        rows_scanned=10,
        duplicates_found=1,
        alerts_sent=2,
        skipped_replay=0,
        failed_deliveries=0,
    )
    run = db.get_run(engine, "run1")
    assert run["status"] == "succeeded"
    assert run["alerts_sent"] == 2
    assert run["completed_at"] is not None


def test_alert_outcome_unique_constraint_upserts_in_place(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    db.create_run(engine, run_id="run1", source_uri="x", month=date(2026, 1, 1), dry_run=False)

    assert db.get_existing_outcome(engine, "a1", date(2026, 1, 1), "at_risk") is None

    db.upsert_alert_outcome(
        engine,
        run_id="run1",
        account_id="a1",
        month=date(2026, 1, 1),
        alert_type="at_risk",
        channel="amer-risk-alerts",
        status="failed",
        error="boom",
    )
    existing = db.get_existing_outcome(engine, "a1", date(2026, 1, 1), "at_risk")
    assert existing["status"] == "failed"

    # Retry on failed: same (account_id, month, alert_type) key upserts the
    # existing row rather than creating a second one.
    db.create_run(engine, run_id="run2", source_uri="x", month=date(2026, 1, 1), dry_run=False)
    db.upsert_alert_outcome(
        engine,
        run_id="run2",
        account_id="a1",
        month=date(2026, 1, 1),
        alert_type="at_risk",
        channel="amer-risk-alerts",
        status="sent",
    )
    existing = db.get_existing_outcome(engine, "a1", date(2026, 1, 1), "at_risk")
    assert existing["status"] == "sent"
    assert existing["run_id"] == "run2"

    # Retrying updates canonical state but does not erase the first run's
    # immutable audit result.
    assert len(db.list_alert_outcomes(engine, "run1")) == 1
    assert db.list_alert_outcomes(engine, "run1")[0]["status"] == "failed"
    assert len(db.list_alert_outcomes(engine, "run2")) == 1


def test_sent_outcome_is_the_replay_skip_signal(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    db.create_run(engine, run_id="run1", source_uri="x", month=date(2026, 1, 1), dry_run=False)
    db.upsert_alert_outcome(
        engine,
        run_id="run1",
        account_id="a1",
        month=date(2026, 1, 1),
        alert_type="at_risk",
        channel="amer-risk-alerts",
        status="sent",
    )

    # main.py's replay-safety contract: on a second run, get_existing_outcome
    # returning status == "sent" means "skip, don't call Slack, don't upsert
    # again" -- confirm the row this decision hinges on is queryable and
    # remains unchanged if that upsert is (correctly) skipped.
    existing = db.get_existing_outcome(engine, "a1", date(2026, 1, 1), "at_risk")
    assert existing["status"] == "sent"
    assert existing["run_id"] == "run1"


def test_claim_alert_is_atomic_and_failed_alert_can_be_reclaimed(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    month = date(2026, 1, 1)
    db.create_run(engine, run_id="run1", source_uri="x", month=month, dry_run=False)
    db.create_run(engine, run_id="run2", source_uri="x", month=month, dry_run=False)

    claim = dict(account_id="a1", month=month, alert_type="at_risk", channel="amer")
    assert db.claim_alert(engine, run_id="run1", **claim) is True
    assert db.claim_alert(engine, run_id="run2", **claim) is False

    db.upsert_alert_outcome(engine, run_id="run1", status="failed", error="boom", **claim)
    assert db.claim_alert(engine, run_id="run2", **claim) is True


def test_stale_pending_claim_can_be_recovered(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    month = date(2026, 1, 1)
    db.create_run(engine, run_id="run1", source_uri="x", month=month, dry_run=False)
    db.create_run(engine, run_id="run2", source_uri="x", month=month, dry_run=False)
    claim = dict(account_id="a1", month=month, alert_type="at_risk", channel="amer")
    assert db.claim_alert(engine, run_id="run1", **claim) is True
    with engine.begin() as conn:
        conn.execute(
            update(db.alert_outcomes).values(
                updated_at=datetime.now(timezone.utc) - timedelta(minutes=10)
            )
        )
    assert db.claim_alert(
        engine, run_id="run2", claim_timeout_seconds=300, **claim
    ) is True
    assert db.get_existing_outcome(engine, "a1", month, "at_risk")["run_id"] == "run2"


def test_sent_canonical_outcome_cannot_be_downgraded(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    month = date(2026, 1, 1)
    for run_id in ("run1", "run2"):
        db.create_run(engine, run_id=run_id, source_uri="x", month=month, dry_run=False)
    common = dict(account_id="a1", month=month, alert_type="at_risk", channel="amer")
    db.upsert_alert_outcome(engine, run_id="run1", status="sent", **common)
    db.upsert_alert_outcome(engine, run_id="run2", status="failed", error="late failure", **common)
    canonical = db.get_existing_outcome(engine, "a1", month, "at_risk")
    assert canonical["status"] == "sent"
    assert canonical["run_id"] == "run1"
    assert db.list_alert_outcomes(engine, "run2")[0]["status"] == "failed"


def test_skipped_replay_is_preserved_for_replaying_run(tmp_path):
    engine = db.get_engine(str(tmp_path / "test.db"))
    month = date(2026, 1, 1)
    db.create_run(engine, run_id="run1", source_uri="x", month=month, dry_run=False)
    db.record_run_result(
        engine,
        run_id="run1",
        account_id="a1",
        month=month,
        alert_type="at_risk",
        channel="amer",
        status="skipped_replay",
        reason="already_sent",
    )
    assert db.list_alert_outcomes(engine, "run1")[0]["status"] == "skipped_replay"
