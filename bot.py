"""
BCHD Marketer Agent — Telegram бот
Google Ads оптимизация с AI-анализом
"""

import asyncio
import logging
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ads_client import GoogleAdsClient
from ai_analyst import AIAnalyst
from config import config
from pending_actions import PendingActions
from report_generator import ReportGenerator

# ── Логирование ──────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Инициализация модулей ────────────────────────────────
ads_client = GoogleAdsClient(config)
ai_analyst = AIAnalyst(config)
report_gen = ReportGenerator()
pending = PendingActions()

NY_TZ = pytz.timezone(config.TIMEZONE)


# ── Утилиты ──────────────────────────────────────────────

def _is_owner(update: Update) -> bool:
    return update.effective_user.id == config.OWNER_CHAT_ID


async def _send_approval_card(update: Update, ctx, action_id: str, action: dict):
    text = report_gen.format_approval_card(action_id, action)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Применить", callback_data=f"approve:{action_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{action_id}"),
        ]
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Команды ──────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    status = "✅ Google Ads API подключён" if config.google_ads_configured else "⚠️ Google Ads API не настроен"
    await update.message.reply_text(
        f"🤖 *BCHD Marketer Agent*\n\n"
        f"{status}\n\n"
        f"Команды:\n"
        f"/report — ежедневный отчёт\n"
        f"/audit — полный аудит кампаний\n"
        f"/budget — анализ бюджетов\n"
        f"/keywords — анализ ключевых слов\n"
        f"/negatives — предложение минус-слов\n"
        f"/competitors — анализ конкурентов\n"
        f"/abtest — A/B тест объявлений\n"
        f"/seasonal — сезонные корректировки\n"
        f"/pending — ожидающие одобрения\n"
        f"/schedule — расписание задач",
        parse_mode="Markdown",
    )


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(
        "📅 *Расписание задач (NY time)*\n\n"
        "Ежедневно:\n"
        "— 08:00: утренний отчёт\n"
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
        await update.message.reply_text("⚠️ Google Ads API не настроен. Добавьте переменные в Railway.")
        return

    msg = await update.message.reply_text("📊 Собираю данные... (~20 сек)")
    try:
        data = await ads_client.get_daily_stats()
        analysis = await ai_analyst.analyze_daily(data)
        report = report_gen.format_daily_summary(data, analysis)
        await msg.edit_text(report, parse_mode="Markdown")

        for action in analysis.get("proposed_actions", []):
            action_id = pending.add(action)
            await _send_approval_card(update, ctx, action_id, action)

    except Exception as e:
        log.error(f"Ошибка отчёта: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🔍 Провожу полный аудит... (~40 сек)")
    try:
        data = await ads_client.get_full_audit_data()
        analysis = await ai_analyst.analyze_full_audit(data)
        report = report_gen.format_full_audit(data, analysis)
        await msg.edit_text(report, parse_mode="Markdown")

        for action in analysis.get("proposed_actions", []):
            action_id = pending.add(action)
            await _send_approval_card(update, ctx, action_id, action)

    except Exception as e:
        log.error(f"Ошибка аудита: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("💰 Анализирую бюджеты... (~20 сек)")
    try:
        data = await ads_client.get_budget_data()
        analysis = await ai_analyst.analyze_budgets(data)
        report = report_gen.format_budget_report(data, analysis)
        await msg.edit_text(report, parse_mode="Markdown")

        for action in analysis.get("proposed_actions", []):
            action_id = pending.add(action)
            await _send_approval_card(update, ctx, action_id, action)

    except Exception as e:
        log.error(f"Ошибка бюджетов: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🔑 Анализирую ключевые слова... (~30 сек)")
    try:
        data = await ads_client.get_keyword_performance()
        analysis = await ai_analyst.analyze_keywords(data)
        report = report_gen.format_keyword_report(data, analysis)
        await msg.edit_text(report, parse_mode="Markdown")

        for action in analysis.get("proposed_actions", []):
            action_id = pending.add(action)
            await _send_approval_card(update, ctx, action_id, action)

    except Exception as e:
        log.error(f"Ошибка анализа ключей: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_negatives(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🚫 Анализирую поисковые запросы... (~30 сек)")
    try:
        data = await ads_client.get_search_terms()
        analysis = await ai_analyst.analyze_search_terms(data)
        report = report_gen.format_search_terms_report(data, analysis)
        await msg.edit_text(report, parse_mode="Markdown")

        for action in analysis.get("proposed_actions", []):
            action_id = pending.add(action)
            await _send_approval_card(update, ctx, action_id, action)

    except Exception as e:
        log.error(f"Ошибка анализа запросов: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


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
        text = report_gen.format_approval_card(action_id, action)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Применить", callback_data=f"approve:{action_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{action_id}"),
            ]
        ])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Новые команды (конкуренты, A/B, сезон) ──────────────

async def cmd_competitors(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🏆 Анализирую конкурентов... (~30 сек)")
    try:
        data = await ads_client.get_auction_insights()
        if not data.get("competitors"):
            await msg.edit_text("ℹ️ Данных аукциона пока нет. Нужно несколько дней активных показов.")
            return
        analysis = await ai_analyst.analyze_auction_insights(data)
        report = report_gen.format_auction_report(data, analysis)
        await msg.edit_text(report, parse_mode="Markdown")

        if analysis.get("bid_recommendations"):
            recs = "\n".join([
                f"• {r['insight']}\n  → {r['action']}"
                for r in analysis["bid_recommendations"][:3]
            ])
            await update.message.reply_text(
                f"💡 *Рекомендации по ставкам:*\n\n{recs}", parse_mode="Markdown"
            )
    except Exception as e:
        log.error(f"Ошибка анализа конкурентов: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_abtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🧪 Анализирую A/B тесты... (~30 сек)")
    try:
        data = await ads_client.get_ad_performance()
        analysis = await ai_analyst.analyze_ab_test(data)
        report = report_gen.format_ab_test_report(data, analysis)
        await msg.edit_text(report, parse_mode="Markdown")

        for result in analysis.get("ab_results", []):
            loser = result.get("loser", {})
            if loser.get("action") == "pause" and result.get("confidence") in ("high", "medium"):
                action = {
                    "type": "pause_ad",
                    "description": f"Поставить на паузу проигравшее объявление в {result['ad_group']}",
                    "reasoning": (
                        f"Победитель: CTR {result['winner'].get('ctr')}%\n"
                        f"Проигравший: CTR {loser.get('ctr')}%\n"
                        f"{result['winner'].get('why_winner', '')}"
                    ),
                    "data_summary": f"Период: {data.get('date_from')} — {data.get('date_to')}",
                    "expected_impact": "Улучшение CTR группы объявлений",
                    "urgency": "medium",
                    "urgency_label": "Средняя",
                    "risks": "Рекомендуется создать новое объявление для следующего теста",
                    "ad_id": loser.get("ad_id"),
                    "resource_name": loser.get("resource_name"),
                }
                action_id = pending.add(action)
                await _send_approval_card(update, ctx, action_id, action)
    except Exception as e:
        log.error(f"Ошибка A/B анализа: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_seasonal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not config.google_ads_configured:
        await update.message.reply_text("⚠️ Google Ads API не настроен.")
        return

    msg = await update.message.reply_text("🍂 Генерирую сезонный план... (~30 сек)")
    try:
        season_data = ads_client.get_current_season_recommendations()
        budget_data = await ads_client.get_budget_data()
        action_plan = await ai_analyst.build_seasonal_action(season_data, budget_data["campaigns"])
        report = report_gen.format_seasonal_report(season_data, action_plan)
        await msg.edit_text(report, parse_mode="Markdown")

        if action_plan.get("adjustments"):
            action = {
                "type": "seasonal_adjustments",
                "description": f"Сезонные корректировки бюджета — {season_data['season_name']}",
                "reasoning": season_data["reason"],
                "data_summary": f"{len(action_plan['adjustments'])} кампаний",
                "expected_impact": action_plan.get("expected_impact", "Оптимизация расхода по сезону"),
                "urgency": "medium",
                "urgency_label": "Средняя",
                "risks": "Изменения обратимы — можно откатить в любой момент",
                "adjustments": action_plan["adjustments"],
            }
            action_id = pending.add(action)
            await _send_approval_card(update, ctx, action_id, action)
    except Exception as e:
        log.error(f"Ошибка сезонного анализа: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ── Обработка кнопок одобрения ───────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != config.OWNER_CHAT_ID:
        return

    parts = query.data.split(":")
    if len(parts) != 2:
        return

    cmd, action_id = parts

    if cmd == "approve":
        action = pending.approve(action_id)
        if not action:
            await query.edit_message_text("⚠️ Действие не найдено или уже выполнено.")
            return

        await query.edit_message_text(f"⏳ Применяю: {action['description']}...")
        try:
            result = await _execute_action(action)
            await query.edit_message_text(
                f"✅ *Выполнено:* {action['description']}\n\n{result}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"Ошибка выполнения действия {action_id}: {e}")
            await query.edit_message_text(f"❌ Ошибка выполнения: {e}")

    elif cmd == "reject":
        action = pending.reject(action_id)
        if action:
            await query.edit_message_text(f"❌ Отклонено: {action['description']}")


async def _execute_action(action: dict) -> str:
    action_type = action.get("type")

    if action_type == "adjust_budget":
        result = await ads_client.execute_action(action)
        return f"Бюджет изменён: {result}"

    elif action_type == "add_negatives":
        result = await ads_client.execute_action(action)
        count = len(action.get("keywords", []))
        return f"Добавлено {count} минус-слов"

    elif action_type == "adjust_bids":
        result = await ads_client.execute_action(action)
        return f"Ставки скорректированы: {result}"

    elif action_type == "pause_campaign":
        result = await ads_client.execute_action(action)
        return f"Кампания поставлена на паузу"

    elif action_type == "pause_ad":
        result = await ads_client.execute_action(action)
        return f"Объявление поставлено на паузу"

    elif action_type == "seasonal_adjustments":
        result = await ads_client.execute_action(action)
        count = len(action.get("adjustments", []))
        return f"Применены сезонные корректировки для {count} кампаний"

    else:
        return f"Тип действия '{action_type}' выполнен"


# ── Расписание задач ─────────────────────────────────────

async def scheduled_morning_report(app):
    """08:00 — утренний отчёт"""
    if not config.google_ads_configured:
        return
    log.info("Утренний отчёт")
    try:
        data = await ads_client.get_daily_stats()
        analysis = await ai_analyst.analyze_daily(data)
        report = report_gen.format_daily_summary(data, analysis)
        await app.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=f"☀️ *Утренний отчёт — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n{report}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка утреннего отчёта: {e}")


async def scheduled_budget_check(app):
    """14:00 — проверка расходов"""
    if not config.google_ads_configured:
        return
    log.info("Дневная проверка бюджетов")
    try:
        data = await ads_client.get_budget_data()
        analysis = await ai_analyst.analyze_budgets(data)
        if analysis.get("proposed_actions"):
            for action in analysis["proposed_actions"]:
                action_id = pending.add(action)
                text = report_gen.format_approval_card(action_id, action)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Применить", callback_data=f"approve:{action_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{action_id}"),
                ]])
                await app.bot.send_message(
                    chat_id=config.OWNER_CHAT_ID,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
    except Exception as e:
        log.error(f"Ошибка проверки бюджетов: {e}")


async def scheduled_evening_summary(app):
    """21:00 — итоги дня"""
    if not config.google_ads_configured:
        return
    log.info("Вечерний итог")
    try:
        data = await ads_client.get_daily_stats()
        analysis = await ai_analyst.analyze_daily(data)
        report = report_gen.format_daily_summary(data, analysis)
        await app.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=f"🌙 *Итоги дня — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n{report}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка вечернего итога: {e}")


async def scheduled_weekly_audit(app):
    """Пн 09:00 — еженедельный аудит"""
    if not config.google_ads_configured:
        return
    log.info("Еженедельный аудит")
    try:
        data = await ads_client.get_full_audit_data()
        analysis = await ai_analyst.analyze_full_audit(data)
        report = report_gen.format_full_audit(data, analysis)
        await app.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=f"📋 *Еженедельный аудит — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n{report}",
            parse_mode="Markdown",
        )
        for action in analysis.get("proposed_actions", []):
            action_id = pending.add(action)
            text = report_gen.format_approval_card(action_id, action)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Применить", callback_data=f"approve:{action_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{action_id}"),
            ]])
            await app.bot.send_message(
                chat_id=config.OWNER_CHAT_ID,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
    except Exception as e:
        log.error(f"Ошибка аудита: {e}")


async def scheduled_competitors_check(app):
    """Вс 09:30 — анализ конкурентов"""
    if not config.google_ads_configured:
        return
    log.info("Анализ конкурентов")
    try:
        data = await ads_client.get_auction_insights()
        if not data.get("competitors"):
            return
        analysis = await ai_analyst.analyze_auction_insights(data)
        report = report_gen.format_auction_report(data, analysis)
        await app.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=f"🏆 *Конкуренты — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n{report}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка анализа конкурентов: {e}")


