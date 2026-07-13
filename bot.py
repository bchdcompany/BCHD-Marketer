"""
BCHD Marketer Agent — Telegram бот
Google Ads оптимизация с AI-анализом
v5 — история команд: команды (/keywords, /audit, /budget, /report) сохраняются
     в chat_history, чтобы агент помнил что уже было сделано и не повторял вопросы.
     Лимит истории увеличен до 30 сообщений (15 обменов).
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

import asyncpg
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ads_client import GoogleAdsClient
from ai_analyst import AIAnalyst
from config import config
from pending_actions import PendingActions, STALE_AFTER_HOURS
from report_generator import ReportGenerator
import workiz_client

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

ads_client = GoogleAdsClient(config)
ai_analyst = AIAnalyst(config)
report_gen = ReportGenerator()
pending = PendingActions()

NY_TZ = pytz.timezone(config.TIMEZONE)

MAX_HISTORY_MESSAGES = 30  # последние 15 обменов (вопрос+ответ)
DATABASE_URL = os.environ.get("DATABASE_URL")
_db_pool = None


async def _get_db_pool():
    global _db_pool
    if DATABASE_URL and _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _db_pool


async def init_db():
    if not DATABASE_URL:
        log.warning("DATABASE_URL не задан — история переписки будет храниться только в памяти")
        return
    try:
        pool = await _get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id_created_at
                ON chat_history (chat_id, created_at)
            """)
        log.info("Таблица chat_history готова")
    except Exception as e:
        log.error(f"Ошибка инициализации таблицы chat_history: {e}")


async def _get_history(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list:
    if DATABASE_URL:
        try:
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT role, content FROM chat_history WHERE chat_id = $1 ORDER BY created_at DESC LIMIT $2",
                    chat_id, MAX_HISTORY_MESSAGES,
                )
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        except Exception as e:
            log.error(f"Ошибка чтения истории из Postgres: {e}")
    return ctx.chat_data.get("history", [])


async def _append_history(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, question: str, answer: str):
    """Сохраняет обмен в историю. Обрезает слишком длинные ответы чтобы не раздувать контекст."""
    # Обрезаем очень длинные ответы (например, большие JSON-данные аудита)
    answer_trimmed = answer[:4000] if len(answer) > 4000 else answer

    if DATABASE_URL:
        try:
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO chat_history (chat_id, role, content) VALUES ($1, 'user', $2), ($1, 'assistant', $3)",
                    chat_id, question, answer_trimmed,
                )
                await conn.execute("""
                    DELETE FROM chat_history
                    WHERE chat_id = $1 AND id NOT IN (
                        SELECT id FROM chat_history WHERE chat_id = $1
                        ORDER BY created_at DESC LIMIT $2
                    )
                """, chat_id, MAX_HISTORY_MESSAGES)
            return
        except Exception as e:
            log.error(f"Ошибка сохранения истории в Postgres: {e}")
    history = ctx.chat_data.get("history", [])
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer_trimmed})
    ctx.chat_data["history"] = history[-MAX_HISTORY_MESSAGES:]


async def _save_cmd_result(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, command_label: str, result_text: str):
    """
    Сохраняет результат команды (/keywords, /audit, /budget и т.д.) в историю переписки.
    Это позволяет агенту помнить что уже было сделано при последующих текстовых вопросах.
    command_label — строка вида "/keywords (Google Ads 936)"
    """
    try:
        await _append_history(ctx, chat_id, command_label, result_text)
        log.info(f"Сохранён результат команды в историю: {command_label[:60]}")
    except Exception as e:
        log.warning(f"Не удалось сохранить результат команды в историю: {e}")


def _is_owner(update: Update) -> bool:
    return update.effective_user.id == config.OWNER_CHAT_ID


def _account_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Google Ads (936)", callback_data=f"{prefix}:ads"),
        InlineKeyboardButton("📍 LSA (667)", callback_data=f"{prefix}:lsa"),
        InlineKeyboardButton("🔀 Оба", callback_data=f"{prefix}:both"),
    ]])


def _translate_google_ads_error(e: Exception) -> str:
    """
    Переводит типовые сырые ошибки Google Ads API (gRPC-исключения) в
    понятный человеку текст вместо стены технического текста. Покрывает
    только самые частые случаи — для остального возвращает укороченный
    оригинал, чтобы не терять диагностическую информацию совсем.
    """
    text = str(e)
    if "RESOURCE_NOT_FOUND" in text:
        return (
            "Этот ресурс (кампания/ключ/бюджет) больше не существует в Google Ads — "
            "скорее всего, он был удалён или изменён вручную уже ПОСЛЕ того, как эта "
            "карточка была создана (карточка могла ждать одобрения долго). Отклони это "
            "действие и запроси свежий анализ той же темы — я соберу актуальные данные заново."
        )
    if "LOCAL_SERVICES" in text or "OPERATION_NOT_PERMITTED_FOR_CONTEXT" in text:
        return (
            "Это действие невозможно для кампании типа Local Services (LSA) — такие "
            "кампании не поддерживают ключевые слова/минус-слова. Похоже, карточка "
            "была создана для кампании не того типа."
        )
    if "QUOTA_EXCEEDED" in text or "RESOURCE_EXHAUSTED" in text:
        return "Превышена квота запросов к Google Ads API. Попробуй ещё раз через несколько минут."
    if "AUTHENTICATION" in text or "AUTHORIZATION" in text:
        return "Проблема с авторизацией в Google Ads API — возможно, истёк токен доступа. Нужна переавторизация."
    # Неизвестный тип ошибки — возвращаем укороченный оригинал, а не всю стену текста
    return text[:400] + ("..." if len(text) > 400 else "")


TELEGRAM_MESSAGE_LIMIT = 4096
_TRUNCATION_SUFFIX = "\n\n_(сообщение обрезано — оно было длиннее лимита Telegram)_"


def _truncate_for_telegram(text: str) -> str:
    """
    Telegram жёстко ограничивает одно сообщение 4096 символами. Если это
    не учесть, send_message/edit_message_text падает с BadRequest:
    Message_too_long — и, в отличие от ошибки парсинга Markdown, повторная
    отправка БЕЗ parse_mode эту проблему НЕ решает (текст всё ещё слишком
    длинный), из-за чего retry падает с ТОЙ ЖЕ ошибкой. Если это исключение
    никто не ловит (а глобального error handler'а раньше не было) — задача
    просто тихо умирает, и "Анализирую..." висит вечно без единого ответа.
    Обрезаем ЗАРАНЕЕ, чтобы это в принципе не могло случиться.
    """
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    cutoff = TELEGRAM_MESSAGE_LIMIT - len(_TRUNCATION_SUFFIX)
    return text[:cutoff] + _TRUNCATION_SUFFIX


async def _safe_send(bot, chat_id: int, text: str, parse_mode="Markdown", **kwargs):
    text = _truncate_for_telegram(text)
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        log.warning(f"Не удалось отправить с parse_mode={parse_mode} ({e}), отправляю как обычный текст")
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except BadRequest as e2:
            # Даже без Markdown не получилось (например, текст всё ещё
            # слишком длинный по каким-то причинам) — режем жёстче и шлём
            # короткое сообщение об ошибке, лишь бы не оставить владельца
            # без ответа вообще.
            log.error(f"Не удалось отправить сообщение даже без Markdown: {e2}")
            fallback = text[:1000] + "\n\n⚠️ _Остальная часть ответа не поместилась и была потеряна._"
            return await bot.send_message(chat_id=chat_id, text=fallback, **kwargs)


async def _safe_edit(target, text: str, parse_mode="Markdown", **kwargs):
    text = _truncate_for_telegram(text)
    edit_fn = getattr(target, "edit_message_text", None) or getattr(target, "edit_text", None)
    if edit_fn is None:
        raise AttributeError(f"{type(target)} не поддерживает редактирование текста")
    try:
        return await edit_fn(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        log.warning(f"Не удалось отредактировать с parse_mode={parse_mode} ({e}), редактирую как обычный текст")
        try:
            return await edit_fn(text, **kwargs)
        except BadRequest as e2:
            log.error(f"Не удалось отредактировать сообщение даже без Markdown: {e2}")
            fallback = text[:1000] + "\n\n⚠️ Остальная часть ответа не поместилась и была потеряна."
            return await edit_fn(fallback)


async def _safe_reply(message, text: str, parse_mode="Markdown", **kwargs):
    text = _truncate_for_telegram(text)
    try:
        return await message.reply_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        log.warning(f"Не удалось ответить с parse_mode={parse_mode} ({e}), отвечаю как обычный текст")
        try:
            return await message.reply_text(text, **kwargs)
        except BadRequest as e2:
            log.error(f"Не удалось ответить даже без Markdown: {e2}")
            fallback = text[:1000] + "\n\n⚠️ Остальная часть ответа не поместилась и была потеряна."
            return await message.reply_text(fallback)


async def _send_approval_card(bot, chat_id: int, action_id: str, action: dict):
    text = report_gen.format_approval_card(action_id, action)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Применить", callback_data=f"approve:{action_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{action_id}"),
    ]])
    await _safe_send(bot, chat_id, text, parse_mode="Markdown", reply_markup=keyboard)


# ── Команды ──────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    ads_status = "✅ Google Ads API подключён" if config.google_ads_configured else "⚠️ Google Ads API не настроен"
    lsa_status = "✅ LSA аккаунт подключён" if config.lsa_configured else "⚠️ LSA аккаунт не настроен"
    await _safe_reply(
        update.message,
        f"🤖 *BCHD Marketer Agent*\n\n"
        f"{ads_status}\n"
        f"{lsa_status}\n\n"
        f"Аккаунты:\n"
        f"• Google Ads: 936-279-9327\n"
        f"• LSA: 667-939-5231\n"
        f"• MCC: 313-939-3264\n\n"
        f"Команды:\n"
        f"/report — ежедневный отчёт\n"
        f"/audit — полный аудит кампаний\n"
        f"/budget — анализ бюджетов\n"
        f"/keywords — анализ ключевых слов\n"
        f"/negatives — предложение минус-слов\n"
        f"/competitors — анализ конкурентов\n"
        f"/abtest — A/B тест объявлений\n"
        f"/seasonal — сезонные корректировки\n"
        f"/both — сводка по обоим аккаунтам\n"
        f"/roas — реальный ROAS по всем каналам (Google + LSA + Thumbtack, на основе джобов из Workiz)\n"
        f"/month [YYYY-MM] — отчёт за конкретный календарный месяц (без аргумента — текущий месяц с 1-го числа)\n"
        f"/auditcalls [дней] — аудит оплаченных LSA-звонков за период\n"
        f"/checklead <lead_id> — прослушать и оценить конкретный LSA-лид\n"
        f"/pending — ожидающие одобрения\n"
        f"/schedule — расписание задач\n"
        f"/checkkeyword <текст> — прямая проверка реальной ставки ключа (без ИИ)\n"
        f"/checknegatives — прямая проверка списка минус-слов (без ИИ)\n"
        f"/history [N] — журнал выполненных действий (последние N, по умолчанию 20)\n"
        f"/reviewnegatives — ИИ-анализ списка минус-слов на риск блокировки релевантного трафика",
        parse_mode="Markdown",
    )


