"""
Workiz API клиент для агента-маркетолога (узкое использование).
Использует официальный Workiz Developer API — те же credentials,
что и у bchd-agent (WORKIZ_API_TOKEN / WORKIZ_API_SECRET), подключённые
через Railway variable reference, а не отдельный набор ключей.

Единственная задача этого модуля: сверять LSA-лиды с реальными джобами
в Workiz по номеру телефона клиента — чтобы понимать, стал ли лид,
за который выставлен счёт, реальным заказом, а не просто верить
метрикам самого Google.

Docs: https://developer.workiz.com
"""

import os
import re
import logging
import aiohttp

log = logging.getLogger(__name__)

WORKIZ_API_TOKEN = os.environ.get("WORKIZ_API_TOKEN", "")
WORKIZ_API_SECRET = os.environ.get("WORKIZ_API_SECRET", "")
BASE_URL = f"https://api.workiz.com/api/v1/{WORKIZ_API_TOKEN}"


def _normalize_phone(phone: str) -> str:
    """Оставляет только цифры, отбрасывает код страны '1', чтобы сравнивать
    номера в разных форматах ('+1 (917) 596-4063' vs '9175964063')."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


async def _get(endpoint: str, params: dict = None) -> dict:
    if not WORKIZ_API_TOKEN or not WORKIZ_API_SECRET:
        return {"error": "WORKIZ_API_TOKEN/WORKIZ_API_SECRET не настроены в переменных окружения"}
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
                    log.error(f"Workiz API GET error {resp.status}: {data}")
                return data
    except Exception as e:
        log.error(f"Workiz API GET {endpoint}: {e}")
        return {"error": str(e)}


async def get_jobs_by_date_range(date_from: str, date_to: str, records: int = 500) -> dict:
    """
    Получает список джобов начиная с date_from (Workiz API поддерживает
    только нижнюю границу через параметр start_date — верхнюю границу
    date_to обрезаем на своей стороне после получения ответа).
    """
    params = {
        "start_date": date_from,
        "records": records,
    }
    result = await _get("job/all/", params)
    if "error" in result:
        return result
    jobs = result.get("data", [])
    if isinstance(jobs, dict):
        jobs = jobs.get("data", [])

    # Обрезаем по верхней границе на своей стороне, т.к. API не принимает to_date
    filtered = []
    for job in jobs:
        created = job.get("CreatedDate") or job.get("JobDateTime") or ""
        created_date_only = created[:10] if created else ""
        if not created_date_only or created_date_only <= date_to:
            filtered.append(job)

    return {"jobs": filtered, "total": len(filtered), "total_before_filter": len(jobs)}


async def find_job_by_phone(phone: str, date_from: str, date_to: str) -> dict:
    """
    Ищет джоб(ы) в Workiz по номеру телефона клиента за указанный период.
    Официальный Workiz API не поддерживает прямой поиск по телефону —
    загружаем джобы за период и фильтруем на своей стороне (аналогично
    тому, как в bchd-agent реализован поиск по SerialId).

    Возвращает {'found': True/False, 'jobs': [...]}.

    NB: точное имя поля с телефоном клиента в ответе Workiz может
    отличаться (Phone / ClientPhone / PhoneNumber и т.п.) — проверяем
    несколько вероятных вариантов. Если ни один не совпадёт реально,
    нужно будет посмотреть сырой JSON одного джоба через Railway Console
    и уточнить имя поля, как мы делали для полей Google Ads API.
    """
    target = _normalize_phone(phone)
    if not target:
        return {'found': False, 'error': 'Пустой номер телефона для поиска'}

    result = await get_jobs_by_date_range(date_from, date_to)
    if "error" in result:
        return {'found': False, 'error': result['error']}

    matches = []
    for job in result.get("jobs", []):
        candidates = [
            job.get("Phone"), job.get("ClientPhone"), job.get("client_phone"),
            job.get("PhoneNumber"), job.get("MobilePhone"), job.get("PhoneNumber2"),
        ]
        for c in candidates:
            if c and _normalize_phone(c) == target:
                matches.append({
                    'uuid': job.get('UUID'),
                    'serial_id': job.get('SerialId'),
                    'status': job.get('Status'),
                    'total_price': job.get('JobTotalPrice'),
                    'amount_due': job.get('JobAmountDue'),
                    'created_date': job.get('CreatedDate') or job.get('JobDateTime'),
                })
                break

    return {'found': len(matches) > 0, 'jobs': matches, 'total_jobs_scanned': result.get('total', 0)}