async def scheduled_ab_test_check(app):
    """Ср 10:00 — A/B тест"""
    if not config.google_ads_configured:
        return
    log.info("Проверка A/B тестов")
    try:
        data = await ads_client.get_ad_performance()
        analysis = await ai_analyst.analyze_ab_test(data)
        if not analysis.get("ab_results"):
            return
        report = report_gen.format_ab_test_report(data, analysis)
        await app.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=f"🧪 *A/B тест — {datetime.now(NY_TZ).strftime('%d.%m.%Y')}*\n\n{report}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка A/B теста: {e}")


async def scheduled_seasonal_check(app):
    """1-е число месяца — сезонная оптимизация"""
    if not config.google_ads_configured:
        return
    log.info("Сезонная оптимизация")
    try:
        season_data = ads_client.get_current_season_recommendations()
        budget_data = await ads_client.get_budget_data()
        action_plan = await ai_analyst.build_seasonal_action(season_data, budget_data["campaigns"])
        report = report_gen.format_seasonal_report(season_data, action_plan)
        await app.bot.send_message(
            chat_id=config.OWNER_CHAT_ID,
            text=f"🗓 *Сезонная оптимизация — {datetime.now(NY_TZ).strftime('%B %Y')}*\n\n{report}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка сезонной оптимизации: {e}")


