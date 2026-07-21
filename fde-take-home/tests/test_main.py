# Tests for app.main: /health, /preview, /runs, /runs/{run_id} via FastAPI TestClient
import json

import pytest
from fastapi.testclient import TestClient

from app import notifications, slack_client
from app.main import app


class FakeHTTPResponse:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SUPPORT_NOTIFICATION_LOG", str(tmp_path / "support.jsonl"))
    monkeypatch.setenv("SLACK_WEBHOOK_BASE_URL", "http://mock-slack.test/slack/webhook")
    monkeypatch.setenv(
        "REGION_CHANNEL_MAP",
        json.dumps({"AMER": "amer-risk-alerts", "EMEA": "emea-risk-alerts", "APAC": "apac-risk-alerts"}),
    )
    monkeypatch.setenv("ARR_THRESHOLD", "10000")


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_preview_computes_alerts_without_sending(client, sample_parquet_path, monkeypatch):
    calls = []
    monkeypatch.setattr(slack_client.requests, "post", lambda *a, **k: calls.append(1))

    resp = client.post(
        "/preview", json={"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-01"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["month"] == "2026-01-01"
    assert len(calls) == 0  # /preview never calls Slack

    by_id = {a["account_id"]: a for a in body["alerts"]}
    assert by_id["acc1"]["status"] == "sent"
    assert by_id["acc2"]["status"] == "failed"
    assert by_id["acc2"]["reason"] == "unknown_region"
    assert "acc3" not in by_id  # excluded by ARR threshold
    assert "acc5" not in by_id  # Healthy in the target month, not a candidate
    assert body["counts"]["duplicates_found"] >= 1


def test_invalid_month_returns_400(client, sample_parquet_path):
    resp = client.post(
        "/preview", json={"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-15"}
    )
    assert resp.status_code == 400


def test_get_unknown_run_returns_404(client):
    resp = client.get("/runs/does-not-exist")
    assert resp.status_code == 404


def test_runs_sends_alerts_and_persists_outcomes(client, sample_parquet_path, monkeypatch, tmp_path):
    monkeypatch.setattr(
        slack_client.requests, "post", lambda url, json, timeout: FakeHTTPResponse(200)
    )

    resp = client.post(
        "/runs",
        json={"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-01", "dry_run": False},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    detail = client.get(f"/runs/{run_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["counts"]["alerts_sent"] == 3  # acc1, acc4, acc6
    assert detail["counts"]["failed_deliveries"] == 1  # acc2: unknown_region
    assert detail["counts"]["skipped_replay"] == 0

    support_log = tmp_path / "support.jsonl"
    assert support_log.exists()
    records = [json.loads(line) for line in support_log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["accounts"][0]["account_id"] == "acc2"


def test_replay_same_month_skips_already_sent_alerts(client, sample_parquet_path, monkeypatch):
    call_count = {"n": 0}

    def fake_post(url, json, timeout):
        call_count["n"] += 1
        return FakeHTTPResponse(200)

    monkeypatch.setattr(slack_client.requests, "post", fake_post)

    req = {"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-01", "dry_run": False}
    client.post("/runs", json=req)
    first_call_count = call_count["n"]
    assert first_call_count == 3  # acc1, acc4, acc6

    second_run_id = client.post("/runs", json=req).json()["run_id"]
    assert call_count["n"] == first_call_count  # no new Slack calls on replay

    detail = client.get(f"/runs/{second_run_id}").json()
    assert detail["counts"]["skipped_replay"] == 3
    assert detail["counts"]["alerts_sent"] == 0
    assert detail["counts"]["failed_deliveries"] == 1  # acc2 unknown_region retried every run
    assert any(a["status"] == "skipped_replay" for a in detail["sample_alerts"])


def test_retry_on_previously_failed_alert(client, sample_parquet_path, monkeypatch):
    monkeypatch.setattr(
        slack_client.requests, "post", lambda url, json, timeout: FakeHTTPResponse(500)
    )
    monkeypatch.setattr(slack_client.time, "sleep", lambda s: None)

    req = {"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-01", "dry_run": False}
    first_run_id = client.post("/runs", json=req).json()["run_id"]
    first_detail = client.get(f"/runs/{first_run_id}").json()
    assert first_detail["counts"]["failed_deliveries"] == 4  # acc1, acc4, acc6 (500s) + acc2

    monkeypatch.setattr(
        slack_client.requests, "post", lambda url, json, timeout: FakeHTTPResponse(200)
    )

    second_run_id = client.post("/runs", json=req).json()["run_id"]
    second_detail = client.get(f"/runs/{second_run_id}").json()
    assert second_detail["counts"]["alerts_sent"] == 3  # acc1, acc4, acc6 retried and now sent
    assert second_detail["counts"]["failed_deliveries"] == 1  # acc2 still unknown_region
    assert second_detail["counts"]["skipped_replay"] == 0


def test_delivery_exception_after_claim_does_not_strand_the_alert(
    client, sample_parquet_path, monkeypatch
):
    # Neither SLACK_WEBHOOK_BASE_URL nor SLACK_WEBHOOK_URL configured: every
    # attempted delivery raises inside slack_client (from _target_url), well
    # after claim_alert has already reserved the row as "pending". If that
    # exception isn't caught, the row is stuck "pending" forever -- claim_alert
    # only ever reclaims rows left "failed" -- and the whole run aborts on the
    # first candidate instead of processing the rest.
    monkeypatch.delenv("SLACK_WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    req = {"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-01", "dry_run": False}
    first_run_id = client.post("/runs", json=req).json()["run_id"]
    first_detail = client.get(f"/runs/{first_run_id}").json()

    assert first_detail["status"] == "succeeded"
    assert first_detail["counts"]["alerts_sent"] == 0
    assert first_detail["counts"]["failed_deliveries"] == 4  # acc1, acc4, acc6, acc2

    # Fix the config and confirm every previously-failed account is
    # reclaimable -- nothing was left permanently stuck "pending".
    monkeypatch.setenv("SLACK_WEBHOOK_BASE_URL", "http://mock-slack.test/slack/webhook")
    monkeypatch.setattr(
        slack_client.requests, "post", lambda url, json, timeout: FakeHTTPResponse(200)
    )

    second_run_id = client.post("/runs", json=req).json()["run_id"]
    second_detail = client.get(f"/runs/{second_run_id}").json()
    assert second_detail["counts"]["alerts_sent"] == 3  # acc1, acc4, acc6 reclaimed and sent
    assert second_detail["counts"]["failed_deliveries"] == 1  # acc2 still unknown_region
    assert second_detail["counts"]["skipped_replay"] == 0


def test_dry_run_via_runs_endpoint_does_not_call_slack(client, sample_parquet_path, monkeypatch):
    calls = []
    monkeypatch.setattr(slack_client.requests, "post", lambda *a, **k: calls.append(1))

    run_id = client.post(
        "/runs",
        json={"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-01", "dry_run": True},
    ).json()["run_id"]

    assert len(calls) == 0
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["dry_run"] is True
    assert detail["counts"]["alerts_sent"] == 3


def test_fatal_post_processing_error_preserves_partial_counts(
    client, sample_parquet_path, monkeypatch
):
    monkeypatch.setattr(
        slack_client.requests, "post", lambda url, json, timeout: FakeHTTPResponse(200)
    )
    monkeypatch.setattr(
        notifications,
        "send_unknown_region_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("notification log unavailable")),
    )
    response = client.post(
        "/runs",
        json={"source_uri": sample_parquet_path.as_uri(), "month": "2026-01-01"},
    )
    detail = client.get(f"/runs/{response.json()['run_id']}").json()
    assert detail["status"] == "failed"
    assert detail["counts"]["alerts_sent"] == 3
    assert detail["counts"]["failed_deliveries"] == 1
