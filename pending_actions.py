"""
BCHD Marketer Agent — очередь действий, ожидающих одобрения
v2 — добавлены:
  1) TTL / проверка "устарела ли карточка" (created_at слишком давно)
  2) validate_before_add() — блокирует создание заведомо невозможных
     действий (например, ключевые слова / минус-слова для LSA-аккаунта)
     ДО того, как карточка попадёт в очередь на одобрение
  3) purge_stale() — очистка старых записей, чтобы _store не рос бесконечно
"""
import uuid
from datetime import datetime, timedelta
from typing import Optional

# Через сколько часов карточка считается устаревшей (данные могли измениться —
# например, ключ уже поставили на паузу вручную, или бюджет уже другой)
STALE_AFTER_HOURS = 24

# Типы действий, которые физически невозможны для LSA (Local Services Ads) —
# этот тип аккаунта не использует ключевые слова, Google сам определяет
# аудиторию на основе категории услуги и профиля.
LSA_UNSUPPORTED_ACTION_TYPES = {
    "pause_keywords",
    "enable_keywords",
    "add_negative_keywords",
}


class PendingActionValidationError(Exception):
    """Действие не прошло валидацию и не должно попадать в очередь на одобрение."""
    pass


class PendingActions:
    """Хранит предложенные изменения до одобрения владельцем"""
    def __init__(self):
        self._store: dict[str, dict] = {}

    def validate_before_add(self, action: dict):
        """
        Проверяет действие ДО добавления в очередь. Бросает
        PendingActionValidationError, если действие заведомо невозможно
        выполнить (например, ключевые слова для LSA-аккаунта).
        Вызывающий код (bot.py) должен ловить это исключение и не
        создавать карточку одобрения, а вместо этого залогировать/
        сообщить владельцу, что предложение было отфильтровано.
        """
        action_type = action.get("type")
        account = action.get("account")
        if action_type in LSA_UNSUPPORTED_ACTION_TYPES and account == "lsa":
            raise PendingActionValidationError(
                f"Действие '{action_type}' невозможно для LSA-аккаунта — "
                f"LSA не использует ключевые слова/минус-слова. "
                f"Карточка одобрения не создана."
            )

    def add(self, action: dict, skip_validation: bool = False) -> str:
        """
        Добавить действие в очередь. Возвращает action_id.
        По умолчанию сначала проверяет действие через validate_before_add() —
        если оно заведомо невозможно (например, keywords для LSA), выбрасывает
        PendingActionValidationError и НЕ добавляет карточку в очередь.
        skip_validation=True можно использовать только для действий, которые
        заведомо не относятся к keyword-типам (например, dispute_lsa_lead).
        """
        if not skip_validation:
            self.validate_before_add(action)

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

    def is_stale(self, action_id: str) -> bool:
        """True, если карточка старше STALE_AFTER_HOURS часов — данные, на
        основе которых она была предложена, могли устареть (кто-то мог
        вручную поставить ключ на паузу, бюджет мог измениться и т.д.)."""
        action = self._store.get(action_id)
        if not action:
            return False
        try:
            created = datetime.fromisoformat(action["created_at"])
        except (KeyError, ValueError):
            return False
        return datetime.now() - created > timedelta(hours=STALE_AFTER_HOURS)

    def approve(self, action_id: str) -> Optional[dict]:
        action = self._store.get(action_id)
        if action and action["status"] == "pending":
            action["status"] = "approved"
            action["stale_when_approved"] = self.is_stale(action_id)
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

    def purge_stale(self, max_age_hours: int = 72) -> int:
        """
        Удаляет из очереди карточки (любого статуса), созданные более
        max_age_hours часов назад. Не удаляет pending-карточки моложе
        STALE_AFTER_HOURS — только явно старые записи, чтобы _store
        не рос бесконечно при долгой работе процесса. Возвращает
        количество удалённых записей.
        """
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        to_remove = []
        for action_id, action in self._store.items():
            try:
                created = datetime.fromisoformat(action["created_at"])
            except (KeyError, ValueError):
                continue
            if created < cutoff:
                to_remove.append(action_id)
        for action_id in to_remove:
            del self._store[action_id]
        return len(to_remove)
