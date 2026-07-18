"""
BCHD Marketer — Стратегическая память и планирование
ПЕРЕСТРОЙКА v2 — добавлены save_weekly_plan, get_weekly_plan_text
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz

log = logging.getLogger(__name__)
NY_TZ = pytz.timezone("America/New_York")


class StrategyMemory:
    def __init__(self):
        self._pool = None

    def set_pool(self, pool):
        self._pool = pool

    async def ensure_tables(self):
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_log (
                    id SERIAL PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    decision TEXT NOT NULL,
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
                    change_type TEXT NOT NULL,
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

    # ── DECISION LOG ──────────────────────────────────────────────────────────

    async def log_decision(self, action_type, target, decision, details=None, reason=""):
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO decision_log (action_type, target, decision, details, reason) VALUES ($1,$2,$3,$4,$5)",
                    action_type, target, decision, json.dumps(details or {}), reason
                )
        except Exception as e:
            log.error(f"strategy.log_decision: {e}")

    async def was_recently_rejected(self, action_type, target, days=7):
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM decision_log WHERE action_type=$1 AND target=$2 AND decision='rejected' AND created_at > now() - interval '1 day' * $3 LIMIT 1",
                    action_type, target, days
                )
            return row is not None
        except Exception as e:
            log.error(f"strategy.was_recently_rejected: {e}")
            return False

    async def was_recently_applied(self, action_type, target, hours=48):
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT details, created_at FROM decision_log WHERE action_type=$1 AND target=$2 AND decision='approved' AND created_at > now() - interval '1 hour' * $3 ORDER BY created_at DESC LIMIT 1",
                    action_type, target, hours
                )
            if row:
                return {"details": json.loads(row["details"]), "applied_at": row["created_at"].isoformat()}
            return None
        except Exception as e:
            log.error(f"strategy.was_recently_applied: {e}")
            return None

    async def get_recent_decisions(self, days=7, limit=50):
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT action_type, target, decision, details, reason, created_at FROM decision_log WHERE created_at > now() - interval '1 day' * $1 ORDER BY created_at DESC LIMIT $2",
                    days, limit
                )
            return [{"action_type": r["action_type"], "target": r["target"], "decision": r["decision"],
                     "details": json.loads(r["details"]), "reason": r["reason"],
                     "when": r["created_at"].strftime("%d.%m %H:%M")} for r in rows]
        except Exception as e:
            log.error(f"strategy.get_recent_decisions: {e}")
            return []

    # ── KEYWORD HISTORY ───────────────────────────────────────────────────────

    async def log_keyword_change(self, keyword, change_type, old_value, new_value, resource_name="", verified=False):
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO keyword_history (keyword, resource_name, change_type, old_value, new_value, verified) VALUES ($1,$2,$3,$4,$5,$6)",
                    keyword, resource_name, change_type, old_value, new_value, verified
                )
        except Exception as e:
            log.error(f"strategy.log_keyword_change: {e}")

    async def get_keyword_history(self, keyword, days=30):
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT change_type, old_value, new_value, applied_at, verified FROM keyword_history WHERE keyword ILIKE $1 AND applied_at > now() - interval '1 day' * $2 ORDER BY applied_at DESC",
                    f"%{keyword}%", days
                )
            return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"strategy.get_keyword_history: {e}")
            return []

    async def get_all_recent_keyword_changes(self, days=7):
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT keyword, change_type, old_value, new_value, applied_at, verified FROM keyword_history WHERE applied_at > now() - interval '1 day' * $1 ORDER BY applied_at DESC LIMIT 100",
                    days
                )
            return [{"keyword": r["keyword"], "change": r["change_type"], "from": r["old_value"],
                     "to": r["new_value"], "when": r["applied_at"].strftime("%d.%m %H:%M"),
                     "verified": r["verified"]} for r in rows]
        except Exception as e:
            log.error(f"strategy.get_all_recent_keyword_changes: {e}")
            return []

    # ── WEEKLY STRATEGY ───────────────────────────────────────────────────────

    async def get_current_week_strategy(self):
        if not self._pool:
            return None
        try:
            today = datetime.now(NY_TZ).date()
            week_start = today - timedelta(days=today.weekday())
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM weekly_strategy WHERE week_start=$1", week_start)
            if row:
                return {"week_start": str(row["week_start"]), "goals": json.loads(row["goals"]),
                        "priorities": json.loads(row["priorities"]), "completed": json.loads(row["completed"]),
                        "notes": row["notes"]}
            return None
        except Exception as e:
            log.error(f"strategy.get_current_week_strategy: {e}")
            return None

    async def save_week_strategy(self, goals, priorities, notes=""):
        if not self._pool:
            return
        try:
            today = datetime.now(NY_TZ).date()
            week_start = today - timedelta(days=today.weekday())
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO weekly_strategy (week_start, goals, priorities, notes) VALUES ($1,$2,$3,$4) ON CONFLICT (week_start) DO UPDATE SET goals=$2, priorities=$3, notes=$4, updated_at=now()",
                    week_start, json.dumps(goals), json.dumps(priorities), notes,
                )
        except Exception as e:
            log.error(f"strategy.save_week_strategy: {e}")

    async def save_weekly_plan(self, plan_text: str, week_from: str, week_to: str) -> None:
        """Сохраняет текстовый AI-план на неделю в поле notes."""
        if not self._pool:
            return
        try:
            from datetime import date as _date
            week_start = _date.fromisoformat(week_from)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO weekly_strategy (week_start, goals, priorities, notes) VALUES ($1,$2,$3,$4) ON CONFLICT (week_start) DO UPDATE SET notes=$4, updated_at=now()",
                    week_start, json.dumps([]), json.dumps([]), plan_text[:4000],
                )
            log.info(f"strategy: план на неделю {week_from} сохранён")
        except Exception as e:
            log.error(f"strategy.save_weekly_plan: {e}")

    async def get_weekly_plan_text(self) -> str:
        """Возвращает текстовый план на текущую неделю."""
        data = await self.get_current_week_strategy()
        if data and data.get("notes"):
            return data["notes"]
        return ""

    async def mark_goal_complete(self, goal):
        if not self._pool:
            return
        try:
            today = datetime.now(NY_TZ).date()
            week_start = today - timedelta(days=today.weekday())
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT completed FROM weekly_strategy WHERE week_start=$1", week_start)
                if row:
                    completed = json.loads(row["completed"])
                    if goal not in completed:
                        completed.append(goal)
                    await conn.execute("UPDATE weekly_strategy SET completed=$1, updated_at=now() WHERE week_start=$2", json.dumps(completed), week_start)
        except Exception as e:
            log.error(f"strategy.mark_goal_complete: {e}")

    # ── PERFORMANCE SNAPSHOTS ─────────────────────────────────────────────────

    async def save_daily_snapshot(self, metrics):
        if not self._pool:
            return
        try:
            today = datetime.now(NY_TZ).date()
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO performance_snapshots (snapshot_date, impressions, clicks, conversions, cost, impression_share, top_keywords) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (snapshot_date) DO UPDATE SET impressions=$2, clicks=$3, conversions=$4, cost=$5, impression_share=$6, top_keywords=$7",
                    today, metrics.get("impressions",0), metrics.get("clicks",0), metrics.get("conversions",0),
                    metrics.get("cost",0), metrics.get("impression_share",0), json.dumps(metrics.get("top_keywords",[]))
                )
        except Exception as e:
            log.error(f"strategy.save_daily_snapshot: {e}")

    async def get_trend(self, days=7):
        if not self._pool:
            return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT snapshot_date, impressions, clicks, conversions, cost, impression_share FROM performance_snapshots WHERE snapshot_date > now() - interval '1 day' * $1 ORDER BY snapshot_date DESC",
                    days
                )
            if len(rows) < 2:
                return {"insufficient_data": True}
            latest, oldest = rows[0], rows[-1]
            def pct(new, old):
                return round((new - old) / old * 100, 1) if old else 0
            return {
                "days": len(rows),
                "conversions_trend": pct(latest["conversions"], oldest["conversions"]),
                "cost_trend": pct(latest["cost"], oldest["cost"]),
                "is_trend": pct(latest["impression_share"], oldest["impression_share"]),
                "latest_cost": float(latest["cost"]),
                "latest_conversions": float(latest["conversions"]),
            }
        except Exception as e:
            log.error(f"strategy.get_trend: {e}")
            return {}

    # ── CONTEXT BUILDER ───────────────────────────────────────────────────────

    async def build_context_for_agent(self) -> dict:
        """
        Собирает весь контекст памяти для передачи агенту.
        Включает текущий недельный план — агент его видит и придерживается.
        """
        recent_decisions = await self.get_recent_decisions(days=7)
        recent_kw_changes = await self.get_all_recent_keyword_changes(days=7)
        week_strategy = await self.get_current_week_strategy()
        trend = await self.get_trend(days=7)
        weekly_plan_text = await self.get_weekly_plan_text()

        return {
            "recent_decisions": recent_decisions,
            "recent_keyword_changes": recent_kw_changes,
            "week_strategy": week_strategy,
            "weekly_plan_text": weekly_plan_text,  # текстовый план — агент его читает
            "performance_trend": trend,
        }


strategy = StrategyMemory()