async def cmd_both(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🔀 Собираю данные по обоим аккаунтам... (~40 сек)")
    try:
        today = datetime.now(NY_TZ)
        month_from = today.replace(day=1).strftime("%Y-%m-%d")
        month_to = today.strftime("%Y-%m-%d")
        data = await ads_client.get_both_accounts_summary(date_from=month_from, date_to=month_to)
        combined = data.get('combined', {})
        ads_data = data.get('google_ads', {})
        lsa_data = data.get('lsa', {})

        text = f"📊 *Сводка по всем аккаунтам — {month_from} — {month_to}*\n\n"
        text += f"💰 *Итого потрачено:* ${combined.get('total_spend', 0):.2f}\n"
        text += f"📞 *Всего конверсий:* {combined.get('total_conversions', 0):.0f}\n"
        if combined.get('avg_cpa'):
            text += f"💵 *Средний CPA:* ${combined.get('avg_cpa'):.2f}\n\n"

        text += f"*Google Ads (936):*\n"
        text += f"• Расход: ${ads_data.get('total_spend', 0):.2f}\n"
        text += f"• Конверсии: {ads_data.get('total_conversions', 0):.0f}\n"
        text += f"• Кликов: {ads_data.get('total_clicks', 0)}\n\n"

        if lsa_data and not lsa_data.get('error'):
            text += f"*LSA (667):*\n"
            text += f"• Расход: ${lsa_data.get('total_spend', 0):.2f}\n"
            text += f"• Конверсии: {lsa_data.get('total_conversions', 0):.0f}\n"
        elif lsa_data and lsa_data.get('error'):
            text += f"*LSA (667):* ⚠️ {lsa_data.get('error')}\n"

        thumbtack_days = (today - today.replace(day=1)).days + 1
        text += await _get_thumbtack_summary_line(days=thumbtack_days)

        await _safe_edit(msg, text, parse_mode="Markdown")

        # Сохраняем в историю
        chat_id = update.effective_chat.id
        await _save_cmd_result(ctx, chat_id, "/both", text)
    except Exception as e:
        log.error(f"Ошибка /both: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(
        "📅 *Расписание задач (NY time)*\n\n"
        "Ежедневно:\n"
        "— 08:00: утренний отчёт (оба аккаунта)\n"
        "— 14:00: проверка бюджетов (создаёт карточку, если нужна коррекция)\n"
        "— 21:00: итоги дня\n\n"
        "Еженедельно (понедельник):\n"
        "— 08:30: аудит LSA-звонков (предлагает оспаривание нерелевантных лидов)\n"
        "— 09:00: полный аудит кампаний (Google Ads + LSA)\n"
        "— 09:15: ROAS-отчёт по всем каналам (Google + LSA + Thumbtack, на основе реальных джобов из Workiz)\n"
        "— 09:20: отдельная проверка Thumbtack (бюджет vs количество джобов, с флагом аномалий)\n\n"
        "Дополнительно:\n"
        "— Вс 09:30: анализ конкурентов\n"
        "— Ср 10:00: проверка A/B тестов\n"
        "— 1-е число месяца 08:00: сезонная оптимизация\n\n"
        "Служебное (не отчёты, но может прислать сообщение):\n"
        "— каждые 4 часа: отложенная перепроверка ранее одобренных действий, "
        "если первая проверка не дала железного подтверждения\n\n"
        "Важно: автоматические задачи только анализируют данные и предлагают "
        "действия через карточки ✅/❌ — сами ничего не меняют без твоего "
        "одобрения (кроме отправки фидбэка Google по явно неподходящим LSA-лидам, "
        "которое тоже требует одобрения через карточку).",
        parse_mode="Markdown",
    )


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "📊 Какой аккаунт анализировать?",
        reply_markup=_account_keyboard("report")
    )


async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "🔍 Какой аккаунт аудировать?",
        reply_markup=_account_keyboard("audit")
    )


async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "💰 Бюджет какого аккаунта анализировать?",
        reply_markup=_account_keyboard("budget")
    )


async def cmd_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "🔑 Ключевые слова какого аккаунта?",
        reply_markup=_account_keyboard("keywords")
    )


async def cmd_negatives(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "🚫 Поисковые запросы какого аккаунта?",
        reply_markup=_account_keyboard("negatives")
    )


async def cmd_competitors(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "🏆 Конкуренты какого аккаунта?",
        reply_markup=_account_keyboard("competitors")
    )


async def cmd_abtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "🧪 A/B тест какого аккаунта?",
        reply_markup=_account_keyboard("abtest")
    )


async def cmd_seasonal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    await update.message.reply_text(
        "🍂 Сезонные корректировки для какого аккаунта?",
        reply_markup=_account_keyboard("seasonal")
    )


async def _build_roas_report(date_from: str, date_to: str) -> str:
    from datetime import datetime as dt

    ads_spend = await ads_client.get_spend_for_period(date_from, date_to, account="ads")
    lsa_spend = await ads_client.get_spend_for_period(date_from, date_to, account="lsa")

    try:
        days_in_period = (dt.strptime(date_to, "%Y-%m-%d") - dt.strptime(date_from, "%Y-%m-%d")).days + 1
    except Exception:
        days_in_period = 7
    thumbtack_weekly_budget = config.THUMBTACK_WEEKLY_BUDGET
    thumbtack_cost = round(thumbtack_weekly_budget / 7 * days_in_period, 2)

    google_jobs = await workiz_client.get_jobs_by_source("Google", date_from, date_to)
    thumbtack_jobs = await workiz_client.get_jobs_by_source("Thumbtack", date_from, date_to)

    ads_cost = ads_spend.get("spend", 0)
    lsa_cost = lsa_spend.get("spend", 0)
    total_ad_spend = ads_cost + lsa_cost + thumbtack_cost

    text = f"📊 *Реальный ROAS — {date_from} — {date_to}*\n\n"
    text += f"💰 *Расходы на рекламу:*\n"
    text += f"• Google Ads (936): ${ads_cost:.2f}\n"
    text += f"• LSA (667): ${lsa_cost:.2f}\n"
    text += f"• Thumbtack (бюджет ${thumbtack_weekly_budget:.0f}/нед, расчётно): ${thumbtack_cost:.2f}\n"
    text += f"• Итого: ${total_ad_spend:.2f}\n\n"

    g = google_jobs
    g_rev = g.get('total_revenue', 0)
    g_jobs = g.get('total_jobs', 0)
    google_spend = ads_cost + lsa_cost

    text += f"🔧 *Google (Ads + LSA):*\n"
    text += f"• Джобов: {g_jobs} | Выручка: ${g_rev:.2f} | Собрано: ${g.get('total_collected', 0):.2f}\n"
    if g.get('total_due', 0) > 0:
        text += f"• Долг: ${g.get('total_due', 0):.2f}\n"
    if google_spend > 0 and g_jobs > 0:
        g_roas = g_rev / google_spend * 100
        g_cpa = google_spend / g_jobs
        text += f"• ROAS: {g_roas:.0f}% | CPA: ${g_cpa:.0f}\n"
    text += "\n"

    t = thumbtack_jobs
    t_rev = t.get('total_revenue', 0)
    t_jobs = t.get('total_jobs', 0)
    text += f"📌 *Thumbtack:*\n"
    text += f"• Джобов: {t_jobs} | Выручка: ${t_rev:.2f} | Собрано: ${t.get('total_collected', 0):.2f}\n"
    if t.get('total_due', 0) > 0:
        text += f"• Долг: ${t.get('total_due', 0):.2f}\n"
    if thumbtack_cost > 0 and t_jobs > 0:
        t_roas = t_rev / thumbtack_cost * 100
        t_cpa = thumbtack_cost / t_jobs
        text += f"• ROAS: {t_roas:.0f}% | CPA: ${t_cpa:.0f}\n"
    elif t_jobs == 0:
        text += f"• Джобов из Thumbtack не найдено за период\n"
    text += "\n"

    total_revenue = g_rev + t_rev
    total_collected = g.get('total_collected', 0) + t.get('total_collected', 0)
    total_jobs = g_jobs + t_jobs
    text += f"📈 *Итого по всем каналам:*\n"
    text += f"• Расходы: ${total_ad_spend:.2f} | Выручка: ${total_revenue:.2f} | Собрано: ${total_collected:.2f}\n"
    if total_ad_spend > 0 and total_revenue > 0:
        total_roas = total_revenue / total_ad_spend * 100
        text += f"• Общий ROAS: {total_roas:.0f}%\n"
    if total_jobs > 0 and total_ad_spend > 0:
        text += f"• Средний CPA по всем каналам: ${total_ad_spend / total_jobs:.0f}\n"

    overdue = [j for j in g.get("jobs", []) if j.get("amount_due", 0) > 0]
    overdue_t = [j for j in t.get("jobs", []) if j.get("amount_due", 0) > 0]
    all_overdue = overdue + overdue_t
    if all_overdue:
        text += f"\n⚠️ *Неоплаченные джобы ({len(all_overdue)}):*\n"
        for j in all_overdue[:5]:
            text += f"• #{j['serial_id']}: ${j['total_price']:.0f} (долг ${j['amount_due']:.0f}, {j['status']})\n"

    return text


async def cmd_check_keyword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Прямая диагностическая команда: /checkkeyword <текст> — находит ключевые
    слова, содержащие этот текст, и показывает их РЕАЛЬНУЮ текущую ставку
    напрямую из Google Ads API. Никакого ИИ-анализа, никаких карточек —
    только сырые факты, чтобы однозначно проверить, применилось ли
    изменение ставки, без риска путаницы с формулировками чата.
    Использование: /checkkeyword refrigerator repair Brooklyn
    """
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /checkkeyword <текст ключа>\nНапример: /checkkeyword refrigerator repair Brooklyn")
        return
    search_text = " ".join(ctx.args)

    msg = await update.message.reply_text(f"🔍 Ищу ключи, содержащие '{search_text}'...")
    try:
        result = await ads_client.get_keyword_current_bid(search_text, account="ads")
    except Exception as e:
        log.error(f"Ошибка /checkkeyword: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    if result.get("error"):
        await msg.edit_text(f"❌ Ошибка: {result['error']}")
        return

    matches = result.get("matches", [])
    if not matches:
        await msg.edit_text(f"Ключей, содержащих '{search_text}', не найдено.")
        return

    text = f"🔍 *Прямая проверка Google Ads API (без ИИ-анализа):*\n\n"
    for m in matches:
        text += f"*\"{m['keyword']}\"* ({m['match_type']})\n"
        text += f"• Кампания: {m['campaign']} / {m['ad_group']}\n"
        text += f"• Статус: {m['status']}\n"
        own = f"${m['own_cpc_bid']:.2f}" if m['own_cpc_bid'] is not None else "не задана (наследуется от группы/автостратегии)"
        text += f"• Собственная ставка (cpc_bid_micros): {own}\n"
        if m['effective_cpc_bid'] is not None:
            text += f"• Эффективная ставка (effective_cpc_bid): ${m['effective_cpc_bid']:.2f}\n"
        final_urls = m.get('final_urls') or []
        if final_urls:
            text += f"• Final URL (собственный, override ключа): {final_urls[0]}\n"
        else:
            text += f"• Final URL: не задан на уровне ключа (наследуется от объявления группы)\n"
        text += "\n"
    await _safe_edit(msg, text, parse_mode="Markdown")


async def cmd_check_negatives(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Прямая диагностическая команда: /checknegatives — показывает ПОЛНЫЙ
    текущий список минус-слов на уровне кампаний напрямую из Google Ads API,
    без анализа ИИ. Используется для однозначной проверки, реально ли
    применилось добавление конкретного минус-слова.
    """
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🔍 Собираю текущий список минус-слов из Google Ads API...")
    try:
        result = await ads_client.get_negative_keywords_list(account="ads")
    except Exception as e:
        log.error(f"Ошибка /checknegatives: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    if result.get("error"):
        await msg.edit_text(f"❌ Ошибка: {result['error']}")
        return

    negatives = result.get("negatives", [])
    if not negatives:
        await msg.edit_text("🔍 *Прямая проверка Google Ads API:*\n\nМинус-слов на уровне кампаний не найдено (список пуст).", parse_mode="Markdown")
        return

    text = f"🔍 *Прямая проверка Google Ads API (без ИИ-анализа) — {len(negatives)} минус-слов:*\n\n"
    lines = [f"• \"{n['term']}\" ({n['match_type']}) — {n['campaign']}\n" for n in negatives]

    # Telegram ограничивает сообщение ~4096 символами — разбиваем на части,
    # чтобы длинный список минус-слов не падал с "Text is too long"
    TELEGRAM_LIMIT = 3500  # с запасом ниже реального лимита 4096
    chunks = []
    current = text
    for line in lines:
        if len(current) + len(line) > TELEGRAM_LIMIT:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)

    await _safe_edit(msg, chunks[0], parse_mode="Markdown")
    for chunk in chunks[1:]:
        await _safe_send(ctx.bot, config.OWNER_CHAT_ID, chunk, parse_mode="Markdown")


