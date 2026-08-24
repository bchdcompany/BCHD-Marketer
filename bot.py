"""
BCHD Marketer Agent — Telegram бот
Google Ads оптимизация с AI-анализом
v6 — голосовые сообщения (Groq Whisper), фото/скриншоты (Claude Vision),
     жёсткая блокировка «зомби-фактов» BCHD/2 из истории,
     /clearmemory теперь чистит ВСЮ историю (все chat_id).
"""

import asyncio
import io
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta

import asyncpg
import base64
import httpx
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
try:
    from strategy import StrategyMemory
    _strategy_available = True
except ImportError:
    _strategy_available = False
try:
    from gbp_client import GBPClient, init_gbp_client
    _gbp_available = True
except ImportError:
    _gbp_available = False
from ai_analyst import AIAnalyst
try:
    import email_agent as _email_agent
    _email_agent_available = True
except ImportError:
    _email_agent_available = False
try:
    from email_sender import send_campaign, generate_banner_base64, build_html_email
    _email_available = True
except ImportError:
    _email_available = False
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
strategy_memory = StrategyMemory() if _strategy_available else None
gbp_client_inst = init_gbp_client(config) if _gbp_available else None
_GBP_PHOTO_PENDING: dict = {}  # chat_id -> {action_id, post_text}

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
        # Передаём пул в pending — карточки теперь в Postgres
        pending.set_pool(pool)
        await pending._ensure_table()
        if strategy_memory:
            strategy_memory.set_pool(pool)
            await strategy_memory.ensure_tables()
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


async def _send_long_message(bot, chat_id: int, text: str, parse_mode=None):
    """Разбивает длинное сообщение на части и отправляет последовательно."""
    if len(text) <= 3800:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return
    parts = []
    while text:
        if len(text) <= 3800:
            parts.append(text)
            break
        # Ищем последний перенос строки до 3800
        cut = text.rfind("\n", 0, 3800)
        if cut == -1:
            cut = 3800
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    for i, part in enumerate(parts):
        prefix = f"📄 Часть {i+1}/{len(parts)}:\n\n" if len(parts) > 1 else ""
        await bot.send_message(chat_id=chat_id, text=prefix + part, parse_mode=parse_mode)

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
            fallback = text[:1000] + "\n\n[остальная часть обрезана]"
            return await bot.send_message(chat_id=chat_id, text=fallback)


async def _safe_edit(target, text: str, parse_mode="Markdown", **kwargs):
    text = _truncate_for_telegram(text)
    if text.startswith("❌"):
        parse_mode = None
        kwargs.pop("parse_mode", None)
    edit_fn = getattr(target, "edit_message_text", None) or getattr(target, "edit_text", None)
    if edit_fn is None:
        raise AttributeError(f"{type(target)} не поддерживает редактирование текста")
    try:
        return await edit_fn(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            # Безобидно: новое содержимое совпадает со старым — Telegram
            # просто отказывается делать no-op запрос. Ничего не сломано,
            # результат уже виден пользователю как надо.
            return None
        log.warning(f"Не удалось отредактировать с parse_mode={parse_mode} ({e}), редактирую как обычный текст")
        try:
            return await edit_fn(text, **kwargs)
        except BadRequest as e2:
            if "Message is not modified" in str(e2):
                return None
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
    action_type = action.get('type', '')

    # Для ключевых слов — три варианта: Пауза / Удалить / Отклонить
    if action_type in ('pause_keywords', 'removekeywords', 'remove_keywords',
                        'deletekeywords', 'delete_keywords'):
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ Пауза", callback_data=f"approve:{action_id}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_keywords:{action_id}"),
            ],
            [
                InlineKeyboardButton("💬 Уточнить", callback_data=f"comment:{action_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{action_id}"),
            ],
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Применить", callback_data=f"approve:{action_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{action_id}"),
            ],
            [
                InlineKeyboardButton("💬 Уточнить", callback_data=f"comment:{action_id}"),
            ],
        ])
    await _safe_send(bot, chat_id, text, parse_mode="Markdown", reply_markup=keyboard)


# ── Команды ──────────────────────────────────────────────

async def cmd_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /email <тема> — создаёт рассылку через email_agent.
    Пример: /email Летняя скидка 30% на ремонт AC
    """
    if not _is_owner(update):
        return

    theme = " ".join(ctx.args) if ctx.args else "seasonal appliance repair promotion"

    if not _email_agent_available:
        await update.message.reply_text("❌ email_agent не установлен")
        return

    try:
        _email_agent.OWNER_CHAT_ID = str(config.OWNER_CHAT_ID)
        msg = await update.message.reply_text("⏳ Генерирую баннер через AI, подожди 30–60 секунд...")

        import concurrent.futures
        loop = asyncio.get_event_loop()

        def _generate():
            campaign_data = _email_agent._generate_campaign_content(theme)
            img_url = _email_agent._generate_ideogram_image(
                campaign_data.get("ideogram_prompt", "Professional appliance repair NYC")
            )

            html = _email_agent._build_html(campaign_data, img_url)
            return campaign_data, html, img_url

        with concurrent.futures.ThreadPoolExecutor() as pool:
            campaign_data, html, image_url = await loop.run_in_executor(pool, _generate)

        # Сохраняем в pending
        _email_agent._pending_campaign = {
            "html": html,
            "subject": campaign_data["email_subject"],
            "preview_text": campaign_data.get("preview_text", ""),
            "generated_at": datetime.now(NY_TZ).isoformat(),
        }

        # Формируем превью
        cards = campaign_data.get("cards", [])
        cards_lines = []
        for c in cards:
            title = c.get("title", "")
            text = c.get("text", "")[:80]
            cards_lines.append(f"- {title}: {text}...")
            cards_lines.append(f"- {title}: {text}...")
        cards_text = "\n".join(cards_lines)

        preview_msg = (
            f"\u2705 *\u0411\u0430\u043d\u043d\u0435\u0440 \u0433\u043e\u0442\u043e\u0432!*\n\n"
            f"\U0001f4cc *\u0422\u0435\u043c\u0430:* {campaign_data.get('campaign_title', '')}\n"
            f"\U0001f4e7 *Subject:* {campaign_data.get('email_subject', '')}\n"
            f"\U0001f4ac *\u041e\u0444\u0444\u0435\u0440:* {campaign_data.get('offer_text', '')}\n\n"
            f"*\u0422\u0435\u0437\u0438\u0441\u044b:*\n{cards_text}\n\n"
            f"\U0001f4e8 *\u041f\u043e\u043b\u0443\u0447\u0430\u0442:* ~957 \u043a\u043b\u0438\u0435\u043d\u0442\u043e\u0432\n\n"
            "\u041d\u0430\u043f\u0438\u0448\u0438 *\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u0439* \u0438\u043b\u0438 \u2705 \u0434\u043b\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438"
        )

        # Превью письма — рендерим HTML в изображение через playwright или отправляем текст
        try:
            # Пробуем скриншот через playwright
            from playwright.async_api import async_playwright
            import io as _io
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                page = await browser.new_page(viewport={"width": 640, "height": 900})
                await page.set_content(html, wait_until="networkidle")
                screenshot = await page.screenshot(full_page=True)
                await browser.close()
            await ctx.bot.send_photo(
                chat_id=config.OWNER_CHAT_ID,
                photo=_io.BytesIO(screenshot),
                caption=(
                    "📧 *Превью письма*\n\n"
                    "Именно так клиенты увидят письмо в почте.\n\n"
                    "Напиши *отправляй* или скажи что исправить."
                ),
                parse_mode="Markdown"
            )
        except Exception as pw_e:
            log.warning(f"Playwright недоступен: {pw_e} — отправляем фон")
            # Fallback — отправляем фоновое изображение
            if image_url:
                try:
                    import httpx as _httpx
                    import io as _io
                    async with _httpx.AsyncClient(timeout=30) as hc:
                        img_resp = await hc.get(image_url)
                    await ctx.bot.send_photo(
                        chat_id=config.OWNER_CHAT_ID,
                        photo=_io.BytesIO(img_resp.content),
                        caption=(
                            "🖼 *Превью фона письма*\n\n"
                            "Клиенты получат HTML письмо с логотипом, текстом и кнопками.\n\n"
                            "Напиши *отправляй* или скажи что исправить."
                        ),
                        parse_mode="Markdown"
                    )
                except Exception as img_e:
                    log.warning(f"Не удалось отправить превью: {img_e}")
                    await ctx.bot.send_message(
                        chat_id=config.OWNER_CHAT_ID,
                        text="📧 Письмо готово. Напиши *отправляй* чтобы разослать.",
                        parse_mode="Markdown"
                    )

    except Exception as e:
        log.error(f"email_agent error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка генерации: {e}")


async def cmd_sendemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/sendemail — отправить последний одобренный баннер по базе клиентов."""
    if not _is_owner(update):
        return
    if not _email_agent_available:
        await update.message.reply_text("❌ email_agent не установлен")
        return
    if not _email_agent.has_pending_campaign():
        await update.message.reply_text("❌ Нет готового баннера. Сначала создай через /email")
        return
    msg = await update.message.reply_text("📤 Отправляю рассылку...")
    try:
        import concurrent.futures
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            sent = await loop.run_in_executor(
                pool,
                lambda: _email_agent._send_via_sendgrid(
                    _email_agent._pending_campaign["html"],
                    _email_agent._pending_campaign["subject"],
                    _email_agent._pending_campaign.get("preview_text", ""),
                )
            )
        _email_agent._pending_campaign = None
        result_text = "✅ Рассылка отправлена! Доставлено: " + str(sent) + " писем"
        await _safe_edit(msg, result_text)
    except Exception as e:
        log.error(f"sendemail error: {e}", exc_info=True)
        await _safe_edit(msg, f"❌ Ошибка отправки: {e}")


async def cmd_sendemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/sendemail — отправить последний одобренный баннер по базе клиентов."""
    if not _is_owner(update):
        return
    if not _email_agent_available:
        await update.message.reply_text("❌ email_agent не установлен")
        return
    if not _email_agent.has_pending_campaign():
        await update.message.reply_text("❌ Нет готового баннера. Создай через /email")
        return
    msg = await update.message.reply_text("📤 Отправляю рассылку...")
    try:
        import concurrent.futures
        loop = asyncio.get_event_loop()
        pending = _email_agent._pending_campaign
        with concurrent.futures.ThreadPoolExecutor() as pool:
            sent = await loop.run_in_executor(
                pool,
                lambda: _email_agent._send_via_sendgrid(
                    pending["html"],
                    pending["subject"],
                    pending.get("preview_text", ""),
                )
            )
        _email_agent._pending_campaign = None
        result_text = "✅ Рассылка отправлена! Доставлено: " + str(sent) + " писем"
        await _safe_edit(msg, result_text)
    except Exception as e:
        log.error(f"sendemail error: {e}", exc_info=True)
        await _safe_edit(msg, "❌ Ошибка отправки: " + str(e))


async def cmd_emailtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/emailtest — отправить тестовое письмо только на you@bchdcompany.com."""
    if not _is_owner(update):
        return
    if not _email_agent_available:
        await update.message.reply_text("❌ email_agent не установлен")
        return
    if not _email_agent.has_pending_campaign():
        await update.message.reply_text("❌ Нет готового баннера. Создай через /email")
        return
    msg = await update.message.reply_text("🧪 Отправляю тестовое письмо на you@bchdcompany.com...")
    try:
        import concurrent.futures, requests
        loop = asyncio.get_event_loop()
        pending = _email_agent._pending_campaign

        def _send_test():
            key = os.environ.get("SENDGRID_API_KEY", "")
            payload = {
                "personalizations": [{"to": [{"email": "you@bchdcompany.com", "name": "Chingis"}]}],
                "from": {"email": "you@bchdcompany.com", "name": "BCHD Appliance Repair"},
                "subject": "[TEST] " + pending["subject"],
                "content": [{"type": "text/html", "value": pending["html"]}],
                "tracking_settings": {
                    "click_tracking": {"enable": True},
                    "open_tracking": {"enable": True}
                }
            }
            resp = requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload, timeout=30
            )
            return resp.status_code

        with concurrent.futures.ThreadPoolExecutor() as pool:
            status = await loop.run_in_executor(pool, _send_test)

        if status == 202:
            await _safe_edit(msg,
                "✅ Тестовое письмо отправлено на you@bchdcompany.com\n\n"
                "Проверь почту — если всё хорошо, напиши *отправляй* для рассылки по всей базе.",
                parse_mode="Markdown"
            )
        else:
            await _safe_edit(msg, f"❌ Ошибка SendGrid: {status}")
    except Exception as e:
        log.error(f"emailtest error: {e}", exc_info=True)
        await _safe_edit(msg, f"❌ Ошибка: {e}")


async def cmd_changes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/changes — журнал изменений рекламной кампании за последние 90 дней."""
    if not _is_owner(update):
        return
    try:
        import json as _json
        pool = await _get_db_pool()
        if not pool:
            await update.message.reply_text("❌ База данных недоступна")
            return
        
        async with pool.acquire() as conn:
            changes = await conn.fetch("""
                SELECT * FROM ads_changes_log
                ORDER BY applied_at DESC
                LIMIT 50
            """)
        
        if not changes:
            await update.message.reply_text("📋 Журнал изменений пуст — изменения начнут записываться автоматически.")
            return
        
        lines = ["📋 *Журнал изменений рекламы* (последние 90 дней)\n"]
        for ch in changes:
            applied = ch["applied_at"].strftime("%d.%m %H:%M")
            due = ch["analysis_due_at"].strftime("%d.%m") if ch["analysis_due_at"] else "—"
            result = ch.get("analysis_result")
            status = "✅ проанализировано" if result else f"⏳ анализ {due}"
            
            change_from = _json.loads(ch["change_from"]) if ch["change_from"] else {}
            change_to = _json.loads(ch["change_to"]) if ch["change_to"] else {}
            
            change_str = ""
            if change_from and change_to:
                from_val = list(change_from.values())[0] if change_from else ""
                to_val = list(change_to.values())[0] if change_to else ""
                change_str = f" ({from_val} → {to_val})"
            
            lines.append(
                f"*{applied}* — {ch['action_type']}{change_str}\n"
                f"  {ch['description'][:60]}\n"
                f"  {status}\n"
            )
        
        text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


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
        f"/checkcampaign <текст> — прямая проверка реального статуса кампании (без ИИ)\n"
        f"/enablecampaign <текст/ID> — гарантированная карточка на включение кампании (без ИИ)\n"
        f"/pausecampaign <текст/ID> — гарантированная карточка на паузу кампании (без ИИ)\n"
        f"/checknegatives — прямая проверка списка минус-слов (без ИИ)\n"
        f"/history [N] — журнал выполненных действий (последние N, по умолчанию 20)\n"
        f"/clearmemory — очистить память переписки (начать с чистого листа)\n"
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



async def cmd_dayparting(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/dayparting — анализ времени звонков за 90 дней."""
    if not _is_owner(update):
        return
    msg = await update.message.reply_text("Анализирую время джобов за 90 дней...")
    try:
        today = datetime.now(NY_TZ)
        date_to = today.strftime("%Y-%m-%d")
        date_from = (today - timedelta(days=90)).strftime("%Y-%m-%d")
        hour_data = await workiz_client.get_jobs_by_hour(date_from, date_to)
        total = hour_data.get("total_jobs", 0)
        if total == 0:
            await _safe_edit(msg, "Нет данных о времени джобов за 90 дней.")
            return
        groups = hour_data.get("groups", {})
        by_hour = hour_data.get("by_hour", [])
        lines_text = []
        lines_text.append(f"Dayparting — {date_from} — {date_to}")
        lines_text.append(f"Всего джобов: {total}")
        lines_text.append(f"Ночь (00-07): {groups.get('night_0_7', 0)}")
        lines_text.append(f"Утро (08-11): {groups.get('morning_8_11', 0)}")
        lines_text.append(f"День (12-17): {groups.get('day_12_17', 0)}")
        lines_text.append(f"Вечер (18-23): {groups.get('evening_18_23', 0)}")
        top_hours = sorted(by_hour, key=lambda x: x["jobs"], reverse=True)[:8]
        for h in top_hours:
            if h["jobs"] > 0:
                bar = chr(9608) * min(int(h["pct"] / 2), 10)
                lines_text.append(f"{h['label']} {bar} {h['jobs']} ({h['pct']}%)")
        off_hours = hour_data.get("recommended_off_hours", [])
        if off_hours:
            off_str = ", ".join(f"{h:02d}:00" for h in sorted(off_hours))
            lines_text.append(f"Рекомендуется отключить: {off_str}")
        text = "\n".join(lines_text)
        await _safe_edit(msg, text)
        budgets_data = await ads_client.get_budget_data(account="ads")
        context_data = {
            "_period": {"date_from": date_from, "date_to": date_to},
            "dayparting": hour_data,
            "budgets": {"ads": budgets_data},
        }
        question = (
            "В context_data есть данные dayparting. Создай карточку set_ad_schedule "
            "используя campaign_id из budgets.ads.campaigns[0].campaign_id. "
            "Расписание: пн-вс, только в часы когда реально приходят лиды."
        )
        result = await ai_analyst.chat_action(question, context_data, "set_ad_schedule")
        reply = result.get("reply", "")
        if reply:
            await _safe_send(ctx.bot, config.OWNER_CHAT_ID, reply, parse_mode="Markdown")
        proposed = result.get("proposed_actions", [])
        proposed = [a for a in proposed if isinstance(a, dict)]
        if not any(a.get("type") in ("set_ad_schedule", "setadschedule") for a in proposed):
            camp_list = budgets_data.get("campaigns", [])
            camp_id = camp_list[0]["campaign_id"] if camp_list else None
            if camp_id:
                days_list = ["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY"]
                proposed.append({
                    "type": "set_ad_schedule", "account": "ads",
                    "campaign_id": camp_id,
                    "campaign_name": camp_list[0].get("campaign_name", "BCHD Appliance Repair NYC"),
                    "schedules": [{"day": d, "start_hour": 8, "end_hour": 21} for d in days_list],
                    "description": "Установить расписание рекламы 08:00-21:00",
                    "reasoning": f"По данным dayparting: 90% лидов с 09:00 до 21:00.",
                    "risks": "Лиды вне расписания не получат показ.",
                    "urgency": "medium", "urgency_label": "Средняя", "confidence": "high",
                })
        for action in proposed:
            if not isinstance(action, dict):
                log.warning(f"proposed_actions: не-dict: {str(action)[:80]}")
                continue
            try:
                action.setdefault("account", "ads")
                action.setdefault("requires_approval", True)
                action_id = await pending.add(action)
                await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
            except Exception as e:
                log.error(f"dayparting card error: {e}")
        await _save_cmd_result(ctx, update.effective_chat.id, "/dayparting", text)
    except Exception as e:
        log.error(f"Ошибка /dayparting: {e}")
        await _safe_edit(msg, f"Ошибка: {e}")

