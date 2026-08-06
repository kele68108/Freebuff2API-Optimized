from __future__ import annotations

import os
from pathlib import Path
import uuid
from dataclasses import dataclass, field

from dotenv import load_dotenv


load_dotenv()


HAR_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Settings:
    codebuff_token: str | None
    local_api_key: str | None
    codebuff_base_url: str = "https://www.codebuff.com"
    acting_user_id: str | None = None
    cli_path: str = ""
    zeroclick_base_url: str = "https://zeroclick.dev"
    session_id: str = ""
    client_id: str = ""
    ad_providers: tuple[str, ...] = ("gravity", "zeroclick")
    request_timeout: float = 60.0
    retry_jitter_ms: int = 250
    # R1: single source of truth for the data dir. Defaults to ~/.freebuff2api
    # (NOT /root) so a non-root service user works out of the box.
    data_dir: Path = field(default_factory=lambda: Path.home() / ".freebuff2api")
    # R3: env-tunable httpx connection pool limits.
    httpx_max_connections: int = 100
    httpx_max_keepalive: int = 20
    httpx_keepalive_expiry: float = 30.0
    httpx_read_timeout: float = 300.0
    # R4: bounded concurrency — max number of requests waiting on a free
    # account before failing fast with 503. 0 = unlimited (old behaviour).
    max_waiters: int = 64
    # R7: proactive account health pinger.
    health_interval: float = 60.0
    health_cooldown: float = 300.0
    # R10: SSE keepalive comment cadence (0 = disabled).
    sse_keepalive_seconds: float = 15.0
    debug: bool = False
    log_level: str = "INFO"
    log_body_chars: int = 2000
    log_color: bool = True
    host: str = "0.0.0.0"
    port: int = 20004
    proxy_enabled: bool = False
    proxy_url: str | None = None
    timezone: str = "Asia/Shanghai"
    locale: str = "zh-CN"
    os_name: str = "windows"

    @property
    def codebuff_api_url(self) -> str:
        return self.codebuff_base_url.strip().rstrip("/")

    @property
    def zeroclick_api_url(self) -> str:
        return self.zeroclick_base_url.rstrip("/")

    @property
    def upstream_proxy_url(self) -> str | None:
        if not self.proxy_enabled:
            return None
        if not self.proxy_url:
            return None
        return self.proxy_url.strip() or None

    @property
    def codebuff_tokens(self) -> tuple[str, ...]:
        if not self.codebuff_token:
            return ()
        values = [item.strip() for item in self.codebuff_token.split(",")]
        return tuple(item for item in values if item)


def _csv(name: str, default: str) -> tuple[str, ...]:
    values = [item.strip() for item in os.getenv(name, default).split(",")]
    return tuple(item for item in values if item)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _api_base_url() -> str:
    return (
        os.getenv("FREEBUFF_API_BASE_URL")
        or os.getenv("CODEBUFF_BASE_URL")
        or "https://www.codebuff.com"
    )


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def load_settings() -> Settings:
    debug = _bool("FREEBUFF_DEBUG", False)
    log_level = "DEBUG" if debug else os.getenv("FREEBUFF_LOG_LEVEL", "INFO")
    color_default = os.getenv("NO_COLOR") is None
    env_data_dir = os.getenv("FREEBUFF2API_DATA_DIR")
    data_dir = Path(env_data_dir) if env_data_dir else Path.home() / ".freebuff2api"
    return Settings(
        codebuff_token=os.getenv("FREEBUFF_TOKEN") or os.getenv("CODEBUFF_TOKEN"),
        local_api_key=os.getenv("FREEBUFF_API_KEY") or os.getenv("OPENAI_API_KEY"),
        codebuff_base_url=_api_base_url(),
        acting_user_id=os.getenv("FREEBUFF_ACTING_USER_ID") or None,
        cli_path=os.getenv("FREEBUFF_CLI_PATH") or str(
            Path.home() / ".config" / "manicode" / "freebuff"
        ),
        zeroclick_base_url=os.getenv("ZEROCLICK_BASE_URL", "https://zeroclick.dev"),
        session_id=os.getenv("FREEBUFF_SESSION_ID", str(uuid.uuid4())),
        client_id=os.getenv("FREEBUFF_CLIENT_ID", uuid.uuid4().hex[:11]),
        ad_providers=_csv("FREEBUFF_AD_PROVIDERS", "gravity,zeroclick"),
        request_timeout=float(os.getenv("FREEBUFF_TIMEOUT", "60")),
        retry_jitter_ms=_int("FREEBUFF_RETRY_JITTER", 250),
        data_dir=data_dir,
        httpx_max_connections=_int("FREEBUFF_HTTPX_MAX_CONNECTIONS", 100),
        httpx_max_keepalive=_int("FREEBUFF_HTTPX_KEEPALIVE", 20),
        httpx_keepalive_expiry=_float_env("FREEBUFF_HTTPX_KEEPALIVE_EXPIRY", 30.0),
        httpx_read_timeout=_float_env("FREEBUFF_HTTPX_READ_TIMEOUT", 300.0),
        max_waiters=_int("FREEBUFF_MAX_WAITERS", 64),
        health_interval=_float_env("FREEBUFF_HEALTH_INTERVAL", 60.0),
        health_cooldown=_float_env("FREEBUFF_HEALTH_COOLDOWN", 300.0),
        sse_keepalive_seconds=_float_env("FREEBUFF_SSE_KEEPALIVE", 15.0),
        debug=debug,
        log_level=log_level,
        log_body_chars=_int("FREEBUFF_LOG_BODY_CHARS", 0 if debug else 2000),
        log_color=_bool("FREEBUFF_LOG_COLOR", color_default),
        host=os.getenv("FREEBUFF_HOST", "0.0.0.0"),
        port=_int("FREEBUFF_PORT", 20004),
        proxy_enabled=_bool("FREEBUFF_PROXY_ENABLED", False),
        proxy_url=os.getenv("FREEBUFF_PROXY_URL"),
        timezone=os.getenv("FREEBUFF_TIMEZONE", "Asia/Shanghai"),
        locale=os.getenv("FREEBUFF_LOCALE", "zh-CN"),
        os_name=os.getenv("FREEBUFF_OS", "windows"),
    )