async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Отчёт за КОНКРЕТНЫЙ календарный месяц (не скользящее окно 30 дней).
    Использование:
    /month — текущий месяц с 1-го числа по сегодня (month-to-date)
    /month 2026-06 — конкретный месяц целиком
    """
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    today = datetime.now(NY_TZ)
    if ctx.args:
        try:
            year, month = map(int, ctx.args[0].split("-"))
            date_from = f"{year:04d}-{month:02d}-01"
            if month == 12:
                next_month_first = datetime(year + 1, 1, 1)
            else:
                next_month_first = datetime(year, month + 1, 1)
            last_day = (next_month_first - timedelta(days=1)).day
            # Не запрашиваем дни в будущем, если это текущий месяц
            if year == today.year and month == today.month:
                date_to = today.strftime("%Y-%m-%d")
            else:
                date_to = f"{year:04d}-{month:02d}-{last_day:02d}"
        except (ValueError, IndexError):
            await update.message.reply_text("⚠️ Использование: /month 2026-06 (год-месяц) или просто /month для текущего месяца")
            return
    else:
        date_from = today.replace(day=1).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")

    msg = await update.message.reply_text(f"📅 Считаю данные за {date_from} — {date_to}...")
    try:
        data = await ads_client.get_both_accounts_summary(date_from=date_from, date_to=date_to)
        text = f"📅 *Отчёт за период {date_from} — {date_to}*\n\n"
        text += _format_both_summary(data)
        thumbtack_days = (datetime.strptime(date_to, "%Y-%m-%d") - datetime.strptime(date_from, "%Y-%m-%d")).days + 1
        text += await _get_thumbtack_summary_line(days=thumbtack_days)
        await _safe_edit(msg, text, parse_mode="Markdown")
        await _save_cmd_result(ctx, update.effective_chat.id, f"/month ({date_from} — {date_to})", text)
    except Exception as e:
        log.error(f"Ошибка /month: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_roas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    days = 30
    if ctx.args:
        try:
            days = max(1, min(90, int(ctx.args[0])))
        except ValueError:
            pass

    date_to = datetime.now(NY_TZ).strftime("%Y-%m-%d")
    date_from = (datetime.now(NY_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")

    msg = await update.message.reply_text(f"📊 Считаю реальный ROAS за {days} дней...")
    try:
        text = await _build_roas_report(date_from, date_to)
        await _safe_edit(msg, text, parse_mode="Markdown")
        # Сохраняем в историю
        chat_id = update.effective_chat.id
        await _save_cmd_result(ctx, chat_id, f"/roas ({days} дней)", text)
    except Exception as e:
        log.error(f"Ошибка /roas: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def scheduled_purge_pending(app):
    """Раз в сутки чистит очень старые записи в очереди одобрения (>72ч),
    чтобы память процесса не росла бесконечно при долгой работе."""
    try:
        removed = pending.purge_stale(max_age_hours=72)
        if removed:
            log.info(f"Очищено {removed} устаревших записей из очереди одобрения")
    except Exception as e:
        log.error(f"Ошибка очистки очереди одобрения: {e}")


async def scheduled_reverify_executed_actions(app):
    """
    Раз в несколько часов повторно проверяет действия, которые при
    первом выполнении НЕ были железно подтверждены (verified=False,
    либо тип действия не поддерживает автопроверку и получил verified=None).
    Это ловит случаи, когда первичная проверка ошиблась из-за задержки
    синхронизации Google Ads API, или когда действие было принято API,
    но реально не применилось так, как ожидалось — владелец должен
    узнать правду, а не жить с ложным ощущением "всё сделано".
    """
    try:
        to_check = pending.get_actions_needing_reverify(min_hours_since_execution=20)
    except Exception as e:
        log.error(f"Ошибка получения действий для отложенной перепроверки: {e}")
        return
    for action in to_check:
        action_id = action.get("id")
        try:
            verification = await ads_client.verify_action(action)
        except Exception as e:
            log.error(f"Ошибка отложенной перепроверки {action_id}: {e}")
            continue
        verified = verification.get("verified")
        pending.mark_reverified(action_id)
        if verified is True:
            text = (
                f"✅ *Отложенная перепроверка подтвердила успех:* {action.get('description')}\n\n"
                f"Изменение реально применилось (проверено повторно через сутки)."
            )
        elif verified is False:
            text = (
                f"🚨 *Отложенная перепроверка нашла расхождение:* {action.get('description')}\n\n"
                f"Похоже, действие НЕ применилось так, как ожидалось, хотя API изначально "
                f"не вернул ошибку:\n`{verification}`\n\n"
                f"Рекомендую проверить и применить вручную в Google Ads при необходимости."
            )
        else:
            text = (
                f"ℹ️ *Отложенная перепроверка:* {action.get('description')}\n\n"
                f"Автоматическое подтверждение по-прежнему недоступно для этого типа "
                f"действия — рекомендую проверить вручную в Google Ads, если это важно."
            )
        try:
            await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Ошибка отправки результата отложенной перепроверки: {e}")


async def scheduled_weekly_roas(app):
    if not config.google_ads_configured:
        return
    log.info("Еженедельный ROAS-отчёт")
    try:
        date_to = datetime.now(NY_TZ).strftime("%Y-%m-%d")
        date_from = (datetime.now(NY_TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
        text = await _build_roas_report(date_from, date_to)
        await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка еженедельного ROAS: {e}")


async def scheduled_thumbtack_check(app):
    """
    Еженедельная выделенная проверка Thumbtack: сопоставляет фиксированный
    недельный бюджет с реальным количеством джобов из Workiz (поле
    JobSource='Thumbtack'), считает эффективный CPA и явно флагает
    аномалии — например, бюджет потрачен, но джобов 0, или CPA сильно
    выше нормы. В отличие от общего /roas (который считает все каналы
    сразу), этот отчёт выделен отдельно, чтобы не потерять сигнал по
    Thumbtack среди Google/LSA цифр.
    """
    log.info("Еженедельная проверка Thumbtack (бюджет vs джобы)")
    try:
        date_to = datetime.now(NY_TZ).strftime("%Y-%m-%d")
        date_from = (datetime.now(NY_TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
        thumbtack_weekly_budget = config.THUMBTACK_WEEKLY_BUDGET
        result = await workiz_client.get_jobs_by_source("Thumbtack", date_from, date_to)
        jobs = result.get('total_jobs', 0)
        revenue = result.get('total_revenue', 0)
        collected = result.get('total_collected', 0)
        due = result.get('total_due', 0)

        text = f"📌 *Thumbtack — бюджет vs джобы, {date_from} — {date_to}*\n\n"
        text += f"💰 Недельный бюджет (расчётно): ${thumbtack_weekly_budget:.2f}\n"
        text += f"🔧 Джобов из Workiz (JobSource='Thumbtack'): {jobs}\n"
        text += f"💵 Выручка: ${revenue:.2f} | Собрано: ${collected:.2f}"
        if due > 0:
            text += f" | Долг: ${due:.2f}"
        text += "\n"

        if jobs > 0:
            cpa = thumbtack_weekly_budget / jobs
            roas = (revenue / thumbtack_weekly_budget * 100) if thumbtack_weekly_budget > 0 else None
            text += f"📊 Эффективный CPA: ${cpa:.2f} за джоб\n"
            if roas is not None:
                text += f"📈 ROAS: {roas:.0f}%\n"
        else:
            text += (
                "\n🚨 *Аномалия:* за неделю потрачен бюджет, но джобов с "
                "источником Thumbtack в Workiz не найдено. Возможные причины: "
                "лиды не конвертируются, проблема с трекингом источника в "
                "Workiz, или сам канал сейчас неэффективен — стоит проверить "
                "вручную в приложении Thumbtack.\n"
            )
        await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка проверки Thumbtack: {e}")


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    actions = pending.all_pending()
    if not actions:
        await update.message.reply_text("✅ Нет ожидающих действий.")
        return
    await update.message.reply_text(f"⏳ Ожидает одобрения: {len(actions)}")
    for action in actions[:5]:
        action_id = action["id"]
        await _send_approval_card(update.get_bot(), config.OWNER_CHAT_ID, action_id, action)


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Журнал выполненных действий (одобрено/отклонено), новые сначала.
    ВАЖНО: хранилище только в памяти процесса — журнал охватывает время
    с последнего рестарта/редеплоя бота, не является постоянным архивом.
    """
    if not _is_owner(update):
        return
    limit = 20
    if ctx.args:
        try:
            limit = max(1, min(50, int(ctx.args[0])))
        except ValueError:
            pass

    history = pending.get_history(limit=limit)
    if not history:
        await update.message.reply_text(
            "📋 Журнал пуст (за время с последнего рестарта бота действий не было)."
        )
        return

    text = f"📋 *Журнал действий (последние {len(history)}):*\n"
    text += "_Только за время с последнего рестарта бота — не постоянный архив._\n\n"
    for a in history:
        if a["status"] == "rejected":
            icon = "❌"
            status_label = "Отклонено"
        else:
            verified = a.get("initial_verified")
            reverified = a.get("reverify_done")
            if verified is True:
                icon, status_label = "✅", "Выполнено, подтверждено"
            elif verified is False:
                icon, status_label = "🚨", "Выполнено, РАСХОЖДЕНИЕ"
            elif reverified:
                icon, status_label = "ℹ️", "Выполнено, перепроверено позже"
            else:
                icon, status_label = "📤", "Отправлено, не подтверждено"
        when = a.get("executed_at") or a.get("created_at", "")
        when_short = when[:16].replace("T", " ") if when else "?"
        text += f"{icon} *{a.get('type', '?')}* — {status_label}\n"
        text += f"   {a.get('description', '')[:100]}\n"
        text += f"   _{when_short}_ · id `{a.get('id')}`\n\n"

    await _safe_reply(update.message, text, parse_mode="Markdown")


