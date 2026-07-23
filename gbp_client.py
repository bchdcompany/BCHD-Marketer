"""
Google Business Profile API клиент
Позволяет агенту:
- Получать новые отзывы
- Публиковать ответы на отзывы (после одобрения владельца)
- Получать статистику профиля

Использует те же OAuth credentials что и Google Ads API.
Scope: https://www.googleapis.com/auth/business.manage

Настройка:
1. Включи Google Business Profile API в console.cloud.google.com
2. Добавь GBP_ACCOUNT_NAME в Railway Variables
   Формат: accounts/123456789
   Найти: GET https://mybusinessaccountmanagement.googleapis.com/v1/accounts
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pytz

log = logging.getLogger(__name__)
NY_TZ = pytz.timezone("America/New_York")

GBP_BASE = "https://mybusiness.googleapis.com/v4"
ACCOUNTS_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"


class GBPClient:
    def __init__(self, config):
        self.config = config
        self._access_token: Optional[str] = None
        self._token_expires: float = 0

    async def _get_access_token(self) -> str:
        """Получает свежий OAuth access token."""
        import time
        if self._access_token and time.time() < self._token_expires - 60:
            return self._access_token

        def _refresh():
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request as GoogleAuthRequest
            creds = Credentials(
                token=None,
                refresh_token=self.config.GOOGLE_ADS_REFRESH_TOKEN,
                client_id=self.config.GOOGLE_ADS_CLIENT_ID,
                client_secret=self.config.GOOGLE_ADS_CLIENT_SECRET,
                token_uri="https://oauth2.googleapis.com/token",
                scopes=["https://www.googleapis.com/auth/business.manage"],
            )
            creds.refresh(GoogleAuthRequest())
            return creds.token, creds.expiry

        import time
        token, expiry = await asyncio.to_thread(_refresh)
        self._access_token = token
        self._token_expires = expiry.timestamp() if expiry else time.time() + 3600
        return self._access_token

    async def _get(self, url: str, params: dict = None) -> dict:
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=headers, params=params or {})
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log.error(f"GBP GET {url}: {e}")
            return {"error": str(e)}

    async def _post(self, url: str, data: dict) -> dict:
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, headers=headers, json=data)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log.error(f"GBP POST {url}: {e}")
            return {"error": str(e)}

    async def get_accounts(self) -> dict:
        """Возвращает список GBP аккаунтов — для поиска account_name."""
        return await self._get(f"{ACCOUNTS_BASE}/accounts")

    async def get_locations(self) -> dict:
        """Возвращает список локаций (профилей) для аккаунта."""
        account = self.config.GBP_ACCOUNT_NAME
        if not account:
            return {"error": "GBP_ACCOUNT_NAME не настроен"}
        return await self._get(f"{GBP_BASE}/{account}/locations")

    async def get_reviews(self, location_name: str = None, days: int = 7) -> dict:
        """
        Возвращает отзывы за последние N дней.
        location_name: формат 'accounts/X/locations/Y'
        """
        account = self.config.GBP_ACCOUNT_NAME
        if not account:
            return {"error": "GBP_ACCOUNT_NAME не настроен", "reviews": []}

        # Если location_name не передан — берём первую локацию
        if not location_name:
            locs = await self.get_locations()
            if "error" in locs:
                return {"error": locs["error"], "reviews": []}
            locations = locs.get("locations", [])
            if not locations:
                return {"error": "Локации не найдены", "reviews": []}
            location_name = locations[0].get("name", "")

        result = await self._get(f"{GBP_BASE}/{location_name}/reviews", {
            "pageSize": 50,
            "orderBy": "updateTime desc",
        })

        if "error" in result:
            return result

        reviews = result.get("reviews", [])
        cutoff = datetime.now(NY_TZ) - timedelta(days=days)

        filtered = []
        for r in reviews:
            update_time = r.get("updateTime", "")
            try:
                dt = datetime.fromisoformat(update_time.replace("Z", "+00:00"))
                dt = dt.astimezone(NY_TZ)
                if dt < cutoff:
                    continue
            except Exception:
                pass

            filtered.append({
                "review_id": r.get("reviewId"),
                "name": r.get("name"),
                "rating": r.get("starRating"),
                "comment": r.get("comment", ""),
                "author": r.get("reviewer", {}).get("displayName", "Аноним"),
                "update_time": update_time,
                "has_reply": bool(r.get("reviewReply")),
                "reply_text": r.get("reviewReply", {}).get("comment", ""),
            })

        unanswered = [r for r in filtered if not r["has_reply"]]

        return {
            "reviews": filtered,
            "unanswered": unanswered,
            "total": len(filtered),
            "unanswered_count": len(unanswered),
            "location_name": location_name,
            "days": days,
        }

    async def reply_to_review(self, review_name: str, reply_text: str) -> dict:
        """
        Публикует ответ на отзыв.
        review_name: полное имя ресурса вида accounts/X/locations/Y/reviews/Z
        """
        url = f"{GBP_BASE}/{review_name}/reply"
        result = await self._post(url, {"comment": reply_text})
        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True, "review_name": review_name}

    async def get_unanswered_reviews(self, days: int = 30) -> dict:
        """Возвращает только отзывы без ответа."""
        result = await self.get_reviews(days=days)
        if "error" in result:
            return result
        return {
            "unanswered": result.get("unanswered", []),
            "count": result.get("unanswered_count", 0),
            "location_name": result.get("location_name", ""),
        }


gbp_client: Optional[GBPClient] = None


def init_gbp_client(config) -> Optional[GBPClient]:
    """Инициализирует GBP клиент если настроен."""
    if config.gbp_configured:
        return GBPClient(config)
    return None
