"""
BCHD Marketer Agent — Telegram бот
Google Ads оптимизация с AI-анализом
v4 — безопасная отправка сообщений (fallback без Markdown при ошибке парсинга сущностей)
"""

import asyncio
import logging
from datetime import datetime

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
from pending_actions import PendingActions
from report_generator import ReportGenerator

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


def _is_owner(update: Update) -> bool:
    return update.effective_user.id == config.OWNER_CHAT_ID


def _account_keyboard(prefix: str) -> InlineKeyboardMarkup:
    """Кнопки выбора аккаунта"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Google Ads (936)", callback_data=f"{prefix}:ads"),
        InlineKeyboardButton("📍 LSA (667)", callback_data=f"{prefix}:lsa"),
        InlineKeyboardButton("🔀 Оба", callback_data=f"{prefix}:both"),
    ]])


async def _safe_send(bot, chat_id: int, text: str, parse_mode="Markdown", **kwargs):
    """
    Отправка сообщения с fallback: если Markdown не парсится (например,
    из-за непарных * _ [ в AI-сгенерированном тексте), отправляет как обычный текст.
    """
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        log.warning(f"Не удалось отправить с parse_mode={parse_mode} ({e}), отправляю как обычный текст")
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


async def _safe_edit(query, text: str, parse_mode="Markdown", **kwargs):
    """
    Редактирование сообщения с fallback: если Markdown не парсится, редактирует как обычный текст.
    """
    try:
        return await query.edit_message_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        log.warning(f"Не удалось отредактировать с parse_mode={parse_mode} ({e}), редактирую как обычный текст")
        return await query.edit_message_text(text, **kwargs)


async def _safe_reply(message, text: str, parse_mode="Markdown", **kwargs):
    """
    Ответ на сообщение с fallback: если Markdown не парсится, отвечает как обычный текст.
    """
    try:
        return await message.reply_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        log.warning(f"Не удалось ответить с parse_mode={parse_mode} ({e}), отвечаю как обычный текст")
        return await message.reply_text(text, **kwargs)


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
        f"/pending — ожидающие одобрения\n"
        f"/schedule — расписание задач",
        parse_mode="Markdown",
    )


async def cmd_both(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сводка по обоим аккаунтам"""
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🔀 Собираю данные по обоим аккаунтам... (~40 сек)")
    try:
        data = await ads_client.get_both_accounts_summary()
        combined = data.get('combined', {})
        ads_data = data.get('google_ads', {})
        lsa_data = data.get('lsa', {})

        text = f"📊 *Сводка по всем аккаунтам — последние 30 дней*\n\n"
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

        await _safe_edit(msg, text, parse_mode="Markdown")
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
        "— 14:00: анализ расходов\n"
        "— 21:00: итоги дня\n\n"
        "Еженедельно:\n"
        "— Пн 09:00: аудит кампаний\n"
        "— Пн 09:30: анализ ключевых слов\n"
        "— Пт 18:00: предложения по оптимизации\n\n"
        "Дополнительно:\n"
        "— Вс 09:30: анализ конкурентов\n"
        "— Ср 10:00: проверка A/B тестов\n"
        "— 1-е число месяца 08:00: сезонная оптимизация",
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


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Свободные текстовые вопросы к агенту (не команды)"""
    if not _is_owner(update):
        return
    question = update.message.text
    if not question or not question.strip():
        return
    thinking_msg = await update.message.reply_text("🤔 Думаю...")
    try:
        answer = await ai_analyst.answer_question(question)
        await _safe_edit(thinking_msg, answer, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка обработки свободного вопроса: {e}")
        await thinking_msg.edit_text(f"❌ Ошибка: {e}")


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

        # Одобрение/отклонение действий
        if cmd == "approve":
            action = pending.approve(param)
            if not action:
                await query.edit_message_text("⚠️ Действие не найдено или уже выполнено.")
                return
            await query.edit_message_text(f"⏳ Применяю: {action['description']}...")
            try:
                result = await ads_client.execute_action(action)
                await _safe_edit(
                    query,
                    f"✅ *Выполнено:* {action['description']}\n\n{result.get('summary', str(result))}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                log.error(f"Ошибка выполнения {param}: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")
            return

        elif cmd == "reject":
            action = pending.reject(param)
            if action:
                await query.edit_message_text(f"❌ Отклонено: {action['description']}")
            return

        # Выбор аккаунта
        account = param  # "ads", "lsa", "both"
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
            except Exception as e:
                log.error(f"Ошибка budget callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "keywords":
            await query.edit_message_text(f"🔑 Ключевые слова: {account_label}... (~30 сек)")
            try:
                accounts = ["ads", "lsa"] if account == "both" else [account]
                texts = []
                for acc in accounts:
                    data_result = await ads_client.get_keywords_analysis(account=acc)
                    analysis = await ai_analyst.analyze_keywords(data_result)
                    texts.append(f"*{'Google Ads' if acc == 'ads' else 'LSA'} ({len(data_result.get('keywords', []))} ключей):*\n{analysis.get('summary', 'Нет данных')}")
                    for action in _build_keyword_actions(analysis, acc):
                        action_id = pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                await _safe_edit(query, "\n\n".join(texts), parse_mode="Markdown")
            except Exception as e:
                log.error(f"Ошибка keywords callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "negatives":
            await query.edit_message_text(f"🚫 Минус-слова: {account_label}... (~30 сек)")
            try:
                accounts = ["ads", "lsa"] if account == "both" else [account]
                for acc in accounts:
                    data_result = await ads_client.get_search_terms(account=acc)
                    analysis = await ai_analyst.find_negative_keywords(data_result)
                    negatives = analysis.get("suggested_negatives", [])
                    acc_label = "Google Ads" if acc == "ads" else "LSA"
                    text = f"*Минус-слова {acc_label}:* {len(negatives)} найдено\n{analysis.get('summary', '')}"
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
                        action_id = pending.add(action)
                        await _send_approval_card(ctx.bot, config.OWNER_CHAT_ID, action_id, action)
                await query.edit_message_text("✅ Анализ завершён — карточки одобрения отправлены выше.")
            except Exception as e:
                log.error(f"Ошибка negatives callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "competitors":
            await query.edit_message_text(f"🏆 Конкуренты: {account_label}... (~30 сек)")
            try:
                accounts = ["ads", "lsa"] if account == "both" else [account]
                for acc in accounts:
                    data_result = await ads_client.get_auction_insights(account=acc)
                    if not data_result.get("competitors"):
                        await _safe_send(ctx.bot, config.OWNER_CHAT_ID, f"ℹ️ Данных аукциона для {'Google Ads' if acc == 'ads' else 'LSA'} пока нет.", parse_mode=None)
                        continue
                    analysis = await ai_analyst.analyze_auction_insights(data_result)
                    text = f"*{'Google Ads' if acc == 'ads' else 'LSA'} — конкуренты:*\n{analysis.get('position_summary', '')}\n\n{analysis.get('summary', '')}"
                    await _safe_send(ctx.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
                await query.edit_message_text("✅ Анализ конкурентов завершён.")
            except Exception as e:
                log.error(f"Ошибка competitors callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "abtest":
            await query.edit_message_text(f"🧪 A/B тест: {account_label}... (~30 сек)")
            try:
                accounts = ["ads", "lsa"] if account == "both" else [account]
                for acc in accounts:
                    data_result = await ads_client.get_ad_performance(account=acc)
                    analysis = await ai_analyst.analyze_ab_test(data_result)
                    results = analysis.get("ab_results", [])
                    acc_label = "Google Ads" if acc == "ads" else "LSA"
                    text = f"*A/B тест {acc_label}:* {len(results)} групп\n{analysis.get('summary', 'Нет данных')}"
                    await _safe_send(ctx.bot, config.OWNER_CHAT_ID, text, parse_mode="Markdown")
                await query.edit_message_text("✅ A/B анализ завершён.")
            except Exception as e:
                log.error(f"Ошибка abtest callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif cmd == "seasonal":
            await query.edit_message_text(f"🍂 Сезонный план: {account_label}... (~30 сек)")
            try:
                accounts = ["ads", "lsa"] if account == "both" else [account]
                season_data = ads_client.get_current_season_recommendations()
                for acc in accounts:
                    budget_data = await ads_client.get_budget_data(account=acc)
                    action_plan = await ai_analyst.build_seasonal_action(season_data, budget_data.get("campaigns", []))
                    acc_label = "Google Ads" if acc == "ads" else "LSA"
                    text = f"*Сезон {season_data['season_name']} — {acc_label}:*\n{action_plan.get('summary', '')}\n{action_plan.get('expected_impact', '')}"
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
            except Exception as e:
                log.error(f"Ошибка seasonal callback: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")


# ── Вспомогательные форматтеры ───────────────────────────

def _format_both_summary(data: dict) -> str:
    combined = data.get('combined', {})
    ads = data.get('google_ads', {})
    lsa = data.get('lsa', {})
    text = f"📊 *Сводка по всем аккаунтам — 30 дней*\n\n"
    text += f"💰 Итого: ${combined.get('total_spend', 0):.2f}\n"
    text += f"📞 Конверсии: {combined.get('total_conversions', 0):.0f}\n"
    if combined.get('avg_cpa'):
        text += f"💵 Средний CPA: ${combined['avg_cpa']:.2f}\n\n"
    text += f"*Google Ads (936):* ${ads.get('total_spend', 0):.2f} / {ads.get('total_conversions', 0):.0f} конв.\n"
    if lsa and not lsa.get('error'):
        text += f"*LSA (667):* ${lsa.get('total_spend', 0):.2f} / {lsa.get('total_conversions', 0):.0f} конв."
    return text


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
        data = await ads_client.get_both_accounts_summary()
        text = f"☀️ *Утренний отчёт — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n"
        text += _format_both_summary(data)
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
        data = await ads_client.get_both_accounts_summary()
        text = f"🌙 *Итоги дня — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n"
        text += _format_both_summary(data)
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

def main():
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

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
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    scheduler = AsyncIOScheduler(timezone=NY_TZ)
    scheduler.add_job(scheduled_morning_report,   "cron", hour=8,  minute=0,  args=[app])
    scheduler.add_job(scheduled_budget_check,     "cron", hour=14, minute=0,  args=[app])
    scheduler.add_job(scheduled_evening_summary,  "cron", hour=21, minute=0,  args=[app])
    scheduler.add_job(scheduled_weekly_audit,     "cron", day_of_week="mon", hour=9,  minute=0,  args=[app])
    scheduler.add_job(scheduled_competitors_check,"cron", day_of_week="sun", hour=9,  minute=30, args=[app])
    scheduler.add_job(scheduled_ab_test_check,    "cron", day_of_week="wed", hour=10, minute=0,  args=[app])
    scheduler.add_job(scheduled_seasonal_check,   "cron", day=1,             hour=8,  minute=0,  args=[app])
    scheduler.start()

    log.info("BCHD Marketer Agent v4 запущен (Google Ads + LSA)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
