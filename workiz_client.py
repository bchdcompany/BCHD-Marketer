"""
Workiz API клиент — ПЕРЕСТРОЙКА v2
Полная ROAS атрибуция: реальная выручка по каналам для агента-маркетолога.

Что умеет:
- get_roas_report() — ROAS по каждому рекламному каналу (Google, LSA, Thumbtack и т.д.)
- get_jobs_by_source() — джобы по источнику с финансовой сводкой
- get_revenue_trend() — тренд выручки по дням
- get_top_services() — топ услуг по выручке (что приносит больше денег)
- find_job_by_phone() — поиск джоба по телефону (для LSA верификации)

JobSource значения в Workiz (уточни реальные через /job/all/):
- "Google" или "Google Ads" — Google Search
- "LSA" или "Local Services" — Local Services Ads
- "Thumbtack" — Thumbtack
- "Yelp" — Yelp (когда подключим)
- "Website" — органический сайт
- "Referral" — рекомендация

Docs: https://developer.workiz.com
"""

import os
import re
import logging
from datetime import datetime, timedelta
import aiohttp
import pytz

log = logging.getLogger(__name__)
NY_TZ = pytz.timezone("America/New_York")

WORKIZ_API_TOKEN = os.environ.get("WORKIZ_API_TOKEN", "")
WORKIZ_API_SECRET = os.environ.get("WORKIZ_API_SECRET", "")
BASE_URL = f"https://api.workiz.com/api/v1/{WORKIZ_API_TOKEN}"

# Маппинг источников → рекламные каналы
# Значения должны совпадать с Ad Groups в Workiz
SOURCE_CHANNEL_MAP = {
    "google": "Google",
    "google ads": "Google",
    "google adwords": "Google",
    "lsa": "LSA",
    "local services ads": "LSA",
    "local services": "LSA",
    "local service ads": "LSA",
    "thumbtack": "Thumbtack",
    "yelp": "Yelp",
    "facebook": "Facebook/Insta",
    "facebook/insta": "Facebook/Insta",
    "instagram": "Facebook/Insta",
    "home advisor": "Home Advisor",
    "homeadvisor": "Home Advisor",
    "angi": "Home Advisor",
    "website": "Website",
    "web": "Website",
    "referral from others": "Referral from others",
    "referral": "Referral from others",
    "customer return": "Customer return",
    "repeat": "Customer return",
}


def _normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


