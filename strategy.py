"""
BCHD Marketer — Стратегическая память и планирование
ПЕРЕСТРОЙКА v1

Этот модуль решает главную проблему агента: отсутствие памяти о принятых
решениях. Без него бот каждый раз предлагает одно и то же, не знает что
уже сделано, и не видит картину шире одного запроса.

Что хранится в Postgres:
- decision_log: история всех принятых/отклонённых решений
- weekly_strategy: текущий стратегический план на неделю
- keyword_history: история изменений ставок и статусов ключей
- performance_snapshots: ежедневные снимки метрик для трендов
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz

log = logging.getLogger(__name__)
NY_TZ = pytz.timezone("America/New_York")


class StrategyMemory:
    """
    Долгосрочная память агента — хранится в Postgres, выживает после редеплоя.
    Позволяет боту:
    1. Помнить что уже сделано → не дублировать действия
    2. Помнить что отклонено → не предлагать снова
    3. Знать когда последний раз менялась ставка/статус ключа
    4. Строить стратегический план на неделю
    """

    def __init__(self):
        self._pool = None

    def set_pool(self, pool):
        self._pool = pool

    async def ensure_tables(self):
        """Создаёт таблицы если их нет."""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_log (
                    id SERIAL PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    decision TEXT NOT NULL,  -- approved / rejected / skipped
                    details JSONB DEFAULT '{}',
                    reason TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS keyword_history (
                    id SERIAL PRIMARY KEY,
                    keyword TEXT NOT NULL,
                    resource_name TEXT DEFAULT '',
                    change_type TEXT NOT NULL,  -- bid_change / status_change / landing_change
                    old_value TEXT DEFAULT '',
                    new_value TEXT NOT NULL,
                    applied_at TIMESTAMPTZ DEFAULT now(),
                    verified BOOLEAN DEFAULT FALSE
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS weekly_strategy (
                    id SERIAL PRIMARY KEY,
                    week_start DATE NOT NULL UNIQUE,
                    goals JSONB DEFAULT '[]',
                    priorities JSONB DEFAULT '[]',
                    completed JSONB DEFAULT '[]',
                    notes TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS performance_snapshots (
                    id SERIAL PRIMARY KEY,
                    snapshot_date DATE NOT NULL UNIQUE,
                    impressions INTEGER DEFAULT 0,
                    clicks INTEGER DEFAULT 0,
                    conversions FLOAT DEFAULT 0,
                    cost FLOAT DEFAULT 0,
                    impression_share FLOAT DEFAULT 0,
                    top_keywords JSONB DEFAULT '[]',
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            log.info("strategy: таблицы проверены/созданы")

    # ── DECISION LOG ─────────────────────────────────────────────────────────

    async def log_decision(
        self,
        action_type: str,
        target: str,
        decision: str,
        details: dict = None,
        reason: str = "",
    ) -> None:
        """Записывает решение в лог."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO decision_log (action_type, target, decision, details, reason)
                       VALUES ($1, $2, $3, $4, $5)""",
                    action_type, target, decision,
                    json.dumps(details or {}), reason
                )
        except Exception as e:
            log.error(f"strategy.log_decision error: {e}")

    async def was_recently_rejected(
        self, action_type: str, target: str, days: int = 7
    ) -> bool:
        """Возвращает True если такое действие было отклонено за последние N дней."""
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT id FROM decision_log
                       WHERE action_type = $1 AND target = $2 AND decision = 'rejected'
                       AND created_at > now() - interval '1 day' * $3
                       LIMIT 1""",
                    action_type, target, days
                )
            return row is not None
        except Exception as e:
            log.error(f"strategy.was_recently_rejected error: {e}")
            return False

    async def was_recently_applied(
        self, action_type: str, target: str, hours: int = 48
    ) -> Optional[dict]:
        """Возвращает последнее применённое действие этого типа для target, если оно было."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT details, created_at FROM decision_log
                       WHERE action_type = $1 AND target = $2 AND decision = 'approved'
                       AND created_at > now() - interval '1 hour' * $3
                       ORDER BY created_at DESC LIMIT 1""",
                    action_type, target, hours
                )
            if row:
                return {
                    "details": json.loads(row["details"]),
                    "applied_at": row["created_at"].isoformat(),
                }
            return None
        except Exception as e:
            log.error(f"strategy.was_recently_applied error: {e}")
            return None

    async def get_recent_decisions(self, days: int = 7, limit: int = 50) -> list:
        """Возвращает последние решения для контекста агента."""
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT action_type, target, decision, details, reason, created_at
                       FROM decision_log
                       WHERE created_at > now() - interval '1 day' * $1
                       ORDER BY created_at DESC LIMIT $2""",
                    days, limit
                )
            return [
                {
                    "action_type": r["action_type"],
                    "target": r["target"],
                    "decision": r["decision"],
                    "details": json.loads(r["details"]),
                    "reason": r["reason"],
                    "when": r["created_at"].strftime("%d.%m %H:%M"),
                }
                for r in rows
            ]
        except Exception as e:
            log.error(f"strategy.get_recent_decisions error: {e}")
            return []

    # ── KEYWORD HISTORY ───────────────────────────────────────────────────────

    async def log_keyword_change(
        self,
        keyword: str,
        change_type: str,
        old_value: str,
        new_value: str,
        resource_name: str = "",
        verified: bool = False,
    ) -> None:
        """Записывает изменение ставки/статуса/лендинга ключа."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO keyword_history
                       (keyword, resource_name, change_type, old_value, new_value, verified)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    keyword, resource_name, change_type, old_value, new_value, verified
                )
        except Exception as e:
            log.error(f"strategy.log_keyword_change error: {e}")

    async def get_keyword_history(self, keyword: str, days: int = 30) -> list:
        """Возвращает историю изменений для конкретного ключа."""
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT change_type, old_value, new_value, applied_at, verified
                       FROM keyword_history
                       WHERE keyword ILIKE $1
                       AND applied_at > now() - interval '1 day' * $2
                       ORDER BY applied_at DESC""",
                    f"%{keyword}%", days
                )
            return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"strategy.get_keyword_history error: {e}")
            return []

    async def get_all_recent_keyword_changes(self, days: int = 7) -> list:
        """Все изменения ключей за последние N дней — для контекста агента."""
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT keyword, change_type, old_value, new_value, applied_at, verified
                       FROM keyword_history
                       WHERE applied_at > now() - interval '1 day' * $1
                       ORDER BY applied_at DESC LIMIT 100""",
                    days
                )
            return [
                {
                    "keyword": r["keyword"],
                    "change": r["change_type"],
                    "from": r["old_value"],
                    "to": r["new_value"],
                    "when": r["applied_at"].strftime("%d.%m %H:%M"),
                    "verified": r["verified"],
                }
                for r in rows
            ]
        except Exception as e:
            log.error(f"strategy.get_all_recent_keyword_changes error: {e}")
            return []

    # ── WEEKLY STRATEGY ───────────────────────────────────────────────────────

    async def get_current_week_strategy(self) -> Optional[dict]:
        """Возвращает стратегический план на текущую неделю."""
        if not self._pool:
            return None
        try:
            today = datetime.now(NY_TZ).date()
            week_start = today - timedelta(days=today.weekday())
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM weekly_strategy WHERE week_start = $1",
                    week_start
                )
            if row:
                return {
                    "week_start": str(row["week_start"]),
                    "goals": json.loads(row["goals"]),
                    "priorities": json.loads(row["priorities"]),
                    "completed": json.loads(row["completed"]),
                    "notes": row["notes"],
                }
            return None
        except Exception as e:
            log.error(f"strategy.get_current_week_strategy error: {e}")
            return None

    async def save_week_strategy(
        self,
        goals: list,
        priorities: list,
        notes: str = "",
    ) -> None:
        """Сохраняет или обновляет план на текущую неделю."""
        if not self._pool:
            return
        try:
            today = datetime.now(NY_TZ).date()
            week_start = today - timedelta(days=today.weekday())
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO weekly_strategy (week_start, goals, priorities, notes)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (week_start) DO UPDATE
                       SET goals=$2, priorities=$3, notes=$4, updated_at=now()""",
                    week_start,
                    json.dumps(goals),
                    json.dumps(priorities),
                    notes,
                )
        except Exception as e:
            log.error(f"strategy.save_week_strategy error: {e}")

    async def mark_goal_complete(self, goal: str) -> None:
        """Помечает цель как выполненную."""
        if not self._pool:
            return
        try:
            today = datetime.now(NY_TZ).date()
            week_start = today - timedelta(days=today.weekday())
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT completed FROM weekly_strategy WHERE week_start = $1",
                    week_start
                )
                if row:
                    completed = json.loads(row["completed"])
                    if goal not in completed:
                        completed.append(goal)
                    await conn.execute(
                        "UPDATE weekly_strategy SET completed=$1, updated_at=now() WHERE week_start=$2",
                        json.dumps(completed), week_start
                    )
        except Exception as e:
            log.error(f"strategy.mark_goal_complete error: {e}")

    # ── PERFORMANCE SNAPSHOTS ─────────────────────────────────────────────────

    async def save_daily_snapshot(self, metrics: dict) -> None:
        """Сохраняет ежедневный снимок метрик."""
        if not self._pool:
            return
        try:
            today = datetime.now(NY_TZ).date()
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO performance_snapshots
                       (snapshot_date, impressions, clicks, conversions, cost,
                        impression_share, top_keywords)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (snapshot_date) DO UPDATE
                       SET impressions=$2, clicks=$3, conversions=$4, cost=$5,
                           impression_share=$6, top_keywords=$7""",
                    today,
                    metrics.get("impressions", 0),
                    metrics.get("clicks", 0),
                    metrics.get("conversions", 0),
                    metrics.get("cost", 0),
                    metrics.get("impression_share", 0),
                    json.dumps(metrics.get("top_keywords", [])),
                )
        except Exception as e:
            log.error(f"strategy.save_daily_snapshot error: {e}")

    async def get_trend(self, days: int = 7) -> dict:
        """Возвращает тренд метрик за последние N дней."""
        if not self._pool:
            return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT snapshot_date, impressions, clicks, conversions, cost, impression_share
                       FROM performance_snapshots
                       WHERE snapshot_date > now() - interval '1 day' * $1
                       ORDER BY snapshot_date DESC""",
                    days
                )
            if len(rows) < 2:
                return {"insufficient_data": True}

            latest = rows[0]
            oldest = rows[-1]

            def pct_change(new, old):
                if not old:
                    return 0
                return round((new - old) / old * 100, 1)

            return {
                "days": len(rows),
                "conversions_trend": pct_change(latest["conversions"], oldest["conversions"]),
                "cost_trend": pct_change(latest["cost"], oldest["cost"]),
                "ctr_trend": pct_change(
                    latest["clicks"] / max(latest["impressions"], 1),
                    oldest["clicks"] / max(oldest["impressions"], 1),
                ),
                "is_trend": pct_change(latest["impression_share"], oldest["impression_share"]),
                "latest_cost": float(latest["cost"]),
                "latest_conversions": float(latest["conversions"]),
                "snapshots": [
                    {
                        "date": str(r["snapshot_date"]),
                        "conv": float(r["conversions"]),
                        "cost": float(r["cost"]),
                        "is": float(r["impression_share"]),
                    }
                    for r in rows
                ],
            }
        except Exception as e:
            log.error(f"strategy.get_trend error: {e}")
            return {}

    # ── CONTEXT BUILDER ───────────────────────────────────────────────────────

    async def build_context_for_agent(self) -> dict:
        """
        Собирает весь контекст памяти для передачи агенту.
        Вызывается перед каждым запросом чтобы агент знал:
        - что было сделано недавно
        - что было отклонено
        - текущий стратегический план
        - тренды метрик
        """
        recent_decisions = await self.get_recent_decisions(days=7)
        recent_kw_changes = await self.get_all_recent_keyword_changes(days=7)
        week_strategy = await self.get_current_week_strategy()
        trend = await self.get_trend(days=7)

        return {
            "recent_decisions": recent_decisions,
            "recent_keyword_changes": recent_kw_changes,
            "week_strategy": week_strategy,
            "performance_trend": trend,
        }


strategy = StrategyMemory()
