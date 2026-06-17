"""
BCHD Marketer Agent — конфигурация
Все переменные берутся из Railway Environment Variables
"""

import os


class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
    OWNER_CHAT_ID: int = int(os.environ.get("OWNER_CHAT_ID", "984556400"))

    # Anthropic
    ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
    CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    # Google Ads API
    GOOGLE_ADS_DEVELOPER_TOKEN: str = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
    GOOGLE_ADS_CLIENT_ID: str = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
    GOOGLE_ADS_CLIENT_SECRET: str = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
    GOOGLE_ADS_REFRESH_TOKEN: str = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", "")
    GOOGLE_ADS_CUSTOMER_ID: str = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: str = os.environ.get(
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
    )

    # Настройки компании
    COMPANY_NAME: str = "BCHD Appliance Repair & HVAC"
    TIMEZONE: str = "America/New_York"

    @property
    def google_ads_configured(self) -> bool:
        """Проверяет, настроен ли Google Ads API"""
        return bool(
            self.GOOGLE_ADS_DEVELOPER_TOKEN
            and self.GOOGLE_ADS_CLIENT_ID
            and self.GOOGLE_ADS_REFRESH_TOKEN
            and self.GOOGLE_ADS_CUSTOMER_ID
        )


config = Config()