async def _get(endpoint: str, params: dict = None) -> dict:
    if not WORKIZ_API_TOKEN or not WORKIZ_API_SECRET:
        return {"error": "WORKIZ_API_TOKEN/WORKIZ_API_SECRET не настроены"}
    url = f"{BASE_URL}/{endpoint}"
    headers = {"Authorization": f"Bearer {WORKIZ_API_SECRET}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, params=params or {},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    log.error(f"Workiz API error {resp.status}: {data}")
                return data
    except Exception as e:
        log.error(f"Workiz API GET {endpoint}: {e}")
        return {"error": str(e)}


async def _get_all_jobs(date_from: str, date_to: str, records: int = 200) -> list:
    """Загружает все джобы за период с пагинацией."""
    all_jobs = []
    offset = 0
    while True:
        params = {
            "start_date": date_from,
            "records": min(records, 100),
            "offset": offset,
        }
        result = await _get("job/all/", params)
        if "error" in result:
            log.error(f"_get_all_jobs error: {result['error']}")
            break

        jobs = result.get("data", [])
        if isinstance(jobs, dict):
            jobs = jobs.get("data", [])
        if not isinstance(jobs, list):
            break

        # Фильтруем по верхней границе даты
        for job in jobs:
            created = (job.get("CreatedDate") or job.get("JobDateTime") or "")[:10]
            if not created or created <= date_to:
                all_jobs.append(job)

        if len(jobs) < 100:
            break  # последняя страница
        offset += 100

    return all_jobs


def _job_financials(job: dict) -> dict:
    """Извлекает финансовые данные из джоба."""
    total = float(job.get("JobTotalPrice") or job.get("TotalPrice") or 0)
    due = float(job.get("JobAmountDue") or job.get("AmountDue") or 0)
    collected = total - due
    return {
        "total": total,
        "collected": collected,
        "due": due,
    }


def _normalize_source(source: str) -> str:
    """Нормализует название источника."""
    if not source:
        return "Unknown"
    normalized = SOURCE_CHANNEL_MAP.get(source.lower().strip())
    return normalized or source.strip()


async def get_all_jobs_with_financials(
    date_from: str, date_to: str
) -> dict:
    """
    Загружает все джобы за период и возвращает их с финансовыми данными.
    Основа для всех ROAS расчётов.
    """
    jobs = await _get_all_jobs(date_from, date_to)
    enriched = []
    for job in jobs:
        fin = _job_financials(job)
        source = job.get("JobSource") or job.get("Source") or "Unknown"
        enriched.append({
            "uuid": job.get("UUID"),
            "serial_id": job.get("SerialId"),
            "status": job.get("Status", "Unknown"),
            "source": _normalize_source(source),
            "source_raw": source,
            "created_date": (job.get("CreatedDate") or "")[:10],
            "service_type": job.get("JobType") or job.get("ServiceType") or "",
            "total": fin["total"],
            "collected": fin["collected"],
            "due": fin["due"],
            "client_phone": _normalize_phone(
                job.get("Phone") or job.get("ClientPhone") or ""
            ),
        })
    return {"jobs": enriched, "total": len(enriched), "date_from": date_from, "date_to": date_to}


async def get_roas_report(
    date_from: str,
    date_to: str,
    ad_spend: dict = None,
) -> dict:
    """
    ГЛАВНЫЙ МЕТОД — ROAS отчёт по каналам.

    ad_spend: словарь расходов по каналам из Google Ads API.
    Формат: {"Google Ads": 860.14, "LSA": 605.52, "Thumbtack": 200.0}

    Возвращает ROAS (Return on Ad Spend) для каждого канала:
    ROAS = Revenue / Ad Spend
    CPA = Ad Spend / Jobs Count
    """
    result = await get_all_jobs_with_financials(date_from, date_to)
    jobs = result.get("jobs", [])
    ad_spend = ad_spend or {}

    # Группируем по каналу
    channels = {}
    for job in jobs:
        ch = job["source"]
        if ch not in channels:
            channels[ch] = {
                "channel": ch,
                "jobs_count": 0,
                "completed_count": 0,
                "total_revenue": 0.0,
                "collected": 0.0,
                "outstanding": 0.0,
                "jobs": [],
            }
        channels[ch]["jobs_count"] += 1
        channels[ch]["total_revenue"] += job["total"]
        channels[ch]["collected"] += job["collected"]
        channels[ch]["outstanding"] += job["due"]
        if job["status"] in ("Completed", "Done", "Closed", "Invoiced"):
            channels[ch]["completed_count"] += 1

    # Добавляем ROAS метрики
    report = []
    for ch, data in channels.items():
        spend = ad_spend.get(ch, 0)
        revenue = data["total_revenue"]
        jobs_count = data["jobs_count"]
        roas = round(revenue / spend, 2) if spend > 0 else None
        cpa = round(spend / jobs_count, 2) if jobs_count > 0 and spend > 0 else None
        avg_ticket = round(revenue / jobs_count, 2) if jobs_count > 0 else 0

        report.append({
            **data,
            "ad_spend": spend,
            "roas": roas,
            "cpa_real": cpa,
            "avg_ticket": avg_ticket,
            "profit_gross": round(revenue - spend, 2) if spend > 0 else revenue,
        })

    # Сортируем по выручке
    report.sort(key=lambda x: x["total_revenue"], reverse=True)

    # Итого
    total_revenue = sum(d["total_revenue"] for d in report)
    total_spend = sum(ad_spend.values())
    total_jobs = sum(d["jobs_count"] for d in report)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "channels": report,
        "summary": {
            "total_revenue": round(total_revenue, 2),
            "total_spend": round(total_spend, 2),
            "total_jobs": total_jobs,
            "overall_roas": round(total_revenue / total_spend, 2) if total_spend > 0 else None,
            "avg_ticket": round(total_revenue / total_jobs, 2) if total_jobs > 0 else 0,
        },
    }


async def get_revenue_trend(date_from: str, date_to: str) -> dict:
    """Тренд выручки по дням — для графика и аномалий."""
    result = await get_all_jobs_with_financials(date_from, date_to)
    jobs = result.get("jobs", [])

    by_date = {}
    for job in jobs:
        d = job["created_date"]
        if not d:
            continue
        if d not in by_date:
            by_date[d] = {"date": d, "jobs": 0, "revenue": 0.0, "collected": 0.0}
        by_date[d]["jobs"] += 1
        by_date[d]["revenue"] += job["total"]
        by_date[d]["collected"] += job["collected"]

    trend = sorted(by_date.values(), key=lambda x: x["date"])

    # Аномалии: дни с 0 джобов
    if trend:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")
        all_dates = {(start + timedelta(days=i)).strftime("%Y-%m-%d")
                     for i in range((end - start).days + 1)}
        dates_with_jobs = {d["date"] for d in trend}
        zero_days = sorted(all_dates - dates_with_jobs)
    else:
        zero_days = []

    return {
        "trend": trend,
        "zero_days": zero_days,
        "total_days": len(trend),
        "avg_daily_revenue": round(
            sum(d["revenue"] for d in trend) / len(trend), 2
        ) if trend else 0,
    }


