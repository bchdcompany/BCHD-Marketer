"""
BCHD Marketer Agent — очередь действий, ожидающих одобрения
"""

import uuid
from datetime import datetime
from typing import Optional


class PendingActions:
    """Хранит предложенные изменения до одобрения владельцем"""

    def __init__(self):
        self._store: dict[str, dict] = {}

    def add(self, action: dict) -> str:
        """Добавить действие в очередь. Возвращает action_id."""
        action_id = str(uuid.uuid4())[:8]
        self._store[action_id] = {
            **action,
            "id": action_id,
            "created_at": datetime.now().isoformat(),
            "status": "pending",
        }
        return action_id

    def get(self, action_id: str) -> Optional[dict]:
        return self._store.get(action_id)

    def approve(self, action_id: str) -> Optional[dict]:
        action = self._store.get(action_id)
        if action and action["status"] == "pending":
            action["status"] = "approved"
            return action
        return None

    def reject(self, action_id: str) -> Optional[dict]:
        action = self._store.get(action_id)
        if action and action["status"] == "pending":
            action["status"] = "rejected"
            return action
        return None

    def pending_count(self) -> int:
        return sum(1 for a in self._store.values() if a["status"] == "pending")

    def all_pending(self) -> list[dict]:
        return [a for a in self._store.values() if a["status"] == "pending"]