async def cmd_review_negatives(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    ИИ-обзор УЖЕ СУЩЕСТВУЮЩЕГО списка минус-слов на риск блокировки
    релевантного трафика (в отличие от /negatives, который ищет НОВЫЕ
    минус-слова по свежим поисковым запросам). Не удаляет ничего сам —
    только предлагает карточки на удаление конкретных рискованных записей.
    """
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🔍 Собираю текущий список минус-слов...")
    try:
        neg_result = await ads_client.get_negative_keywords_list(account="ads")
    except Exception as e:
        log.error(f"Ошибка сбора минус-слов для /reviewnegatives: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    if neg_result.get("error"):
        await msg.edit_text(f"❌ Ошибка: {neg_result['error']}")
        return

    negatives = neg_result.get("negatives", [])
    if not negatives:
        await msg.edit_text("Список минус-слов пуст — нечего анализировать.")
        return

    await _safe_edit(msg, f"🤔 Анализирую {len(negatives)} минус-слов на риск блокировки релевантного трафика...")
    try:
        analysis = await ai_analyst.review_negative_keywords_list(negatives)
    except Exception as e:
        log.error(f"Ошибка анализа минус-слов: {e}")
        await msg.edit_text(f"❌ Ошибка анализа: {e}")
        return

    risky = analysis.get("risky_terms", [])
    duplicates = analysis.get("duplicate_groups", [])
    summary = analysis.get("summary", "")

    text = f"🔍 *Обзор минус-слов ({len(negatives)} всего)*\n\n{summary}\n\n"
    if risky:
        text += f"⚠️ *Найдено рискованных: {len(risky)}*\n"
        text += "Карточки на удаление отправлены ниже.\n"
    else:
        text += "✅ Рискованных записей не найдено.\n"
    if duplicates:
        text += f"\n📋 Возможные дубликаты ({len(duplicates)} групп) — требуют ручного решения, карточки не создаются:\n"
        for d in duplicates[:10]:
            text += f"• {', '.join(d.get('terms', []))} — {d.get('note', '')}\n"

    await _safe_edit(msg, text, parse_mode="Markdown")
    await _save_cmd_result(ctx, update.effective_chat.id, "/reviewnegatives", text)

    # Сопоставляем найденные рискованные термины с их resource_name из
    # исходного списка, чтобы можно было предложить конкретное удаление
    by_term = {n['term'].strip().lower(): n for n in negatives}
    for r in risky:
        term_key = r.get('term', '').strip().lower()
        match = by_term.get(term_key)
        if not match:
            continue
        action = {
            "type": "remove_negative_keyword",
            "account": "ads",
            "resource_name": match['resource_name'],
            "term": match['term'],
            "description": f"Удалить рискованное минус-слово '{match['term']}'",
            "reasoning": r.get("risk", ""),
            "data_summary": f"Кампания: {match.get('campaign', '')}, тип: {match.get('match_type', '')}",
            "expected_impact": "Восстановление релевантного трафика, который могло блокировать это минус-слово",
            "urgency": "low",
            "urgency_label": "Низкая",
            "risks": "Проверь причину перед удалением — если минус-слово всё же нужно, отклони карточку",
            "confidence": "medium",
        }
        try:
            action_id = pending.add(action)
            await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
        except Exception as e:
            log.warning(f"Не удалось создать карточку для рискованного минус-слова: {e}")


async def cmd_checklead(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /checklead <lead_id>")
        return
    lead_id = ctx.args[0]

    msg = await update.message.reply_text(f"🎧 Ищу запись звонка по лиду {lead_id}...")
    try:
        convs = await ads_client.get_lsa_lead_conversations(lead_id)
    except Exception as e:
        log.error(f"Ошибка получения бесед по лиду {lead_id}: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    calls = [c for c in convs.get('conversations', []) if c.get('recording_url')]
    if not calls:
        await msg.edit_text(
            f"❌ Не нашёл запись звонка для лида {lead_id}.\n"
            f"Возможно, это текстовый лид или запись недоступна."
        )
        return

    await _safe_edit(msg, "📥 Скачиваю и транскрибирую запись звонка...")
    result = await ads_client.download_and_transcribe_call(calls[0]['recording_url'])
    if not result.get('success'):
        await msg.edit_text(f"❌ Ошибка транскрипции: {result.get('error')}")
        return

    await _safe_edit(msg, "🤔 Анализирую содержание звонка...")
    try:
        opinion = await ai_analyst.analyze_lsa_call(result['transcript'], {'lead_id': lead_id})
    except Exception as e:
        log.error(f"Ошибка анализа звонка {lead_id}: {e}")
        await msg.edit_text(f"❌ Ошибка анализа: {e}")
        return

    text = f"🎧 *Анализ звонка (лид {lead_id})*\n\n{opinion.get('summary', 'Нет данных')}"
    await _safe_edit(msg, text, parse_mode="Markdown")

    if opinion.get('recommend_dispute'):
        status = await ads_client.get_lsa_lead_feedback_status(lead_id)
        if status.get('feedback_submitted'):
            await update.message.reply_text(
                f"ℹ️ По лиду {lead_id} фидбэк уже был отправлен ранее."
            )
            return
        action = {
            'type': 'dispute_lsa_lead',
            'account': 'lsa',
            'lead_id': lead_id,
            'description': f"Оспорить лид {lead_id} — услуга не по профилю",
            'reasoning': opinion.get('dispute_reason', ''),
            'risks': 'Кредит не гарантирован, но фидбэк обучает алгоритм',
            'urgency': 'low',
            'urgency_label': 'Низкая',
            'confidence': opinion.get('confidence', 'medium'),
            'data_summary': result['transcript'][:300],
            'expected_impact': 'Возможный возврат средств + более релевантные лиды',
            'requires_approval': True,
        }
        action_id = pending.add(action)
        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)


async def _run_lsa_audit(bot, progress_msg=None, days: int = 7, limit: int = 20,
                          date_from: str = None, date_to: str = None) -> dict:
    leads_data = await ads_client.get_lsa_leads(days=days, account="lsa", date_from=date_from, date_to=date_to)
    period_label = f"{leads_data.get('date_from')} — {leads_data.get('date_to')}"
    leads = leads_data.get('leads', [])
    charged_leads = [l for l in leads if l.get('charged')]
    if not charged_leads:
        return {'checked': 0, 'disputed': 0, 'total_charged': 0, 'period_label': period_label}

    to_process = charged_leads[:limit]
    disputed_count = 0
    checked_count = 0
    already_submitted_count = 0
    workiz_checked_count = 0
    workiz_not_found_count = 0
    for i, lead in enumerate(to_process):
        lead_id = lead['id']
        if progress_msg:
            try:
                await progress_msg.edit_text(f"🎧 Проверяю звонки... ({i + 1}/{len(to_process)})")
            except Exception:
                pass
        if lead.get('feedback_submitted'):
            already_submitted_count += 1
            continue
        try:
            convs = await ads_client.get_lsa_lead_conversations(lead_id)
            calls = [c for c in convs.get('conversations', []) if c.get('recording_url')]

            if calls:
                result = await ads_client.download_and_transcribe_call(calls[0]['recording_url'])
                if not result.get('success'):
                    continue
                checked_count += 1
                opinion = await ai_analyst.analyze_lsa_call(result['transcript'], lead)
                if opinion.get('recommend_dispute'):
                    action = {
                        'type': 'dispute_lsa_lead',
                        'account': 'lsa',
                        'lead_id': lead_id,
                        'description': f"Оспорить лид {lead_id} — услуга не по профилю",
                        'reasoning': opinion.get('dispute_reason', ''),
                        'risks': 'Кредит не гарантирован, но фидбэк обучает алгоритм',
                        'urgency': 'low',
                        'urgency_label': 'Низкая',
                        'confidence': opinion.get('confidence', 'medium'),
                        'data_summary': result['transcript'][:300],
                        'expected_impact': 'Возможный возврат средств + более релевантные лиды',
                        'requires_approval': True,
                    }
                    action_id = pending.add(action)
                    await _send_approval_card(bot, config.OWNER_CHAT_ID, action_id, action)
                    disputed_count += 1
                continue

            phone = lead.get('phone')
            created = lead.get('created', '')
            created_date = created[:10] if created else None
            if not phone or not created_date:
                continue

            try:
                check_to = (datetime.strptime(created_date, "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")
            except ValueError:
                continue

            wz_result = await workiz_client.find_job_by_phone(phone, created_date, check_to)
            if wz_result.get('error'):
                log.warning(f"Ошибка сверки с Workiz для лида {lead_id}: {wz_result['error']}")
                continue
            workiz_checked_count += 1

            if not wz_result.get('found'):
                workiz_not_found_count += 1
                action = {
                    'type': 'dispute_lsa_lead',
                    'account': 'lsa',
                    'lead_id': lead_id,
                    'description': f"Оспорить лид {lead_id} — не найден джоб в Workiz",
                    'reasoning': (
                        f"У лида нет записи звонка. Сверка с Workiz по номеру "
                        f"{phone} за период {created_date} — {check_to} не нашла ни одного джоба."
                    ),
                    'risks': 'Кредит не гарантирован. Стоит перепроверить перед одобрением.',
                    'urgency': 'low',
                    'urgency_label': 'Низкая',
                    'confidence': 'low',
                    'data_summary': f'Нет записи звонка + нет джоба в Workiz по номеру {phone}',
                    'expected_impact': 'Возможный возврат средств',
                    'requires_approval': True,
                }
                action_id = pending.add(action)
                await _send_approval_card(bot, config.OWNER_CHAT_ID, action_id, action)
                disputed_count += 1
        except Exception as e:
            log.error(f"Ошибка обработки лида {lead_id} в аудите LSA: {e}")
            continue

    return {
        'checked': checked_count,
        'workiz_checked': workiz_checked_count,
        'workiz_not_found': workiz_not_found_count,
        'disputed': disputed_count,
        'total_charged': len(charged_leads),
        'already_submitted': already_submitted_count,
        'period_label': period_label,
    }


async def cmd_audit_calls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.lsa_configured:
        await update.message.reply_text("⚠️ LSA аккаунт не настроен.")
        return

    days = 7
    date_from = None
    date_to = None
    if len(ctx.args) == 2:
        date_from, date_to = ctx.args[0], ctx.args[1]
    elif len(ctx.args) == 1:
        try:
            days = int(ctx.args[0])
            if days <= 0 or days > 90:
                await update.message.reply_text("⚠️ Укажи период от 1 до 90 дней.")
                return
        except ValueError:
            await update.message.reply_text("⚠️ Использование: /auditcalls [дней] или /auditcalls 2026-05-01 2026-05-31")
            return

    limit = 50 if (days > 7 or date_from) else 20

    msg = await update.message.reply_text("🎧 Собираю оплаченные лиды LSA...")
    try:
        stats = await _run_lsa_audit(ctx.bot, progress_msg=msg, days=days, limit=limit, date_from=date_from, date_to=date_to)
    except Exception as e:
        log.error(f"Ошибка ручного аудита LSA: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    period_label = stats.get('period_label', f'последние {days} дней')
    if stats['total_charged'] == 0:
        await msg.edit_text(f"✅ За период {period_label} нет оплаченных LSA-лидов для проверки.")
        return

    text = (
        f"🎧 *Аудит LSA-звонков завершён*\n\n"
        f"Период: {period_label}\n"
        f"Проверено звонков: {stats['checked']} из {stats['total_charged']} оплаченных лидов.\n"
        f"Предложено к оспариванию: {stats['disputed']}."
        + (f"\nУже был отправлен фидбэк ранее (пропущено): {stats['already_submitted']}." if stats.get('already_submitted') else "")
        + (f"\nСверено с Workiz (без записи звонка): {stats['workiz_checked']}, не найден джоб: {stats['workiz_not_found']}." if stats.get('workiz_checked') else "")
    )
    if stats['total_charged'] > limit:
        text += f"\n\n⚠️ Найдено больше лидов ({stats['total_charged']}), чем обработано ({limit}). Запусти команду ещё раз."
    if stats['disputed'] > 0:
        text += "\n\nКарточки одобрения отправлены выше."
    await _safe_edit(msg, text, parse_mode="Markdown")


async def scheduled_lsa_weekly_audit(app):
    if not config.lsa_configured:
        return
    log.info("Еженедельный аудит LSA-звонков")
    try:
        stats = await _run_lsa_audit(app.bot)
        if stats['checked'] > 0:
            text = (
                f"🎧 *Еженедельный аудит LSA-звонков*\n\n"
                f"Проверено звонков: {stats['checked']} из {stats['total_charged']} оплаченных лидов за 7 дней.\n"
                f"Предложено к оспариванию: {stats['disputed']}."
                + (f"\nУже был отправлен фидбэк ранее: {stats['already_submitted']}." if stats.get('already_submitted') else "")
                + (f"\nСверено с Workiz: {stats['workiz_checked']}, не найден джоб: {stats['workiz_not_found']}." if stats.get('workiz_checked') else "")
            )
            if stats['disputed'] > 0:
                text += "\n\nКарточки одобрения отправлены выше."
            await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка еженедельного аудита LSA: {e}")


def _action_ids_verified(action: dict, context_data: dict) -> bool:
    import json as _json
    context_str = _json.dumps(context_data, ensure_ascii=False)
    ids_to_check = []
    a_type = action.get("type")

    if a_type in ("pause_campaign", "enable_campaign", "remove_campaign"):
        if action.get("campaign_id"):
            ids_to_check.append(str(action["campaign_id"]))
    elif a_type == "budget_change":
        if action.get("budget_id"):
            ids_to_check.append(str(action["budget_id"]))
    elif a_type == "update_bid":
        if action.get("resource_name"):
            ids_to_check.append(str(action["resource_name"]))
    elif a_type == "update_final_url":
        if action.get("resource_name"):
            ids_to_check.append(str(action["resource_name"]))
    elif a_type in ("pause_keywords", "enable_keywords"):
        for kw in action.get("keywords", []):
            if kw.get("resource_name"):
                ids_to_check.append(str(kw["resource_name"]))
    elif a_type == "seasonal_adjustments":
        for adj in action.get("adjustments", []):
            if adj.get("campaign_id"):
                ids_to_check.append(str(adj["campaign_id"]))
            if adj.get("budget_id"):
                ids_to_check.append(str(adj["budget_id"]))
    elif a_type == "dispute_lsa_lead":
        if action.get("lead_id"):
            ids_to_check.append(str(action["lead_id"]))

    if not ids_to_check:
        return True

    return all(id_val in context_str for id_val in ids_to_check)


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    question = update.message.text
    if not question or not question.strip():
        return

    chat_id = update.effective_chat.id
    history = await _get_history(ctx, chat_id)

    if not config.google_ads_configured:
        thinking_msg = await update.message.reply_text("🤔 Думаю...")
        answer = await ai_analyst.answer_question(question, history=history)
        answer = _guard_against_hallucinated_execution(answer)
        await _safe_edit(thinking_msg, answer, parse_mode="Markdown")
        await _append_history(ctx, chat_id, question, answer)
        return

    thinking_msg = await update.message.reply_text("🧭 Определяю, что нужно...")
    try:
        classification = await ai_analyst.classify_request(question, history=history)
    except Exception as e:
        log.error(f"Ошибка классификации запроса: {e}")
        classification = {"intent": "chat", "action_type": "none", "days": 0, "data_needed": ["campaigns"], "account": "both"}

    data_needed = classification.get("data_needed", ["campaigns"])
    if isinstance(data_needed, str):
        data_needed = [data_needed] if data_needed != "none" else []
    account_scope = classification.get("account", "both")
    accounts = ["ads", "lsa"] if account_scope == "both" else [account_scope]

    if classification.get("action_type") == "audit_lsa_calls":
        if not config.lsa_configured:
            await thinking_msg.edit_text("⚠️ LSA аккаунт не настроен.")
            return
        cls_date_from = classification.get("date_from") or None
        cls_date_to = classification.get("date_to") or None
        days = classification.get("days") or 7
        try:
            days = max(1, min(90, int(days)))
        except (TypeError, ValueError):
            days = 7
        limit = 50 if (days > 7 or cls_date_from) else 20

        await _safe_edit(thinking_msg, "🎧 Собираю оплаченные лиды LSA...")
        try:
            stats = await _run_lsa_audit(
                ctx.bot, progress_msg=thinking_msg, days=days, limit=limit,
                date_from=cls_date_from, date_to=cls_date_to,
            )
        except Exception as e:
            log.error(f"Ошибка аудита LSA из чата: {e}")
            await thinking_msg.edit_text(f"❌ Ошибка: {e}")
            return

        period_label = stats.get('period_label', f'последние {days} дней')
        if stats['total_charged'] == 0:
            text = f"✅ За период {period_label} нет оплаченных LSA-лидов для проверки."
        else:
            text = (
                f"🎧 *Аудит LSA-звонков завершён*\n\n"
                f"Период: {period_label}\n"
                f"Проверено звонков: {stats['checked']} из {stats['total_charged']} оплаченных лидов.\n"
                f"Предложено к оспариванию: {stats['disputed']}."
                + (f"\nУже был отправлен фидбэк ранее: {stats['already_submitted']}." if stats.get('already_submitted') else "")
                + (f"\nСверено с Workiz: {stats['workiz_checked']}, не найден джоб: {stats['workiz_not_found']}." if stats.get('workiz_checked') else "")
            )
            if stats['total_charged'] > limit:
                text += f"\n\n⚠️ Найдено больше лидов ({stats['total_charged']}), чем обработано ({limit}). Повтори запрос."
            if stats['disputed'] > 0:
                text += "\n\nКарточки одобрения отправлены выше."
        await _safe_edit(thinking_msg, text, parse_mode="Markdown")
        await _append_history(ctx, chat_id, question, text)
        return

    if not data_needed:
        await _safe_edit(thinking_msg, "🤔 Думаю...")
        answer = await ai_analyst.answer_question(question, history=history)
        answer = _guard_against_hallucinated_execution(answer)
        await _safe_edit(thinking_msg, answer, parse_mode="Markdown")
        await _append_history(ctx, chat_id, question, answer)
        return

    await _safe_edit(thinking_msg, "📊 Собираю данные... (~20-60 сек)")

    # Определяем период: если владелец явно назвал календарный период
    # ("за июнь", "с 1 по 10 июля") — используем его. Если нет — по
    # умолчанию берём КАЛЕНДАРНЫЙ МЕСЯЦ ДО СЕГОДНЯ (month-to-date), а НЕ
    # скользящее окно "последние 30 дней" — это даёт стабильные, предсказуемые
    # цифры, не меняющиеся день ото дня из-за сдвига окна.
    period_from = classification.get("period_date_from") or None
    period_to = classification.get("period_date_to") or None
    if not period_from or not period_to:
        today = datetime.now(NY_TZ)
        period_from = today.replace(day=1).strftime("%Y-%m-%d")
        period_to = today.strftime("%Y-%m-%d")

    context_data = {}
    context_data["_period"] = {"date_from": period_from, "date_to": period_to}
    try:
        if "campaigns" in data_needed:
            context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary(date_from=period_from, date_to=period_to)
        if "budgets" in data_needed:
            context_data["budgets"] = {}
            for acc in accounts:
                context_data["budgets"][acc] = await ads_client.get_budget_data(account=acc)
        if "keywords" in data_needed:
            context_data["keywords"] = {}
            for acc in accounts:
                context_data["keywords"][acc] = await ads_client.get_keywords_analysis(account=acc, date_from=period_from, date_to=period_to)
        if "search_terms" in data_needed:
            context_data["search_terms"] = {}
            for acc in accounts:
                context_data["search_terms"][acc] = await ads_client.get_search_terms(account=acc)
        if "ad_performance" in data_needed:
            # LSA не имеет "объявлений" в этом смысле (Google сам формирует
            # объявление из профиля) — собираем только для Google Ads
            context_data["ad_performance"] = {}
            for acc in accounts:
                if acc == "lsa":
                    continue
                context_data["ad_performance"][acc] = await ads_client.get_ad_performance(account=acc)
        if "landing_pages" in data_needed:
            # Проверка лендингов требует списка ключевых слов (URL берётся
            # с конкретного ключа/его ad group) — подтягиваем keywords
            # автоматически, если ещё не собраны в этом запросе.
            context_data.setdefault("keywords", {})
            context_data["landing_pages"] = {}
            for acc in accounts:
                if acc == "lsa":
                    continue
                if acc not in context_data["keywords"]:
                    context_data["keywords"][acc] = await ads_client.get_keywords_analysis(account=acc)
                kw_list = context_data["keywords"][acc].get("keywords", [])
                context_data["landing_pages"][acc] = await ads_client.get_landing_pages_for_keywords(kw_list, account=acc)
        if "lsa_leads" in data_needed:
            context_data["lsa_leads"] = await ads_client.get_lsa_leads(account="lsa")
        if "seasonal" in data_needed:
            context_data["season"] = ads_client.get_current_season_recommendations()
            context_data.setdefault("budgets", {})
            for acc in accounts:
                context_data["budgets"][acc] = await ads_client.get_budget_data(account=acc)
        if not context_data:
            context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary()
    except Exception as e:
        log.error(f"Ошибка сбора данных для чата: {e}")
        await thinking_msg.edit_text(f"❌ Ошибка получения данных: {e}")
        return

    await _safe_edit(thinking_msg, "🤔 Анализирую...")
    action_type = classification.get("action_type", "none")
    try:
        result = await ai_analyst.chat_action(question, context_data, action_type, history=history)
    except Exception as e:
        log.error(f"Ошибка chat_action: {e}")
        await thinking_msg.edit_text(f"❌ Ошибка: {e}")
        return

    if "reply" not in result:
        log.error(f"chat_action вернул результат без ключа 'reply': {result}")
    reply = result.get("reply", "⚠️ Ответ получен, но в неожиданном формате (без текста). Попробуй переформулировать вопрос.")
    reply = _guard_against_hallucinated_execution(reply)
    await _safe_edit(thinking_msg, reply, parse_mode="Markdown")
    await _append_history(ctx, chat_id, question, reply)

    blocked_actions = 0
    proposed = result.get("proposed_actions", [])
    MAX_ACTIONS_PER_RESPONSE = 8
    if len(proposed) > MAX_ACTIONS_PER_RESPONSE:
        log.warning(f"ИИ предложил {len(proposed)} действий за раз — обрезаю до {MAX_ACTIONS_PER_RESPONSE}")
        proposed = proposed[:MAX_ACTIONS_PER_RESPONSE]
        await ctx.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=(
                f"ℹ️ Предложено больше {MAX_ACTIONS_PER_RESPONSE} действий за раз — "
                f"показаны первые {MAX_ACTIONS_PER_RESPONSE}. Напиши \"продолжи\", "
                f"чтобы получить оставшиеся."
            ),
        )
    for action in proposed:
        try:
            action.setdefault("account", accounts[0])
            action.setdefault("data_summary", action.get("reasoning", ""))
            action.setdefault("expected_impact", "")
            action.setdefault("requires_approval", True)

            if not _action_ids_verified(action, context_data):
                log.warning(f"Действие заблокировано — ID не найдены в свежих данных: {action}")
                blocked_actions += 1
                continue

            action_id = pending.add(action)
            await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
        except Exception as e:
            log.error(f"Ошибка создания карточки одобрения из чата: {e}")

    if blocked_actions:
        await ctx.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=(
                f"⚠️ {blocked_actions} предложенное действие заблокировано: "
                f"ID не найдены в свежих данных этого запроса. "
                f"Запроси данные заново явной командой."
            ),
        )


# ── Обработка кнопок ────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != config.OWNER_CHAT_ID:
        return

    data = query.data
    parts = data.split(":")

    if len(parts) == 2:
        cmd, param = parts
        chat_id = query.message.chat_id  # для сохранения в историю

        if cmd == "approve":
            action = pending.approve(param)
            if not action:
                await query.edit_message_text("⚠️ Действие не найдено или уже выполнено.")
                return
            stale_note = ""
            if action.get("stale_when_approved"):
                stale_note = (
                    f"\n\n⚠️ *Внимание:* эта карточка была создана более "
                    f"{STALE_AFTER_HOURS} часов назад. "
                    f"Данные могли устареть (кто-то мог изменить кампанию вручную). "
                    f"Рекомендую перепроверить перед следующим одобрением."
                )
                log.warning(f"Одобрена устаревшая карточка {param}: {action.get('description')}")
            await query.edit_message_text(f"⏳ Применяю: {action['description']}...{stale_note}", parse_mode="Markdown")
            try:
                result = await ads_client.execute_action(action)
            except Exception as e:
                log.error(f"Ошибка выполнения {param}: {e}")
                friendly = _translate_google_ads_error(e)
                await query.edit_message_text(f"❌ Ошибка: {friendly}")
                return

            await query.edit_message_text(f"🔍 Перепроверяю фактическое состояние...")
            # Небольшая пауза перед проверкой: у Google Ads API бывает
            # задержка синхронизации между mutate и последующим search —
            # без неё verify_action может ошибочно показать "не применилось",
            # хотя изменение реально прошло.
            await asyncio.sleep(3)
            try:
                verification = await ads_client.verify_action(action)
            except Exception as e:
                log.error(f"Ошибка перепроверки {param}: {e}")
                verification = {"verified": None, "note": f"Ошибка перепроверки: {e}"}

            log.info(f"VERIFY_ACTION RESULT: action_id={param}, type={action.get('type')}, verification={verification}")
            verified = verification.get("verified")
            pending.record_execution_result(param, verified)
            if verified is True:
                text = (
                    f"✅ *Выполнено и подтверждено:* {action['description']}\n\n"
                    f"{result.get('summary', str(result))}\n\n"
                    f"_Подтверждено повторным запросом к Google Ads API — изменение реально применилось._"
                )
            elif verified is False:
                text = (
                    f"⚠️ *Расхождение после выполнения:* {action['description']}\n\n"
                    f"API не вернул ошибку, но перепроверка показала несоответствие:\n`{verification}`\n\n"
                    f"Рекомендую проверить вручную в Google Ads. Я также автоматически "
                    f"перепроверю это ещё раз через сутки и сообщу результат."
                )
            else:
                text = (
                    f"📤 *Отправлено в Google Ads:* {action['description']}\n\n"
                    f"{result.get('summary', str(result))}\n\n"
                    f"⚠️ _API принял запрос без ошибок, но автоматическое подтверждение "
                    f"результата недоступно для этого типа действия — это НЕ гарантия, "
                    f"что изменение реально применилось так, как ожидалось. Я перепроверю "
                    f"это ещё раз через сутки и напишу, подтвердилось ли на самом деле._"
                )
            await _safe_edit(query, text, parse_mode="Markdown")
            return

        elif cmd == "reject":
            action = pending.reject(param)
            if action:
                await query.edit_message_text(f"❌ Отклонено: {action['description']}")
            return

        account = param
        account_label = {"ads": "Google Ads (936)", "lsa": "LSA (667)", "both": "Оба аккаунта"}.get(account, account)

        if cmd == "report":
            await query.edit_message_text(f"📊 Отчёт: {account_label}... (~30 сек)")
            try:
                if account == "both":
                    data_result = await ads_client.get_both_accounts_summary()
                    text = _format_both_summary(data_result)
                else:
                    data_result = await ads_client.get_performance_report(days=7, account=account)
                    analysis = await ai_analyst.analyze_performance(data_result)
                    text = report_gen.format_performance_report(data_result, analysis) if hasattr(report_gen, 'format_performance_report') else _format_performance(data_result, analysis)
                await _safe_edit(query, text, parse_mode="Markdown")
                # Сохраняем в историю
                await _save_cmd_result(ctx, chat_id, f"/report ({account_label})", text)
            except Exception as e:
                log.error(f"Ошибка report callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "audit":
            await query.edit_message_text(f"🔍 Аудит: {account_label}... (~40 сек)")
            try:
                if account == "both":
                    data_result = await ads_client.get_both_accounts_summary()
                    text = _format_both_summary(data_result)
                    analysis = {}
                else:
                    data_result = await ads_client.get_full_audit_data(account=account)
                    analysis = await ai_analyst.analyze_campaigns(data_result)
                    text = _format_audit(data_result, analysis)
                await _safe_edit(query, text, parse_mode="Markdown")
                # Сохраняем в историю
                await _save_cmd_result(ctx, chat_id, f"/audit ({account_label})", text)
                if account != "both":
                    for action in analysis.get("recommendations", []):
                        action["account"] = account
                        action_id = pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
            except Exception as e:
                log.error(f"Ошибка audit callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "budget":
            await query.edit_message_text(f"💰 Бюджет: {account_label}... (~20 сек)")
            try:
                if account == "both":
                    ads_b = await ads_client.get_budget_data(account="ads")
                    lsa_b = await ads_client.get_budget_data(account="lsa")
                    text = f"*Бюджеты — Google Ads:*\n• Дневной: ${ads_b.get('total_daily_budget', 0):.2f}\n• Сегодня: ${ads_b.get('total_spend_today', 0):.2f}\n\n"
                    text += f"*Бюджеты — LSA:*\n• Дневной: ${lsa_b.get('total_daily_budget', 0):.2f}\n• Сегодня: ${lsa_b.get('total_spend_today', 0):.2f}"
                else:
                    data_result = await ads_client.get_budget_data(account=account)
                    analysis = await ai_analyst.analyze_budget(data_result)
                    text = _format_budget(data_result, analysis)
                    if analysis.get("budget_recommendation"):
                        rec = analysis["budget_recommendation"]
                        action = {
                            "type": "budget_change",
                            "account": account,
                            "description": f"Изменить бюджет: {rec.get('campaign_name')}",
                            "reasoning": rec.get("reasoning", ""),
                            "data_summary": f"Текущий: ${rec.get('current_budget')}/день",
                            "expected_impact": rec.get("expected_result", ""),
                            "urgency": "medium",
                            "urgency_label": "Средняя",
                            "risks": "Изменение бюджета — обратимо",
                            "proposed_budget": rec.get("proposed_budget"),
                            "budget_id": rec.get("budget_id"),
                        }
                        action_id = pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                await _safe_edit(query, text, parse_mode="Markdown")
                # Сохраняем в историю
                await _save_cmd_result(ctx, chat_id, f"/budget ({account_label})", text)
            except Exception as e:
                log.error(f"Ошибка budget callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "keywords":
            await query.edit_message_text(f"🔑 Ключевые слова: {account_label}... (~30 сек)")
            try:
                accounts_list = ["ads"] if account in ("lsa", "both") else [account]
                if account == "lsa":
                    result_text = (
                        "ℹ️ *LSA не использует ключевые слова*\n\n"
                        "Local Services Ads работает иначе — Google сам определяет, "
                        "кому показывать объявления, на основе категории услуги и профиля. "
                        "Ключевые слова доступны только для Google Ads (936)."
                    )
                    await _safe_edit(query, result_text, parse_mode="Markdown")
                    await _save_cmd_result(ctx, chat_id, f"/keywords ({account_label})", result_text)
                    return
                texts = []
                for acc in accounts_list:
                    data_result = await ads_client.get_keywords_analysis(account=acc)
                    analysis = await ai_analyst.analyze_keywords(data_result)
                    texts.append(f"*Google Ads ({len(data_result.get('keywords', []))} ключей):*\n{analysis.get('summary', 'Нет данных')}")
                    for action in _build_keyword_actions(analysis, acc):
                        action_id = pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                if account == "both":
                    texts.append("ℹ️ *LSA:* ключевые слова не используются (Google определяет аудиторию автоматически)")
                result_text = "\n\n".join(texts)
                await _safe_edit(query, result_text, parse_mode="Markdown")
                # Сохраняем в историю — ключевой фикс: агент теперь помнит результат /keywords
                await _save_cmd_result(ctx, chat_id, f"/keywords ({account_label})", result_text)
            except Exception as e:
                log.error(f"Ошибка keywords callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "negatives":
            await query.edit_message_text(f"🚫 Минус-слова: {account_label}... (~30 сек)")
            try:
                # LSA не поддерживает минус-слова (как и обычные ключевые слова) —
                # Google сам определяет аудиторию. Обрабатываем только Google Ads.
                accounts_list = ["ads"] if account in ("lsa", "both") else [account]
                if account == "lsa":
                    result_text = (
                        "ℹ️ *LSA не использует минус-слова*\n\n"
                        "Local Services Ads работает иначе — Google сам определяет, "
                        "кому показывать объявления. Минус-слова доступны только для "
                        "Google Ads (936)."
                    )
                    await _safe_edit(query, result_text, parse_mode="Markdown")
                    await _save_cmd_result(ctx, chat_id, f"/negatives ({account_label})", result_text)
                    return
                summary_parts = []
                any_cards_created = False
                for acc in accounts_list:
                    data_result = await ads_client.get_search_terms(account=acc)
                    analysis = await ai_analyst.find_negative_keywords(data_result)
                    negatives = analysis.get("suggested_negatives", [])
                    acc_label = "Google Ads" if acc == "ads" else "LSA"
                    text = f"*Минус-слова {acc_label}:* {len(negatives)} найдено\n{analysis.get('summary', '')}"
                    summary_parts.append(text)
                    await _safe_send(ctx.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
                    if negatives:
                        action = {
                            "type": "add_negative_keywords",
                            "account": acc,
                            "description": f"Добавить {len(negatives)} минус-слов ({acc_label})",
                            "reasoning": analysis.get("summary", ""),
                            "data_summary": f"{len(negatives)} нерелевантных запросов",
                            "expected_impact": "Снижение нецелевых кликов",
                            "urgency": "medium",
                            "urgency_label": "Средняя",
                            "risks": "Проверь список перед применением",
                            "negatives": negatives,
                        }
                        try:
                            action_id = pending.add(action)
                            await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                            any_cards_created = True
                        except Exception as ve:
                            # PendingActionValidationError или любая другая ошибка валидации —
                            # не прерываем весь цикл, просто пропускаем эту карточку
                            log.warning(f"Карточка минус-слов не создана ({acc}): {ve}")
                if account == "both":
                    await _safe_send(
                        ctx.bot, config.OWNER_CHAT_ID,
                        "ℹ️ *LSA:* минус-слова не применимы (Google определяет аудиторию автоматически)",
                        parse_mode="Markdown",
                    )
                if any_cards_created:
                    await query.edit_message_text("✅ Анализ завершён — карточки одобрения отправлены выше.")
                else:
                    await query.edit_message_text(
                        "✅ Анализ завершён — нерелевантных запросов не найдено, "
                        "минус-слова добавлять не требуется. Карточек одобрения нет."
                    )
                await _save_cmd_result(ctx, chat_id, f"/negatives ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка negatives callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "competitors":
            await query.edit_message_text(f"🏆 Конкуренты: {account_label}... (~30 сек)")
            try:
                accounts_list = ["ads", "lsa"] if account == "both" else [account]
                summary_parts = []
                for acc in accounts_list:
                    data_result = await ads_client.get_auction_insights(account=acc)
                    if not data_result.get("competitors"):
                        await _safe_send(ctx.bot, config.OWNER_CHAT_ID, f"ℹ️ Данных аукциона для {'Google Ads' if acc == 'ads' else 'LSA'} пока нет.", parse_mode=None)
                        continue
                    analysis = await ai_analyst.analyze_auction_insights(data_result)
                    text = f"*{'Google Ads' if acc == 'ads' else 'LSA'} — конкуренты:*\n{analysis.get('position_summary', '')}\n\n{analysis.get('summary', '')}"
                    summary_parts.append(text)
                    await _safe_send(ctx.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
                await query.edit_message_text("✅ Анализ конкурентов завершён.")
                if summary_parts:
                    await _save_cmd_result(ctx, chat_id, f"/competitors ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка competitors callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "abtest":
            await query.edit_message_text(f"🧪 A/B тест: {account_label}... (~30 сек)")
            try:
                accounts_list = ["ads", "lsa"] if account == "both" else [account]
                summary_parts = []
                for acc in accounts_list:
                    data_result = await ads_client.get_ad_performance(account=acc)
                    analysis = await ai_analyst.analyze_ab_test(data_result)
                    results = analysis.get("ab_results", [])
                    acc_label = "Google Ads" if acc == "ads" else "LSA"
                    text = f"*A/B тест {acc_label}:* {len(results)} групп\n{analysis.get('summary', 'Нет данных')}"
                    summary_parts.append(text)
                    await _safe_send(ctx.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
                await query.edit_message_text("✅ A/B анализ завершён.")
                if summary_parts:
                    await _save_cmd_result(ctx, chat_id, f"/abtest ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка abtest callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "seasonal":
            await query.edit_message_text(f"🍂 Сезонный план: {account_label}... (~30 сек)")
            try:
                accounts_list = ["ads", "lsa"] if account == "both" else [account]
                season_data = ads_client.get_current_season_recommendations()
                summary_parts = []
                for acc in accounts_list:
                    budget_data = await ads_client.get_budget_data(account=acc)
                    action_plan = await ai_analyst.build_seasonal_action(season_data, budget_data.get("campaigns", []))
                    acc_label = "Google Ads" if acc == "ads" else "LSA"
                    text = f"*Сезон {season_data['season_name']} — {acc_label}:*\n{action_plan.get('summary', '')}\n{action_plan.get('expected_impact', '')}"
                    summary_parts.append(text)
                    await _safe_send(ctx.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
                    if action_plan.get("adjustments"):
                        action = {
                            "type": "seasonal_adjustments",
                            "account": acc,
                            "description": f"Сезонные корректировки {season_data['season_name']} — {acc_label}",
                            "reasoning": season_data["reason"],
                            "data_summary": f"{len(action_plan['adjustments'])} кампаний",
                            "expected_impact": action_plan.get("expected_impact", ""),
                            "urgency": "medium",
                            "urgency_label": "Средняя",
                            "risks": "Изменения обратимы",
                            "adjustments": action_plan["adjustments"],
                        }
                        action_id = pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                await query.edit_message_text("✅ Сезонный план готов — карточки отправлены выше.")
                if summary_parts:
                    await _save_cmd_result(ctx, chat_id, f"/seasonal ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка seasonal callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")


# ── Вспомогательные форматтеры ───────────────────────────

_HALLUCINATED_EXECUTION_PHRASES = [
    "выполнено и подтверждено",
    "подтверждено повторным запросом",
    "изменение реально применилось",
    "ставка обновлена:",
    "выполнено и перепроверено",
]


def _guard_against_hallucinated_execution(reply: str) -> str:
    """
    Защитный код-level фильтр (второй эшелон защиты помимо промпта):
    свободный текстовый ответ модели (chat_action/answer_question) не
    должен заявлять о реальном выполнении действия — это язык, который
    зарезервирован за кодом handle_callback (approve), где execute_action()
    и verify_action() реально вызываются. Если модель всё же проговорилась
    такой фразой в свободном тексте, добавляем явное предупреждение
    владельцу, чтобы он не принял это за подтверждённый факт.
    """
    lower = reply.lower()
    if any(phrase in lower for phrase in _HALLUCINATED_EXECUTION_PHRASES):
        log.warning(f"Обнаружена потенциально ложная формулировка о выполнении в свободном тексте: {reply[:200]!r}")
        reply = (
            "⚠️ *Внимание:* следующий текст сгенерирован в свободном чате и МОГ ошибочно "
            "заявить о выполнении действия, хотя это НЕ гарантия — реальное выполнение "
            "подтверждается только через карточку одобрения с кнопками. Проверь фактический "
            "статус через /audit или /budget при сомнении.\n\n"
            + reply
        )
    return reply


def _format_both_summary(data: dict) -> str:
    combined = data.get('combined', {})
    ads = data.get('google_ads', {})
    lsa = data.get('lsa', {})
    period_from = ads.get('date_from', '')
    period_to = ads.get('date_to', '')
    period_label = f" — {period_from} — {period_to}" if period_from and period_to else ""
    text = f"📊 *Сводка по всем аккаунтам{period_label}*\n\n"
    text += f"💰 Итого: ${combined.get('total_spend', 0):.2f}\n"
    text += f"📞 Конверсии: {combined.get('total_conversions', 0):.0f}\n"
    if combined.get('avg_cpa'):
        text += f"💵 Средний CPA: ${combined['avg_cpa']:.2f}\n\n"
    text += f"*Google Ads (936):* ${ads.get('total_spend', 0):.2f} / {ads.get('total_conversions', 0):.0f} конв.\n"
    if lsa and not lsa.get('error'):
        text += f"*LSA (667):* ${lsa.get('total_spend', 0):.2f} / {lsa.get('total_conversions', 0):.0f} конв."
    return text


async def _get_thumbtack_summary_line(days: int = 30) -> str:
    """
    Короткая строка по Thumbtack (расход, джобы, выручка) за период —
    используется в ежедневных отчётах, чтобы канал не оставался невидимым
    в промежутках между еженедельной детальной проверкой (scheduled_thumbtack_check).
    """
    try:
        date_to = datetime.now(NY_TZ).strftime("%Y-%m-%d")
        date_from = (datetime.now(NY_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        thumbtack_cost = round(config.THUMBTACK_WEEKLY_BUDGET / 7 * days, 2)
        result = await workiz_client.get_jobs_by_source("Thumbtack", date_from, date_to)
        jobs = result.get('total_jobs', 0)
        revenue = result.get('total_revenue', 0)
        line = f"\n*Thumbtack (расчётно):* ${thumbtack_cost:.2f} / {jobs} джоб(ов)"
        if jobs > 0:
            line += f" / выручка ${revenue:.2f}"
        elif thumbtack_cost > 0:
            line += " ⚠️ бюджет потрачен, джобов не найдено"
        return line
    except Exception as e:
        log.error(f"Ошибка получения сводки Thumbtack: {e}")
        return ""


def _format_audit(data: dict, analysis: dict) -> str:
    campaigns = data.get('campaigns', [])
    text = f"🔍 *Аудит {data.get('account', '').upper()} — {data.get('date_from')} — {data.get('date_to')}*\n\n"
    text += f"💰 Расход: ${data.get('total_spend', 0):.2f}\n"
    text += f"📞 Конверсии: {data.get('total_conversions', 0):.0f}\n"
    text += f"🖱 Клики: {data.get('total_clicks', 0)}\n"
    text += f"📋 Кампаний: {len(campaigns)}\n\n"
    if analysis.get('summary'):
        text += f"*Вывод:* {analysis['summary']}\n\n"
    findings = analysis.get('key_findings', [])
    if findings:
        text += "*Ключевые находки:*\n"
        for f in findings[:3]:
            text += f"• {f}\n"
    return text


def _format_performance(data: dict, analysis: dict) -> str:
    text = f"📊 *Отчёт за {data.get('days', 7)} дней ({data.get('account', '').upper()})*\n\n"
    text += f"💰 Расход: ${data.get('total_spend', 0):.2f}\n"
    text += f"📞 Конверсии: {data.get('total_conversions', 0):.0f}\n"
    if data.get('avg_cpa'):
        text += f"💵 CPA: ${data['avg_cpa']:.2f}\n"
    if analysis.get('trend'):
        trend_emoji = {"improving": "📈", "stable": "➡️", "declining": "📉"}.get(analysis['trend'], "")
        text += f"\n{trend_emoji} *Тренд:* {analysis.get('trend_explanation', analysis['trend'])}\n"
    for insight in analysis.get('insights', [])[:3]:
        text += f"• {insight}\n"
    return text


def _format_budget(data: dict, analysis: dict) -> str:
    text = f"💰 *Бюджет {data.get('account', '').upper()}*\n\n"
    text += f"• Дневной бюджет: ${data.get('total_daily_budget', 0):.2f}\n"
    text += f"• Потрачено сегодня: ${data.get('total_spend_today', 0):.2f}\n\n"
    health = analysis.get('budget_health', 'unknown')
    emoji = {"good": "✅", "warning": "⚠️", "critical": "🚨"}.get(health, "ℹ️")
    text += f"{emoji} *Статус:* {analysis.get('budget_summary', health)}\n"
    return text


def _build_keyword_actions(analysis: dict, account: str) -> list:
    actions = []
    weak = analysis.get('weak_keywords', [])
    if weak:
        pause_kws = [k for k in weak if k.get('recommendation') == 'пауза' and k.get('resource_name')]
        if pause_kws:
            actions.append({
                "type": "pause_keywords",
                "account": account,
                "description": f"Поставить на паузу {len(pause_kws)} слабых ключей",
                "reasoning": analysis.get('summary', ''),
                "data_summary": f"{len(pause_kws)} ключей с низким CTR",
                "expected_impact": "Снижение нецелевого расхода",
                "urgency": "medium",
                "urgency_label": "Средняя",
                "risks": "Проверь список перед применением",
                "keywords": pause_kws,
            })
    return actions


# ── Расписание ───────────────────────────────────────────

async def scheduled_morning_report(app):
    if not config.google_ads_configured:
        return
    log.info("Утренний отчёт (оба аккаунта)")
    try:
        today = datetime.now(NY_TZ)
        month_from = today.replace(day=1).strftime("%Y-%m-%d")
        month_to = today.strftime("%Y-%m-%d")
        data = await ads_client.get_both_accounts_summary(date_from=month_from, date_to=month_to)
        text = f"☀️ *Утренний отчёт — {today.strftime('%d.%m.%Y')}*\n\n"
        text += _format_both_summary(data)
        thumbtack_days = (today - today.replace(day=1)).days + 1
        text += await _get_thumbtack_summary_line(days=thumbtack_days)
        await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка утреннего отчёта: {e}")
        await app.bot.send_message(chat_id=config.OWNER_CHAT_ID, text=f"⚠️ Ошибка утреннего отчёта: {e}")


async def scheduled_budget_check(app):
    if not config.google_ads_configured:
        return
    log.info("Дневная проверка бюджетов")
    try:
        for account in ["ads", "lsa"]:
            data = await ads_client.get_budget_data(account=account)
            analysis = await ai_analyst.analyze_budget(data)
            if analysis.get("budget_recommendation"):
                rec = analysis["budget_recommendation"]
                action = {
                    "type": "budget_change",
                    "account": account,
                    "description": f"Изменить бюджет: {rec.get('campaign_name')}",
                    "reasoning": rec.get("reasoning", ""),
                    "data_summary": f"Текущий: ${rec.get('current_budget')}/день",
                    "expected_impact": rec.get("expected_result", ""),
                    "urgency": "medium",
                    "urgency_label": "Средняя",
                    "risks": "Обратимо",
                    "proposed_budget": rec.get("proposed_budget"),
                    "budget_id": rec.get("budget_id"),
                }
                action_id = pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
    except Exception as e:
        log.error(f"Ошибка проверки бюджетов: {e}")


async def scheduled_evening_summary(app):
    if not config.google_ads_configured:
        return
    log.info("Вечерний итог")
    try:
        today = datetime.now(NY_TZ)
        month_from = today.replace(day=1).strftime("%Y-%m-%d")
        month_to = today.strftime("%Y-%m-%d")
        data = await ads_client.get_both_accounts_summary(date_from=month_from, date_to=month_to)
        text = f"🌙 *Итоги дня — {today.strftime('%d.%m.%Y')}*\n\n"
        text += _format_both_summary(data)
        thumbtack_days = (today - today.replace(day=1)).days + 1
        text += await _get_thumbtack_summary_line(days=thumbtack_days)
        await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка вечернего итога: {e}")


async def scheduled_weekly_audit(app):
    if not config.google_ads_configured:
        return
    log.info("Еженедельный аудит")
    try:
        for account in ["ads", "lsa"]:
            data = await ads_client.get_full_audit_data(account=account)
            analysis = await ai_analyst.analyze_campaigns(data)
            text = f"📋 *Аудит {'Google Ads' if account == 'ads' else 'LSA'} — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n"
            text += _format_audit(data, analysis)
            await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
            for action in analysis.get("recommendations", []):
                action["account"] = account
                action_id = pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
    except Exception as e:
        log.error(f"Ошибка аудита: {e}")


async def scheduled_competitors_check(app):
    if not config.google_ads_configured:
        return
    log.info("Анализ конкурентов")
    try:
        data = await ads_client.get_auction_insights(account="ads")
        if not data.get("competitors"):
            return
        analysis = await ai_analyst.analyze_auction_insights(data)
        text = f"🏆 *Конкуренты — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n{analysis.get('position_summary', '')}\n{analysis.get('summary', '')}"
        await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка анализа конкурентов: {e}")


async def scheduled_ab_test_check(app):
    if not config.google_ads_configured:
        return
    log.info("Проверка A/B тестов")
    try:
        data = await ads_client.get_ad_performance(account="ads")
        analysis = await ai_analyst.analyze_ab_test(data)
        if not analysis.get("ab_results"):
            return
        text = f"🧪 *A/B тест — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n{analysis.get('summary', '')}"
        await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка A/B теста: {e}")


async def scheduled_seasonal_check(app):
    if not config.google_ads_configured:
        return
    log.info("Сезонная оптимизация")
    try:
        season_data = ads_client.get_current_season_recommendations()
        for account in ["ads", "lsa"]:
            budget_data = await ads_client.get_budget_data(account=account)
            action_plan = await ai_analyst.build_seasonal_action(season_data, budget_data.get("campaigns", []))
            if action_plan.get("adjustments"):
                action = {
                    "type": "seasonal_adjustments",
                    "account": account,
                    "description": f"Сезонные корректировки {season_data['season_name']}",
                    "reasoning": season_data["reason"],
                    "data_summary": f"{len(action_plan['adjustments'])} кампаний",
                    "expected_impact": action_plan.get("expected_impact", ""),
                    "urgency": "medium",
                    "urgency_label": "Средняя",
                    "risks": "Обратимо",
                    "adjustments": action_plan["adjustments"],
                }
                action_id = pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
    except Exception as e:
        log.error(f"Ошибка сезонной оптимизации: {e}")


# ── main ─────────────────────────────────────────────────

async def global_error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Глобальный обработчик — ловит ЛЮБОЕ необработанное исключение в любом
    хендлере. Без него (как было раньше) любая непойманная ошибка просто
    тихо убивает задачу: сообщение "Анализирую..." остаётся висеть
    навсегда, владелец не получает вообще никакой реакции и не понимает,
    что что-то сломалось. Это и было причиной множественных "зависаний"
    за сессию (например, telegram.error.BadRequest: Message_too_long,
    когда ответ ИИ превышал лимит Telegram в 4096 символов).
    """
    log.error(f"Необработанное исключение: {ctx.error}", exc_info=ctx.error)
    try:
        if config.OWNER_CHAT_ID:
            error_text = str(ctx.error)[:300]
            await ctx.bot.send_message(
                chat_id=config.OWNER_CHAT_ID,
                text=(
                    f"⚠️ *Внутренняя ошибка бота:* `{type(ctx.error).__name__}`\n"
                    f"{error_text}\n\n"
                    f"Если видел зависшее \"Анализирую...\" перед этим — вот что "
                    f"реально произошло. Попробуй переформулировать запрос короче."
                ),
                parse_mode="Markdown",
            )
    except Exception as notify_error:
        log.error(f"Не удалось уведомить владельца об ошибке: {notify_error}")


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Fallback для любой команды без зарегистрированного обработчика.
    Часто срабатывает случайно — ИИ в тексте иногда упоминает слаг
    страницы сайта вида "/hvac" или "/washer", Telegram автоматически
    делает такой текст кликабельной "командой", и без этого fallback
    владелец при нажатии не получал вообще никакой реакции от бота.
    """
    if not _is_owner(update):
        return
    command_text = update.message.text if update.message else "?"
    await update.message.reply_text(
        f"ℹ️ Команда {command_text} не существует.\n\n"
        f"Возможно, это была ссылка на страницу сайта, которую Telegram "
        f"ошибочно распознал как команду бота. Список реальных команд — /start.",
    )


async def _on_startup(app):
    await init_db()


def main():
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(_on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("negatives", cmd_negatives))
    app.add_handler(CommandHandler("competitors", cmd_competitors))
    app.add_handler(CommandHandler("abtest", cmd_abtest))
    app.add_handler(CommandHandler("seasonal", cmd_seasonal))
    app.add_handler(CommandHandler("both", cmd_both))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("reviewnegatives", cmd_review_negatives))
    app.add_handler(CommandHandler("checklead", cmd_checklead))
    app.add_handler(CommandHandler("auditcalls", cmd_audit_calls))
    app.add_handler(CommandHandler("roas", cmd_roas))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("checkkeyword", cmd_check_keyword))
    app.add_handler(CommandHandler("checknegatives", cmd_check_negatives))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    # Fallback ПОСЛЕДНИМ: ловит любую команду, для которой нет обработчика
    # выше (например, ИИ иногда упоминает в тексте слаги вроде "/hvac",
    # "/washer" как название страницы сайта — Telegram автоматически
    # превращает такой текст в кликабельную команду, и без этого fallback
    # владелец при нажатии получал полную тишину без всякой реакции бота).
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_error_handler(global_error_handler)

    scheduler = AsyncIOScheduler(timezone=NY_TZ)
    scheduler.add_job(scheduled_morning_report,   "cron", hour=8,  minute=0,  args=[app])
    scheduler.add_job(scheduled_budget_check,     "cron", hour=14, minute=0,  args=[app])
    scheduler.add_job(scheduled_evening_summary,  "cron", hour=21, minute=0,  args=[app])
    scheduler.add_job(scheduled_weekly_audit,     "cron", day_of_week="mon", hour=9,  minute=0,  args=[app])
    scheduler.add_job(scheduled_competitors_check,"cron", day_of_week="sun", hour=9,  minute=30, args=[app])
    scheduler.add_job(scheduled_ab_test_check,    "cron", day_of_week="wed", hour=10, minute=0,  args=[app])
    scheduler.add_job(scheduled_seasonal_check,   "cron", day=1,             hour=8,  minute=0,  args=[app])
    scheduler.add_job(scheduled_lsa_weekly_audit, "cron", day_of_week="mon", hour=8,  minute=30, args=[app])
    scheduler.add_job(scheduled_weekly_roas,      "cron", day_of_week="mon", hour=9,  minute=15, args=[app])
    scheduler.add_job(scheduled_thumbtack_check,  "cron", day_of_week="mon", hour=9,  minute=20, args=[app])
    scheduler.add_job(scheduled_purge_pending,    "cron", hour=3,  minute=0,  args=[app])
    scheduler.add_job(scheduled_reverify_executed_actions, "interval", hours=4, args=[app])
    scheduler.start()

    log.info("BCHD Marketer Agent v5 запущен (история команд включена)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
