"""
BCHD Marketer Agent — очередь действий, ожидающих одобрения
v3 — КРИТИЧЕСКИЙ ФИX: хранилище перенесено из памяти процесса в Postgres.
     Теперь карточки выживают после редеплоя Railway — "не найдено или уже
     выполнено" при нажатии кнопки после перезапуска больше не происходит.
     Добавлена поддержка комментариев владельца к карточкам.
"""
import uuid
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

STALE_AFTER_HOURS = 24

LSA_UNSUPPORTED_ACTION_TYPES = {
    "pause_keywords", "pausekeywords",
    "enable_keywords", "enablekeywords",
    "add_negative_keywords", "addnegativekeywords",
}


class PendingActionValidationError(Exception):
    pass


class PendingActions:
    """
    Хранит предложенные изменения до одобрения владельцем.
    Использует Postgres как persistent storage — выживает после редеплоя.
    Fallback на dict в памяти если DATABASE_URL не задан.
    """

    def __init__(self):
        self._store: dict[str, dict] = {}  # fallback если нет Postgres
        self._pool = None  # устанавливается через set_pool()

    def set_pool(self, pool):
        """Вызывается из bot.py после инициализации DB пула."""
        self._pool = pool

    async def _ensure_table(self):
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_actions (
                    action_id TEXT PRIMARY KEY,
                    action_data JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Добавляем колонку comment если её нет (для старых инсталляций)
            await conn.execute("""
                ALTER TABLE pending_actions
                ADD COLUMN IF NOT EXISTS comment TEXT DEFAULT NULL
            """)

    def validate_before_add(self, action: dict):
        action_type = action.get("type")
        account = action.get("account")
        if action_type in LSA_UNSUPPORTED_ACTION_TYPES and account == "lsa":
            raise PendingActionValidationError(
                f"Действие '{action_type}' невозможно для LSA-аккаунта."
            )

    async def add(self, action: dict, skip_validation: bool = False) -> str:
        if not skip_validation:
            self.validate_before_add(action)
        action_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        record = {
            **action,
            "id": action_id,
            "created_at": now,
            "status": "pending",
        }
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO pending_actions (action_id, action_data, status) VALUES ($1, $2, $3)",
                        action_id, json.dumps(record), "pending"
                    )
                return action_id
            except Exception as e:
                log.error(f"pending_actions.add DB error: {e}")
        # fallback
        self._store[action_id] = record
        return action_id

    async def get(self, action_id: str) -> Optional[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT action_data, status, comment FROM pending_actions WHERE action_id = $1",
                        action_id
                    )
                if row:
                    data = json.loads(row["action_data"])
                    data["status"] = row["status"]
                    data["comment"] = row["comment"]
                    return data
                return None
            except Exception as e:
                log.error(f"pending_actions.get DB error: {e}")
        return self._store.get(action_id)

    async def is_stale(self, action_id: str) -> bool:
        record = await self.get(action_id)
        if not record:
            return False
        try:
            created = datetime.fromisoformat(record["created_at"])
        except (KeyError, ValueError):
            return False
        return datetime.now() - created > timedelta(hours=STALE_AFTER_HOURS)

    async def approve(self, action_id: str) -> Optional[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "UPDATE pending_actions SET status='approved', updated_at=now() "
                        "WHERE action_id=$1 AND status='pending' "
                        "RETURNING action_data, comment",
                        action_id
                    )
                if row:
                    data = json.loads(row["action_data"])
                    data["status"] = "approved"
                    data["comment"] = row["comment"]
                    data["stale_when_approved"] = await self.is_stale(action_id)
                    return data
                return None
            except Exception as e:
                log.error(f"pending_actions.approve DB error: {e}")
        # fallback
        action = self._store.get(action_id)
        if action and action["status"] == "pending":
            action["status"] = "approved"
            return action
        return None

    async def reject(self, action_id: str, reason: str = "") -> Optional[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "UPDATE pending_actions SET status='rejected', updated_at=now() "
                        "WHERE action_id=$1 AND status='pending' "
                        "RETURNING action_data",
                        action_id
                    )
                if row:
                    data = json.loads(row["action_data"])
                    data["status"] = "rejected"
                    data["reject_reason"] = reason
                    return data
                # Уже не pending — возвращаем что есть
                row = await conn.fetchrow(
                    "SELECT action_data, status FROM pending_actions WHERE action_id=$1",
                    action_id
                )
                if row:
                    data = json.loads(row["action_data"])
                    data["status"] = row["status"]
                    return data
                return None
            except Exception as e:
                log.error(f"pending_actions.reject DB error: {e}")
        # fallback
        action = self._store.get(action_id)
        if action and action["status"] == "pending":
            action["status"] = "rejected"
            return action
        return None

    async def add_comment(self, action_id: str, comment: str) -> bool:
        """Добавляет комментарий владельца к карточке."""
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    result = await conn.execute(
                        "UPDATE pending_actions SET comment=$1, updated_at=now() WHERE action_id=$2",
                        comment, action_id
                    )
                return result == "UPDATE 1"
            except Exception as e:
                log.error(f"pending_actions.add_comment DB error: {e}")
        action = self._store.get(action_id)
        if action:
            action["comment"] = comment
            return True
        return False

    async def record_execution_result(self, action_id: str, verified) -> None:
        update_data = {
            "executed_at": datetime.now().isoformat(),
            "initial_verified": verified,
            "needs_reverify": verified is not True,
            "reverify_done": False,
        }
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT action_data FROM pending_actions WHERE action_id=$1", action_id
                    )
                    if row:
                        data = json.loads(row["action_data"])
                        data.update(update_data)
                        await conn.execute(
                            "UPDATE pending_actions SET action_data=$1, updated_at=now() WHERE action_id=$2",
                            json.dumps(data), action_id
                        )
                return
            except Exception as e:
                log.error(f"pending_actions.record_execution_result DB error: {e}")
        action = self._store.get(action_id)
        if action:
            action.update(update_data)

    async def get_actions_needing_reverify(self, min_hours_since_execution: int = 20) -> list[dict]:
        cutoff = (datetime.now() - timedelta(hours=min_hours_since_execution)).isoformat()
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT action_data FROM pending_actions "
                        "WHERE status='approved' "
                        "AND (action_data->>'needs_reverify')::boolean = true "
                        "AND (action_data->>'reverify_done')::boolean = false "
                        "AND action_data->>'executed_at' IS NOT NULL "
                        "AND action_data->>'executed_at' <= $1",
                        cutoff
                    )
                return [json.loads(r["action_data"]) for r in rows]
            except Exception as e:
                log.error(f"pending_actions.get_actions_needing_reverify DB error: {e}")
        cutoff_dt = datetime.now() - timedelta(hours=min_hours_since_execution)
        result = []
        for action in self._store.values():
            if not action.get("needs_reverify") or action.get("reverify_done"):
                continue
            try:
                executed = datetime.fromisoformat(action.get("executed_at", ""))
            except ValueError:
                continue
            if executed <= cutoff_dt:
                result.append(action)
        return result

    async def mark_reverified(self, action_id: str) -> None:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT action_data FROM pending_actions WHERE action_id=$1", action_id
                    )
                    if row:
                        data = json.loads(row["action_data"])
                        data["reverify_done"] = True
                        await conn.execute(
                            "UPDATE pending_actions SET action_data=$1, updated_at=now() WHERE action_id=$2",
                            json.dumps(data), action_id
                        )
                return
            except Exception as e:
                log.error(f"pending_actions.mark_reverified DB error: {e}")
        action = self._store.get(action_id)
        if action:
            action["reverify_done"] = True

    async def get_history(self, limit: int = 20) -> list[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT action_data, status FROM pending_actions "
                        "WHERE status IN ('approved', 'rejected') "
                        "ORDER BY updated_at DESC LIMIT $1",
                        limit
                    )
                result = []
                for r in rows:
                    data = json.loads(r["action_data"])
                    data["status"] = r["status"]
                    result.append(data)
                return result
            except Exception as e:
                log.error(f"pending_actions.get_history DB error: {e}")
        actions = [a for a in self._store.values() if a["status"] in ("approved", "rejected")]
        actions.sort(key=lambda a: a.get("executed_at") or a.get("created_at", ""), reverse=True)
        return actions[:limit]

    async def pending_count(self) -> int:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    return await conn.fetchval(
                        "SELECT COUNT(*) FROM pending_actions WHERE status='pending'"
                    )
            except Exception as e:
                log.error(f"pending_actions.pending_count DB error: {e}")
        return sum(1 for a in self._store.values() if a["status"] == "pending")

    async def all_pending(self) -> list[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT action_data, comment FROM pending_actions WHERE status='pending' ORDER BY created_at"
                    )
                result = []
                for r in rows:
                    data = json.loads(r["action_data"])
                    data["comment"] = r["comment"]
                    result.append(data)
                return result
            except Exception as e:
                log.error(f"pending_actions.all_pending DB error: {e}")
        return [a for a in self._store.values() if a["status"] == "pending"]

    async def purge_stale(self, max_age_hours: int = 72) -> int:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    result = await conn.execute(
                        "DELETE FROM pending_actions WHERE status='pending' "
                        "AND created_at < now() - interval '$1 hours'",
                        max_age_hours
                    )
                try:
                    return int(result.split()[-1])
                except (ValueError, IndexError):
                    return 0
            except Exception as e:
                log.error(f"pending_actions.purge_stale DB error: {e}")
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        to_remove = [
            aid for aid, a in self._store.items()
            if datetime.fromisoformat(a.get("created_at", "2000-01-01")) < cutoff
        ]
        for aid in to_remove:
            del self._store[aid]
        return len(to_remove)
