"""
BCHD-Marketer — Revenue by Lead Source (для расчёта ROAS)

Даёт Маркетологу прямой доступ к реальной выручке по источникам лидов
(Google Ads, LSA, Thumbtack и т.д.) напрямую из Workiz V2 — без обращения
к bchd-agent в реальном времени.

АРХИТЕКТУРНОЕ РЕШЕНИЕ 06.09.2026 (согласовано с Chingis):
- bchd-agent (Техагент) — единственный источник правды для ОБЩЕЙ финансовой
  картины бизнеса (все джобы, все долги, зарплаты техников) — теснее
  интегрирован с операционкой.
- Marketer считает ТОЛЬКО маркетинговую эффективность: расход на
  Ads/LSA/Thumbtack vs выручка от джобов ИМЕННО ЭТИХ источников (ROAS).
- Чтобы оба агента никогда не расходились в цифрах — этот модуль
  переиспользует ТОЧНО ТУ ЖЕ логику нормализации источников (_normalize_source),
  что уже проверена и используется в bchd-agent/weekly_report.py. Если
  бренд/название кампании в Workiz называется как-то нестандартно —
  правь ОБА файла синхронно (weekly_report.py и этот), иначе агенты
  начнут показывать разные числа для одного и того же периода.

Использует тот же WORKIZ_V2_TOKEN, что и bchd-agent (токен уровня аккаунта
Workiz, не привязан к конкретному боту).
"""

import os
import logging
import aiohttp
from datetime import datetime
from typing import Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

WORKIZ_V2_BASE_URL = "https://app.workiz.com/crm/api/v2"
WORKIZ_V2_TOKEN = os.environ.get("WORKIZ_V2_TOKEN", "")

# СИНХРОНИЗИРОВАНО с bchd-agent/weekly_report.py — держи одинаковым!
SOURCE_LABELS = {
    "google": "Google Ads", "lsa": "Google LSA",
    "thumbtack": "Thumbtack", "yelp": "Yelp",
    "referral": "Рекомендация", "repeat": "Повторный клиент",
    "customer return": "Повторный клиент", "website": "Сайт",
    "phone": "Входящий звонок", "lessen": "Lessen",
    "servicechannel": "ServiceChannel", "word of mouth": "Рекомендация",
}


def _normalize_source(raw: str) -> str:
    """СИНХРОНИЗИРОВАНО с bchd-agent/weekly_report.py — держи одинаковым!"""
    if not raw:
        return "Не указан"
    key = raw.lower().strip()
    for k, label in SOURCE_LABELS.items():
        if k in key:
            return label
    return raw.strip().title()


def _get_ad_group_name(job: dict) -> str:
    ad_group = job.get("adGroup") or job.get("AdGroup") or {}
    if isinstance(ad_group, dict):
        return ad_group.get("name", "") or ""
    return ""


def _safe_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WORKIZ_V2_TOKEN}",
        "Content-Type": "application/json",
    }


async def _get_all_jobs_between(start_date: str, end_date: str, date_property: str = "created") -> list:
    """Постранично получить ВСЕ заявки за период через официальный V2 API."""
    all_jobs = []
    page = 1
    while True:
        url = f"{WORKIZ_V2_BASE_URL}/jobs"
        params = {
            "pageSize": 100,
            "page": page,
            "dateProperty": date_property,
            "dateOperator": "between",
            "date": f"{start_date}_{end_date}",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=_headers(), params=params) as resp:
                    result = await resp.json()
        except Exception as e:
            logger.error(f"_get_all_jobs_between error на странице {page}: {e}")
            break
        if "error" in result:
            logger.error(f"_get_all_jobs_between API error: {result.get('error')}")
            break
        data = result.get("data", [])
        all_jobs.extend(data)
        if not result.get("hasMore") or not data:
            break
        page += 1
        if page > 30:  # защита от бесконечного цикла
            break
    return all_jobs


async def _get_all_invoices_between(start_date: str, end_date: str, date_property: str = "updated") -> list:
    """Постранично получить ВСЕ инвойсы за период через официальный V2 API."""
    all_invoices = []
    page = 1
    while True:
        url = f"{WORKIZ_V2_BASE_URL}/invoices"
        params = {
            "pageSize": 100,
            "page": page,
            "dateProperty": date_property,
            "dateOperator": "between",
            "date": f"{start_date}_{end_date}",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=_headers(), params=params) as resp:
                    result = await resp.json()
        except Exception as e:
            logger.error(f"_get_all_invoices_between error на странице {page}: {e}")
            break
        if "error" in result:
            logger.error(f"_get_all_invoices_between API error: {result.get('error')}")
            break
        data = result.get("data", [])
        all_invoices.extend(data)
        if not result.get("hasMore") or not data:
            break
        page += 1
        if page > 30:
            break
    return all_invoices


async def get_revenue_by_source(start_date: str, end_date: str) -> dict:
    """
    Главная функция для Маркетолога: реальная выручка по источникам лидов
    за период — то, что нужно чтобы честно посчитать ROAS (сравнить с
    расходом на рекламу за тот же период по тому же источнику).

    ВАЖНО про интерпретацию:
    - "jobs_count" — сколько заявок создано с этим источником за период
      (по dateProperty=created)
    - "collected_revenue" — реально СОБРАННЫЕ деньги (amountDue=0) по
      инвойсам, ОБНОВЛЁННЫМ за этот же период — это может включать оплаты
      за заявки, СОЗДАННЫЕ раньше периода (визит был позже) — так же, как
      это уже работает в bchd-agent, чтобы не терять реальные деньги.
    - Один источник может не совпадать 1:1 между "новых заявок" и
      "собранных денег" за один и тот же период — это нормально и
      ожидаемо, это НЕ ошибка расчёта.

    Возвращает dict: {source_name: {"jobs_count": N, "collected_revenue": X}}
    плюс "_period" с датами для контекста в ответе Маркетолога.
    """
    if not WORKIZ_V2_TOKEN:
        return {"error": "WORKIZ_V2_TOKEN не задан в переменных окружения"}

    jobs = await _get_all_jobs_between(start_date, end_date, date_property="created")
    invoices = await _get_all_invoices_between(start_date, end_date, date_property="updated")

    job_by_id = {j.get("id") or j.get("UUID"): j for j in jobs}

    by_source: dict = defaultdict(lambda: {"jobs_count": 0, "collected_revenue": 0.0})

    for j in jobs:
        source = _normalize_source(_get_ad_group_name(j))
        by_source[source]["jobs_count"] += 1

    for inv in invoices:
        try:
            is_paid = _safe_float(inv.get("amountDue")) == 0 and _safe_float(inv.get("totalPrice")) > 0
        except (ValueError, TypeError):
            is_paid = False
        if not is_paid:
            continue
        job = job_by_id.get(inv.get("jobId"))
        source = _normalize_source(_get_ad_group_name(job)) if job else "Не найдено (визит вне периода)"
        by_source[source]["collected_revenue"] += _safe_float(inv.get("totalPrice"))

    result = {k: v for k, v in by_source.items()}
    result["_period"] = {"start": start_date, "end": end_date}
    result["_note"] = (
        "jobs_count = заявки СОЗДАННЫЕ за период по источнику. "
        "collected_revenue = деньги РЕАЛЬНО СОБРАННЫЕ за период (может включать оплаты "
        "за заявки, созданные раньше) — источник правды для ROAS: сравнивай "
        "collected_revenue конкретного источника (Google Ads/LSA/Thumbtack) с расходом "
        "на этот же источник за тот же период."
    )
    return result
