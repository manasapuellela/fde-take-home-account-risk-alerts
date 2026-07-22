# Tests for app.slack_client: message formatting, channel routing, retry/backoff
from datetime import date

from app import slack_client
from app.config import Settings
from app.risk_logic import RiskAlert


def _alert(**overrides):
    defaults = dict(
        account_id="a1",
        account_name="Account One",
        account_region="AMER",
        month=date(2026, 1, 1),
        status="At Risk",
        renewal_date=date(2026, 6, 1),
        account_owner="owner1@example.com",
        arr=50000,
        duration_months=3,
        risk_start_month=date(2025, 11, 1),
    )
    defaults.update(overrides)
    return RiskAlert(**defaults)


def _settings(**overrides):
    defaults = dict(
        slack_webhook_base_url="http://mock-slack.test/slack/webhook",
        slack_webhook_url=None,
        retry_max_attempts=3,
        retry_base_delay_seconds=0.01,
        retry_backoff_factor=2.0,
        http_timeout_seconds=1.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeResponse:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


def test_route_channel_uses_region_map():
    assert slack_client.route_channel("AMER", {"AMER": "amer-risk-alerts"}) == "amer-risk-alerts"


def test_route_channel_missing_region_has_no_default():
    assert slack_client.route_channel(None, {"AMER": "amer-risk-alerts"}) is None


def test_route_channel_unmapped_region_returns_none():
    assert slack_client.route_channel("LATAM", {"AMER": "amer-risk-alerts"}) is None


def test_format_alert_message_includes_required_fields():
    payload = slack_client.format_alert_message(_alert(), "https://app.yourcompany.com/accounts")
    text = payload["text"]
    assert "At Risk: Account One (a1)" in text
    assert "AMER" in text
    assert "At Risk for: 3 months (since 2025-11-01)" in text
    assert "50000" in text
    assert "2026-06-01" in text
    assert "owner1@example.com" in text
    assert "https://app.yourcompany.com/accounts/a1" in text


def test_format_alert_message_handles_missing_renewal_and_owner():
    payload = slack_client.format_alert_message(
        _alert(renewal_date=None, account_owner=None), "https://app.yourcompany.com/accounts"
    )
    assert "Unknown" in payload["text"]
    assert "Owner" not in payload["text"]


def test_format_alert_message_uses_singular_month():
    payload = slack_client.format_alert_message(
        _alert(duration_months=1, risk_start_month=date(2026, 1, 1)),
        "https://app.yourcompany.com/accounts",
    )
    assert "At Risk for: 1 month (since 2026-01-01)" in payload["text"]
    assert "1 months" not in payload["text"]


def test_send_alert_retries_on_429_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse(429, headers={"Retry-After": "0"})
        return FakeResponse(200)

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    monkeypatch.setattr(slack_client.time, "sleep", lambda s: None)

    result = slack_client.send_alert("amer-risk-alerts", {"text": "hi"}, _settings())

    assert result.status == "sent"
    assert result.attempts == 2
    assert calls == ["http://mock-slack.test/slack/webhook/amer-risk-alerts"] * 2


def test_send_alert_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(slack_client.requests, "post", lambda url, json, timeout: FakeResponse(500))
    monkeypatch.setattr(slack_client.time, "sleep", lambda s: None)

    result = slack_client.send_alert("amer-risk-alerts", {"text": "hi"}, _settings(retry_max_attempts=3))

    assert result.status == "failed"
    assert result.attempts == 3
    assert "500" in result.error


def test_send_alert_retries_any_5xx(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(1)
        return FakeResponse(599 if len(calls) == 1 else 200)

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    monkeypatch.setattr(slack_client.time, "sleep", lambda s: None)
    result = slack_client.send_alert("amer", {"text": "hi"}, _settings())
    assert result.status == "sent"
    assert len(calls) == 2


def test_malformed_retry_after_falls_back_to_backoff(monkeypatch):
    sleeps = []
    calls = []

    def fake_post(url, json, timeout):
        calls.append(1)
        return FakeResponse(429, headers={"Retry-After": "not-a-date"}) if len(calls) == 1 else FakeResponse(200)

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    monkeypatch.setattr(slack_client.time, "sleep", sleeps.append)
    result = slack_client.send_alert("amer", {"text": "hi"}, _settings())
    assert result.status == "sent"
    assert sleeps == [0.01]


def test_send_alert_does_not_retry_non_retryable_status(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(1)
        return FakeResponse(400)

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    monkeypatch.setattr(slack_client.time, "sleep", lambda s: None)

    result = slack_client.send_alert("amer-risk-alerts", {"text": "hi"}, _settings())

    assert result.status == "failed"
    assert result.attempts == 1
    assert len(calls) == 1


def test_base_url_takes_precedence_over_single_webhook():
    settings = _settings(
        slack_webhook_base_url="http://base.test/hook", slack_webhook_url="http://single.test/hook"
    )
    url = slack_client._target_url("amer-risk-alerts", settings)
    assert url == "http://base.test/hook/amer-risk-alerts"


def test_single_webhook_used_when_base_url_unset():
    settings = _settings(slack_webhook_base_url=None, slack_webhook_url="http://single.test/hook")
    url = slack_client._target_url("amer-risk-alerts", settings)
    assert url == "http://single.test/hook"