async def cmd_check_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Прямая диагностическая команда: /checkcampaign <текст> — находит
    кампании (в ОБОИХ аккаунтах — Google Ads и LSA), содержащие этот текст
    в названии, и показывает их РЕАЛЬНЫЙ текущий статус напрямую из Google
    Ads API. Никакого ИИ-анализа — только сырые факты, чтобы однозначно
    проверить, реально ли кампания включена/выключена.
    Использование: /checkcampaign BCHD/2
    Без аргумента — показывает ВСЕ кампании в обоих аккаунтах (полный список).
    """
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    search_text = " ".join(ctx.args) if ctx.args else ""

    search_label = f"содержащие '{search_text}'" if search_text else "все кампании"
    msg = await update.message.reply_text(f"🔍 Ищу {search_label}...")
    all_matches = []
    for acc in (["ads", "lsa"] if config.lsa_configured else ["ads"]):
        try:
            result = await ads_client.get_campaign_status(search_text, account=acc)
            if not result.get("error"):
                all_matches.extend(result.get("matches", []))
        except Exception as e:
            log.error(f"Ошибка /checkcampaign для {acc}: {e}")

    if not all_matches:
        await _safe_edit(msg, f"Кампаний ({search_label}) не найдено ни в Google Ads, ни в LSA.")
        return

    text = f"🔍 *Прямая проверка Google Ads API (без ИИ-анализа) — история за 90 дней:*\n\n"
    for m in all_matches:
        text += f"*\"{m['name']}\"*\n"
        text += f"• Тип: {m['channel_type']}\n"
        text += f"• Статус: **{m['status']}**\n"
        text += f"• ID: `{m['campaign_id']}`\n"
        text += f"• Расход за 90 дней: ${m.get('cost_90d', 0):.2f}\n"
        text += f"• Конверсии за 90 дней: {m.get('conversions_90d', 0):.0f}\n"
        text += f"• Показы за 90 дней: {m.get('impressions_90d', 0)}\n\n"
    if len(all_matches) > 1:
        text += "_Сравни расход и конверсии — кампания с реальной историей, вероятно, и есть \"боевая\", привязанная к карточке лидов._"
    await _safe_edit(msg, text, parse_mode="Markdown")


async def _cmd_toggle_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target_status: str):
    """
    Общая логика для /enablecampaign и /pausecampaign — создаёт карточку
    одобрения НАПРЯМУЮ В КОДЕ, БЕЗ УЧАСТИЯ ИИ. Использует тот же прямой
    запрос к Google Ads API, что и /checkcampaign, чтобы найти campaign_id.
    Это сделано как максимально надёжный обходной путь на случай, если
    свободный чат с ИИ не создаёт реальную карточку (proposed_actions
    пустой, хотя текст утверждает обратное) — этот путь гарантированно
    создаёт настоящую карточку с кнопками, минуя генерацию JSON моделью.
    """
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return
    if not ctx.args:
        action_word = "включить" if target_status == "ENABLED" else "поставить на паузу"
        await update.message.reply_text(f"Использование: команда <текст названия или ID кампании>\nНапример, чтобы {action_word} кампанию.")
        return
    search_text = " ".join(ctx.args)

    msg = await update.message.reply_text(f"🔍 Ищу кампанию '{search_text}'...")
    all_matches = []
    for acc in (["ads", "lsa"] if config.lsa_configured else ["ads"]):
        try:
            result = await ads_client.get_campaign_status(search_text, account=acc)
            if not result.get("error"):
                for m in result.get("matches", []):
                    m["account"] = acc
                    all_matches.append(m)
        except Exception as e:
            log.error(f"Ошибка поиска кампании: {e}")

    # Поддержка поиска по числовому ID (если search_text — просто цифры)
    if not all_matches and search_text.strip().isdigit():
        for acc in (["ads", "lsa"] if config.lsa_configured else ["ads"]):
            try:
                result = await ads_client.get_campaign_status("", account=acc)
            except Exception:
                result = {"matches": []}
            for m in result.get("matches", []):
                if str(m.get("campaign_id")) == search_text.strip():
                    m["account"] = acc
                    all_matches.append(m)

    if not all_matches:
        await _safe_edit(msg, f"Кампаний, соответствующих '{search_text}', не найдено ни в одном аккаунте.")
        return
    if len(all_matches) > 1:
        names = "\n".join(f"• {m['name']} (ID {m['campaign_id']}, {m['account']})" for m in all_matches)
        await _safe_edit(msg, f"Найдено несколько кампаний — уточни запрос:\n{names}")
        return

    m = all_matches[0]
    action_type = "enable_campaign" if target_status == "ENABLED" else "pause_campaign"
    action = {
        "type": action_type,
        "account": m["account"],
        "campaign_id": m["campaign_id"],
        "campaign_name": m["name"],
        "description": f"{'Включить' if target_status == 'ENABLED' else 'Поставить на паузу'} кампанию '{m['name']}' (создано напрямую, без ИИ, по прямому запросу владельца)",
        "reasoning": "Прямая команда владельца, в обход ИИ-анализа — для максимальной надёжности.",
        "data_summary": f"Текущий статус: {m['status']}",
        "expected_impact": "",
        "risks": "Прямое действие по явной команде владельца.",
        "urgency": "high",
        "urgency_label": "Высокая",
        "confidence": "high",
    }
    action_id = await pending.add(action)
    await _safe_edit(msg, f"✅ Карточка создана для кампании '{m['name']}' (текущий статус: {m['status']}).")
    await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)


async def cmd_enable_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/enablecampaign <текст или ID> — гарантированно создаёт карточку на включение, в обход ИИ."""
    await _cmd_toggle_campaign(update, ctx, "ENABLED")