async def get_top_services(date_from: str, date_to: str, limit: int = 10) -> dict:
    """
    Топ услуг по выручке.
    Показывает что реально приносит деньги — важно для стратегии ключей.
    """
    result = await get_all_jobs_with_financials(date_from, date_to)
    jobs = result.get("jobs", [])

    by_service = {}
    for job in jobs:
        svc = job.get("service_type") or "Unknown"
        if not svc:
            svc = "Unknown"
        if svc not in by_service:
            by_service[svc] = {"service": svc, "jobs": 0, "revenue": 0.0}
        by_service[svc]["jobs"] += 1
        by_service[svc]["revenue"] += job["total"]

    top = sorted(by_service.values(), key=lambda x: x["revenue"], reverse=True)[:limit]
    for item in top:
        item["avg_ticket"] = round(item["revenue"] / item["jobs"], 2) if item["jobs"] > 0 else 0
        item["revenue"] = round(item["revenue"], 2)

    return {"top_services": top, "date_from": date_from, "date_to": date_to}


async def get_jobs_by_source(
    source: str, date_from: str, date_to: str, records: int = 100
) -> dict:
    """Джобы по конкретному источнику с финансовой сводкой."""
    result = await get_all_jobs_with_financials(date_from, date_to)
    all_jobs = result.get("jobs", [])

    source_norm = _normalize_source(source)
    source_jobs = [
        j for j in all_jobs
        if j["source"] == source_norm or j["source_raw"] == source
    ]

    total_revenue = sum(j["total"] for j in source_jobs)
    total_collected = sum(j["collected"] for j in source_jobs)
    total_due = sum(j["due"] for j in source_jobs)
    completed = [j for j in source_jobs if j["status"] in ("Completed", "Done", "Closed")]

    by_status = {}
    for j in source_jobs:
        s = j["status"]
        if s not in by_status:
            by_status[s] = {"count": 0, "revenue": 0.0}
        by_status[s]["count"] += 1
        by_status[s]["revenue"] += j["total"]

    return {
        "source": source,
        "date_from": date_from,
        "date_to": date_to,
        "total_jobs": len(source_jobs),
        "total_revenue": round(total_revenue, 2),
        "total_collected": round(total_collected, 2),
        "total_due": round(total_due, 2),
        "completed_jobs": len(completed),
        "completed_revenue": round(sum(j["total"] for j in completed), 2),
        "avg_ticket": round(total_revenue / len(source_jobs), 2) if source_jobs else 0,
        "by_status": by_status,
        "jobs": source_jobs[:50],
    }


async def find_job_by_phone(phone: str, date_from: str, date_to: str) -> dict:
    """Ищет джоб по номеру телефона."""
    target = _normalize_phone(phone)
    if not target:
        return {"found": False, "error": "Пустой телефон"}

    result = await get_all_jobs_with_financials(date_from, date_to)
    matches = [j for j in result.get("jobs", []) if j["client_phone"] == target]

    return {
        "found": len(matches) > 0,
        "jobs": matches,
        "total_scanned": result.get("total", 0),
    }


async def get_outstanding_debts(threshold_days: int = 14) -> dict:
    """
    Джобы с неоплаченным остатком старше N дней.
    Для интеграции с bchd-agent (напоминания о долгах).
    """
    today = datetime.now(NY_TZ).date()
    date_from = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    result = await get_all_jobs_with_financials(date_from, date_to)
    jobs = result.get("jobs", [])

    debts = []
    for job in jobs:
        if job["due"] <= 0:
            continue
        created = job.get("created_date")
        if not created:
            continue
        age_days = (today - datetime.strptime(created, "%Y-%m-%d").date()).days
        if age_days >= threshold_days:
            debts.append({**job, "age_days": age_days})

    debts.sort(key=lambda x: x["due"], reverse=True)
    total_outstanding = sum(d["due"] for d in debts)

    return {
        "debts": debts,
        "count": len(debts),
        "total_outstanding": round(total_outstanding, 2),
        "threshold_days": threshold_days,
    }
