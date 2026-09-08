from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_env: str
    host: str
    port: int
    database_path: Path
    timezone: str
    scheduler_enabled: bool
    initial_lookback_days: int
    overlap_hours: int
    request_timeout_seconds: int
    max_articles_per_fetch: int
    admin_api_key: str
    bing_news_rss_enabled: bool
    google_news_rss_enabled: bool
    news_api_key: str
    gnews_api_key: str
    serper_api_key: str
    google_cse_api_key: str
    google_cse_id: str
    google_sheets_enabled: bool
    google_sheet_id: str
    google_worksheet_name: str
    google_service_account_json: str
    email_enabled: bool
    email_on_manual_fetch: bool
    email_recipients: tuple[str, ...]
    resend_api_key: str
    email_from: str
    email_reply_to: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_use_tls: bool

    @classmethod
    def from_env(cls) -> "Settings":
        recipients = tuple(
            address.strip()
            for address in os.getenv("EMAIL_RECIPIENTS", "").split(",")
            if address.strip()
        )
        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            host=os.getenv("APP_HOST", "0.0.0.0"),
            port=_int("APP_PORT", 8000),
            database_path=Path(os.getenv("DATABASE_PATH", "data/monitor.db")).resolve(),
            timezone=os.getenv("APP_TIMEZONE", "Asia/Kolkata"),
            scheduler_enabled=_bool("SCHEDULER_ENABLED", True),
            initial_lookback_days=max(1, _int("INITIAL_LOOKBACK_DAYS", 30)),
            overlap_hours=max(0, _int("OVERLAP_HOURS", 48)),
            request_timeout_seconds=max(3, _int("REQUEST_TIMEOUT_SECONDS", 15)),
            max_articles_per_fetch=max(1, _int("MAX_ARTICLES_PER_FETCH", 150)),
            admin_api_key=os.getenv("ADMIN_API_KEY", ""),
            bing_news_rss_enabled=_bool("BING_NEWS_RSS_ENABLED", True),
            google_news_rss_enabled=_bool("GOOGLE_NEWS_RSS_ENABLED", True),
            news_api_key=os.getenv("NEWS_API_KEY", ""),
            gnews_api_key=os.getenv("GNEWS_API_KEY", ""),
            serper_api_key=os.getenv("SERPER_API_KEY", ""),
            google_cse_api_key=os.getenv("GOOGLE_CSE_API_KEY", ""),
            google_cse_id=os.getenv("GOOGLE_CSE_ID", ""),
            google_sheets_enabled=_bool("GOOGLE_SHEETS_ENABLED", True),
            google_sheet_id=os.getenv(
                "GOOGLE_SHEET_ID", "11KZA69WxbxLeDP0O_Fg94CnooIWpzVnvJyatRuSP0rw"
            ),
            google_worksheet_name=os.getenv("GOOGLE_WORKSHEET_NAME", ""),
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
            email_enabled=_bool("EMAIL_ENABLED", True),
            email_on_manual_fetch=_bool("EMAIL_ON_MANUAL_FETCH", False),
            email_recipients=recipients,
            resend_api_key=os.getenv("RESEND_API_KEY", ""),
            email_from=os.getenv("EMAIL_FROM", os.getenv("SMTP_FROM", "")),
            email_reply_to=os.getenv("EMAIL_REPLY_TO", ""),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from=os.getenv("SMTP_FROM", ""),
            smtp_use_tls=_bool("SMTP_USE_TLS", True),
        )

    @property
    def search_provider_names(self) -> list[str]:
        providers: list[str] = []
        if self.bing_news_rss_enabled:
            providers.append("Bing News RSS")
        if self.news_api_key:
            providers.append("NewsAPI")
        if self.gnews_api_key:
            providers.append("GNews")
        if self.serper_api_key:
            providers.append("Serper")
        if self.google_cse_api_key and self.google_cse_id:
            providers.append("Google Programmable Search")
        return providers