async def cmd_pause_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pausecampaign <текст или ID> — гарантированно создаёт карточку на паузу, в обход ИИ."""
    await _cmd_toggle_campaign(update, ctx, "PAUSED")


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



# Кэш для дедупликации алертов
_anomaly_alert_cache: dict = {}
_ANOMALY_COOLDOWN_HOURS = 12


async def scheduled_anomaly_check(app):
    """Проактивные инсайты каждые 4 часа. Пишет только при новых аномалиях."""
    global _anomaly_alert_cache
    if not config.google_ads_configured:
        return
    try:
        today = datetime.now(NY_TZ)
        alerts = []
        date_2d_from = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        date_today = today.strftime("%Y-%m-%d")
        try:
            perf_2d = await ads_client.get_spend_for_period(date_2d_from, date_today, account="ads")
            spend_2d = perf_2d.get("spend", 0)
            conv_2d = perf_2d.get("conversions", 0)
            if spend_2d > 5 and conv_2d == 0:
                alerts.append(
                    f"0 конверсий за 2 дня при расходе ${spend_2d:.2f}. "
                    f"Проверь кампанию."
                )
        except Exception as e:
            log.warning(f"anomaly_check 2d error: {e}")
        try:
            week_from = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            prev_week_from = (today - timedelta(days=14)).strftime("%Y-%m-%d")
            prev_week_to = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            curr = await ads_client.get_spend_for_period(week_from, date_today, account="ads")
            prev = await ads_client.get_spend_for_period(prev_week_from, prev_week_to, account="ads")
            curr_cpa = curr["spend"] / curr["conversions"] if curr.get("conversions", 0) > 0 else None
            prev_cpa = prev["spend"] / prev["conversions"] if prev.get("conversions", 0) > 0 else None
            if curr_cpa and prev_cpa and curr_cpa > prev_cpa * 1.4:
                pct = (curr_cpa / prev_cpa - 1) * 100
                alerts.append(f"CPA вырос на {pct:.0f}% (${curr_cpa:.0f} vs ${prev_cpa:.0f})")
        except Exception as e:
            log.warning(f"anomaly_check cpa error: {e}")
        try:
            audit = await ads_client.get_full_audit_data(
                account="ads",
                date_from=(today - timedelta(days=3)).strftime("%Y-%m-%d"),
                date_to=date_today
            )
            for camp in audit.get("campaigns", []):
                if camp.get("status") != "ENABLED":
                    continue
                is_val = camp.get("impression_share", 0)
                if is_val and 0 < is_val < 20:
                    alerts.append(f"IS упал до {is_val:.1f}% в кампании '{camp['name']}'")
                daily_budget = camp.get("budget_daily", 0)
                camp_cost = camp.get("cost", 0)
                daily_spend = camp_cost / 3 if camp_cost else 0
                if daily_budget > 0 and daily_spend > daily_budget * 2.0:
                    overspend_pct = (daily_spend / daily_budget - 1) * 100
                    alerts.append(f"Перерасход +{overspend_pct:.0f}% в кампании '{camp['name']}'")
        except Exception as e:
            log.warning(f"anomaly_check IS error: {e}")
        if not alerts:
            log.info("anomaly_check: аномалий не найдено")
            return
        now_ts = today.timestamp()
        new_alerts = []
        for alert in alerts:
            import re as _re
            alert_type = _re.sub(r'[0-9$+%.]+', '', alert[:60]).strip()[:40]
            last_sent = _anomaly_alert_cache.get(alert_type, 0)
            if now_ts - last_sent > _ANOMALY_COOLDOWN_HOURS * 3600:
                new_alerts.append(alert)
                _anomaly_alert_cache[alert_type] = now_ts
        if not new_alerts:
            log.info("anomaly_check: все аномалии уже отправлены недавно")
            return
        alerts = new_alerts
        log.info(f"anomaly_check: {len(alerts)} новых аномалий")
        try:
            period_from = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            context_data = {"_period": {"date_from": period_from, "date_to": date_today}}
            # Загружаем agent_memory и rejected_actions в проактивный анализ
            try:
                _pool_am = await _get_db_pool()
                if _pool_am:
                    async with _pool_am.acquire() as _conn_am:
                        _mem = await _conn_am.fetch("SELECT category, key, value FROM agent_memory ORDER BY category, key")
                        _rej = await _conn_am.fetch("SELECT action_type, description, keyword, rejected_at, retry_after FROM rejected_actions WHERE retry_after > NOW() ORDER BY rejected_at DESC LIMIT 20")
                        _chg = await _conn_am.fetch("SELECT action_type, description, keyword, applied_at FROM ads_changes_log ORDER BY applied_at DESC LIMIT 15")
                    if _mem:
                        _am = {}
                        for _r in _mem:
                            if _r["category"] not in _am:
                                _am[_r["category"]] = {}
                            _am[_r["category"]][_r["key"]] = _r["value"]
                        context_data["agent_memory"] = _am
                    context_data["rejected_actions"] = [
                        {"type": r["action_type"], "description": r["description"], "keyword": r["keyword"],
                         "rejected_at": r["rejected_at"].strftime("%d.%m"), "retry_after": r["retry_after"].strftime("%d.%m")}
                        for r in _rej
                    ]
                    context_data["recent_changes"] = [
                        {"type": r["action_type"], "description": r["description"], "keyword": r["keyword"],
                         "applied_at": r["applied_at"].strftime("%d.%m")}
                        for r in _chg
                    ]
            except Exception as _e_am:
                log.warning(f"anomaly_check: ошибка загрузки agent_memory: {_e_am}")
            context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary(
                date_from=period_from, date_to=date_today
            )
            context_data["keywords"] = {"ads": await ads_client.get_keywords_analysis(
                account="ads", date_from=period_from, date_to=date_today
            )}
            context_data["budgets"] = {"ads": await ads_client.get_budget_data(account="ads")}
            anomaly_desc = "\n".join(f"- {a}" for a in alerts)
            question = (
                f"Обнаружены аномалии:\n{anomaly_desc}\n\n"
                f"Проанализируй и предложи конкретные действия."
            )
            result = await ai_analyst.chat_action(question, context_data, "action")
            reply = result.get("reply", "")
            header = f"Проактивный инсайт — {today.strftime('%d.%m %H:%M')}\n\n"
            await _send_long_message(app.bot, config.OWNER_CHAT_ID, header + reply)
            for action in result.get("proposed_actions", []):
                try:
                    action.setdefault("account", "ads")
                    action.setdefault("requires_approval", True)
                    action_id = await pending.add(action)
                    await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
                except Exception as e:
                    log.error(f"anomaly card error: {e}")
        except Exception as e:
            log.error(f"anomaly_check AI error: {e}")
            text = f"Проактивный инсайт\n\n" + "\n".join(alerts)
            await _safe_send(app.bot, config.OWNER_CHAT_ID, text)
    except Exception as e:
        log.error(f"Ошибка anomaly_check: {e}")

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
        to_check = await pending.get_actions_needing_reverify(min_hours_since_execution=20)
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
        await pending.mark_reverified(action_id)
        if verified is True:
            text = (
                f"✅ *Отложенная перепроверка подтвердила успех:* {action.get('description')}\n\n"
                f"Изменение реально применилось (проверено повторно через сутки)."
            )
        elif verified is False:
            # Минус-слова из REMOVED кампании не влияют на трафик — молчим
            desc = action.get('description', '')
            action_str = str(action)
            if 'BCHD Appliance Repair Service' in desc or '20424210216' in action_str:
                log.info(f"reverify: пропускаем REMOVED кампанию: {desc}")
                continue
            # Пробуем выполнить заново автоматически
            try:
                await ads_client.execute_action(action)
                retry_v = await ads_client.verify_action(action)
                if retry_v.get('verified'):
                    text = (
                        f"✅ *Автоматически исправлено:* {action.get('description')}\n\n"
                        f"Действие повторно выполнено и подтверждено."
                    )
                else:
                    text = (
                        f"⚠️ *Требует внимания:* {action.get('description')}\n\n"
                        f"Автоматическое исправление не помогло — {retry_v.get('note', '')}\n"
                        f"Проверь в Google Ads UI."
                    )
            except Exception as _re:
                log.error(f"reverify retry error: {_re}")
                text = (
                    f"⚠️ *Требует внимания:* {action.get('description')}\n\n"
                    f"Не применилось: {verification.get('note', '')}. Ошибка retry: {_re}"
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
        # Добавляем метрики GBP если доступны
        _gbp_inst = globals().get("gbp_client_inst")
        if _gbp_available and _gbp_inst:
            try:
                ins = await _gbp_inst.get_insights(days=7)
                t = ins.get("totals", {})
                if not ins.get("error"):
                    text += (
                        f"\n\n📍 *Google Business Profile (7 дней):*\n"
                        f"• Просмотры: {t.get('views_total', 0)} "
                        f"(карты: {t.get('views_maps', 0)}, поиск: {t.get('views_search', 0)})\n"
                        f"• Звонки из профиля: {t.get('call_clicks', 0)}\n"
                        f"• Клики на сайт: {t.get('website_clicks', 0)}\n"
                        f"• Запросы маршрута: {t.get('direction_requests', 0)}"
                    )
            except Exception as e:
                log.warning(f"GBP insights в ROAS: {e}")
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
    actions = await pending.all_pending()
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

    history = await pending.get_history(limit=limit)
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
            action_id = await pending.add(action)
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
        action_id = await pending.add(action)
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
                    action_id = await pending.add(action)
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
                action_id = await pending.add(action)
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


def _action_already_applied(action: dict, context_data: dict) -> tuple:
    a_type = action.get("type", "")
    keywords_data = []
    for v in context_data.values():
        if isinstance(v, dict) and "keywords" in v:
            keywords_data.extend(v["keywords"])

    if a_type == "update_bid":
        rn = action.get("resource_name")
        new_bid = action.get("new_bid")
        if rn and new_bid:
            for kw in keywords_data:
                if kw.get("resource_name") == rn:
                    current = kw.get("current_bid")
                    if current and abs(float(current) - float(new_bid)) < 0.01:
                        return True, f"Ставка уже ${current:.2f} — совпадает с предлагаемой"
    elif a_type == "update_final_url":
        rn = action.get("resource_name")
        new_url = action.get("new_url", "")
        kw_text = action.get("keyword", "").strip().lower()
        if new_url:
            for kw in keywords_data:
                existing = kw.get("final_urls", [])
                rn_match = rn and kw.get("resource_name") == rn
                text_match = kw_text and kw.get("keyword", "").strip().lower() == kw_text
                if (rn_match or text_match) and new_url in existing:
                    return True, f"Лендинг уже привязан: {new_url}"
    elif a_type == "pause_keywords":
        kws = action.get("keywords", [])
        if kws:
            rns = {k.get("resource_name") for k in kws}
            paused = [kw for kw in keywords_data if kw.get("resource_name") in rns and kw.get("status") == "PAUSED"]
            if len(paused) == len(kws):
                return True, f"Все {len(kws)} ключей уже PAUSED"
    elif a_type == "enable_keywords":
        kws = action.get("keywords", [])
        if kws:
            rns = {k.get("resource_name") for k in kws}
            enabled = [kw for kw in keywords_data if kw.get("resource_name") in rns and kw.get("status") == "ENABLED"]
            if len(enabled) == len(kws):
                return True, f"Все {len(kws)} ключей уже ENABLED"
    return False, ""



def _validate_action(action: dict) -> tuple[bool, str]:
    """
    Проверяет что карточка содержит необходимые поля.
    Возвращает (valid, reason).
    """
    a_type = action.get("type", "")
    
    if a_type == "update_bid":
        if not action.get("keyword") and not action.get("resource_name"):
            return False, "update_bid требует keyword или resource_name"
        if not action.get("new_bid"):
            return False, "update_bid требует new_bid"
    
    elif a_type in ("pause_keywords", "enable_keywords"):
        kws = action.get("keywords", [])
        if not kws:
            return False, f"{a_type} требует непустой список keywords"
    
    elif a_type == "add_negative_keywords":
        if not action.get("keywords") and not action.get("negatives"):
            return False, "add_negative_keywords требует keywords или negatives"
    
    elif a_type == "update_ad_headlines":
        if not action.get("headlines"):
            return False, "update_ad_headlines требует headlines"
        if not action.get("ad_id") and not action.get("ad_group"):
            return False, "update_ad_headlines требует ad_id или ad_group"
    
    elif a_type == "reply_to_review":
        if not action.get("reply_text") and not action.get("body_text"):
            return False, "reply_to_review требует reply_text или body_text"
        if not action.get("review_name") and not action.get("review_id"):
            return False, "reply_to_review требует review_name или review_id"
    
    elif a_type == "budget_change":
        if not action.get("proposed_budget") and not action.get("new_budget"):
            return False, "budget_change требует proposed_budget"
        # Убеждаемся что current_budget есть
        if not action.get("current_budget"):
            return False, "budget_change требует current_budget"
    
    if not action.get("description"):
        return False, "Карточка без description"
    
    return True, ""

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
        # Если есть keyword — не блокируем, _update_bid сам найдёт resource_name
        if action.get("keyword") or action.get("text"):
            return True
        if action.get("resource_name"):
            ids_to_check.append(str(action["resource_name"]))
    elif a_type == "update_final_url":
        if action.get("resource_name"):
            ids_to_check.append(str(action["resource_name"]))
    elif a_type in ("pause_keywords", "enable_keywords",
                    "remove_keywords", "removekeywords", "delete_keywords"):
        # Для паузы/удаления/добавления ключей НЕ блокируем по ID — проверяется на уровне API
        return True
    elif a_type in ("add_keywords", "addkeywords", "enable_ad_group", "pause_ad_group",
                    "create_gbp_post", "send_email_campaign"):
        return True
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

    # Email agent: подтверждение или запрос рассылки — ПРИОРИТЕТ над обычным чатом
    if _email_agent_available:
        text_lower = question.lower().strip()
        confirm_kw = [
            "отправляй", "отправить", "давай отправляй", "подтверждаю",
            "send it", "go ahead", "да отправляй", "отправь",
            "отправить рассылку", "запускай рассылку", "запускай"
        ]
        campaign_kw = ["рассылк", "баннер для рассылки", "письмо клиент", "email кампани"]

        is_confirm = (any(kw in text_lower for kw in confirm_kw) or "✅" in question)

        if is_confirm and _email_agent.has_pending_campaign():
            msg = await update.message.reply_text("📤 Отправляю рассылку...")
            try:
                import concurrent.futures
                loop = asyncio.get_event_loop()
                email_pending_data = _email_agent._pending_campaign
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    sent = await loop.run_in_executor(
                        pool,
                        lambda: _email_agent._send_via_sendgrid(
                            email_pending_data["html"],
                            email_pending_data["subject"],
                            email_pending_data.get("preview_text", ""),
                        )
                    )
                _email_agent._pending_campaign = None
                await _safe_edit(msg, "✅ Рассылка отправлена! Доставлено: " + str(sent) + " писем")
            except Exception as e:
                log.error(f"email send error: {e}", exc_info=True)
                await _safe_edit(msg, "❌ Ошибка отправки: " + str(e))
            return

        elif is_confirm and not _email_agent.has_pending_campaign():
            await update.message.reply_text(
                "❌ Нет готового баннера. Сначала создай через /email"
            )
            return

    # Если владелец пишет уточнение к карточке (после нажатия "💬 Уточнить")
    awaiting_action_id = ctx.user_data.pop("awaiting_comment_for", None)
    if awaiting_action_id:
        await pending.add_comment(awaiting_action_id, question)
        # Отвечаем с учётом контекста карточки
        action = await pending.get(awaiting_action_id)
        if action:
            context_prompt = (
                f"Владелец задал вопрос или уточнение к предложенному действию:\n"
                f"Действие: {action.get('description', '')}\n"
                f"Обоснование бота: {action.get('reasoning', '')}\n"
                f"Вопрос владельца: {question}\n\n"
                f"Ответь на вопрос конкретно. Если ключи без лендинга — объясни "
                f"что лучше: привязать лендинг и оставить, или паузировать. "
                f"Не создавай новых карточек — только объясни."
            )
            answer = await ai_analyst.answer_question(context_prompt)
            await update.message.reply_text(
                f"💬 *По карточке `{awaiting_action_id}`:*\n\n{answer}\n\n"
                f"_Карточка по-прежнему ждёт одобрения._",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"⚠️ Карточка `{awaiting_action_id}` не найдена.", parse_mode="Markdown")
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

    # Журнал изменений — ДО classify, не нужен Claude
    if any(w in question.lower() for w in ["журнал изменений", "покажи журнал", "история изменений", "что меняли"]):
        _tm = await update.message.reply_text("📋 Загружаю...")
        try:
            _p = await _get_db_pool()
            if _p:
                from datetime import datetime as _dt2
                _q_lower2 = question.lower()
                _month_map = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
                _month_filter = None
                for _m_name, _m_num in _month_map.items():
                    if _m_name in _q_lower2:
                        _month_filter = _m_num
                        break
                _year = _dt2.now().year
                if _month_filter:
                    _date_from2 = _dt2(_year, _month_filter, 1)
                    _date_to2 = _dt2(_year, _month_filter, 28, 23, 59, 59)
                    _sql2 = "SELECT action_type, description, applied_at FROM ads_changes_log WHERE applied_at >= $1 AND applied_at <= $2 ORDER BY applied_at DESC"
                    _args2 = [_date_from2, _date_to2]
                else:
                    _now2 = _dt2.now()
                    _date_from2 = _dt2(_now2.year, _now2.month, 1)
                    _sql2 = "SELECT action_type, description, applied_at FROM ads_changes_log WHERE applied_at >= $1 ORDER BY applied_at DESC"
                    _args2 = [_date_from2]
                async with _p.acquire() as _c:
                    _chs = await _c.fetch(_sql2, *_args2)
                if _chs:
                    _month_label = f"за {_month_filter}-й месяц" if _month_filter else "за август"
                    _o = f"📋 Изменения {_month_label}:\n\n"
                    for _r in _chs:
                        _d = _r["applied_at"].strftime("%d.%m")
                        _t = _r["action_type"].replace("update_ad_headlines","заголовки").replace("add_negative_keywords","минус-слова").replace("update_bid","ставка").replace("enable_ad_group","вкл.группу").replace("pause_ad_group","пауза группы").replace("remove_negative_keyword","удалить минус")
                        _o += f"{_d} {_t}: {str(_r['description'])[:50]}\n"
                    if len(_o) > 3800:
                        _o = _o[:3800] + "\n...запроси конкретный период"
                    await _tm.edit_text(_o)
                else:
                    await _tm.edit_text("Журнал пуст за этот период.")
        except Exception as _ex:
            await _tm.edit_text(f"Ошибка: {_ex}")
        return
    thinking_msg = await update.message.reply_text("🧭 Определяю, что нужно...")
    try:
        classification = await ai_analyst.classify_request(question, history=history)
    except Exception as e:
        log.error(f"Ошибка классификации запроса: {e}")
        classification = {"intent": "chat", "action_type": "none", "days": 0, "data_needed": ["campaigns"], "account": "both"}


    # БЫСТРАЯ ПРОВЕРКА — показать журнал без загрузки данных
    _q_low = question.lower()
    if classification.get("action_type") == "show_changes_log" or any(w in _q_low for w in ["журнал изменений", "история изменений", "покажи журнал", "что меняли в рекламе"]):
        await _safe_edit(thinking_msg, "Загружаю журнал...")
        try:
            import json as _jj
            _pool = await _get_db_pool()
            if _pool:
                async with _pool.acquire() as _conn:
                    _changes = await _conn.fetch("SELECT * FROM ads_changes_log ORDER BY applied_at DESC LIMIT 30")
                if not _changes:
                    await _safe_edit(thinking_msg, "Журнал пуст — изменения записываются при одобрении карточек.")
                else:
                    _out = "Журнал изменений рекламы:\n\n"
                    for _ch in _changes:
                        _applied = _ch["applied_at"].strftime("%d.%m %H:%M")
                        _due = _ch["analysis_due_at"].strftime("%d.%m") if _ch["analysis_due_at"] else "-"
                        _done = bool(_ch.get("analysis_result"))
                        _st = "OK" if _done else f"анализ {_due}"
                        _cf = _jj.loads(_ch["change_from"]) if _ch["change_from"] else {}
                        _ct = _jj.loads(_ch["change_to"]) if _ch["change_to"] else {}
                        _chg = ""
                        if _cf and _ct:
                            _fv = list(_cf.values())[0] if _cf else ""
                            _tv = list(_ct.values())[0] if _ct else ""
                            _chg = f" ({_fv} -> {_tv})"
                        _desc = str(_ch["description"])[:60]
                        _out += f"{_applied} [{_st}] {_ch['action_type']}{_chg}\n{_desc}\n\n"
                    await thinking_msg.edit_text(_out)
                await _append_history(ctx, chat_id, question, "Показал журнал изменений")
            return
        except Exception as _e:
            await _safe_edit(thinking_msg, f"Ошибка: {_e}")
            return

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
    # Загружаем долгосрочную память агента
    try:
        pool_mem = await _get_db_pool()
        if pool_mem:
            async with pool_mem.acquire() as conn_mem:
                mem_rows = await conn_mem.fetch("SELECT category, key, value FROM agent_memory ORDER BY category, key")
                if mem_rows:
                    agent_mem = {}
                    for r in mem_rows:
                        cat = r["category"]
                        if cat not in agent_mem:
                            agent_mem[cat] = {}
                        agent_mem[cat][r["key"]] = r["value"]
                    context_data["agent_memory"] = agent_mem
    except Exception as _me:
        log.warning(f"Ошибка загрузки agent_memory: {_me}")
    # Добавляем rejected_actions и changes_log в контекст
    try:
        pool = await _get_db_pool()
        if pool:
            from datetime import datetime as _dt2
            async with pool.acquire() as conn:
                # Отклонённые карточки (ещё в периоде retry_after)
                rejected = await conn.fetch(
                    "SELECT action_type, description, keyword, rejected_at, retry_after FROM rejected_actions WHERE retry_after > NOW() ORDER BY rejected_at DESC LIMIT 20"
                )
                context_data["rejected_actions"] = [
                    {"type": r["action_type"], "description": r["description"], "keyword": r["keyword"],
                     "rejected_at": r["rejected_at"].strftime("%d.%m"), "retry_after": r["retry_after"].strftime("%d.%m")}
                    for r in rejected
                ]
                # Последние изменения из журнала
                changes = await conn.fetch(
                    "SELECT action_type, description, keyword, applied_at FROM ads_changes_log ORDER BY applied_at DESC LIMIT 10"
                )
                context_data["recent_changes"] = [
                    {"type": r["action_type"], "description": r["description"], "keyword": r["keyword"],
                     "applied_at": r["applied_at"].strftime("%d.%m")}
                    for r in changes
                ]
    except Exception as _e:
        log.warning(f"Ошибка загрузки rejected/changes: {_e}")
    # ВСЕГДА добавляем keywords в data_needed — реальные настройки из API.
    # Исключение: если запрос только про GBP отзывы — не нужны keywords/negatives
    _gbp_only = data_needed == ["gbp_reviews"]
    if not _gbp_only:
        if "keywords" not in data_needed and data_needed != ["none"]:
            data_needed.append("keywords")
        # ВСЕГДА добавляем негативы — без них бот не может создать карточки удаления.
        if "negatives" not in data_needed and data_needed != ["none"]:
            data_needed.append("negatives")
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
        if "negatives" in data_needed:
            context_data["negatives"] = {}
            for acc in ["ads"]:  # минус-слова только для Search, не LSA
                context_data["negatives"][acc] = await ads_client.get_negative_keywords_list(account=acc)
        if "lsa_leads" in data_needed:
            context_data["lsa_leads"] = await ads_client.get_lsa_leads(account="lsa")
        if "ad_schedule" in data_needed:
            try:
                context_data["ad_schedule"] = await ads_client.get_ad_schedule(account="ads")
            except Exception as e:
                log.warning(f"Ошибка сбора ad_schedule: {e}")
        if "thumbtack" in data_needed or "roas" in data_needed:
            try:
                context_data["thumbtack"] = await workiz_client.get_jobs_by_source(
                    "Thumbtack", period_from, period_to
                )
                context_data["thumbtack"]["budget_weekly"] = config.THUMBTACK_WEEKLY_BUDGET
                days_in_period = (
                    datetime.strptime(period_to, "%Y-%m-%d") -
                    datetime.strptime(period_from, "%Y-%m-%d")
                ).days + 1
                context_data["thumbtack"]["budget_period"] = round(
                    config.THUMBTACK_WEEKLY_BUDGET / 7 * days_in_period, 2
                )
            except Exception as e:
                log.warning(f"Ошибка сбора Thumbtack: {e}")
        if "gbp_reviews" in data_needed:
            try:
                _gbp_inst = globals().get("gbp_client_inst")
                if _gbp_inst:
                    context_data["gbp_reviews"] = await _gbp_inst.get_reviews(days=30)
                else:
                    context_data["gbp_reviews"] = {"error": "GBP не настроен", "reviews": [], "unanswered": []}
            except Exception as e:
                log.warning(f"Ошибка сбора gbp_reviews: {e}")
        if "gbp_profile" in data_needed:
            try:
                _gbp_inst = globals().get("gbp_client_inst")
                if _gbp_inst:
                    context_data["gbp_profile"] = await _gbp_inst.get_profile()
                else:
                    context_data["gbp_profile"] = {"error": "GBP не настроен"}
            except Exception as e:
                log.warning(f"Ошибка сбора gbp_profile: {e}")
        if "gbp_posts" in data_needed:
            try:
                _gbp_inst = globals().get("gbp_client_inst")
                if _gbp_inst:
                    context_data["gbp_posts"] = await _gbp_inst.get_posts(page_size=5)
                else:
                    context_data["gbp_posts"] = {"error": "GBP не настроен", "posts": []}
            except Exception as e:
                log.warning(f"Ошибка сбора gbp_posts: {e}")
        if "gbp_insights" in data_needed:
            try:
                _gbp_inst = globals().get("gbp_client_inst")
                if _gbp_inst:
                    context_data["gbp_insights"] = await _gbp_inst.get_insights(days=28)
                else:
                    context_data["gbp_insights"] = {"error": "GBP не настроен"}
            except Exception as e:
                log.warning(f"Ошибка сбора gbp_insights: {e}")
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

    # Журнал изменений — без загрузки данных из API
    # Также проверяем по тексту запроса если классификатор не распознал
    _q_lower = question.lower()
    if action_type == "show_changes_log" or any(w in _q_lower for w in ["журнал изменений", "история изменений", "что меняли", "какие правки", "покажи журнал"]):
        await _safe_edit(thinking_msg, "📋 Загружаю журнал изменений...")
        try:
            import json as _json
            pool = await _get_db_pool()
            if pool:
                async with pool.acquire() as conn:
                    changes = await conn.fetch(
                        "SELECT * FROM ads_changes_log ORDER BY applied_at DESC LIMIT 50"
                    )
                if not changes:
                    await _safe_edit(thinking_msg, "📋 Журнал пуст — изменения записываются автоматически при одобрении карточек.")
                else:
                    out = "Журнал изменений рекламы:\n\n"
                    for ch in changes:
                        applied = ch["applied_at"].strftime("%d.%m %H:%M")
                        due = ch["analysis_due_at"].strftime("%d.%m") if ch["analysis_due_at"] else "-"
                        has_result = bool(ch.get("analysis_result"))
                        status = "OK" if has_result else f"анализ {due}"
                        cf = _json.loads(ch["change_from"]) if ch["change_from"] else {}
                        ct = _json.loads(ch["change_to"]) if ch["change_to"] else {}
                        chg = ""
                        if cf and ct:
                            fv = list(cf.values())[0] if cf else ""
                            tv = list(ct.values())[0] if ct else ""
                            chg = f" ({fv} -> {tv})"
                        desc = str(ch["description"])[:60]
                        out += f"{applied} [{status}] {ch['action_type']}{chg}\n{desc}\n\n"
                    await thinking_msg.edit_text(out)
                await _append_history(ctx, chat_id, question, "Показал журнал изменений")
            return
        except Exception as e:
            await _safe_edit(thinking_msg, f"❌ Ошибка: {e}")
            return

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
    if len(reply) > 3800:
        await thinking_msg.delete()
        await _send_long_message(ctx.bot, chat_id, reply)
    else:
        await _safe_edit(thinking_msg, reply, parse_mode=None)
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
    already_applied_count = 0
    for action in proposed:
        if not isinstance(action, dict):
            log.warning(f"proposed_actions: не-dict: {str(action)[:80]}")
            continue
        try:
            action.setdefault("account", accounts[0])
            action.setdefault("data_summary", action.get("reasoning", ""))
            action.setdefault("expected_impact", "")
            action.setdefault("requires_approval", True)

            if not _action_ids_verified(action, context_data):
                log.warning(f"Действие заблокировано — ID не найдены в свежих данных: type={action.get('type')} keyword={action.get('keyword')} rn={action.get('resource_name','')[:50]}")
                blocked_actions += 1
                continue

            # Проверяем не применено ли уже это действие по текущим настройкам
            already, reason = _action_already_applied(action, context_data)
            if already:
                log.info(f"Действие пропущено — уже применено: {action.get('type')} | {reason}")
                already_applied_count += 1
                continue

            valid, reason = _validate_action(action)
            if not valid:
                log.warning(f"Карточка отклонена валидацией: {reason} | type={action.get('type')}")
                blocked_actions += 1
                continue
            action_id = await pending.add(action)
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
    if already_applied_count:
        await ctx.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=(
                f"ℹ️ {already_applied_count} действие пропущено — уже применено "
                f"по текущим настройкам Google Ads."
            ),
        )


# ── Голос и фото ────────────────────────────────────────

async def _transcribe_voice(file_bytes: bytes, mime: str = "audio/ogg") -> str:
    """
    Транскрибирует голосовое сообщение через Groq Whisper.
    Возвращает текст или бросает исключение.
    """
    groq_key = getattr(config, "GROQ_API_KEY", None) or os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY не задан — голосовые сообщения недоступны")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {groq_key}"},
            files={"file": ("voice.ogg", file_bytes, mime)},
            data={"model": "whisper-large-v3-turbo", "language": "ru"},
        )
        resp.raise_for_status()
        return resp.json().get("text", "").strip()


async def handle_voice_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Принимает голосовое/аудио сообщение, транскрибирует через Groq Whisper,
    затем передаёт текст в стандартный pipeline (как обычный текстовый запрос).
    """
    if not _is_owner(update):
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    status_msg = await update.message.reply_text("🎙 Транскрибирую голосовое...")
    try:
        tg_file = await ctx.bot.get_file(voice.file_id)
        import httpx as _hx_dl
        _tg_fp = tg_file.file_path
        _tg_full_url = f'https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{_tg_fp}' if not _tg_fp.startswith('http') else _tg_fp
        async with _hx_dl.AsyncClient(timeout=30) as _hx_dl_c:
            _tg_dl_r = await _hx_dl_c.get(_tg_full_url)
            file_bytes = _tg_dl_r.content
        question = await _transcribe_voice(bytes(file_bytes))
    except Exception as e:
        log.error(f"Ошибка транскрипции голоса: {e}")
        await status_msg.edit_text(f"❌ Не удалось транскрибировать: {e}")
        return

    if not question:
        await status_msg.edit_text("⚠️ Голосовое сообщение не распознано (пустой текст).")
        return

    await status_msg.edit_text(f"🎙 Распознано: _{question}_\n\n🧭 Определяю, что нужно...", parse_mode="Markdown")

    # Передаём в тот же pipeline что и текст, но со status_msg вместо новой "думалки"
    chat_id = update.effective_chat.id
    history = await _get_history(ctx, chat_id)

    if not config.google_ads_configured:
        answer = await ai_analyst.answer_question(question, history=history)
        answer = _guard_against_hallucinated_execution(answer)
        await _safe_edit(status_msg, f"🎙 _{question}_\n\n{answer}", parse_mode="Markdown")
        await _append_history(ctx, chat_id, question, answer)
        return

    try:
        classification = await ai_analyst.classify_request(question, history=history)
    except Exception as e:
        log.error(f"Ошибка классификации голосового: {e}")
        classification = {"intent": "chat", "action_type": "none", "days": 0, "data_needed": ["campaigns"], "account": "both"}

    # Создаём фиктивный update.message с нужным текстом для переиспользования pipeline
    # Проще — просто скопируем логику напрямую через уже разобранный question
    data_needed = classification.get("data_needed", ["campaigns"])
    if isinstance(data_needed, str):
        data_needed = [data_needed] if data_needed != "none" else []
    account_scope = classification.get("account", "both")
    accounts = ["ads", "lsa"] if account_scope == "both" else [account_scope]

    await _safe_edit(status_msg, f"🎙 _{question}_\n\n📊 Собираю данные...", parse_mode="Markdown")

    period_from = classification.get("period_date_from") or None
    period_to = classification.get("period_date_to") or None
    if not period_from or not period_to:
        today = datetime.now(NY_TZ)
        period_from = today.replace(day=1).strftime("%Y-%m-%d")
        period_to = today.strftime("%Y-%m-%d")

    context_data = {}
    context_data["_period"] = {"date_from": period_from, "date_to": period_to}
    # ВСЕГДА добавляем keywords в data_needed — реальные настройки из API.
    # Исключение: если запрос только про GBP отзывы — не нужны keywords/negatives
    _gbp_only = data_needed == ["gbp_reviews"]
    if not _gbp_only:
        if "keywords" not in data_needed and data_needed != ["none"]:
            data_needed.append("keywords")
        # ВСЕГДА добавляем негативы — без них бот не может создать карточки удаления.
        if "negatives" not in data_needed and data_needed != ["none"]:
            data_needed.append("negatives")
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
        if not context_data or list(context_data.keys()) == ["_period"]:
            context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary()
    except Exception as e:
        log.error(f"Ошибка сбора данных для голосового: {e}")
        await _safe_edit(status_msg, f"❌ Ошибка получения данных: {e}")
        return

    await _safe_edit(status_msg, f"🎙 _{question}_\n\n🤔 Анализирую...", parse_mode="Markdown")
    action_type = classification.get("action_type", "none")
    try:
        result = await ai_analyst.chat_action(question, context_data, action_type, history=history)
    except Exception as e:
        log.error(f"Ошибка chat_action (голос): {e}")
        await _safe_edit(status_msg, f"❌ Ошибка анализа: {e}")
        return

    reply = result.get("reply", "⚠️ Ответ получен в неожиданном формате.")
    reply = _guard_against_hallucinated_execution(reply)
    full_reply = f"🎙 _{question}_\n\n{reply}"
    await _safe_edit(status_msg, full_reply, parse_mode="Markdown")
    await _append_history(ctx, chat_id, question, reply)

    for action in result.get("proposed_actions", []):
        try:
            action.setdefault("account", accounts[0])
            action.setdefault("data_summary", action.get("reasoning", ""))
            action.setdefault("expected_impact", "")
            action.setdefault("requires_approval", True)
            if not _action_ids_verified(action, context_data):
                continue
            action_id = await pending.add(action)
            await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
        except Exception as e:
            log.error(f"Ошибка карточки из голосового: {e}")


