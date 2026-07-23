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

    # Google Ads (936-279-9327) — основная кампания
    GOOGLE_ADS_CUSTOMER_ID: str = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")

    # LSA (667-939-5231) — Local Services Ads
    GOOGLE_ADS_LSA_CUSTOMER_ID: str = os.environ.get("GOOGLE_ADS_LSA_CUSTOMER_ID", "")

    # MCC (313-939-3264) — управляющий аккаунт
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: str = os.environ.get(
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
    )

    # Настройки компании
    COMPANY_NAME: str = "BCHD Appliance Repair & HVAC"
    TIMEZONE: str = "America/New_York"

    # Пороговые значения для анализа ключевых слов (appliance repair, USA бенчмарки)
    # Слабый ключ: CTR ниже этого % при MIN_IMPRESSIONS_FOR_JUDGMENT+ показах
    MIN_CTR_THRESHOLD: float = 2.0
    # Минимум показов для статистически значимого вывода о ключе
    MIN_IMPRESSIONS_FOR_JUDGMENT: int = 100
    # CPA выше этого порога = дорогой ключ, требует внимания
    MAX_CPA_THRESHOLD: float = 80.0

    # Рекламные бюджеты сторонних платформ (не Google Ads)
    # Thumbtack: фиксированный недельный бюджет в USD.
    # Чтобы изменить: Railway → BCHD-Marketer → Variables → THUMBTACK_WEEKLY_BUDGET
    # Деплой не нужен — переменная подхватывается при следующем перезапуске.
    THUMBTACK_WEEKLY_BUDGET: float = float(os.environ.get("THUMBTACK_WEEKLY_BUDGET", "200"))

    # Google Business Profile API
    # Использует те же OAuth credentials что и Google Ads
    # (client_id, client_secret, refresh_token)
    GBP_ACCOUNT_NAME: str = os.environ.get("GBP_ACCOUNT_NAME", "")
    # Формат: accounts/123456789 (найти в GBP API или Google My Business)

    @property
    def google_ads_configured(self) -> bool:
        """Проверяет, настроен ли Google Ads API"""
        return bool(
            self.GOOGLE_ADS_DEVELOPER_TOKEN
            and self.GOOGLE_ADS_CLIENT_ID
            and self.GOOGLE_ADS_REFRESH_TOKEN
            and self.GOOGLE_ADS_CUSTOMER_ID
        )

    @property
    def lsa_configured(self) -> bool:
        """Проверяет, настроен ли LSA аккаунт"""
        return bool(self.GOOGLE_ADS_LSA_CUSTOMER_ID and self.google_ads_configured)

    @property
    def gbp_configured(self) -> bool:
        """Проверяет, настроен ли Google Business Profile API"""
        return bool(
            self.GBP_ACCOUNT_NAME
            and self.GOOGLE_ADS_CLIENT_ID
            and self.GOOGLE_ADS_CLIENT_SECRET
            and self.GOOGLE_ADS_REFRESH_TOKEN
        )


config = Config()
