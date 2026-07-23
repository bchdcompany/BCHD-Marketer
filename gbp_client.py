"""
Google Business Profile API клиент
Подключён и протестирован 23.07.2026.

Account: accounts/114321652527408044999
Location: accounts/114321652527408044999/locations/3876947509759665211

Рабочие endpoints:
- Locations: mybusinessaccountmanagement.googleapis.com/v1/accounts/{id}/locations
- Reviews: mybusiness.googleapis.com/v4/accounts/{id}/locations/{lid}/reviews
- Reply: mybusiness.googleapis.com/v4/{review_name}/reply
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pytz

log = logging.getLogger(__name__)
NY_TZ = pytz.timezone("America/New_York")

ACCOUNT_ID = "114321652527408044999"
LOCATION_ID = "3876947509759665211"
ACCOUNT_NAME = f"accounts/{ACCOUNT_ID}"
LOCATION_NAME = f"accounts/{ACCOUNT_ID}/locations/{LOCATION_ID}"
REVIEWS_BASE = f"https://mybusiness.googleapis.com/v4/{LOCATION_NAME}"
LOCATIONS_BASE = f"https://mybusinessaccountmanagement.googleapis.com/v1/{ACCOUNT_NAME}"


class GBPClient:
    def __init__(self, config):
        self.config = config
        self._access_token: Optional[str] = None
        self._token_expires: float = 0

    async def _get_access_token(self) -> str:
        import time
        if self._access_token and time.time() < self._token_expires - 60:
            return self._access_token

        def _refresh():
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request as GoogleAuthRequest
            gbp_refresh_token = (
                getattr(self.config, 'GBP_REFRESH_TOKEN', '') or
                os.environ.get('GBP_REFRESH_TOKEN', '')
            )
            creds = Credentials(
                token=None,
                refresh_token=gbp_refresh_token,
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
                return resp.json() if resp.text else {"success": True}
        except Exception as e:
            log.error(f"GBP POST {url}: {e}")
            return {"error": str(e)}

    async def _delete(self, url: str) -> dict:
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(url, headers=headers)
                resp.raise_for_status()
                return {"success": True}
        except Exception as e:
            log.error(f"GBP DELETE {url}: {e}")
            return {"error": str(e)}

    async def get_reviews(self, days: int = 7) -> dict:
        """Возвращает отзывы за последние N дней."""
        result = await self._get(f"{REVIEWS_BASE}/reviews", {
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
            "location_name": LOCATION_NAME,
            "days": days,
        }

    async def get_unanswered_reviews(self, days: int = 30) -> dict:
        """Возвращает только отзывы без ответа."""
        result = await self.get_reviews(days=days)
        if "error" in result:
            return result
        return {
            "unanswered": result.get("unanswered", []),
            "count": result.get("unanswered_count", 0),
            "location_name": LOCATION_NAME,
        }

    async def reply_to_review(self, review_name: str, reply_text: str) -> dict:
        """Публикует ответ на отзыв."""
        url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
        result = await self._post(url, {"comment": reply_text})
        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True, "review_name": review_name}

    async def delete_reply(self, review_name: str) -> dict:
        """Удаляет ответ на отзыв."""
        url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
        return await self._delete(url)


def init_gbp_client(config) -> Optional["GBPClient"]:
    """Инициализирует GBP клиент если настроен."""
    gbp_token = getattr(config, 'GBP_REFRESH_TOKEN', '') or os.environ.get('GBP_REFRESH_TOKEN', '')
    if gbp_token:
        return GBPClient(config)
    return None
