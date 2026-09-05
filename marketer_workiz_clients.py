"""
BCHD-Marketer — Workiz V2 Client Access

Даёт Маркетологу прямой доступ к списку клиентов Workiz (для email-рассылок),
без необходимости обращаться к bchd-agent в реальном времени.

Использует WORKIZ_V2_TOKEN — токен уровня аккаунта Workiz (112863),
можно использовать в обоих сервисах одновременно.
"""

import os
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

WORKIZ_V2_BASE_URL = "https://app.workiz.com/crm/api/v2"
WORKIZ_V2_TOKEN = os.environ.get("WORKIZ_V2_TOKEN", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WORKIZ_V2_TOKEN}",
        "Content-Type": "application/json",
    }


async def _get(endpoint: str, params: dict = None) -> dict:
    url = f"{WORKIZ_V2_BASE_URL}/{endpoint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), params=params or {}) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error(f"Workiz V2 GET error {resp.status}: {data}")
                return data
    except Exception as e:
        logger.error(f"Workiz V2 GET {endpoint}: {e}")
        return {"error": str(e)}


async def get_all_clients(
    page_size: int = 50,
    page: int = 1,
    search_query: Optional[str] = None,
) -> dict:
    """Список клиентов с пагинацией."""
    params = {"pageSize": page_size, "page": page}
    if search_query:
        params["searchQuery"] = search_query
    return await _get("clients", params)


async def get_all_clients_paginated(max_pages: int = 40, page_size: int = 100) -> list:
    """
    Постранично собрать ВСЕХ клиентов компании.
    При ~1500+ клиентах и page_size=100 это до 15-20 запросов.
    Задержка между запросами защищает от rate limit Workiz API.
    """
    import asyncio
    all_clients = []
    page = 1
    while page <= max_pages:
        result = await get_all_clients(page_size=page_size, page=page)
        if "error" in result:
            # Rate limit — ждём и повторяем эту же страницу один раз
            if "Rate limit" in str(result.get("error", "")) or "429" in str(result):
                logger.warning(f"Rate limit на странице {page} — жду 3 сек и повторяю")
                await asyncio.sleep(3)
                result = await get_all_clients(page_size=page_size, page=page)
                if "error" in result:
                    logger.error(f"Повторная попытка страницы {page} тоже failed: {result['error']}")
                    break
            else:
                logger.error(f"get_all_clients_paginated error на странице {page}: {result['error']}")
                break
        data = result.get("data", [])
        if not data:
            break
        all_clients.extend(data)
        if not result.get("hasMore"):
            break
        page += 1
        await asyncio.sleep(0.3)  # защита от rate limit между запросами
    logger.info(f"get_all_clients_paginated: собрано {len(all_clients)} клиентов за {page} страниц")
    return all_clients


async def get_client_emails_for_campaign() -> list:
    """
    Готовый список email-адресов для рассылки.
    Использовать перед каждой рассылкой — Workiz остаётся единственным
    источником правды.
    """
    if not WORKIZ_V2_TOKEN:
        logger.warning("WORKIZ_V2_TOKEN не настроен — email список недоступен")
        return []
    clients = await get_all_clients_paginated()
    result = []
    for c in clients:
        email = c.get("email")
        if not email or "@" not in email:
            continue
        result.append({
            "email": email,
            "first_name": c.get("firstName", ""),
            "last_name": c.get("lastName", ""),
            "full_name": c.get("fullName", ""),
            "client_id": c.get("entityId") or c.get("id"),
        })
    logger.info(f"get_client_emails_for_campaign: {len(result)} клиентов с email")
    return result