async def handle_photo_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Принимает фото/скриншот, отправляет в Claude Vision для анализа,
    затем продолжает как обычный текстовый запрос (с контекстом из фото).
    """
    if not _is_owner(update):
        return

    caption = update.message.caption or ""
    photo = update.message.photo[-1]  # наибольшее разрешение

    # Проверяем — ждём ли фото для GBP поста
    pending_gbp_data = _GBP_PHOTO_PENDING.get(update.effective_chat.id)
    if pending_gbp_data:
        status_msg = await update.message.reply_text("📸 Загружаю фото в GBP...")
        try:
            tg_file = await ctx.bot.get_file(photo.file_id)
            import httpx as _hx_dl
            _tg_fp = tg_file.file_path
            _tg_full_url = f'https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{_tg_fp}' if not _tg_fp.startswith('http') else _tg_fp
            async with _hx_dl.AsyncClient(timeout=30) as _hx_dl_c:
                _tg_dl_r = await _hx_dl_c.get(_tg_full_url)
                file_bytes = _tg_dl_r.content
            # Загружаем фото напрямую в GBP
            _gbp = globals().get("gbp_client_inst")
            if _gbp:
                import tempfile, os as _os
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp.write(bytes(file_bytes))
                    tmp_path = tmp.name
                # Загружаем через upload_media с file_path
                import httpx as _httpx
                token = await _gbp._get_access_token()
                loc_id = _gbp.LOCATION_NAME.split("/locations/")[-1] if hasattr(_gbp, "LOCATION_NAME") else ""
                # Получаем текст поста
                post_text = pending_gbp_data.get("post_text", "")
                # Публикуем с фото через Telegram file URL
                # Скачиваем фото из Telegram и загружаем на ImgBB
                import httpx as _hx, base64 as _b64
                _fp2 = tg_file.file_path
                tg_dl_url = _fp2 if _fp2.startswith("http") else f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{_fp2}"
                async with _hx.AsyncClient(timeout=30) as _hxc:
                    _img_resp = await _hxc.get(tg_dl_url)
                    _img_bytes = _img_resp.content
                _imgbb_key = getattr(config, "IMGBB_API_KEY", "")
                if not _imgbb_key:
                    raise ValueError("IMGBB_API_KEY не настроен")
                _img_b64 = _b64.b64encode(_img_bytes).decode()
                async with _hx.AsyncClient(timeout=30) as _hxc2:
                    _ib_resp = await _hxc2.post(
                        "https://api.imgbb.com/1/upload",
                        data={"key": _imgbb_key, "image": _img_b64, "expiration": 600}
                    )
                    _ib_data = _ib_resp.json()
                _pub_url = _ib_data.get("data", {}).get("url", "")
                if not _pub_url:
                    raise ValueError(f"ImgBB upload failed: {_ib_data}")
                result = await _gbp.create_post(text=post_text, image_url=_pub_url)
                _os.unlink(tmp_path)
                _GBP_PHOTO_PENDING.pop(update.effective_chat.id, None)
                if result.get("success"):
                    await status_msg.edit_text(f"✅ Пост с фото опубликован в GBP!")
                else:
                    await status_msg.edit_text(f"❌ Ошибка: {result.get('error')}")
            else:
                await status_msg.edit_text("❌ GBP не подключён")
        except Exception as _pe:
            log.error(f"GBP photo post error: {_pe}")
            await status_msg.edit_text(f"❌ Ошибка загрузки фото: {_pe}")
        return

    status_msg = await update.message.reply_text("🖼 Анализирую скриншот...")
    try:
        tg_file = await ctx.bot.get_file(photo.file_id)
        import httpx as _hx_dl
        _tg_fp = tg_file.file_path
        _tg_full_url = f'https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{_tg_fp}' if not _tg_fp.startswith('http') else _tg_fp
        async with _hx_dl.AsyncClient(timeout=30) as _hx_dl_c:
            _tg_dl_r = await _hx_dl_c.get(_tg_full_url)
            file_bytes = _tg_dl_r.content
        image_b64 = base64.b64encode(bytes(file_bytes)).decode()
    except Exception as e:
        log.error(f"Ошибка загрузки фото: {e}")
        await status_msg.edit_text(f"❌ Не удалось загрузить фото: {e}")
        return

    # Отправляем в Claude Vision для получения текстового описания
    try:
        vision_response = await ai_analyst.client.messages.create(
            model=ai_analyst.model,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Это скриншот из Google Ads или Telegram-бота BCHD Marketer. "
                            f"Опиши что на нём: цифры, статусы кампаний, ключевые слова, ошибки. "
                            f"Будь конкретным. Пользователь добавил подпись: '{caption}'"
                            if caption else
                            f"Это скриншот из Google Ads или Telegram-бота BCHD Marketer. "
                            f"Опиши что на нём: цифры, статусы кампаний, ключевые слова, ошибки. Будь конкретным."
                        ),
                    },
                ],
            }],
        )
        vision_text = vision_response.content[0].text if vision_response.content else ""
    except Exception as e:
        log.error(f"Ошибка Vision анализа: {e}")
        await status_msg.edit_text(f"❌ Ошибка анализа изображения: {e}")
        return

    # Формируем вопрос из описания + подписи пользователя
    question = f"[Скриншот]\n{vision_text}"
    if caption:
        question = f"{caption}\n\n[Скриншот содержит:]\n{vision_text}"

    await _safe_edit(status_msg, f"🖼 Вижу: _{vision_text[:200]}..._\n\n🧭 Анализирую...", parse_mode="Markdown")

    chat_id = update.effective_chat.id
    history = await _get_history(ctx, chat_id)

    if not config.google_ads_configured:
        answer = await ai_analyst.answer_question(question, history=history)
        answer = _guard_against_hallucinated_execution(answer)
        await _safe_edit(status_msg, answer, parse_mode="Markdown")
        await _append_history(ctx, chat_id, question, answer)
        return

    try:
        classification = await ai_analyst.classify_request(question, history=history)
    except Exception as e:
        classification = {"intent": "chat", "action_type": "none", "days": 0, "data_needed": ["campaigns"], "account": "both"}

    context_data = {}
    today = datetime.now(NY_TZ)
    period_from = today.replace(day=1).strftime("%Y-%m-%d")
    period_to = today.strftime("%Y-%m-%d")
    context_data["_period"] = {"date_from": period_from, "date_to": period_to}
    try:
        context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary(date_from=period_from, date_to=period_to)
    except Exception as e:
        log.error(f"Ошибка сбора данных для фото: {e}")

    action_type = classification.get("action_type", "none")
    try:
        result = await ai_analyst.chat_action(question, context_data, action_type, history=history)
    except Exception as e:
        await _safe_edit(status_msg, f"❌ Ошибка: {e}")
        return

    reply = result.get("reply", "⚠️ Ответ в неожиданном формате.")
    reply = _guard_against_hallucinated_execution(reply)
    await _safe_edit(status_msg, reply, parse_mode="Markdown")
    await _append_history(ctx, chat_id, question, reply)


# ── Обработка кнопок ────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as e:
        # "Query is too old" — безобидная ошибка Telegram: callback устарел
        # (например, обработка предыдущего нажатия заняла много времени).
        # Сама операция (execute_action и т.д.) ниже всё равно выполнится
        # нормально — просто не показываем "часики" на кнопке. Не стоит
        # тревожить владельца этим как "внутренней ошибкой".
        log.warning(f"Не удалось ответить на callback query (не критично): {e}")

    if update.effective_user.id != config.OWNER_CHAT_ID:
        return

    data = query.data
    parts = data.split(":")

    if len(parts) == 2:
        cmd, param = parts
        chat_id = query.message.chat_id  # для сохранения в историю

        if cmd == "approve":
            action = await pending.approve(param)
            if not action:
                await _safe_edit(query, "⚠️ Действие не найдено или уже выполнено.")
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
            await _safe_edit(query, f"⏳ Применяю: {action['description']}...{stale_note}", parse_mode="Markdown")
            # Специальная обработка GBP действий
            # Email рассылка
            if action.get("type") == "send_email_campaign":
                try:
                    await _safe_edit(query, f"📧 Отправляю рассылку по {action.get('total_clients', '?')} клиентам...")
                    result = await _execute_email_campaign(action)
                    if result.get("success"):
                        await _safe_edit(query,
                            f"✅ *Рассылка завершена*\n\n"
                            f"• Отправлено: {result.get('sent', 0)}\n"
                            f"• Ошибок: {result.get('failed', 0)}\n"
                            f"• Всего получателей: {result.get('total', 0)}",
                            parse_mode="Markdown"
                        )
                    else:
                        await _safe_edit(query, f"❌ Ошибка рассылки: {result.get('error')}")
                    return
                except Exception as e:
                    await _safe_edit(query, f"❌ Ошибка: {e}")
                    return

            _gbp_action_types = ("reply_to_review", "update_gbp_description",
                                  "update_gbp_categories", "create_gbp_post")
            if action.get("type") in _gbp_action_types:
                try:
                    _gbp_inst = globals().get("gbp_client_inst")
                    if not _gbp_inst:
                        await _safe_edit(query, "GBP API не настроен")
                        return
                    atype = action.get("type")
                    if atype == "reply_to_review":
                        result = await _gbp_inst.reply_to_review(
                            action.get("review_name") or f"accounts/114321652527408044999/locations/3876947509759665211/reviews/{action.get('review_id', '')}", action.get("reply_text") or action.get("body_text", "")
                        )
                    elif atype == "update_gbp_description":
                        result = await _gbp_inst.update_description(action.get("description_text") or action.get("description") or action.get("text", ""))
                    elif atype == "update_gbp_categories":
                        _primary = action.get("primary_category_id") or action.get("primary_category") or ""
                        _additional = action.get("additional_category_ids") or action.get("additional_categories") or []
                        result = await _gbp_inst.update_categories(_primary, _additional)
                    elif atype in ("enable_ad_group", "pause_ad_group"):
                        ag_id = action.get("ad_group_id", "")
                        ag_name = action.get("ad_group_name", ag_id)
                        if atype == "enable_ad_group":
                            result = await ads_client.enable_ad_group(ag_id)
                        else:
                            result = await ads_client.pause_ad_group(ag_id)
                        if result.get("success"):
                            status_word = "включена" if atype == "enable_ad_group" else "приостановлена"
                            await _safe_edit(query, f"✅ Группа '{ag_name}' {status_word}")
                        else:
                            await _safe_edit(query, f"❌ Ошибка: {result.get('error')}")
                    elif atype == "create_gbp_post":
                        _post_text = action.get("post_text") or action.get("summary") or action.get("text", "")
                        _image_url = None
                        # Генерируем изображение через Ideogram если есть промпт
                        _img_prompt = action.get("ideogram_prompt") or action.get("image_prompt")
                        if _img_prompt:
                            try:
                                from email_agent import generate_ideogram_image
                                await _safe_edit(query, "🎨 Генерирую изображение для поста...")
                                _image_url = await generate_ideogram_image(_img_prompt)
                                log.info(f"GBP пост: изображение сгенерировано {_image_url}")
                            except Exception as _ie:
                                log.warning(f"Ошибка генерации изображения для GBP: {_ie}")
                        result = await _gbp_inst.create_post(
                            text=_post_text,
                            image_url=_image_url
                        )
                    else:
                        result = {"success": False, "error": "Неизвестный тип"}
                    if result.get("success"):
                        _state = result.get("state", "")
                        _state_label = " (LIVE ✅)" if _state == "LIVE" else f" ({_state})" if _state else ""
                        await _safe_edit(query, f"✅ Пост опубликован в Google Business Profile{_state_label}")
                    else:
                        await _safe_edit(query, "Ошибка публикации: " + str(result.get("error")))
                    return
                except Exception as e:
                    await _safe_edit(query, "Ошибка GBP: " + str(e))
                    return

            try:
                result = await ads_client.execute_action(action)
            except Exception as e:
                log.error(f"Ошибка выполнения {param}: {e}")
                friendly = _translate_google_ads_error(e)
                await _safe_edit(query, f"❌ Ошибка: {friendly}")
                return

            await _safe_edit(query, f"🔍 Перепроверяю фактическое состояние...")
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
            await pending.record_execution_result(param, verified)
            if verified is True:
                # Формируем детали проверки в зависимости от типа действия
                action_type = action.get('type', '')
                verify_details = ""
                if action_type == 'update_bid':
                    actual = verification.get('actual_bid')
                    expected = verification.get('expected_bid')
                    if actual is not None:
                        verify_details = f"\n📊 Текущая ставка в настройках: *${actual:.2f}* (ожидалось ${expected:.2f})"
                elif action_type in ('pause_keywords', 'enable_keywords'):
                    cnt = verification.get('verified_count', 0)
                    total = verification.get('total', 0)
                    status = 'PAUSED' if action_type == 'pause_keywords' else 'ENABLED'
                    verify_details = f"\n📊 Статус в настройках: {cnt}/{total} ключей = {status}"
                elif action_type == 'add_negative_keywords':
                    missing = verification.get('missing_terms', [])
                    expected = verification.get('expected_terms', [])
                    if not missing:
                        verify_details = f"\n📊 Все {len(expected)} минус-слов найдены в настройках кампании"
                elif action_type == 'budget_change':
                    actual = verification.get('actual_budget')
                    if actual is not None:
                        verify_details = f"\n📊 Текущий бюджет в настройках: *${actual:.2f}/день*"
                elif action_type == 'remove_negative_keyword':
                    verify_details = f"\n📊 Минус-слово отсутствует в настройках — удаление подтверждено"

                # Логируем изменение для анализа через 7 дней
                await _log_ads_change(action, result)
                text = (
                    f"✅ *Выполнено и подтверждено:* {action['description']}\n\n"
                    f"{result.get('summary', str(result))}"
                    f"{verify_details}\n\n"
                    f"_Проверено по текущим настройкам Google Ads (не по метрикам). "
                    f"В отчётах изменение отразится через 3-24 часа._"
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
                    f"⚠️ *Не подтверждено:* {action['description']}\n\n"
                    f"{result.get('summary', str(result))}\n\n"
                    f"_API принял запрос без ошибок, но перепроверить факт применения "
                    f"автоматически невозможно для этого типа действия. "
                    f"Проверь вручную в Google Ads UI._"
                )
            await _safe_edit(query, text, parse_mode="Markdown")
            return

        elif cmd == "delete_keywords":
            action = await pending.approve(param)
            if not action:
                await _safe_edit(query, "⚠️ Действие не найдено или уже выполнено.")
                return
            await _safe_edit(query, f"⏳ Удаляю ключевые слова (необратимо): {action['description']}...")
            try:
                # Вызываем delete_keywords напрямую — НЕ через execute_action
                result = await ads_client.delete_keywords(action)
                summary = result.get('summary', '')
                await _safe_edit(
                    query,
                    f"🗑 *Удалено (необратимо):* {action['description']}\n\n{summary}",
                    parse_mode="Markdown"
                )
                log.info(f"delete_keywords SUCCESS: {summary}")
            except Exception as e:
                log.error(f"delete_keywords FAILED: {e}")
                friendly = _translate_google_ads_error(e)
                await _safe_edit(query, f"❌ Ошибка удаления: {friendly}")
            return

        elif cmd == "reject":
            action = await pending.reject(param)
            if action:
                status = action.get("status", "rejected")
                if status == "rejected":
                    await _safe_edit(query, f"❌ Отклонено: {action.get('description', param)}")
                else:
                    await _safe_edit(query, f"⚠️ Действие уже имеет статус '{status}'.")
            else:
                await _safe_edit(query, "⚠️ Карточка не найдена (бот перезапускался). Теперь это исправлено — карточки хранятся в Postgres.")
            return

        elif cmd == "comment":
            ctx.user_data["awaiting_comment_for"] = param
            await _safe_edit(query, f"💬 Напиши свой вопрос или уточнение — я отвечу и обновлю карточку.\n\n🆔 `{param}`", parse_mode="Markdown")
            return

        account = param
        account_label = {"ads": "Google Ads (936)", "lsa": "LSA (667)", "both": "Оба аккаунта"}.get(account, account)

        if cmd == "report":
            await _safe_edit(query, f"📊 Отчёт: {account_label}... (~30 сек)")
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
                await _safe_edit(query, f"❌ Ошибка: {e}")

        elif cmd == "audit":
            await _safe_edit(query, f"🔍 Аудит: {account_label}... (~40 сек)")
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
                        action_id = await pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
            except Exception as e:
                log.error(f"Ошибка audit callback: {e}")
                await _safe_edit(query, f"❌ Ошибка: {e}")

        elif cmd == "budget":
            await _safe_edit(query, f"💰 Бюджет: {account_label}... (~20 сек)")
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
                        action_id = await pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                await _safe_edit(query, text, parse_mode="Markdown")
                # Сохраняем в историю
                await _save_cmd_result(ctx, chat_id, f"/budget ({account_label})", text)
            except Exception as e:
                log.error(f"Ошибка budget callback: {e}")
                await _safe_edit(query, f"❌ Ошибка: {e}")

        elif cmd == "keywords":
            await _safe_edit(query, f"🔑 Ключевые слова: {account_label}... (~30 сек)")
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
                        action_id = await pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                if account == "both":
                    texts.append("ℹ️ *LSA:* ключевые слова не используются (Google определяет аудиторию автоматически)")
                result_text = "\n\n".join(texts)
                await _safe_edit(query, result_text, parse_mode="Markdown")
                # Сохраняем в историю — ключевой фикс: агент теперь помнит результат /keywords
                await _save_cmd_result(ctx, chat_id, f"/keywords ({account_label})", result_text)
            except Exception as e:
                log.error(f"Ошибка keywords callback: {e}")
                await _safe_edit(query, f"❌ Ошибка: {e}")

        elif cmd == "negatives":
            await _safe_edit(query, f"🚫 Минус-слова: {account_label}... (~30 сек)")
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
                            action_id = await pending.add(action)
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
                    await _safe_edit(query, "✅ Анализ завершён — карточки одобрения отправлены выше.")
                else:
                    await _safe_edit(query, 
                        "✅ Анализ завершён — нерелевантных запросов не найдено, "
                        "минус-слова добавлять не требуется. Карточек одобрения нет."
                    )
                await _save_cmd_result(ctx, chat_id, f"/negatives ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка negatives callback: {e}")
                await _safe_edit(query, f"❌ Ошибка: {e}")

        elif cmd == "competitors":
            await _safe_edit(query, f"🏆 Конкуренты: {account_label}... (~30 сек)")
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
                await _safe_edit(query, "✅ Анализ конкурентов завершён.")
                if summary_parts:
                    await _save_cmd_result(ctx, chat_id, f"/competitors ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка competitors callback: {e}")
                await _safe_edit(query, f"❌ Ошибка: {e}")

        elif cmd == "abtest":
            await _safe_edit(query, f"🧪 A/B тест: {account_label}... (~30 сек)")
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
                await _safe_edit(query, "✅ A/B анализ завершён.")
                if summary_parts:
                    await _save_cmd_result(ctx, chat_id, f"/abtest ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка abtest callback: {e}")
                await _safe_edit(query, f"❌ Ошибка: {e}")

        elif cmd == "seasonal":
            await _safe_edit(query, f"🍂 Сезонный план: {account_label}... (~30 сек)")
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
                        action_id = await pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                await _safe_edit(query, "✅ Сезонный план готов — карточки отправлены выше.")
                if summary_parts:
                    await _save_cmd_result(ctx, chat_id, f"/seasonal ({account_label})", "\n\n".join(summary_parts))
            except Exception as e:
                log.error(f"Ошибка seasonal callback: {e}")
                await _safe_edit(query, f"❌ Ошибка: {e}")


# ── Вспомогательные форматтеры ───────────────────────────

# Только фразы которые бот физически не может написать в свободном чате —
# они зарезервированы за кодом handle_callback после реального execute_action().
# НЕ используем широкие паттерны — они давали ложные срабатывания на
# обычный аналитический текст (✅ в списке кампаний, "активна" как статус и т.п.)
_HALLUCINATED_EXECUTION_PHRASES = [
    "выполнено и подтверждено",
    "подтверждено повторным запросом к google ads api",
    "изменение реально применилось",
    "выполнено и перепроверено",
    "действие выполнено через google ads api",
    "применяю действие прямо сейчас",
]


def _guard_against_hallucinated_execution(reply: str) -> str:
    """
    Узкий фильтр: ловит только фразы, которые бот физически не должен
    писать в свободном чате (они зарезервированы за handle_callback).
    НЕ срабатывает на обычный аналитический текст с ✅/🟢 или словами
    "активна", "применилось" в контексте описания данных.
    """
    lower = reply.lower()
    triggered = any(phrase in lower for phrase in _HALLUCINATED_EXECUTION_PHRASES)
    if triggered:
        log.warning(f"Ложная формулировка о выполнении в свободном тексте: {reply[:200]!r}")
        # Не добавляем страшное предупреждение в начало — это путает больше чем помогает.
        # Вместо этого просто логируем и возвращаем ответ как есть.
        # Реальная защита — в system_prompt ai_analyst.py (запрет этих фраз).
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


async def scheduled_gbp_weekly_post(app):
    """
    Каждую среду в 10:00 — генерирует сезонный пост для GBP и присылает
    карточку на одобрение. Владелец одобряет → пост публикуется автоматически.
    """
    _inst = globals().get("gbp_client_inst")
    if not _gbp_available or not _inst:
        return
    log.info("Генерация еженедельного GBP поста")
    try:
        # Получаем последние посты чтобы не повторяться
        posts = await _inst.get_posts(page_size=3)
        last_posts = posts.get("posts", [])
        last_topics = [p.get("text", "")[:50] for p in last_posts]

        today = datetime.now(NY_TZ)
        month = today.month

        # Сезонная тема
        if month in [6, 7, 8]:
            theme = "AC / refrigerator repair (summer peak season)"
            hint = "Focus on same-day AC repair, fridge not cooling, ice maker issues"
        elif month in [9, 10, 11]:
            theme = "oven, stove, dishwasher repair (pre-holiday season)"
            hint = "Focus on getting appliances ready before holidays"
        elif month in [12, 1, 2]:
            theme = "heating, washer/dryer, refrigerator (winter)"
            hint = "Focus on heating systems, winter appliance issues"
        else:
            theme = "general appliance repair (spring)"
            hint = "Focus on spring cleaning, washer/dryer tune-up"

        context_data = {
            "_period": {"date_from": "", "date_to": ""},
            "gbp_posts": posts,
            "current_season": theme,
            "posting_hint": hint,
            "last_post_topics": last_topics,
        }

        question = (
            f"Напиши новый пост для Google Business Profile на тему: {theme}. "
            f"Подсказка: {hint}. "
            f"Последние посты были про: {last_topics}. "
            f"НЕ повторяй те же темы. "
            f"Пост должен быть на английском, 150-250 слов, "
            f"с призывом позвонить (917) 935-4553 или зайти на bchdcompany.com. "
            f"Создай карточку create_gbp_post."
        )

        result = await ai_analyst.chat_action(question, context_data, "create_gbp_post")
        reply = result.get("reply", "")
        if reply:
            await _safe_send(app.bot, config.OWNER_CHAT_ID,
                f"📝 *Еженедельный пост GBP*\n\n{reply}", parse_mode="Markdown")

        for action in result.get("proposed_actions", []):
            if action.get("type") == "create_gbp_post":
                action.setdefault("requires_approval", True)
                action_id = await pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)

    except Exception as e:
        log.error(f"Ошибка еженедельного GBP поста: {e}")


async def scheduled_gbp_profile_check(app):
    """
    Еженедельная проверка профиля GBP — каждый понедельник в 09:45.
    Анализирует заполненность, категории, посты и создаёт карточки на улучшения.
    """
    _inst = globals().get("gbp_client_inst")
    if not _gbp_available or not _inst:
        return
    log.info("Мониторинг профиля GBP")
    try:
        profile = await _inst.get_profile()
        posts = await _inst.get_posts(page_size=3)
        reviews = await _inst.get_unanswered_reviews(days=7)
        insights = await _inst.get_insights(days=28)

        context_data = {
            "_period": {"date_from": "", "date_to": ""},
            "gbp_profile": profile,
            "gbp_posts": posts,
            "gbp_reviews": reviews,
            "gbp_insights": insights,
        }

        question = (
            "Проанализируй данные Google Business Profile. "
            "Проверь категории, описание, посты, отзывы без ответа. "
            "Создай карточки на все улучшения которые можно сделать через API: "
            "добавить категории, обновить описание, опубликовать пост, ответить на отзывы. "
            "Не создавай карточки на фото и логотип — только текстовое напоминание."
        )

        result = await ai_analyst.chat_action(question, context_data, "action")
        reply = result.get("reply", "")
        if reply:
            header = f"📍 *Еженедельный мониторинг GBP*\n\n"
            await _send_long_message(app.bot, config.OWNER_CHAT_ID, header + reply)

        for action in result.get("proposed_actions", []):
            try:
                action.setdefault("requires_approval", True)
                action_id = await pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
            except Exception as e:
                log.error(f"GBP profile card error: {e}")

    except Exception as e:
        log.error(f"Ошибка мониторинга профиля GBP: {e}")


async def scheduled_gbp_reviews(app):
    """Ежедневная проверка отзывов GBP в 09:30."""
    _inst = globals().get("gbp_client_inst")
    if not _gbp_available or not _inst:
        return
    log.info("Проверка отзывов GBP")
    try:
        result = await _inst.get_unanswered_reviews(days=3)
        reviews = result.get("unanswered", [])
        if not reviews:
            return
        for review in reviews[:5]:
            rating = review.get("rating", "")
            star_map = {"ONE": "1*", "TWO": "2*", "THREE": "3*", "FOUR": "4*", "FIVE": "5*"}
            stars = star_map.get(rating, rating)
            author = review.get("author", "Аноним")
            comment = review.get("comment", "")[:400]
            text = (
                "*Новый отзыв Google без ответа*\n\n"
                + stars + " — *" + author + "*\n"
                + comment + "\n\n"
                + "Напиши боту: Ответь на отзыв от " + author
            )
            await _safe_send(app.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка проверки отзывов GBP: {e}")


async def cmd_reviews(update, ctx):
    """/reviews — показать отзывы GBP без ответа."""
    if not _is_owner(update):
        return
    _inst = globals().get("gbp_client_inst")
    if not _gbp_available or not _inst:
        await update.message.reply_text("GBP API не настроен.")
        return
    msg = await update.message.reply_text("Загружаю отзывы...")
    try:
        result = await _inst.get_unanswered_reviews(days=30)
        reviews = result.get("unanswered", [])
        if not reviews:
            await _safe_edit(msg, "Все отзывы за 30 дней имеют ответ.")
            return
        count = len(reviews)
        text = "*Отзывы без ответа (" + str(count) + "):*\n\n"
        for r in reviews[:5]:
            rating = r.get("rating", "")
            star_map = {"ONE": "1*", "TWO": "2*", "THREE": "3*", "FOUR": "4*", "FIVE": "5*"}
            stars = star_map.get(rating, rating)
            author = r.get("author", "Аноним")
            comment = r.get("comment", "")[:200]
            text += stars + " *" + author + "*\n" + comment + "\n\n"
        await _safe_edit(msg, text, parse_mode="Markdown")
    except Exception as e:
        await _safe_edit(msg, "Ошибка: " + str(e))


async def _build_weekly_strategy() -> str:
    """Строит еженедельный стратегический план на основе данных."""
    today = datetime.now(NY_TZ)
    week_to = today.strftime("%Y-%m-%d")
    week_from = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        ads_data = await ads_client.get_both_accounts_summary(date_from=week_from, date_to=week_to)
        combined = ads_data.get("combined", {})
        google = ads_data.get("google_ads", {})
        lsa = ads_data.get("lsa", {})
    except Exception:
        combined = google = lsa = {}
    strategy_ctx = {}
    if strategy_memory:
        try:
            strategy_ctx = await strategy_memory.build_context_for_agent()
        except Exception:
            pass
    context = {
        "_period": {"date_from": week_from, "date_to": week_to},
        "google_ads_week": {
            "spend": combined.get("total_spend", 0),
            "conversions": combined.get("total_conversions", 0),
            "avg_cpa": combined.get("avg_cpa", 0),
            "search_spend": google.get("total_spend", 0),
            "search_conv": google.get("total_conversions", 0),
            "lsa_spend": lsa.get("total_spend", 0),
            "lsa_conv": lsa.get("total_conversions", 0),
        },
        "strategy_context": strategy_ctx,
        "thumbtack_budget": config.THUMBTACK_WEEKLY_BUDGET,
    }
    try:
        prompt = (
            "На основе данных за прошлую неделю составь КОНКРЕТНЫЙ стратегический план "
            "на следующие 7 дней для BCHD Appliance Repair. "
            "Формат: 3-5 конкретных приоритетов с обоснованием. "
            "Учитывай историю принятых решений. "
            "Не повторяй уже выполненные действия. "
            "Отвечай на русском."
        )
        result = await ai_analyst.chat_action(prompt, context, "strategy_plan")
        plan_text = result.get("reply", "")
    except Exception as e:
        plan_text = f"Ошибка генерации плана: {e}"
    if strategy_memory and plan_text:
        try:
            await strategy_memory.save_weekly_plan(plan_text, week_from, week_to)
        except Exception:
            pass
    spend = combined.get("total_spend", 0)
    conv = combined.get("total_conversions", 0)
    text = f"Стратегия на неделю — {today.strftime('%d.%m.%Y')}\n"
    text += f"На основе {week_from} — {week_to}\n\n"
    text += f"Прошлая неделя: расход ${spend:.2f} | конверсии {conv:.0f}\n\n"
    text += f"План на эту неделю:\n{plan_text}"
    return text


async def cmd_strategy(update, ctx):
    """/strategy — стратегический план на текущую неделю."""
    if not _is_owner(update):
        return
    msg = await update.message.reply_text("Формирую стратегический план...")
    try:
        text = await _build_weekly_strategy()
        await _safe_edit(msg, text)
        await _save_cmd_result(ctx, update.effective_chat.id, "/strategy", text)
    except Exception as e:
        log.error(f"Ошибка /strategy: {e}")
        await _safe_edit(msg, f"Ошибка: {e}")


async def scheduled_weekly_strategy(app):
    """Еженедельный стратегический план — каждый понедельник в 08:45."""
    if not config.google_ads_configured:
        return
    log.info("Еженедельный стратегический план")
    try:
        text = await _build_weekly_strategy()
        await _safe_send(app.bot, config.OWNER_CHAT_ID, text)
    except Exception as e:
        log.error(f"Ошибка еженедельной стратегии: {e}")



async def scheduled_gbp_review_check(app):
    """
    Ежедневно в 10:00 NY — проверяет новые отзывы GBP.
    Создаёт карточки на одобрение ответов.
    """
    if not _gbp_available:
        return
    try:
        gbp = init_gbp_client(config)
        reviews_data = await gbp.get_unanswered_reviews()
        reviews = reviews_data if isinstance(reviews_data, list) else reviews_data.get("reviews", [])

        if not reviews:
            log.info("GBP: новых отзывов без ответа нет")
            return

        log.info(f"GBP: найдено {len(reviews)} отзывов без ответа")

        for review in reviews:
            review_id = review.get("review_id") or review.get("reviewId", "")
            review_name = review.get("name", "")  # полный resource_name для API
            author = review.get("author") or review.get("reviewer", {}).get("displayName", "Клиент")
            rating = review.get("rating") or review.get("starRating", "")
            comment = review.get("comment", "")[:300]
            create_time = review.get("update_time") or review.get("createTime", "")[:10]

            # Генерируем ответ через Claude
            stars = {"ONE": "1★", "TWO": "2★", "THREE": "3★", "FOUR": "4★", "FIVE": "5★"}.get(rating, rating)

            reply_prompt = (
                f"You are responding to a Google review for BCHD Appliance Repair & HVAC in NYC. "
                f"Write a professional, warm, personalized response in English. "
                f"Reviewer: {author}. Rating: {stars}. Review: '{comment}'. "
                f"Guidelines: thank by name, reference specific details from review, "
                f"mention BCHD team if relevant, invite to contact again. "
                f"Max 150 words. No generic templates."
            )

            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{"role": "user", "content": reply_prompt}]
            )
            suggested_reply = resp.content[0].text.strip()

            action = {
                "type": "reply_to_review",
                "review_id": review_id,
                "review_name": review_name,
                "review_author": author,
                "review_rating": stars,
                "review_comment": comment,
                "review_date": create_time,
                "suggested_reply": suggested_reply,
                "reply_text": suggested_reply,
                "description": f"Ответить на отзыв от {author} ({stars}, {create_time})",
                "reasoning": f"Отзыв без ответа — отвечаем в течение 24 часов для GBP рейтинга",
                "urgency": "low",
                "urgency_label": "Низкая",
                "confidence": "high",
                "requires_approval": True
            }
            action_id = await pending.add(action)
            await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)

    except Exception as e:
        log.error(f"Ошибка GBP review check: {e}", exc_info=True)


async def scheduled_gbp_weekly_audit(app):
    """
    Еженедельно в понедельник 10:30 NY — аудит GBP профиля.
    Анализирует заполненность и создаёт карточки на улучшения.
    """
    if not _gbp_available:
        return
    try:
        gbp = init_gbp_client(config)
        data = await gbp.get_profile_completeness()

        score = data.get("score", 0)
        tips = data.get("tips", [])
        photo_count = data.get("photo_count", 0)
        review_count = data.get("review_count", 0)
        rating = data.get("rating", 0)

        # Отправляем сводку
        msg = (
            "\U0001f4ca *GBP Еженедельный аудит*\n\n"
            f"\U0001f3af Заполненность: *{score}%*\n"
            f"\U0001f4f8 Фото: {photo_count}\n"
            f"\u2b50 Рейтинг: {rating} ({review_count} отзывов)\n"
        )
        if tips:
            msg += "\n*Найдено улучшений:* " + str(len(tips)) + "\n"
            for tip in tips[:5]:
                msg += f"\u2022 {tip}\n"
        else:
            msg += "\n\u2705 Профиль полностью заполнен!"

        await _safe_send(app.bot, config.OWNER_CHAT_ID, msg, parse_mode="Markdown")

        # Создаём карточки на конкретные улучшения
        action_map = {
            "Добавь логотип компании": {
                "type": "gbp_add_logo",
                "description": "Добавить логотип в GBP (+7% completeness)",
                "reasoning": "Логотип повышает доверие клиентов и заполненность профиля",
                "urgency": "low", "urgency_label": "Низкая"
            },
            "Добавь дополнительные категории услуг": {
                "type": "gbp_update_categories",
                "description": "Добавить категории: Refrigerator Repair, Washer & Dryer Repair, Dishwasher Repair",
                "reasoning": "Больше категорий = больше запросов в которых показывается профиль",
                "urgency": "low", "urgency_label": "Низкая"
            },
        }

        # Карточка на новый пост если давно не публиковали
        posts = await gbp.get_posts()
        post_list = posts if isinstance(posts, list) else posts.get("localPosts", [])
        if not post_list:
            action = {
                "type": "gbp_create_post",
                "description": "Создать пост в GBP — давно не публиковали",
                "reasoning": "Регулярные посты повышают видимость профиля в поиске Google",
                "suggested_text": (
                    "Summer Special: 30% OFF appliance repair in Brooklyn, Queens & Manhattan! "
                    "AC not cooling? Fridge acting up? Call BCHD — same-day service available. "
                    "(917) 935-4553"
                ),
                "urgency": "medium", "urgency_label": "Средняя",
                "confidence": "high", "requires_approval": True
            }
            action_id = await pending.add(action)
            await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)

        # Карточка если мало фото
        if photo_count < 50:
            action = {
                "type": "gbp_add_photos",
                "description": f"Добавить фото в GBP (сейчас {photo_count}, рекомендуется 50+)",
                "reasoning": "Профили с 50+ фото получают в 2 раза больше кликов. Добавь фото техников за работой, офиса, транспорта.",
                "urgency": "low", "urgency_label": "Низкая",
                "confidence": "high", "requires_approval": False
            }
            action_id = await pending.add(action)
            await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)

    except Exception as e:
        log.error(f"Ошибка GBP weekly audit: {e}", exc_info=True)


async def cmd_gbp_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /gbppost <текст> — создаёт карточку на согласование поста в GBP.
    /gbppost offer <заголовок> | <текст> — карточка для акции.
    """
    if not _is_owner(update):
        return
    if not _gbp_available:
        await update.message.reply_text("\u274c GBP не подключён")
        return

    args_text = " ".join(ctx.args) if ctx.args else ""
    if not args_text:
        await update.message.reply_text("Использование: /gbppost <текст поста>\nИли: /gbppost offer Заголовок | Текст акции")
        return

    try:
        if args_text.startswith("offer "):
            parts = args_text[6:].split("|", 1)
            title = parts[0].strip()
            post_text = parts[1].strip() if len(parts) > 1 else title
            action_type = "create_gbp_post"
            topic_type = "OFFER"
        else:
            post_text = args_text
            action_type = "create_gbp_post"
            topic_type = "STANDARD"

        # Генерируем Ideogram промпт для изображения
        _service = "appliance repair"
        if "refrigerator" in post_text.lower() or "fridge" in post_text.lower():
            _service = "refrigerator repair"
        elif "washer" in post_text.lower() or "dryer" in post_text.lower():
            _service = "washer dryer repair"
        elif "dishwasher" in post_text.lower():
            _service = "dishwasher repair"
        elif "stove" in post_text.lower() or "oven" in post_text.lower():
            _service = "stove oven repair"
        elif "ac" in post_text.lower() or "hvac" in post_text.lower():
            _service = "AC HVAC repair"
        _ideogram_prompt = f"Real photo of a professional appliance repair technician fixing a {_service} in a New York City home kitchen, candid documentary style, natural lighting, high resolution DSLR photo, 35mm lens, authentic and realistic, no cartoon, no illustration"
        action = {
            "type": action_type,
            "post_text": post_text,
            "topic_type": topic_type,
            "ideogram_prompt": _ideogram_prompt,
            "description": f"Опубликовать пост в GBP",
            "reasoning": "Владелец запросил публикацию поста в Google Business Profile",
            "urgency": "low",
            "urgency_label": "Низкая",
            "confidence": "high",
            "requires_approval": True
        }
        action_id = await pending.add(action)
        # Сохраняем состояние — ждём фото от пользователя
        _GBP_PHOTO_PENDING[config.OWNER_CHAT_ID] = {"action_id": action_id, "post_text": post_text}
        await update.message.reply_text(
            f"📝 Текст поста готов:\n\n_{post_text[:100]}_\n\n📸 Пришли фото для публикации в GBP — или напиши \"без фото\" чтобы опубликовать без изображения."
        )

    except Exception as e:
        log.error(f"GBP post error: {e}", exc_info=True)
        await update.message.reply_text(f"\u274c Ошибка: {e}")


