"""
BCHD-Marketer — Facebook Page publishing client.

Публикует посты (текст + опционально фото) на страницу Facebook BCHD
через Graph API. Использует долгоживущий Page Access Token (META_PAGE_TOKEN),
который нужно периодически обновлять (~раз в 60 дней) через
refresh_long_lived_token().
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

META_PAGE_ID = os.environ.get("META_PAGE_ID", "")
META_PAGE_TOKEN = os.environ.get("META_PAGE_TOKEN", "")
META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")


async def create_post(message: str, image_url: str = None) -> dict:
    """
    Публикует пост на странице Facebook.
    Если передан image_url — публикует фото с подписью (message становится caption).
    Если нет — публикует обычный текстовый пост.
    """
    if not META_PAGE_ID or not META_PAGE_TOKEN:
        return {"success": False, "error": "META_PAGE_ID/META_PAGE_TOKEN не настроены"}

    if image_url:
        url = f"{GRAPH_API_BASE}/{META_PAGE_ID}/photos"
        payload = {
            "url": image_url,
            "caption": message,
            "access_token": META_PAGE_TOKEN,
        }
    else:
        url = f"{GRAPH_API_BASE}/{META_PAGE_ID}/feed"
        payload = {
            "message": message,
            "access_token": META_PAGE_TOKEN,
        }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=payload)
            data = resp.json()
        if "error" in data:
            logger.error(f"Facebook post error: {data['error']}")
            return {"success": False, "error": data["error"].get("message", str(data["error"]))}
        post_id = data.get("post_id") or data.get("id", "")
        return {"success": True, "post_id": post_id, "result": data}
    except Exception as e:
        logger.error(f"Facebook post exception: {e}")
        return {"success": False, "error": str(e)}


async def get_page_info() -> dict:
    """Проверка токена и доступа к странице — имя, id, категория."""
    if not META_PAGE_ID or not META_PAGE_TOKEN:
        return {"success": False, "error": "META_PAGE_ID/META_PAGE_TOKEN не настроены"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{GRAPH_API_BASE}/{META_PAGE_ID}",
                params={"fields": "name,category,fan_count", "access_token": META_PAGE_TOKEN},
            )
            data = resp.json()
        if "error" in data:
            return {"success": False, "error": data["error"].get("message", str(data["error"]))}
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def refresh_long_lived_token() -> dict:
    """
    Обменивает текущий META_PAGE_TOKEN на новый долгоживущий токен (~60 дней).
    ВАЖНО: возвращает новый токен, но НЕ обновляет переменную окружения
    автоматически — Railway env vars нельзя менять из кода процесса.
    Вызывающий код должен прислать новый токен владельцу для ручного
    обновления в Railway Variables.
    """
    if not META_APP_ID or not META_APP_SECRET or not META_PAGE_TOKEN:
        return {"success": False, "error": "META_APP_ID/META_APP_SECRET/META_PAGE_TOKEN не настроены"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{GRAPH_API_BASE}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": META_APP_ID,
                    "client_secret": META_APP_SECRET,
                    "fb_exchange_token": META_PAGE_TOKEN,
                },
            )
            data = resp.json()
        if "error" in data:
            return {"success": False, "error": data["error"].get("message", str(data["error"]))}
        return {
            "success": True,
            "new_token": data.get("access_token", ""),
            "expires_in_seconds": data.get("expires_in", 0),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
