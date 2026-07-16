"""
Генератор отчётов для Telegram
v3 — format_approval_card показывает список ключевых слов и человекочитаемые типы действий
"""

from datetime import datetime


class ReportGenerator:

    def format_audit_report(self, data: dict, analysis: dict) -> str:
        campaigns = data.get('campaigns', [])
        score = analysis.get('score', 0)
        score_emoji = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"
        lines = [
            f"📊 *Аудит кампаний — {datetime.now().strftime('%d.%m.%Y')}*",
            f"{'─' * 28}", f"",
            f"{score_emoji} *Оценка эффективности: {score}/100*", f"",
            f"💰 Общий расход: *${data.get('total_spend', 0):.2f}*",
            f"🎯 Конверсий: *{data.get('total_conversions', 0):.0f}*",
            f"👆 Кликов: *{data.get('total_clicks', 0):,}*", f"",
            f"📋 *Сводка:*", analysis.get('summary', 'Нет данных'), f"",
        ]
        if analysis.get('key_findings'):
            lines.append("🔍 *Ключевые наблюдения:*")
            for finding in analysis['key_findings']:
                lines.append(f"• {finding}")
            lines.append("")
        if campaigns:
            lines.append("📈 *Кампании:*")
            for c in campaigns[:5]:
                status_e = "✅" if c['status'] == 'ENABLED' else "⏸"
                lines.append(
                    f"{status_e} *{c['name']}*\n"
                    f"   CTR: {c['ctr']}% | CPC: ${c['avg_cpc']} | "
                    f"Расход: ${c['cost']} | Лидов: {c['conversions']:.0f}"
                )
            lines.append("")
        if analysis.get('opportunities'):
            lines.append("💡 *Возможности:*")
            for opp in analysis['opportunities']:
                lines.append(f"→ {opp}")
        return "\n".join(lines)

    def format_campaigns_list(self, campaigns: list) -> str:
        if not campaigns:
            return "❌ Кампании не найдены"
        lines = [f"📋 *Кампании Google Ads* ({len(campaigns)} шт)\n{'─' * 28}\n"]
        for i, c in enumerate(campaigns, 1):
            status_e = "✅" if c['status'] == 'ENABLED' else "⏸" if c['status'] == 'PAUSED' else "🔴"
            cpa_text = f"${c['cpa']:.2f}" if c.get('cpa') else "нет данных"
            is_text = f"{c.get('impression_share', 0):.0f}%" if c.get('impression_share') else "—"
            lines.append(
                f"*{i}. {c['name']}* {status_e}\n"
                f"   💰 Бюджет: ${c['budget_daily']:.2f}/день\n"
                f"   📊 CTR: {c['ctr']}% | CPC: ${c['avg_cpc']}\n"
                f"   🎯 Лидов: {c['conversions']:.0f} | CPA: {cpa_text}\n"
                f"   📱 IS: {is_text} | Расход: ${c['cost']}\n"
            )
        return "\n".join(lines)

    def format_keywords_report(self, data: dict, analysis: dict) -> str:
        strong = analysis.get('strong_keywords', [])
        weak = analysis.get('weak_keywords', [])
        qs_issues = analysis.get('quality_score_issues', [])
        lines = [
            f"🔑 *Анализ ключевых слов*\n{'─' * 28}\n",
            f"📊 Всего ключей: *{data.get('total', 0)}*",
            f"💪 Сильных: *{len(strong)}*",
            f"⚠️ Слабых (кандидаты на паузу): *{len(weak)}*",
            f"📉 Проблем с Quality Score: *{len(qs_issues)}*\n",
            analysis.get('summary', ''),
        ]
        if strong:
            lines.append("\n✅ *Топ сильных ключей:*")
            for kw in strong[:5]:
                lines.append(f"• `{kw['keyword']}` — {kw['why_strong']}")
        if qs_issues:
            lines.append("\n⚠️ *Проблемы Quality Score:*")
            for issue in qs_issues[:3]:
                lines.append(f"• `{issue['keyword']}` QS={issue['quality_score']}/10\n  ↳ {issue['fix']}")
        return "\n".join(lines)

    def format_search_terms_report(self, data: dict, analysis: dict) -> str:
        terms = data.get('terms', [])
        negatives = analysis.get('suggested_negatives', [])
        lines = [
            f"🔎 *Поисковые запросы — {data.get('days', 30)} дней*\n{'─' * 28}\n",
            f"📊 Всего запросов: *{len(terms)}*",
            f"🚫 Предлагаемых минус-слов: *{len(negatives)}*\n",
            analysis.get('summary', ''),
        ]
        if negatives:
            waste = sum(n.get('spend', 0) for n in negatives)
            lines.append(f"\n💸 Потрачено на нерелевантные запросы: *${waste:.2f}*")
            lines.append("\n*Нерелевантные запросы:*")
            for n in negatives[:10]:
                lines.append(f"• `{n['term']}` — {n['impressions']} показов, ${n.get('spend', 0):.2f}")
                lines.append(f"  ↳ {n['reason']}")
        return "\n".join(lines)

    def format_budget_report(self, data: dict, analysis: dict) -> str:
        health = analysis.get('budget_health', 'unknown')
        health_e = {"good": "🟢", "warning": "🟡", "critical": "🔴"}.get(health, "⚪")
        lines = [
            f"💰 *Анализ бюджета*\n{'─' * 28}\n",
            f"{health_e} Статус: *{health.upper()}*\n",
            f"📊 Общий дневной бюджет: *${data.get('total_daily_budget', 0):.2f}*",
            f"💸 Расход сегодня: *${data.get('total_spend_today', 0):.2f}*\n",
            analysis.get('budget_summary', ''),
        ]
        for c in data.get('campaigns', []):
            pct = (c['spend_today'] / c['daily_budget'] * 100) if c['daily_budget'] > 0 else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            lines.append(
                f"\n*{c['campaign_name']}*\n"
                f"{bar} {pct:.0f}%\n"
                f"${c['spend_today']:.2f} / ${c['daily_budget']:.2f}"
            )
        return "\n".join(lines)

    def format_daily_summary(self, data: dict, analysis: dict) -> str:
        trend_e = {"improving": "📈", "stable": "➡️", "declining": "📉"}.get(
            analysis.get('trend', 'stable'), "➡️"
        )
        lines = [
            f"🌙 *Итоги дня*\n{'─' * 28}\n",
            f"{trend_e} Тренд: *{analysis.get('trend_explanation', 'Нет данных')}*\n",
            f"💰 Расход: *${data.get('total_spend', 0):.2f}*",
            f"🎯 Лидов: *{data.get('total_conversions', 0):.0f}*",
            f"💵 Средний CPA: *${data.get('avg_cpa', 0) or 0:.2f}*\n",
        ]
        if analysis.get('insights'):
            lines.append("💡 *Инсайты:*")
            for insight in analysis['insights']:
                lines.append(f"• {insight}")
            lines.append("")
        if analysis.get('next_week_focus'):
            lines.append(f"🎯 *Фокус на завтра:*\n{analysis['next_week_focus']}")
        return "\n".join(lines)

    def format_weekly_report(self, data: dict, analysis: dict) -> str:
        lines = [
            f"📅 *Еженедельный отчёт*\n{'─' * 28}\n",
            f"📆 Период: {data.get('date_from')} — {data.get('date_to')}\n",
            f"💰 Общий расход: *${data.get('total_spend', 0):.2f}*",
            f"🎯 Всего лидов: *{data.get('total_conversions', 0):.0f}*",
            f"💵 Средний CPA: *${data.get('avg_cpa', 0) or 0:.2f}*\n",
        ]
        if analysis.get('best_day'):
            lines.append(f"🏆 Лучший день: {analysis['best_day']}")
        if analysis.get('worst_day'):
            lines.append(f"📉 Слабый день: {analysis['worst_day']}\n")
        if analysis.get('insights'):
            lines.append("💡 *Ключевые инсайты:*")
            for insight in analysis['insights']:
                lines.append(f"• {insight}")
            lines.append("")
        if analysis.get('next_week_focus'):
            lines.append(f"🎯 *Приоритет на следующую неделю:*\n{analysis['next_week_focus']}")
        return "\n".join(lines)

    def format_approval_card(self, action_id: str, action: dict) -> str:
        urgency_e = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(action.get('urgency', 'low'), "⚪")
        action_type = action.get('type', '')

        type_labels = {
            'pause_keywords':          '⏸ Пауза ключевых слов',
            'enable_keywords':         '▶️ Активация ключевых слов',
            'add_negative_keywords':   '🚫 Добавить минус-слова',
            'remove_negative_keyword': '✅ Удалить минус-слово',
            'budget_change':           '💰 Изменение бюджета',
            'update_bid':              '💵 Изменение ставки',
            'pause_campaign':          '⏸ Пауза кампании',
            'enable_campaign':         '▶️ Активация кампании',
            'remove_campaign':         '🗑 Удаление кампании',
            'update_final_url':        '🔗 Изменение лендинга',
            'seasonal_adjustments':    '📅 Сезонные корректировки',
            'dispute_lsa_lead':        '⚖️ Оспорить LSA лид',
        }
        type_label = type_labels.get(action_type, action_type)

        lines = [
            f"⚡ *Требует одобрения*",
            f"{'─' * 28}",
            f"",
            f"*Тип:* {type_label}",
            f"*Срочность:* {urgency_e} {action.get('urgency_label', '')}",
            f"",
            f"📋 *Действие:*",
            action.get('description', ''),
        ]

        # Для паузы/активации ключей — полный список с деталями
        if action_type in ('pause_keywords', 'enable_keywords') and action.get('keywords'):
            kws = action['keywords']
            lines.append(f"")
            lines.append(f"🔑 *Ключевые слова ({len(kws)} шт):*")
            for kw in kws:
                keyword = kw.get('keyword') or kw.get('text') or '?'
                parts = []
                if kw.get('impressions'): parts.append(f"{kw['impressions']} показов")
                if kw.get('clicks'):      parts.append(f"{kw['clicks']} кликов")
                if kw.get('cost'):        parts.append(f"${kw['cost']}")
                if kw.get('ctr'):         parts.append(f"CTR {kw['ctr']}%")
                detail = ' | '.join(parts) if parts else '0 показов, 0 кликов'
                lines.append(f"• `{keyword}` — {detail}")

        # Для минус-слов — список терминов с причиной
        if action_type == 'add_negative_keywords' and action.get('negatives'):
            negs = action['negatives']
            lines.append(f"")
            lines.append(f"🚫 *Минус-слова ({len(negs)} шт):*")
            for neg in negs:
                term   = neg.get('term', '?')
                reason = neg.get('reason', '')
                lines.append(f"• `{term}`" + (f" — {reason}" if reason else ""))

        # Для update_final_url — показываем ключ и URL
        if action_type == 'update_final_url':
            lines.append(f"")
            lines.append(f"🔑 Ключ: `{action.get('keyword', '?')}`")
            lines.append(f"🔗 Новый URL: {action.get('new_url', '?')}")
            if action.get('current_url'):
                lines.append(f"📌 Текущий URL: {action.get('current_url')}")

        # Для изменения бюджета — текущий и новый
        if action_type == 'budget_change':
            lines.append(f"")
            lines.append(f"💰 Текущий: ${action.get('current_budget', 0):.2f}/день")
            lines.append(f"💰 Новый: ${action.get('proposed_budget', 0):.2f}/день")

        lines += [
            f"",
            f"🧠 *Обоснование:*",
            action.get('reasoning', ''),
            f"",
            f"⚠️ *Риски:*",
            action.get('risks', 'Минимальные'),
            f"{'─' * 28}",
            f"🆔 `{action_id}`",
        ]

        return "\n".join(lines)

    def format_auction_report(self, data: dict, analysis: dict) -> str:
        position = analysis.get('competitive_position', 'unknown')
        pos_e = {"strong": "💪", "average": "📊", "weak": "⚠️"}.get(position, "❓")
        lines = [
            f"🏆 *Анализ конкурентов — Auction Insights*",
            f"{'─' * 28}",
            f"📅 Период: {data.get('date_from')} — {data.get('date_to')}",
            f"", f"{pos_e} *Позиция: {position.upper()}*",
            analysis.get('position_summary', ''), f"",
            f"👥 *Конкуренты ({data.get('total', 0)} доменов):*",
        ]
        for i, comp in enumerate(data.get('competitors', [])[:8], 1):
            overlap_bar = "█" * int(comp['overlap_rate'] / 10) + "░" * (10 - int(comp['overlap_rate'] / 10))
            lines.append(
                f"\n*{i}. {comp['domain']}*\n"
                f"   📊 IS: {comp['impression_share']}% | Топ: {comp['top_is']}% | #1: {comp['abs_top_is']}%\n"
                f"   🔄 Overlap: {overlap_bar} {comp['overlap_rate']}%\n"
                f"   📈 Мы выше: {comp['outranking_share']}% случаев"
            )
        if analysis.get('main_threats'):
            lines.append(f"\n🚨 *Главные угрозы:*")
            for threat in analysis['main_threats'][:3]:
                threat_e = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(threat.get('threat_level', 'low'), "⚪")
                lines.append(
                    f"{threat_e} *{threat['domain']}*\n"
                    f"   {threat['reason']}\n"
                    f"   → {threat['recommended_action']}"
                )
        if analysis.get('opportunities'):
            lines.append(f"\n💡 *Возможности:*")
            for opp in analysis['opportunities']:
                lines.append(f"→ {opp}")
        if analysis.get('summary'):
            lines.append(f"\n📌 *Вывод:*\n{analysis['summary']}")
        return "\n".join(lines)

    def format_ab_test_report(self, data: dict, analysis: dict) -> str:
        ab_results = analysis.get('ab_results', [])
        not_ready = analysis.get('not_ready', [])
        lines = [
            f"🧪 *A/B Тест объявлений*", f"{'─' * 28}",
            f"📊 Объявлений проанализировано: *{data.get('total', 0)}*",
            f"✅ Готовы к решению: *{len(ab_results)}* групп",
            f"⏳ Ещё тестируются: *{len(not_ready)}* групп", f"",
            analysis.get('summary', ''),
        ]
        if ab_results:
            lines.append(f"\n🏆 *Результаты тестов:*")
            for result in ab_results:
                confidence_e = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(result.get('confidence', 'low'), "⚪")
                winner = result.get('winner', {})
                loser = result.get('loser', {})
                lines.append(
                    f"\n📁 *{result['ad_group']}* ({result['campaign']})\n"
                    f"{confidence_e} Уверенность: {result.get('confidence', '?').upper()}\n\n"
                    f"✅ *Победитель:*\n"
                    f"   `{' | '.join(winner.get('headlines', [])[:2])}`\n"
                    f"   CTR: {winner.get('ctr', 0)}% | Conv: {winner.get('conv_rate', 0)}%\n"
                    f"   📝 {winner.get('why_winner', '')}\n\n"
                    f"❌ *Проигравший:*\n"
                    f"   `{' | '.join(loser.get('headlines', [])[:2])}`\n"
                    f"   CTR: {loser.get('ctr', 0)}% | Conv: {loser.get('conv_rate', 0)}%\n"
                    f"   Действие: {loser.get('action', 'pause')}"
                )
        if not_ready:
            lines.append(f"\n⏳ *Ещё собирают данные:*")
            for nr in not_ready[:3]:
                lines.append(f"• *{nr['ad_group']}* — {nr['reason']}")
                if nr.get('impressions_needed'):
                    lines.append(f"  Нужно ещё ~{nr['impressions_needed']} показов")
        return "\n".join(lines)

    def format_seasonal_report(self, season_data: dict, action_plan: dict) -> str:
        season_e = {'winter': '❄️', 'spring': '🌱', 'summer': '☀️', 'fall': '🍂'}.get(season_data.get('season', ''), '📅')
        adjustments = action_plan.get('adjustments', [])
        ad_changes = action_plan.get('ad_copy_changes', [])
        lines = [
            f"{season_e} *Сезонная оптимизация — {season_data.get('season_name', '')}*",
            f"{'─' * 28}", f"",
            f"📌 *Обоснование:*\n{season_data.get('reason', '')}", f"",
            f"💡 *Совет по текстам:*\n_{season_data.get('ad_copy_tip', '')}_", f"",
        ]
        if adjustments:
            boost  = [a for a in adjustments if a.get('direction') == 'increase']
            reduce = [a for a in adjustments if a.get('direction') == 'decrease']
            if boost:
                lines.append(f"📈 *Повысить бюджет (+{season_data.get('boost_pct', 0)}%):*")
                for adj in boost:
                    new_budget = adj['current_budget'] * (1 + adj['adjustment_pct'] / 100)
                    lines.append(f"• *{adj['campaign_name']}*\n  ${adj['current_budget']:.2f} → ${new_budget:.2f}/день\n  _{adj['reason']}_")
                lines.append("")
            if reduce:
                lines.append(f"📉 *Снизить бюджет ({season_data.get('reduce_pct', 0)}%):*")
                for adj in reduce:
                    new_budget = adj['current_budget'] * (1 + adj['adjustment_pct'] / 100)
                    lines.append(f"• *{adj['campaign_name']}*\n  ${adj['current_budget']:.2f} → ${new_budget:.2f}/день\n  _{adj['reason']}_")
                lines.append("")
        if ad_changes:
            lines.append(f"✏️ *Рекомендуемые изменения в объявлениях:*")
            for change in ad_changes[:3]:
                lines.append(
                    f"• *{change['campaign']}*\n"
                    f"  Текущие: `{' | '.join(change.get('current_headlines', []))}`\n"
                    f"  Новые: `{' | '.join(change.get('suggested_headlines', []))}`\n"
                    f"  _{change.get('reason', '')}_"
                )
        if action_plan.get('expected_impact'):
            lines.append(f"\n📊 *Ожидаемый эффект:*\n{action_plan['expected_impact']}")
        return "\n".join(lines)

    def format_performance_report(self, data: dict, analysis: dict) -> str:
        return self.format_audit_report(data, analysis)
