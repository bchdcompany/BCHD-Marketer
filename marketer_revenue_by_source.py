"""
BCHD-Marketer — Revenue by Lead Source (для расчёта ROAS)

Даёт Маркетологу реальную выручку по источникам лидов (Google Ads, LSA,
Thumbtack и т.д.) — используется в утреннем/вечернем отчёте и в /roas.

ПЕРЕПИСАНО 29.09.2026 (по факту найденного бага: утренний отчёт и /roas
показывали РАЗНЫЕ цифры "собрано" за один и тот же период).

Было: этот модуль ходил в отдельный Workiz V2 API (app.workiz.com/crm/api/v2)
и определял источник лида по полю adGroup самого джоба (_get_ad_group_name).
Оказалось, что у реальных джобов в этом аккаунте поля adGroup просто нет —
Workiz различает источники через простое поле JobSource ("Google", "LSA",
"Thumbtack", "Referral from others", "Customer return" и т.п.), которое
возвращает V1 API (api.workiz.com/api/v1, job/all). Именно JobSource уже
использует workiz_client.py (и на нём построен /roas), поэтому два отчёта
расходились в числах.

Стало: этот модуль больше не делает собственных HTTP-запросов к Workiz —
он переиспользует workiz_client.get_jobs_by_source() (тот же клиент, что и
у /roas, с уже исправленной пагинацией — см. workiz_client.py, 28.09.2026).
Так утренний/вечерний отчёт и /roas теперь считают ОДИНАКОВО и не могут
разойтись.

Компромисс: "collected_revenue" теперь считается как и в /roas — по джобам,
СОЗДАННЫМ в периоде (JobTotalPrice - JobAmountDue на момент запроса), а не
по инвойсам, ОБНОВЛЁННЫМ в периоде (как было раньше в V2-версии). Это может
несколько отличаться для джобов, оплаченных позже создания — так же, как в
/roas. Если это станет проблемой — вернуться к идее считать по дате оплаты,
но тогда сначала нужно завести такую же логику и в workiz_client.py, чтобы
оба отчёта по-прежнему совпадали.
"""

import logging

import workiz_client

logger = logging.getLogger(__name__)

# Метки под ключи, которые ожидают вызывающие (bot.py): "Google Ads",
# "Google LSA", "Thumbtack" читаются по имени напрямую. Остальные — для
# полноты картины (используются, например, в /roas отдельными блоками).
SOURCE_MAP = {
    "Google": "Google Ads",
    "LSA": "Google LSA",
    "Thumbtack": "Thumbtack",
    "Referral from others": "Рекомендация",
    "Customer return": "Повторный клиент",
}


async def get_revenue_by_source(start_date: str, end_date: str) -> dict:
    """
    Главная функция для Маркетолога: реальная выручка по источникам лидов
    за период — то, что нужно чтобы честно посчитать ROAS (сравнить с
    расходом на рекламу за тот же период по тому же источнику).

    Возвращает dict: {source_label: {"jobs_count": N, "collected_revenue": X,
    "revenue": X, "due": X}} плюс "_period" с датами для контекста.

    ВАЖНО: считает по JobSource и по джобам, СОЗДАННЫМ в периоде — тем же
    способом, что и /roas (workiz_client.get_jobs_by_source). Если тут и
    в /roas когда-нибудь снова разойдутся цифры — значит кто-то поменял
    только один из двух мест, ищи баг там.
    """
    result: dict = {}
    for job_source, label in SOURCE_MAP.items():
        try:
            data = await workiz_client.get_jobs_by_source(job_source, start_date, end_date)
        except Exception as e:
            logger.error(f"get_revenue_by_source: ошибка по источнику {job_source}: {e}")
            continue
        if isinstance(data, dict) and data.get("error"):
            logger.error(f"get_revenue_by_source: Workiz API error по источнику {job_source}: {data.get('error')}")
            continue
        result[label] = {
            "jobs_count": data.get("total_jobs", 0),
            "collected_revenue": data.get("total_collected", 0),
            "revenue": data.get("total_revenue", 0),
            "due": data.get("total_due", 0),
        }

    result["_period"] = {"start": start_date, "end": end_date}
    result["_note"] = (
        "jobs_count/collected_revenue считаются по JobSource и по джобам, "
        "СОЗДАННЫМ в периоде — тем же способом, что и /roas "
        "(workiz_client.get_jobs_by_source). Сравнивай collected_revenue "
        "конкретного источника с расходом на этот же источник за тот же период."
    )
    return result
