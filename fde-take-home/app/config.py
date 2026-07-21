"""Configuration loading.

Precedence: environment variables > JSON config file (CONFIG_FILE) > defaults.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Optional


def _load_config_file(path: Optional[str]) -> dict:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get(env: Dict[str, str], file_cfg: dict, key: str, default=None):
    if key in env:
        return env[key]
    if key in file_cfg:
        return file_cfg[key]
    return default


@dataclass
class Settings:
    arr_threshold: int = 0
    slack_webhook_base_url: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    details_base_url: str = "https://app.yourcompany.com/accounts"
    region_channel_map: Dict[str, str] = field(default_factory=dict)
    sqlite_path: str = "./risk_alerts.db"
    support_notification_log: str = "./support_notifications.jsonl"
    support_email: str = "support@quadsci.ai"
    retry_max_attempts: int = 4
    retry_base_delay_seconds: float = 0.5
    retry_backoff_factor: float = 2.0
    retry_max_delay_seconds: float = 30.0
    http_timeout_seconds: float = 10.0
    claim_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.arr_threshold < 0:
            raise ValueError("ARR_THRESHOLD must be non-negative")
        if self.retry_max_attempts < 1:
            raise ValueError("RETRY_MAX_ATTEMPTS must be at least 1")
        if self.retry_base_delay_seconds < 0:
            raise ValueError("RETRY_BASE_DELAY_SECONDS must be non-negative")
        if self.retry_backoff_factor <= 0:
            raise ValueError("RETRY_BACKOFF_FACTOR must be greater than 0")
        if self.retry_max_delay_seconds < 0:
            raise ValueError("RETRY_MAX_DELAY_SECONDS must be non-negative")
        if self.http_timeout_seconds <= 0:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be greater than 0")
        if self.claim_timeout_seconds <= 0:
            raise ValueError("CLAIM_TIMEOUT_SECONDS must be greater than 0")
        if not isinstance(self.region_channel_map, dict):
            raise ValueError("REGION_CHANNEL_MAP must be a JSON object")
        invalid_routes = [
            region
            for region, channel in self.region_channel_map.items()
            if not isinstance(region, str)
            or not region.strip()
            or not isinstance(channel, str)
            or not channel.strip()
        ]
        if invalid_routes:
            raise ValueError("REGION_CHANNEL_MAP must map non-empty strings to non-empty strings")

    @classmethod
    def load(cls, env: Optional[Dict[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        file_cfg = _load_config_file(env.get("CONFIG_FILE"))

        region_channel_map = _get(env, file_cfg, "REGION_CHANNEL_MAP", {})
        if isinstance(region_channel_map, str):
            region_channel_map = json.loads(region_channel_map) if region_channel_map else {}
        # Support the spec's {"regions": {...}} shape as well as a flat map.
        if isinstance(region_channel_map, dict) and "regions" in region_channel_map:
            region_channel_map = region_channel_map["regions"]

        return cls(
            arr_threshold=int(_get(env, file_cfg, "ARR_THRESHOLD", 0)),
            slack_webhook_base_url=_get(env, file_cfg, "SLACK_WEBHOOK_BASE_URL"),
            slack_webhook_url=_get(env, file_cfg, "SLACK_WEBHOOK_URL"),
            details_base_url=_get(
                env, file_cfg, "DETAILS_BASE_URL", "https://app.yourcompany.com/accounts"
            ),
            region_channel_map=region_channel_map,
            sqlite_path=_get(env, file_cfg, "SQLITE_PATH", "./risk_alerts.db"),
            support_notification_log=_get(
                env, file_cfg, "SUPPORT_NOTIFICATION_LOG", "./support_notifications.jsonl"
            ),
            support_email=_get(env, file_cfg, "SUPPORT_EMAIL", "support@quadsci.ai"),
            retry_max_attempts=int(_get(env, file_cfg, "RETRY_MAX_ATTEMPTS", 4)),
            retry_base_delay_seconds=float(
                _get(env, file_cfg, "RETRY_BASE_DELAY_SECONDS", 0.5)
            ),
            retry_backoff_factor=float(_get(env, file_cfg, "RETRY_BACKOFF_FACTOR", 2.0)),
            retry_max_delay_seconds=float(_get(env, file_cfg, "RETRY_MAX_DELAY_SECONDS", 30.0)),
            http_timeout_seconds=float(_get(env, file_cfg, "HTTP_TIMEOUT_SECONDS", 10.0)),
            claim_timeout_seconds=float(_get(env, file_cfg, "CLAIM_TIMEOUT_SECONDS", 300.0)),
        )


def get_settings() -> Settings:
    return Settings.load()