async def cmd_gbp_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/gbpaudit — проверяет заполненность GBP профиля."""
    if not _is_owner(update):
        return
    if not _gbp_available:
        await update.message.reply_text("\u274c GBP не подключён")
        return
    try:
        gbp = init_gbp_client(config)
        data = await gbp.get_profile_completeness()
        score = data.get("score", 0)
        tips = data.get("tips", [])
        photo_count = data.get("photo_count", 0)
        review_count = data.get("review_count", 0)
        rating = data.get("rating", 0)

        msg = (
            f"\U0001f4ca *GBP Профиль BCHD*\n\n"
            f"\U0001f3af Заполненность: *{score}%*\n"
            f"\U0001f4f8 Фото: {photo_count}\n"
            f"\u2b50 Рейтинг: {rating} ({review_count} отзывов)\n"
        )
        if tips:
            msg += f"\n*Что улучшить:*\n"
            for tip in tips:
                msg += f"\u2022 {tip}\n"
        else:
            msg += "\n\u2705 Профиль полностью заполнен!"

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"\u274c Ошибка: {e}")


async def _log_ads_change(action: dict, result: dict) -> None:
    """Логирует выполненное изменение в ads_changes_log для последующего анализа эффекта."""
    try:
        import json as _json
        from datetime import datetime as _dt, timedelta as _td
        pool = await _get_db_pool()
        if not pool:
            return
        
        action_type = action.get("type", "")
        
        # Определяем что изменилось
        change_from = {}
        change_to = {}
        keyword = action.get("keyword") or (action.get("keywords", [None])[0] if action.get("keywords") else None)
        if isinstance(keyword, dict):
            keyword = keyword.get("keyword", "")
        
        if action_type == "update_bid":
            change_from = {"bid": action.get("current_bid")}
            change_to = {"bid": action.get("new_bid")}
        elif action_type == "pause_keywords":
            change_from = {"status": "ENABLED"}
            change_to = {"status": "PAUSED"}
        elif action_type == "enable_keywords":
            change_from = {"status": "PAUSED"}
            change_to = {"status": "ENABLED"}
        elif action_type == "budget_change":
            change_from = {"budget": action.get("current_budget")}
            change_to = {"budget": action.get("proposed_budget")}
        elif action_type == "update_ad_headlines":
            change_from = {"headlines": action.get("current_headlines", [])}
            change_to = {"headlines": action.get("headlines", [])}
        elif action_type == "add_negative_keywords":
            change_to = {"negatives": action.get("keywords", action.get("negatives", []))}
        
        # Анализ через 7 дней
        analysis_due = _dt.now() + _td(days=7)
        
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO ads_changes_log 
                (action_id, action_type, description, account, keyword, 
                 change_from, change_to, applied_at, analysis_due_at, analyzed)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8, FALSE)
            """,
                action.get("id", ""),
                action_type,
                action.get("description", ""),
                action.get("account", "ads"),
                str(keyword) if keyword else None,
                _json.dumps(change_from, ensure_ascii=False),
                _json.dumps(change_to, ensure_ascii=False),
                analysis_due,
            )
        log.info(f"Изменение залогировано: {action_type} — {action.get('description', '')[:50]}")
    except Exception as e:
        log.warning(f"Ошибка логирования изменения: {e}")


