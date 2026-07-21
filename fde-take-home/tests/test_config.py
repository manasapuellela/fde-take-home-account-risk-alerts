import pytest

from app.config import Settings


def test_settings_rejects_invalid_retry_attempts():
    with pytest.raises(ValueError, match="RETRY_MAX_ATTEMPTS"):
        Settings(retry_max_attempts=0)


def test_settings_rejects_invalid_region_map_shape():
    with pytest.raises(ValueError, match="REGION_CHANNEL_MAP"):
        Settings(region_channel_map={"AMER": ""})


def test_settings_rejects_nonpositive_claim_timeout():
    with pytest.raises(ValueError, match="CLAIM_TIMEOUT_SECONDS"):
        Settings(claim_timeout_seconds=0)


def test_settings_accepts_valid_operational_values():
    settings = Settings(region_channel_map={"AMER": "amer-risk-alerts"})
    assert settings.claim_timeout_seconds == 300.0
