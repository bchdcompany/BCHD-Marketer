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
            # В BCHD-Marketer OAuth credentials хранятся как GMAIL_CLIENT_ID/SECRET
            client_id = (getattr(self.config, 'GOOGLE_ADS_CLIENT_ID', '') or
                        getattr(self.config, 'GMAIL_CLIENT_ID', '') or
                        os.environ.get('GMAIL_CLIENT_ID', ''))
            client_secret = (getattr(self.config, 'GOOGLE_ADS_CLIENT_SECRET', '') or
                            getattr(self.config, 'GMAIL_CLIENT_SECRET', '') or
                            os.environ.get('GMAIL_CLIENT_SECRET', ''))
            creds = Credentials(
                token=None,
                refresh_token=gbp_refresh_token,
                client_id=client_id,
                client_secret=client_secret,
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
        # review_name формат: accounts/X/locations/Y/reviews/Z
        # Endpoint: PUT https://mybusiness.googleapis.com/v4/{review_name}/reply
        url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.put(url, headers=headers, json={"comment": reply_text})
                if resp.status_code in (200, 201):
                    return {"success": True, "review_name": review_name}
                return {"success": False, "error": f"{resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_profile(self) -> dict:
        """Полные данные профиля GBP для аналитики заполненности."""
        # mybusinessbusinessinformation API v1 — основные данные
        # Endpoint: /v1/locations/{locationId}
        MBIZ_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
        # location_name формат: accounts/X/locations/Y
        # для v1 API нужен просто locations/Y
        loc_id = LOCATION_NAME.split("/locations/")[-1]
        location_url = f"{MBIZ_BASE}/locations/{loc_id}"
        read_mask = (
            "name,title,phoneNumbers,categories,storefrontAddress,"
            "websiteUri,regularHours,profile,serviceArea,metadata,latlng"
        )
        location_data = await self._get(location_url, {"readMask": read_mask})
        log.info(f"GBP profile response keys: {list(location_data.keys())}")

        # Фото через v4 API (рабочий endpoint)
        media_url = f"https://mybusiness.googleapis.com/v4/{LOCATION_NAME}/media"
        media_data = await self._get(media_url, {"pageSize": 100})

        # Услуги через v1 API
        # services endpoint
        services_url = f"{MBIZ_BASE}/locations/{loc_id}/serviceList"
        services_data = await self._get(services_url, {})

        missing = []
        score = 0
        total = 0

        def check(cond, name, w=1):
            nonlocal score, total
            total += w
            if cond:
                score += w
            else:
                missing.append(name)

        has_phone = bool(location_data.get("phoneNumbers", {}).get("primaryPhone"))
        has_website = bool(location_data.get("websiteUri"))
        description = location_data.get("profile", {}).get("description", "")
        has_description = bool(description)
        has_primary_cat = bool(location_data.get("categories", {}).get("primaryCategory"))
        has_add_cats = len(location_data.get("categories", {}).get("additionalCategories", [])) > 0
        has_hours = bool(location_data.get("regularHours", {}).get("periods"))
        has_service_area = bool(location_data.get("serviceArea"))

        check(has_phone, "Телефон", 2)
        check(has_website, "Сайт", 2)
        check(has_description, "Описание бизнеса", 3)
        check(len(description) >= 400, f"Описание 400+ символов (сейчас {len(description)})", 2)
        check(has_primary_cat, "Основная категория", 3)
        check(has_add_cats, "Дополнительные категории", 2)
        check(has_hours, "Часы работы", 2)
        check(has_service_area, "Зона обслуживания", 1)

        media_items = media_data.get("mediaItems", []) if "error" not in media_data else []
        photos_by_cat = {}
        for m in media_items:
            cat = m.get("locationAssociation", {}).get("category", "ADDITIONAL")
            photos_by_cat[cat] = photos_by_cat.get(cat, 0) + 1

        check(photos_by_cat.get("LOGO", 0) > 0, "Логотип", 2)
        check(photos_by_cat.get("COVER", 0) > 0, "Обложка", 2)
        check(len(media_items) >= 5, f"5+ фото (сейчас {len(media_items)})", 2)
        check(len(media_items) >= 10, f"10+ фото (сейчас {len(media_items)})", 1)

        # Услуги берём из serviceTypes основной категории
        service_types = (location_data.get("categories", {})
                        .get("primaryCategory", {})
                        .get("serviceTypes", []))
        service_items = service_types
        check(len(service_items) >= 3, f"3+ услуги (сейчас {len(service_items)})", 3)

        completion_pct = round(score / total * 100) if total > 0 else 0
        primary_cat = location_data.get("categories", {}).get("primaryCategory", {})
        add_cats = location_data.get("categories", {}).get("additionalCategories", [])

        return {
            "location_name": LOCATION_NAME,
            "title": location_data.get("title", ""),
            "phone": location_data.get("phoneNumbers", {}).get("primaryPhone", ""),
            "website": location_data.get("websiteUri", ""),
            "description": description,
            "description_length": len(description),
            "primary_category": primary_cat.get("displayName", ""),
            "additional_categories": [c.get("displayName", "") for c in add_cats],
            "has_hours": has_hours,
            "photos_total": len(media_items),
            "photos_by_category": photos_by_cat,
            "services_count": len(service_items),
            "services": [s.get("displayName", s.get("serviceTypeId", "")) for s in service_items[:20]],
            "completion_pct": completion_pct,
            "missing": missing,
            "score": score,
            "total_points": total,
            "errors": {
                k: v.get("error") for k, v in {
                    "location": location_data,
                    "media": media_data,
                }.items() if isinstance(v, dict) and "error" in v
            }
        }


    async def _patch(self, url: str, data: dict, update_mask: str) -> dict:
        """PATCH запрос для обновления данных профиля."""
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.patch(
                    url, headers=headers, json=data,
                    params={"updateMask": update_mask}
                )
                if resp.status_code in (200, 201):
                    return resp.json() if resp.text else {"success": True}
                return {"error": f"{resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            log.error(f"GBP PATCH {url}: {e}")
            return {"error": str(e)}

    async def update_description(self, description: str) -> dict:
        """Обновляет описание бизнеса в GBP."""
        MBIZ_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
        loc_id = LOCATION_NAME.split("/locations/")[-1]
        url = f"{MBIZ_BASE}/locations/{loc_id}"
        result = await self._patch(url, {"profile": {"description": description}}, "profile.description")
        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True, "description": description}

    async def update_categories(self, primary_category_id: str, additional_category_ids: list) -> dict:
        """Обновляет категории бизнеса в GBP."""
        MBIZ_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
        loc_id = LOCATION_NAME.split("/locations/")[-1]
        url = f"{MBIZ_BASE}/locations/{loc_id}"
        data = {
            "categories": {
                "primaryCategory": {"name": primary_category_id},
                "additionalCategories": [{"name": cid} for cid in additional_category_ids]
            }
        }
        result = await self._patch(url, data, "categories")
        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True, "categories_updated": True}

    async def create_post(self, text: str, topic_type: str = "STANDARD") -> dict:
        """
        Публикует пост в GBP (Google Business Profile Posts).
        topic_type: STANDARD (обычный пост), EVENT (событие), OFFER (акция)
        """
        url = f"https://mybusiness.googleapis.com/v4/{LOCATION_NAME}/localPosts"
        data = {
            "languageCode": "en-US",
            "summary": text,
            "topicType": topic_type,
        }
        result = await self._post(url, data)
        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {
            "success": True,
            "post_name": result.get("name", ""),
            "text": text[:100],
        }

    async def get_posts(self, page_size: int = 10) -> dict:
        """Получает последние посты GBP."""
        url = f"https://mybusiness.googleapis.com/v4/{LOCATION_NAME}/localPosts"
        result = await self._get(url, {"pageSize": page_size})
        if "error" in result:
            return {"error": result["error"], "posts": []}
        posts = result.get("localPosts", [])
        return {
            "posts": [{
                "name": p.get("name"),
                "text": p.get("summary", "")[:200],
                "state": p.get("state"),
                "create_time": p.get("createTime"),
                "topic_type": p.get("topicType"),
            } for p in posts],
            "total": len(posts),
        }

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