async def scheduled_changes_analysis(app):
    """
    Ежедневно в 09:15 NY — проверяет изменения которые были сделаны 7 дней назад
    и анализирует их эффект на метрики кампании.
    """
    if not config.google_ads_configured:
        return
    try:
        from datetime import datetime as _dt, timedelta as _td
        import json as _json
        
        pool = await _get_db_pool()
        if not pool:
            return
        
        # Берём ВСЕ изменения которые пора проверить — без ограничения по дате
        async with pool.acquire() as conn:
            changes = await conn.fetch("""
                SELECT * FROM ads_changes_log 
                WHERE analysis_due_at <= NOW()
                ORDER BY applied_at DESC
                LIMIT 20
            """)
        
        if not changes:
            return
        
        log.info(f"Анализ эффекта изменений: {len(changes)} записей")
        
        today = _dt.now(NY_TZ)
        date_to = today.strftime("%Y-%m-%d")
        date_from = (today - _td(days=7)).strftime("%Y-%m-%d")
        
        # Получаем текущие данные кампании
        context_data = {"_period": {"date_from": date_from, "date_to": date_to}}
        context_data["keywords"] = {
            "ads": await ads_client.get_keywords_analysis(
                account="ads", date_from=date_from, date_to=date_to
            )
        }
        context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary(
            date_from=date_from, date_to=date_to
        )
        
        # Формируем список изменений для анализа
        changes_text = ""
        for ch in changes:
            applied = ch["applied_at"].strftime("%d.%m.%Y %H:%M")
            changes_text += (
                f"- {applied}: {ch['action_type']} -- {ch['description']}\n"
                f"  Было: {ch['change_from']} -> Стало: {ch['change_to']}\n"
                f"  Ключ: {ch['keyword'] or 'н/д'}\n\n"
            )
        
        question = (
            f"Еженедельный анализ эффекта изменений в рекламной кампании BCHD.\n\n"
            f"Изменения которые нужно проанализировать:\n{changes_text}\n"
            f"Данные за последние 7 дней ({date_from} — {date_to}) прикреплены.\n\n"
            f"Для КАЖДОГО изменения в журнале:\n"
            f"1. Найди соответствующий ключ/кампанию в свежих данных\n"
            f"2. Оцени метрики: CPA, CTR, конверсии, расход, QS\n"
            f"3. Вердикт: ПОМОГЛО / НЕ ПОМОГЛО / НЕЙТРАЛЬНО / РАНО СУДИТЬ\n"
            f"4. Следующий шаг: оставить / усилить / откатить / подождать ещё неделю\n"
            f"5. Если изменение работает хорошо >2 недель — отметь как СТАБИЛЬНОЕ\n"
            f"6. Для приостановленных/отменённых изменений — оцени стоит ли их возобновить\n"
            f"   Если ключ был на паузе >2 недель и ситуация изменилась (QS вырос, новый лендинг) — предложи возобновить\n\n"
            f"Формат: по одному блоку на каждое изменение. "
            f"Дай цифры до/после где возможно. "
            f"В конце — топ-3 рекомендации на следующую неделю."
        )
        
        result = await ai_analyst.chat_action(question, context_data, "action")
        reply = result.get("reply", "")
        
        if reply:
            header = f"📊 *Анализ эффекта изменений за 7 дней*\n\n"
            await _send_long_message(app.bot, config.OWNER_CHAT_ID, header + reply)
        
        # Обновляем дату следующего анализа — через 7 дней снова
        change_ids = [ch["id"] for ch in changes]
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ads_changes_log 
                   SET analysis_due_at = NOW() + INTERVAL '7 days',
                       analysis_result = $1
                   WHERE id = ANY($2::int[])""",
                _json.dumps({"reply": reply[:500]}),
                change_ids
            )
        
        # Создаём карточки если агент предложил действия
        for action in result.get("proposed_actions", []):
            if not isinstance(action, dict):
                continue
            action.setdefault("requires_approval", True)
            action_id = await pending.add(action)
            await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
            
    except Exception as e:
        log.error(f"Ошибка анализа изменений: {e}", exc_info=True)


async def scheduled_lsa_workiz_reconciliation(app):
    """
    Еженедельно в понедельник 09:30 NY — сверка LSA лидов с Workiz.
    Находит LSA лиды которые стали джобами с неправильным источником.
    Присылает отчёт с рекомендациями по исправлению.
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        log.info("Запуск LSA↔Workiz сверки")

        # Берём LSA лиды за последние 14 дней
        lsa_result = await ads_client.get_lsa_leads(days=14)
        lsa_leads = lsa_result.get("leads", []) if isinstance(lsa_result, dict) else lsa_result
        if not lsa_leads:
            return

        # Берём джобы Workiz за последние 14 дней
        import workiz_client as wz
        date_from = (_dt.now() - _td(days=14)).strftime("%Y-%m-%d")
        date_to = _dt.now().strftime("%Y-%m-%d")
        wz_result = await wz.get_jobs_by_date_range(date_from=date_from, date_to=date_to, records=100)
        wz_jobs = wz_result.get("jobs", [])

        # Строим индекс Workiz джобов по номеру телефона
        def _normalize_phone(p):
            import re
            return re.sub(r"\D", "", str(p or ""))[-10:]

        wz_by_phone = {}
        for job in wz_jobs:
            phone = _normalize_phone(job.get("Phone") or job.get("ClientPhone") or "")
            if phone:
                wz_by_phone[phone] = job

        # Сверяем
        matched = []
        unmatched = []
        wrong_source = []

        for lead in lsa_leads:
            lead_phone = _normalize_phone(lead.get("phone_number") or lead.get("phone") or "")
            if not lead_phone:
                continue

            if lead_phone in wz_by_phone:
                job = wz_by_phone[lead_phone]
                job_source = (job.get("JobSource") or "").strip()
                job_id = job.get("UUID") or job.get("SerialId") or ""
                client_name = f"{job.get('FirstName','')} {job.get('LastName','')}".strip()

                if job_source.lower() not in ["lsa", "google lsa", "local services"]:
                    wrong_source.append({
                        "lead_id": lead.get("id"),
                        "phone": lead_phone,
                        "job_id": job_id,
                        "client": client_name,
                        "current_source": job_source or "не указан",
                    })
                else:
                    matched.append(lead_phone)
            else:
                unmatched.append({
                    "phone": lead_phone,
                    "lead_id": lead.get("id"),
                    "date": str(lead.get("creation_date_time", ""))[:10],
                })

        if not wrong_source and not unmatched:
            log.info("LSA сверка: все лиды правильно атрибуированы")
            return

        # Формируем отчёт
        lines = ["📊 *Еженедельная сверка LSA ↔ Workiz*\n"]

        if wrong_source:
            lines.append(f"⚠️ *Неправильный источник ({len(wrong_source)} джобов):*")
            lines.append("Эти джобы пришли из LSA но в Workiz указан другой источник.\nИсправь вручную в Workiz:\n")
            for item in wrong_source:
                lines.append(
                    f"• Джоб {item['job_id']} — {item['client']}\n"
                    f"  📞 {item['phone']} | Источник: *{item['current_source']}* → нужно: LSA\n"
                )

        if unmatched:
            lines.append(f"\n❓ *Не найдены в Workiz ({len(unmatched)} лидов):*")
            lines.append("Эти LSA лиды не создали джоб в Workiz:\n")
            for item in unmatched[:10]:
                lines.append(f"• 📞 {item['phone']} (лид {item['lead_id']}, {item['date']})")

        if matched:
            lines.append(f"\n✅ Правильно атрибуированы: {len(matched)} лидов")

        text = "\n".join(lines)
        await _send_long_message(app.bot, config.OWNER_CHAT_ID, text)

    except Exception as e:
        log.error(f"Ошибка LSA сверки: {e}", exc_info=True)

