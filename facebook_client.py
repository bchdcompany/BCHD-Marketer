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


async def create_video_post(message: str, video_url: str) -> dict:
    """
    Публикует видео на странице Facebook с описанием (caption).
    Facebook Graph API поддерживает видео напрямую в постах (в отличие от
    Google Business Profile, где видео разрешено только в общей галерее).
    """
    if not META_PAGE_ID or not META_PAGE_TOKEN:
        return {"success": False, "error": "META_PAGE_ID/META_PAGE_TOKEN не настроены"}

    url = f"{GRAPH_API_BASE}/{META_PAGE_ID}/videos"
    payload = {
        "file_url": video_url,
        "description": message,
        "access_token": META_PAGE_TOKEN,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, data=payload)
            data = resp.json()
        if "error" in data:
            logger.error(f"Facebook video post error: {data['error']}")
            return {"success": False, "error": data["error"].get("message", str(data["error"]))}
        return {"success": True, "video_id": data.get("id", ""), "result": data}
    except Exception as e:
        logger.error(f"Facebook video post exception: {e}")
        return {"success": False, "error": str(e)}


META_INSTAGRAM_ID = os.environ.get("META_INSTAGRAM_ID", "")


async def create_instagram_post(caption: str, image_url: str) -> dict:
    """
    Публикует фото-пост в Instagram через двухшаговый процесс Graph API:
    1. Создаём media container (загружаем фото + caption)
    2. Публикуем контейнер (media_publish)
    Instagram Graph API требует фото по прямой публичной ссылке (не Telegram URL).
    """
    if not META_INSTAGRAM_ID or not META_PAGE_TOKEN:
        return {"success": False, "error": "META_INSTAGRAM_ID/META_PAGE_TOKEN не настроены"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Шаг 1 — создаём container. ВАЖНО: явно указываем media_type="IMAGE" —
            # без него Instagram Graph API иногда не может однозначно определить
            # тип медиа по URL (особенно если сервис-хостер отдаёт нестандартные
            # заголовки/редиректы) и отвечает ошибкой "Only photo or video can be
            # accepted as media type." даже для нормальной прямой ссылки на фото.
            # Обнаружено 23.09.2026 на реальной публикации через ImgBB-ссылку.
            # У видео (create_instagram_video_post ниже) media_type уже передавался
            # явно ("REELS") — для фото этого не хватало, отсюда асимметрия.
            resp1 = await client.post(
                f"{GRAPH_API_BASE}/{META_INSTAGRAM_ID}/media",
                data={"image_url": image_url, "caption": caption, "media_type": "IMAGE", "access_token": META_PAGE_TOKEN},
            )
            data1 = resp1.json()
            if "error" in data1:
                logger.error(f"Instagram container error: {data1['error']}")
                return {"success": False, "error": data1["error"].get("message", str(data1["error"]))}
            container_id = data1.get("id", "")

            # Ждём, пока Instagram обработает контейнер, ПЕРЕД публикацией — как
            # уже делается для видео ниже (create_instagram_video_post). Раньше
            # для фото этого шага не было, и media_publish вызывался сразу же,
            # что иногда даёт ошибку "Media ID is not available" / "The media
            # is not ready for publishing" (code 9007, subcode 2207027) —
            # обнаружено 23.09.2026 на реальной публикации. Фото обычно готовы
            # намного быстрее видео, поэтому ждём максимум ~15 секунд.
            import asyncio as _asyncio_ig
            for _ in range(5):
                status_resp = await client.get(
                    f"{GRAPH_API_BASE}/{container_id}",
                    params={"fields": "status_code", "access_token": META_PAGE_TOKEN},
                )
                status_data = status_resp.json()
                if status_data.get("status_code") == "FINISHED":
                    break
                if status_data.get("status_code") == "ERROR":
                    logger.error(f"Instagram container processing error: {status_data}")
                    return {"success": False, "error": f"Container processing failed: {status_data}"}
                await _asyncio_ig.sleep(3)

            # Шаг 2 — публикуем container
            resp2 = await client.post(
                f"{GRAPH_API_BASE}/{META_INSTAGRAM_ID}/media_publish",
                data={"creation_id": container_id, "access_token": META_PAGE_TOKEN},
            )
            data2 = resp2.json()
            if "error" in data2:
                logger.error(f"Instagram publish error: {data2['error']}")
                return {"success": False, "error": data2["error"].get("message", str(data2["error"]))}
            return {"success": True, "post_id": data2.get("id", ""), "result": data2}
    except Exception as e:
        logger.error(f"Instagram post exception: {e}")
        return {"success": False, "error": str(e)}


async def create_instagram_video_post(caption: str, video_url: str) -> dict:
    """
    Публикует видео (Reel) в Instagram через тот же двухшаговый процесс,
    но с media_type=REELS.
    """
    if not META_INSTAGRAM_ID or not META_PAGE_TOKEN:
        return {"success": False, "error": "META_INSTAGRAM_ID/META_PAGE_TOKEN не настроены"}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp1 = await client.post(
                f"{GRAPH_API_BASE}/{META_INSTAGRAM_ID}/media",
                data={
                    "video_url": video_url,
                    "caption": caption,
                    "media_type": "REELS",
                    "access_token": META_PAGE_TOKEN,
                },
            )
            data1 = resp1.json()
            if "error" in data1:
                logger.error(f"Instagram video container error: {data1['error']}")
                return {"success": False, "error": data1["error"].get("message", str(data1["error"]))}
            container_id = data1.get("id", "")

            # Видео требует времени на обработку — ждём готовности контейнера
            import asyncio as _asyncio
            for _ in range(15):  # до ~45 секунд ожидания
                status_resp = await client.get(
                    f"{GRAPH_API_BASE}/{container_id}",
                    params={"fields": "status_code", "access_token": META_PAGE_TOKEN},
                )
                status_data = status_resp.json()
                if status_data.get("status_code") == "FINISHED":
                    break
                await _asyncio.sleep(3)

            resp2 = await client.post(
                f"{GRAPH_API_BASE}/{META_INSTAGRAM_ID}/media_publish",
                data={"creation_id": container_id, "access_token": META_PAGE_TOKEN},
            )
            data2 = resp2.json()
            if "error" in data2:
                logger.error(f"Instagram video publish error: {data2['error']}")
                return {"success": False, "error": data2["error"].get("message", str(data2["error"]))}
            return {"success": True, "post_id": data2.get("id", ""), "result": data2}
    except Exception as e:
        logger.error(f"Instagram video post exception: {e}")
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