# ── main ─────────────────────────────────────────────────

def main():
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("negatives", cmd_negatives))
    app.add_handler(CommandHandler("competitors", cmd_competitors))
    app.add_handler(CommandHandler("abtest", cmd_abtest))
    app.add_handler(CommandHandler("seasonal", cmd_seasonal))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Расписание
    scheduler = AsyncIOScheduler(timezone=NY_TZ)
    scheduler.add_job(scheduled_morning_report,  "cron", hour=8,  minute=0,  args=[app])
    scheduler.add_job(scheduled_budget_check,    "cron", hour=14, minute=0,  args=[app])
    scheduler.add_job(scheduled_evening_summary, "cron", hour=21, minute=0,  args=[app])
    scheduler.add_job(scheduled_weekly_audit,    "cron", day_of_week="mon", hour=9, minute=0,  args=[app])
    scheduler.add_job(scheduled_competitors_check,"cron", day_of_week="sun", hour=9, minute=30, args=[app])
    scheduler.add_job(scheduled_ab_test_check,   "cron", day_of_week="wed", hour=10, minute=0, args=[app])
    scheduler.add_job(scheduled_seasonal_check,  "cron", day=1, hour=8, minute=0, args=[app])
    scheduler.start()

    log.info("BCHD Marketer Agent запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