async def scheduled_lsa_workiz_reconciliation(app):
    """
    Еженедельно в понедельник 09:30 NY — сверка LSA лидов с Workiz.
    Находит LSA лиды которые стали джобами с неправильным источником.
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        log.info("Запуск LSA↔Workiz сверки")
        lsa_result = await ads_client.get_lsa_leads(days=14)
        lsa_leads = lsa_result.get("leads", []) if isinstance(lsa_result, dict) else lsa_result
        if not lsa_leads:
            return
        import workiz_client as wz
        date_from = (_dt.now() - _td(days=14)).strftime("%Y-%m-%d")
        date_to = _dt.now().strftime("%Y-%m-%d")
        wz_result = await wz.get_jobs_by_date_range(date_from=date_from, date_to=date_to, records=100)
        wz_jobs = wz_result.get("jobs", [])
        def _norm(p):
            import re
            return re.sub(r"\D", "", str(p or ""))[-10:]
        wz_by_phone = {}
        for job in wz_jobs:
            phone = _norm(job.get("Phone") or job.get("ClientPhone") or "")
            if phone:
                wz_by_phone[phone] = job
        matched, unmatched, wrong_source = [], [], []
        for lead in lsa_leads:
            lead_phone = _norm(lead.get("phone_number") or lead.get("phone") or "")
            if not lead_phone:
                continue
            if lead_phone in wz_by_phone:
                job = wz_by_phone[lead_phone]
                job_source = (job.get("JobSource") or "").strip()
                if job_source.lower() not in ["lsa", "google lsa", "local services"]:
                    wrong_source.append({
                        "phone": lead_phone,
                        "job_id": job.get("UUID") or job.get("SerialId") or "",
                        "client": f"{job.get('FirstName','')} {job.get('LastName','')}".strip(),
                        "current_source": job_source or "не указан",
                    })
                else:
                    matched.append(lead_phone)
            else:
                unmatched.append({"phone": lead_phone, "lead_id": lead.get("id"), "date": str(lead.get("creation_date_time", ""))[:10]})
        if not wrong_source and not unmatched:
            return
        lines = ["📊 *Еженедельная сверка LSA ↔ Workiz*\n"]
        if wrong_source:
            lines.append(f"⚠️ *Неправильный источник ({len(wrong_source)} джобов):*")
            lines.append("Исправь вручную в Workiz:\n")
            for item in wrong_source:
                lines.append(f"• Джоб {item['job_id']} — {item['client']}\n  📞 {item['phone']} | Источник: *{item['current_source']}* → нужно: LSA\n")
        if unmatched:
            lines.append(f"\n❓ *Не найдены в Workiz ({len(unmatched)} лидов):*")
            for item in unmatched[:10]:
                lines.append(f"• 📞 {item['phone']} (лид {item['lead_id']}, {item['date']})")
        if matched:
            lines.append(f"\n✅ Правильно атрибуированы: {len(matched)} лидов")
        await _send_long_message(app.bot, config.OWNER_CHAT_ID, "\n".join(lines))
    except Exception as e:
        log.error(f"Ошибка LSA сверки: {e}", exc_info=True)

async def scheduled_campaign_audit(app):
    """
    Каждый понедельник в 09:55 — полный проактивный аудит кампании.
    Агент сам анализирует ключи, минус-слова, QS, IS, объявления
    и создаёт карточки на конкретные улучшения без запроса от владельца.
    """
    if not config.google_ads_configured:
        return
    log.info("Проактивный аудит кампании")
    try:
        today = datetime.now(NY_TZ)
        date_from = today.replace(day=1).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")

        # Собираем все данные для полного аудита
        context_data = {"_period": {"date_from": date_from, "date_to": date_to}}
        context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary(
            date_from=date_from, date_to=date_to
        )
        context_data["keywords"] = {
            "ads": await ads_client.get_keywords_analysis(
                account="ads", date_from=date_from, date_to=date_to
            )
        }
        context_data["budgets"] = {"ads": await ads_client.get_budget_data(account="ads")}
        context_data["negatives"] = {
            "ads": await ads_client.get_negative_keywords_list(account="ads")
        }
        context_data["search_terms"] = {
            "ads": await ads_client.get_search_terms(account="ads")
        }
        context_data["ad_performance"] = {
            "ads": await ads_client.get_ad_performance(account="ads")
        }

        question = (
            "Выполни ПОЛНЫЙ проактивный аудит рекламной кампании. "
            "Проверь ВСЁ и создай карточки на конкретные улучшения:\n"
            "1. Ключи с QS < 5 — предложи обновить заголовки объявлений (update_ad_headlines)\n"
            "2. Ключи с 0 показов за 14+ дней — предложи поднять ставку или паузу\n"
            "3. Ключи с CPA > $100 при 5+ кликах — предложи снизить ставку или паузу\n"
            "4. Поисковые запросы нерелевантные — предложи добавить минус-слова\n"
            "5. Impression Share < 40% — предложи увеличить ставки на топ-ключах\n"
            "6. Объявления с CTR < 3% при 100+ показах — предложи обновить заголовки\n"
            "7. Конфликты ключей с минус-словами активной кампании\n"
            "Создавай карточки СРАЗУ — не спрашивай разрешения. "
            "Максимум 8 карточек за раз, приоритизируй по влиянию на бизнес."
        )

        result = await ai_analyst.chat_action(question, context_data, "action")
        reply = result.get("reply", "")

        if reply:
            header = f"🔍 *Еженедельный аудит кампании — {today.strftime('%d.%m.%Y')}*\n\n"
            await _send_long_message(app.bot, config.OWNER_CHAT_ID, header + reply)

        proposed = result.get("proposed_actions", [])
        log.info(f"scheduled_campaign_audit: {len(proposed)} карточек")

        for action in proposed:
            if not isinstance(action, dict):
                log.warning(f"proposed_actions: не-dict: {str(action)[:80]}")
                continue
            try:
                action.setdefault("account", "ads")
                action.setdefault("requires_approval", True)
                action_id = await pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
            except Exception as e:
                log.error(f"campaign_audit card error: {e}")

    except Exception as e:
        log.error(f"Ошибка scheduled_campaign_audit: {e}")


async def scheduled_campaign_audit(app):
    """
    Каждый понедельник в 09:55 — полный проактивный аудит кампании:
    ключи, QS, IS, объявления, минус-слова. Создаёт карточки без запроса.
    """
    if not config.google_ads_configured:
        return
    log.info("Проактивный аудит кампании")
    try:
        today = datetime.now(NY_TZ)
        date_from = (today - timedelta(days=14)).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")

        context_data = {"_period": {"date_from": date_from, "date_to": date_to}}
        context_data["campaigns_summary"] = await ads_client.get_both_accounts_summary(
            date_from=date_from, date_to=date_to
        )
        context_data["keywords"] = {
            "ads": await ads_client.get_keywords_analysis(
                account="ads", date_from=date_from, date_to=date_to
            )
        }
        context_data["negatives"] = {
            "ads": await ads_client.get_negative_keywords_list(account="ads")
        }
        context_data["search_terms"] = {
            "ads": await ads_client.get_search_terms(account="ads")
        }
        context_data["ad_performance"] = {
            "ads": await ads_client.get_ad_performance(account="ads")
        }
        context_data["budgets"] = {
            "ads": await ads_client.get_budget_data(account="ads")
        }

        question = (
            f"Проактивный еженедельный аудит кампании BCHD Appliance Repair NYC "
            f"за {date_from} — {date_to}.\n\n"
            f"Проверь ВСЁ и создай карточки на конкретные улучшения:\n"
            f"1. ПОИСКОВЫЕ ЗАПРОСЫ — ПРИОРИТЕТ #1: проверь все запросы с расходом > $5 "
            f"и currently_excluded=false. Найди нерелевантные локации (Staten Island, Bronx, "
            f"NJ, Long Island, Westchester и т.п.), нерелевантные категории техники "
            f"(TV, microwave, vacuum, coffee, laptop и т.п.), DIY/самостоятельный ремонт. "
            f"Для каждого нерелевантного — создай карточку add_negative_keywords.\n"
            f"2. Ключи с QS < 5 — что мешает, как исправить (лендинг или заголовки)\n"
            f"3. Ключи с 0 показов > 7 дней — ставка ниже порога или другая причина\n"
            f"4. Минус-слова — нет ли конфликтов с нашими услугами\n"
            f"5. Объявления — заголовки релевантны ключам группы\n"
            f"6. IS < 40% — что конкретно мешает (ставка или QS)\n"
            f"7. Ключи с CPA > $100 при 3+ конверсиях — снизить ставку или паузировать\n\n"
            f"Для каждой найденной проблемы создай карточку. "
            f"Не более 8 карточек за раз — выбирай самые приоритетные по влиянию на бюджет."
        )

        result = await ai_analyst.chat_action(question, context_data, "action")
        reply = result.get("reply", "")

        if reply:
            header = f"🔍 *Еженедельный аудит кампании — {today.strftime('%d.%m.%Y')}*\n\n"
            await _send_long_message(app.bot, config.OWNER_CHAT_ID, header + reply)

        proposed = result.get("proposed_actions", [])
        if not proposed:
            await _safe_send(
                app.bot, config.OWNER_CHAT_ID,
                "✅ Аудит завершён — критических проблем не найдено."
            )
            return

        for action in proposed[:8]:
            try:
                action.setdefault("account", "ads")
                action.setdefault("requires_approval", True)
                if not _action_ids_verified(action, context_data):
                    log.warning(f"campaign_audit: ID не верифицирован: {action}")
                    continue
                already, reason = _action_already_applied(action, context_data)
                if already:
                    log.info(f"campaign_audit: уже применено: {reason}")
                    continue
                action_id = await pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
            except Exception as e:
                log.error(f"campaign_audit card error: {e}")

    except Exception as e:
        log.error(f"Ошибка проактивного аудита кампании: {e}")


async def scheduled_weekly_email(app):
    """
    Каждое воскресенье в 12:00 NY — спрашивает тему рассылки через email_agent.
    """
    if _email_agent_available:
        try:
            _email_agent.OWNER_CHAT_ID = str(config.OWNER_CHAT_ID)
            _email_agent.ask_campaign_topic(app.bot)
            log.info("scheduled_weekly_email: вопрос о теме отправлен")
            return
        except Exception as e:
            log.error(f"email_agent.ask_campaign_topic error: {e}")
    # Fallback — старая логика
    log.info("Fallback: старая логика weekly email")
    async def _old_weekly_email():
        """
        Каждое воскресенье в 12:00 NY — генерирует письмо для рассылки
        и присылает карточку на одобрение. После ✅ — рассылка по всем клиентам.
        """
    if not _email_available:
        return
    log.info("Генерация еженедельного email письма")
    try:
        today = datetime.now(NY_TZ)
        month = today.month

        if month in [6, 7, 8]:
            theme = "Summer AC & Refrigerator Repair Special"
            headline = "Beat the Heat This Summer"
            subheadline = "$20 OFF Your AC or Fridge Repair"
            body = (
                "Summer is tough on your appliances — especially your AC and refrigerator. "
                "If your AC isn't cooling or your fridge isn't keeping temperature, "
                "don't wait. Our licensed technicians offer same-day service across "
                "Brooklyn, Queens, and Manhattan. Call us today and get $20 off your repair!"
            )
            offer = "Same-day service • All brands • 90-day warranty"
        elif month in [9, 10, 11]:
            theme = "Get Ready for the Holidays"
            headline = "Holiday Season Appliance Check"
            subheadline = "Is Your Oven & Dishwasher Ready?"
            body = (
                "The holiday season is coming — don't let a broken oven or dishwasher "
                "ruin your family dinner. BCHD offers fast, reliable appliance repair "
                "across Brooklyn, Queens, and Manhattan. Book your service today!"
            )
            offer = "Same-day service • Licensed technicians • 90-day warranty"
        elif month in [12, 1, 2]:
            theme = "Winter Heating & Appliance Special"
            headline = "Stay Warm This Winter"
            subheadline = "Heating & Appliance Repair — Same Day"
            body = (
                "Cold weather puts extra strain on your heating system and appliances. "
                "If your furnace, washer, or refrigerator needs attention, "
                "BCHD is here 7 days a week across Brooklyn, Queens, and Manhattan."
            )
            offer = "Same-day service • All brands • Licensed & Insured"
        else:
            theme = "Spring Appliance Tune-Up"
            headline = "Spring Clean Your Appliances"
            subheadline = "Professional Appliance Repair & Maintenance"
            body = (
                "Spring is the perfect time to make sure all your appliances are "
                "running efficiently. From washers to refrigerators, BCHD provides "
                "expert repair and maintenance across Brooklyn, Queens, and Manhattan."
            )
            offer = "Same-day service • All brands • 90-day warranty"

        subject = f"BCHD: {theme}"

        # Получаем количество клиентов
        clients_data = await workiz_client.get_clients_with_email()
        total_clients = clients_data.get("total", 0)

        # Генерируем баннер через Ideogram
        banner_bytes_weekly = None
        try:
            banner_bytes_weekly = await generate_banner_ideogram(headline, subheadline, offer, theme)
        except Exception as e:
            log.warning(f"Ideogram недоступен: {e} — используем Pillow")
            try:
                import base64 as _b64
                banner_bytes_weekly = _b64.b64decode(generate_banner_base64(headline, subheadline, offer))
            except Exception as e2:
                log.warning(f"Pillow тоже не работает: {e2}")

        # Сохраняем данные рассылки для использования после одобрения
        campaign_data = {
            "subject": subject,
            "headline": headline,
            "subheadline": subheadline,
            "body_text": body,
            "offer": offer,
            "theme": theme,
            "total_clients": total_clients,
        }

        action = {
            "type": "send_email_campaign",
            "account": "email",
            "description": f"Email рассылка: {theme}",
            "subject": subject,
            "headline": headline,
            "subheadline": subheadline,
            "body_text": body,
            "offer": offer,
            "theme": theme,
            "total_clients": total_clients,
            "reasoning": (
                f"Еженедельная рассылка по {total_clients} клиентам из Workiz. "
                f"Тема: {theme}."
            ),
            "risks": "Проверь текст и баннер перед отправкой",
            "urgency": "medium",
            "urgency_label": "Средняя",
            "confidence": "high",
            "requires_approval": True,
        }

        # Отправляем превью баннера в Telegram
        if banner_b64:
            try:
                import io as _io, base64 as _b64
                banner_bytes = _b64.b64decode(banner_b64)
                banner_bio = _io.BytesIO(banner_bytes)
                banner_bio.name = "bchd_banner.png"
                await app.bot.send_photo(
                    chat_id=config.OWNER_CHAT_ID,
                    photo=banner_bio,
                    caption=(
                        f"📧 *Еженедельная email рассылка*\n\n"
                        f"*Тема:* {subject}\n"
                        f"*Оффер:* {subheadline}\n"
                        f"*Текст:* {body[:200]}...\n\n"
                        f"*Получателей:* {total_clients} клиентов из Workiz\n\n"
                        f"Если баннер устраивает — одобри карточку ниже.\n"
                        f"Если нужно изменить — напиши что поправить."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                log.warning(f"Ошибка отправки превью: {e}")
                await _safe_send(app.bot, config.OWNER_CHAT_ID,
                    f"📧 *Еженедельная email рассылка*\n\nТема: {subject}\nПолучателей: {total_clients}",
                    parse_mode="Markdown")
        else:
            await _safe_send(app.bot, config.OWNER_CHAT_ID,
                f"📧 *Еженедельная email рассылка*\n\nТема: {subject}\nПолучателей: {total_clients}",
                parse_mode="Markdown")

        action_id = await pending.add(action)
        await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)

    except Exception as e:
        log.error(f"Ошибка генерации email рассылки: {e}")


async def _execute_email_campaign(action: dict) -> dict:
    """Выполняет email рассылку после одобрения."""
    if not _email_available:
        return {"success": False, "error": "email_sender не установлен"}

    # Пробуем получить из Postgres (импортированная база)
    clients = []
    pool = await _get_db_pool()
    if pool:
        try:
            db_result = await workiz_client.get_clients_from_db(pool)
            if db_result.get("total", 0) > 0:
                clients = db_result["clients"]
                log.info(f"Email: используем БД — {len(clients)} клиентов")
        except Exception as e:
            log.warning(f"Ошибка получения из БД: {e}")

    # Fallback — из Workiz API
    if not clients:
        clients_data = await workiz_client.get_clients_with_email()
        clients = clients_data.get("clients", [])

    if not clients:
        return {"success": False, "error": "Клиентов с email не найдено"}

    # Всегда добавляем владельца первым для контроля рассылки
    owner_email = "you@bchdcompany.com"
    if not any(c["email"] == owner_email for c in clients):
        clients = [{"email": owner_email, "name": "Chingis", "first_name": "Chingis"}] + clients

    result = await send_campaign(
        clients=clients,
        subject=action.get("subject", "BCHD Appliance Repair"),
        headline=action.get("headline", "BCHD Appliance Repair & HVAC"),
        subheadline=action.get("subheadline", "Same-day service in NYC"),
        body_text=action.get("body_text", ""),
        offer=action.get("offer", ""),
    )
    return result


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
                action_id = await pending.add(action)
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
                action_id = await pending.add(action)
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
    """
    Сезонные корректировки бюджета — ТОЛЬКО если кампания уже эффективна.
    Правило: предлагаем увеличить бюджет только когда IS > 60% и CPA < $65.
    Пока IS низкий или CPA высокий — сначала оптимизируем текущие настройки.
    """
    if not config.google_ads_configured:
        return
    log.info("Сезонная оптимизация — проверка условий")
    try:
        from datetime import datetime as _dt
        today = _dt.now(NY_TZ)
        date_from = (today - timedelta(days=14)).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")

        # Проверяем текущую эффективность кампании
        summary = await ads_client.get_both_accounts_summary(date_from=date_from, date_to=date_to)
        ads_data = summary.get("google_ads", {})

        spend = ads_data.get("total_spend", 0)
        conversions = ads_data.get("total_conversions", 0)
        impression_share = ads_data.get("impression_share", 0) or 0
        cpa = spend / conversions if conversions > 0 else 999

        log.info(f"seasonal_check: IS={impression_share:.1f}%, CPA=${cpa:.0f}, конверсий={conversions:.0f}")

        # Условие: предлагаем сезонные корректировки только при хорошей эффективности
        IS_THRESHOLD = 60.0   # IS должен быть > 60%
        CPA_THRESHOLD = 65.0  # CPA должен быть < $65

        if impression_share < IS_THRESHOLD or cpa > CPA_THRESHOLD:
            log.info(
                f"seasonal_check: условия не выполнены — "
                f"IS={impression_share:.1f}% (нужно >{IS_THRESHOLD}%), "
                f"CPA=${cpa:.0f} (нужно <${CPA_THRESHOLD}). "
                f"Сначала оптимизируем текущие настройки."
            )
            return

        # Условия выполнены — предлагаем сезонные корректировки
        season_data = ads_client.get_current_season_recommendations()
        for account in ["ads"]:  # LSA бюджет меняется только вручную
            budget_data = await ads_client.get_budget_data(account=account)
            action_plan = await ai_analyst.build_seasonal_action(
                season_data, budget_data.get("campaigns", [])
            )
            if action_plan.get("adjustments"):
                action = {
                    "type": "seasonal_adjustments",
                    "account": account,
                    "description": f"Сезонные корректировки {season_data['season_name']}",
                    "reasoning": (
                        f"Условия выполнены: IS={impression_share:.1f}% > {IS_THRESHOLD}%, "
                        f"CPA=${cpa:.0f} < ${CPA_THRESHOLD}. "
                        f"{season_data['reason']}"
                    ),
                    "data_summary": (
                        f"IS={impression_share:.1f}%, CPA=${cpa:.0f}, "
                        f"конверсий за 14 дней: {conversions:.0f}"
                    ),
                    "expected_impact": action_plan.get("expected_impact", ""),
                    "urgency": "low",
                    "urgency_label": "Низкая",
                    "risks": "Обратимо. Увеличение бюджета оправдано только при текущей эффективности.",
                    "adjustments": action_plan["adjustments"],
                }
                action_id = await pending.add(action)
                await _send_approval_card(app.bot, config.OWNER_CHAT_ID, action_id, action)
    except Exception as e:
        log.error(f"Ошибка сезонной оптимизации: {e}")


# ── main ─────────────────────────────────────────────────

_last_conflict_notify_time = None

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
    global _last_conflict_notify_time
    log.error(f"Необработанное исключение: {ctx.error}", exc_info=ctx.error)

    error_str = str(ctx.error)
    if "Can't parse entities" in error_str or "parse entities" in error_str.lower():
        return
    is_conflict = "Conflict" in type(ctx.error).__name__ or "terminated by other getUpdates" in error_str

    if is_conflict:
        # Два инстанса бота одновременно опрашивают Telegram — обычно
        # кратковременный эффект передеплоя (Railway на миг запускает новый
        # процесс до полной остановки старого). Самоустраняется без
        # вмешательства. НЕ тот же класс проблемы, что зависание из-за
        # длинного ответа — не стоит присылать вводящий в заблуждение совет
        # "переформулируй короче". Также гасим повторные уведомления в
        # течение 2 минут, чтобы не заспамить одним и тем же сообщением
        # (оба конфликтующих инстанса ловят эту ошибку одновременно).
        now = datetime.now(NY_TZ)
        if _last_conflict_notify_time and (now - _last_conflict_notify_time).total_seconds() < 120:
            return
        _last_conflict_notify_time = now
        try:
            if config.OWNER_CHAT_ID:
                await ctx.bot.send_message(
                    chat_id=config.OWNER_CHAT_ID,
                    text=(
                        "ℹ️ Обнаружены два одновременных инстанса бота (обычно "
                        "кратковременный эффект передеплоя в Railway). Должно "
                        "само устраниться за несколько секунд. Если повторяется "
                        "часто вне передеплоев — проверь в Railway, что у сервиса "
                        "ровно 1 реплика (Settings -> Replicas)."
                    ),
                )
        except Exception as notify_error:
            log.error(f"Не удалось уведомить владельца об ошибке: {notify_error}")
        return

    try:
        if config.OWNER_CHAT_ID:
            error_text = error_str[:300]
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


async def cmd_clear_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Полностью очищает память переписки бота (chat_history в Postgres и
    fallback в памяти процесса) для этого чата. Полезно после запутанной
    сессии с противоречивыми фактами (например, кампания то удалена, то
    нет) — чтобы ИИ начал следующий разговор с чистого листа, без риска
    "зомби-фактов" из старой истории (см. правило в system_prompt про
    приоритет свежих данных над историей чата).
    НЕ трогает: сами данные в Google Ads, очередь pending-действий,
    memory_user_edits — только историю СВОБОДНОГО ЧАТА.
    """
    if not _is_owner(update):
        return
    chat_id = update.effective_chat.id
    deleted_count = 0
    if DATABASE_URL:
        try:
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
                # Чистим ВСЮ таблицу, а не только текущий chat_id —
                # «зомби-факты» (например, BCHD/2) могут жить в записях
                # другого chat_id (личный vs групповой) и продолжать
                # всплывать даже после очистки "своего" chat_id.
                result = await conn.execute("DELETE FROM chat_history")
                try:
                    deleted_count = int(result.split()[-1])
                except (ValueError, IndexError):
                    pass
        except Exception as e:
            log.error(f"Ошибка очистки истории в Postgres: {e}")
            await update.message.reply_text(f"❌ Ошибка при очистке памяти: {e}")
            return
    ctx.chat_data["history"] = []
    await update.message.reply_text(
        f"🧹 Память переписки ПОЛНОСТЬЮ очищена ({deleted_count} записей удалено, все чаты). "
        f"Начинаем с чистого листа — данные в Google Ads и очередь одобрения не затронуты."
    )


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
    app.add_handler(CommandHandler("checkcampaign", cmd_check_campaign))
    app.add_handler(CommandHandler("dayparting", cmd_dayparting))
    app.add_handler(CommandHandler("enablecampaign", cmd_enable_campaign))
    app.add_handler(CommandHandler("pausecampaign", cmd_pause_campaign))
    app.add_handler(CommandHandler("checknegatives", cmd_check_negatives))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    # Fallback ПОСЛЕДНИМ: ловит любую команду, для которой нет обработчика
    # выше (например, ИИ иногда упоминает в тексте слаги вроде "/hvac",
    # "/washer" как название страницы сайта — Telegram автоматически
    # превращает такой текст в кликабельную команду, и без этого fallback
    # владелец при нажатии получал полную тишину без всякой реакции бота).
    app.add_handler(CommandHandler("reviews", cmd_reviews))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("clearmemory", cmd_clear_memory))
    app.add_handler(CommandHandler("email", cmd_email))
    app.add_handler(CommandHandler("sendemail", cmd_sendemail))
    app.add_handler(CommandHandler("gbppost", cmd_gbp_post))
    app.add_handler(CommandHandler("gbpaudit", cmd_gbp_audit))
    app.add_handler(CommandHandler("changes", cmd_changes))
    app.add_handler(CommandHandler("emailtest", cmd_emailtest))
    app.add_handler(CommandHandler("sendemail", cmd_sendemail))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_error_handler(global_error_handler)

    scheduler = AsyncIOScheduler(timezone=NY_TZ)
    scheduler.add_job(
        scheduled_lsa_workiz_reconciliation, "cron",
        day_of_week="mon", hour=9, minute=30,
        timezone=NY_TZ, args=[app],
        id="lsa_workiz_reconciliation", replace_existing=True,
    )
    # Разовое напоминание — 16.08.2026 09:00 NY
    # Понедельник 09:30 — сверка LSA с Workiz
    scheduler.add_job(
        scheduled_lsa_workiz_reconciliation, "cron",
        day_of_week="mon", hour=9, minute=30,
        timezone=NY_TZ, args=[app],
        id="lsa_workiz_reconciliation", replace_existing=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(
            app.bot.send_message(
                chat_id=config.OWNER_CHAT_ID,
                text="📌 Напоминание: сегодня нужно поднять бюджет LSA с $35.71 до $45/день.\nGoogle Ads UI → Local Services Ads → Budget."
            )
        ),
        "date",
        run_date="2026-08-16 09:00:00",
        timezone=NY_TZ,
        id="lsa_budget_reminder",
        replace_existing=True,
    )
    scheduler.add_job(scheduled_morning_report,   "cron", hour=8,  minute=0,  args=[app])
    scheduler.add_job(scheduled_budget_check,     "cron", hour=14, minute=0,  args=[app])
    scheduler.add_job(scheduled_evening_summary,  "cron", hour=21, minute=0,  args=[app])
    scheduler.add_job(scheduled_weekly_audit,     "cron", day_of_week="mon", hour=9,  minute=0,  args=[app])
    scheduler.add_job(scheduled_campaign_audit,   "cron", day_of_week="mon", hour=9,  minute=55, args=[app])
    scheduler.add_job(scheduled_competitors_check,"cron", day_of_week="sun", hour=9,  minute=30, args=[app])
    scheduler.add_job(scheduled_ab_test_check,    "cron", day_of_week="wed", hour=10, minute=0,  args=[app])
    scheduler.add_job(scheduled_seasonal_check,   "cron", day=1,             hour=8,  minute=0,  args=[app])
    scheduler.add_job(scheduled_lsa_weekly_audit, "cron", day_of_week="mon", hour=8,  minute=30, args=[app])
    scheduler.add_job(scheduled_weekly_roas,      "cron", day_of_week="mon", hour=9,  minute=15, args=[app])
    scheduler.add_job(scheduled_thumbtack_check,  "cron", day_of_week="mon", hour=9,  minute=20, args=[app])
    scheduler.add_job(scheduled_purge_pending,    "cron", hour=3,  minute=0,  args=[app])
    scheduler.add_job(scheduled_reverify_executed_actions, "interval", hours=4, args=[app])
    scheduler.add_job(scheduled_anomaly_check, "interval", hours=4, args=[app])
    scheduler.add_job(scheduled_campaign_audit, "cron",
                      day_of_week="mon", hour=9, minute=55, args=[app])
    scheduler.add_job(scheduled_weekly_strategy, "cron", day_of_week="mon", hour=8, minute=45, args=[app])
    if _gbp_available and globals().get("gbp_client_inst"):
        scheduler.add_job(scheduled_gbp_reviews, "cron", hour=9, minute=30, args=[app])
        scheduler.add_job(scheduled_gbp_profile_check, "cron",
                          day_of_week="mon", hour=9, minute=45, args=[app])
    if _email_available:
        scheduler.add_job(scheduled_weekly_email, "cron",
                          day_of_week="sun", hour=12, minute=0, args=[app])
        scheduler.add_job(scheduled_gbp_weekly_post, "cron",
                          day_of_week="wed", hour=10, minute=0, args=[app])
    scheduler.start()

    log.info("BCHD Marketer Agent v5 запущен (история команд включена)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
