"""
AI Аналитик — Claude API
v3 — добавлена диагностика пустых/некорректных ответов + общий метод вызова
"""

import json
import logging
import re
import anthropic

log = logging.getLogger(__name__)


class AIAnalyst:
    """Использует Claude для глубокого анализа данных Google Ads"""

    def __init__(self, config):
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.model = config.CLAUDE_MODEL

        self.system_prompt = """
Ты — экспертный Google Ads специалист с 10+ годами опыта в сфере
appliance repair бизнеса в США. Ты работаешь как автономный агент
и анализируешь данные рекламных кампаний.

ТВОЯ ЗАДАЧА:
- Находить конкретные проблемы и возможности в данных
- Давать чёткие, обоснованные рекомендации с цифрами
- Объяснять ПОЧЕМУ нужно что-то изменить (данные + логика)
- Оценивать риски каждого изменения
- Приоритизировать по влиянию на бизнес

СТРОГИЕ ПРАВИЛА ЧЕСТНОСТИ (важнее всего остального):
- Используй ТОЛЬКО цифры, которые реально есть в переданных данных.
  Никогда не придумывай и не "округляй в уме" метрики, которых нет во входном JSON.
- Прежде чем дать любую рекомендацию — пересчитай ключевые цифры
  из самих данных (например: CPA = cost / conversions, а не оценка "на глаз").
  Если пересчёт не сходится с тем, что во входных данных — не давай
  эту рекомендацию, вместо этого укажи расхождение в summary.
- НЕ рекомендуй действие (пауза ключа/кампании, изменение ставки, бюджета)
  если объём данных недостаточен для выводов:
  * меньше 100 показов — недостаточно для суждения о CTR/качестве
  * меньше 10 кликов — недостаточно для суждения о конверсии/CPA
  * период короче 14 дней — недостаточно для сезонных/трендовых выводов
  В этих случаях явно пиши "недостаточно данных для вывода" вместо
  того чтобы делать вид, что вывод уверенный.
- Указывай уровень уверенности (confidence: high/medium/low) для каждой
  рекомендации, основываясь на объёме данных, а не на тоне текста.
- Если данные показывают что-то нейтральное или неопределённое — так и скажи.
  Не нужно искусственно находить "проблему" или "возможность" там, где
  цифры в пределах нормы. Достаточно сказать "показатели в норме, изменений
  не требуется".
- Никогда не используй формулировки, которые звучат увереннее, чем
  позволяют данные ("критически", "срочно", "гарантированно") если
  объём данных пограничный.
- Если в данных есть противоречия (например, конверсий больше чем кликов,
  отрицательные значения, N/A там где ожидалось число) — сообщи об этом
  в summary как техническую проблему с данными, а не игнорируй.

ВАЖНЫЕ ПРАВИЛА:
- Изменение бюджета — ВСЕГДА требует одобрения владельца
- Все рекомендации должны иметь обоснование с конкретными цифрами из данных
- Указывай ожидаемый результат конкретными цифрами, но помечай его как
  оценку ("ожидаемо", "по опыту отрасли"), а не как гарантию
- Если данных недостаточно — говори об этом честно, не заполняй пробелы
  правдоподобными на вид цифрами

ОТРАСЛЕВЫЕ БЕНЧМАРКИ (appliance repair, USA) — используй только как
ориентир для сравнения, не как факт о конкретном аккаунте:
- Хороший CTR: 4-8%
- Хороший CPA: $15-35 за лид
- Хороший Quality Score: 7+
- Целевой ROAS: 400%+

Отвечай на русском языке. Будь конкретным, профессиональным и трезвым —
лучше сказать "недостаточно данных" или "всё в норме", чем выдумать
рекомендацию ради видимости работы.
"""

    def _call_claude(self, prompt: str, max_tokens: int = 2000) -> dict:
        """
        Общий метод вызова Claude с диагностикой и защитой от пустых/некорректных ответов.
        Возвращает распарсенный JSON или {"_error": "..."} при неудаче.
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=self.system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            log.error(f"Ошибка вызова Claude API: {e}")
            return {"_error": f"api_error: {e}"}

        # Диагностика: логируем stop_reason и состав content на случай проблем
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason not in ("end_turn", "stop_sequence", None):
            log.warning(f"Claude ответил с stop_reason={stop_reason} (не end_turn)")

        if not response.content:
            log.error(f"Claude вернул пустой content. stop_reason={stop_reason}, usage={getattr(response, 'usage', None)}")
            return {"_error": "empty_content"}

        # Ищем первый текстовый блок (на случай, если первым идёт не text-блок)
        text = None
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break

        if text is None:
            log.error(f"Claude не вернул текстовый блок. content types={[getattr(b, 'type', '?') for b in response.content]}")
            return {"_error": "no_text_block"}

        text = text.strip()

        if not text:
            log.error(f"Claude вернул пустой текст. stop_reason={stop_reason}, usage={getattr(response, 'usage', None)}")
            return {"_error": "empty_text"}

        # На случай если модель всё же обернула в markdown ```json ... ```
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.error(f"Ошибка парсинга JSON от Claude: {e}. Сырой ответ (первые 500 симв.): {text[:500]!r}")
            return {"_error": f"json_decode_error: {e}", "_raw": text[:500]}

    async def analyze_campaigns(self, data: dict) -> dict:
        """Полный анализ кампаний + рекомендации"""
        prompt = f"""
Проанализируй данные Google Ads кампаний за последние 30 дней:

{json.dumps(data, ensure_ascii=False, indent=2)}

Выполни детальный анализ и верни JSON со следующей структурой:
{{
  "summary": "2-3 предложения об общем состоянии кампаний",
  "score": 75,
  "key_findings": [
    "Находка 1 с конкретными цифрами",
    "Находка 2",
    "Находка 3"
  ],
  "recommendations": [
    {{
      "type": "pause_keywords|add_negative_keywords|budget_change|update_bid|pause_campaign",
      "description": "Что именно сделать",
      "reasoning": "Подробное обоснование с цифрами из данных",
      "data_summary": "Конкретные данные которые привели к этой рекомендации",
      "expected_impact": "Ожидаемый результат в числах",
      "urgency": "high|medium|low",
      "urgency_label": "Высокая|Средняя|Низкая",
      "risks": "Возможные риски изменения",
      "requires_approval": true,
      "confidence": "high|medium|low",
      "confidence_reason": "почему такая уверенность — сколько показов/кликов/дней в основе вывода"
    }}
  ],
  "opportunities": [
    "Возможность для роста 1",
    "Возможность 2"
  ],
  "data_quality_notes": "если в данных мало показов/кликов/дней или есть противоречия — укажи здесь честно, иначе оставь пустую строку"
}}

Если ни одна рекомендация не проходит проверку на достаточность данных
(смотри правила выше), верни "recommendations": [] и объясни это в summary.

Верни ТОЛЬКО валидный JSON без markdown разметки.
"""
        result = self._call_claude(prompt, max_tokens=4096)
        if "_error" in result:
            return {"summary": "Ошибка анализа", "recommendations": [], "key_findings": [], "_error": result["_error"]}
        return result

    async def analyze_keywords(self, data: dict) -> dict:
        """Анализ ключевых слов — находит слабые и сильные"""
        prompt = f"""
Проанализируй ключевые слова Google Ads:

{json.dumps(data, ensure_ascii=False, indent=2)}

Пороговые значения:
- Слабый ключ: CTR < {self.config.MIN_CTR_THRESHOLD}% при {self.config.MIN_IMPRESSIONS_FOR_JUDGMENT}+ показах
- Высокий CPA: > ${self.config.MAX_CPA_THRESHOLD}

Верни JSON:
{{
  "strong_keywords": [
    {{
      "keyword": "название",
      "why_strong": "почему эффективный",
      "recommendation": "что делать"
    }}
  ],
  "weak_keywords": [
    {{
      "keyword": "название",
      "ctr": 0.5,
      "cpc": 3.20,
      "spend": 45.00,
      "conversions": 0,
      "impressions": 250,
      "resource_name": "customers/xxx/adGroupCriteria/xxx",
      "why_weak": "конкретное объяснение",
      "recommendation": "пауза|снизить ставку|изменить match type"
    }}
  ],
  "quality_score_issues": [
    {{
      "keyword": "название",
      "quality_score": 3,
      "fix": "как улучшить"
    }}
  ],
  "summary": "общий вывод по ключам"
}}

ТОЛЬКО валидный JSON.
"""
        result = self._call_claude(prompt, max_tokens=3000)
        if "_error" in result:
            return {"strong_keywords": [], "weak_keywords": [], "quality_score_issues": [], "summary": result["_error"]}
        return result

    async def find_negative_keywords(self, data: dict) -> dict:
        """Находит нерелевантные запросы для добавления в минус-слова"""
        prompt = f"""
Это данные поисковых запросов из Google Ads для бизнеса по ремонту бытовой техники (appliance repair) в США.

{json.dumps(data, ensure_ascii=False, indent=2)}

Найди нерелевантные запросы которые нужно добавить как минус-слова.

Нерелевантные для appliance repair бизнеса:
- Запросы о покупке новой техники (buy new, price new)
- DIY / самостоятельный ремонт (how to fix yourself, DIY)
- Запчасти (parts, spare parts) — если мы не продаём детали
- Бренды которых мы не обслуживаем
- Другие города/штаты за пределами нашей зоны

Верни JSON:
{{
  "suggested_negatives": [
    {{
      "term": "поисковый запрос",
      "impressions": 45,
      "spend": 12.50,
      "reason": "почему нерелевантный"
    }}
  ],
  "keep_terms": [
    {{
      "term": "хороший запрос",
      "why": "почему релевантный"
    }}
  ],
  "summary": "сколько нашёл минус-слов и какая ожидаемая экономия"
}}

ТОЛЬКО валидный JSON.
"""
        result = self._call_claude(prompt, max_tokens=3000)
        if "_error" in result:
            return {"suggested_negatives": [], "summary": result["_error"]}
        return result

    async def analyze_budget(self, data: dict) -> dict:
        """Анализ бюджета и рекомендации по его распределению"""
        prompt = f"""
Проанализируй данные бюджета Google Ads:

{json.dumps(data, ensure_ascii=False, indent=2)}

Дай рекомендации по оптимизации бюджета. ВАЖНО: любое изменение бюджета
требует одобрения владельца. Обоснуй каждое изменение данными.

Верни JSON:
{{
  "budget_health": "good|warning|critical",
  "budget_summary": "текущее состояние расхода",
  "budget_recommendation": {{
    "campaign_name": "название",
    "campaign_id": "id",
    "budget_id": "id",
    "current_budget": 50.00,
    "proposed_budget": 65.00,
    "reasoning": "подробное обоснование с данными",
    "expected_result": "что изменится в числах",
    "ctr": "текущий CTR",
    "cpa": "текущий CPA",
    "conversions": "конверсий за период",
    "roas": "текущий ROAS"
  }},
  "redistribution": [
    {{
      "from_campaign": "название",
      "to_campaign": "название",
      "amount": 10.00,
      "reason": "почему"
    }}
  ]
}}

Если изменения не нужны, верни budget_recommendation как null.
ТОЛЬКО валидный JSON.
"""
        result = self._call_claude(prompt, max_tokens=2500)
        if "_error" in result:
            return {"budget_health": "unknown", "budget_recommendation": None, "_error": result["_error"]}
        return result

    async def analyze_performance(self, data: dict) -> dict:
        """Анализ производительности за период"""
        prompt = f"""
Проанализируй производительность Google Ads за {data.get('days', 7)} дней:

{json.dumps(data, ensure_ascii=False, indent=2)}

Верни JSON:
{{
  "trend": "improving|stable|declining",
  "trend_explanation": "объяснение тренда с данными",
  "best_day": "дата лучшего дня и почему",
  "worst_day": "дата худшего дня и почему",
  "key_metrics": {{
    "total_spend": 0,
    "total_leads": 0,
    "avg_cpa": 0,
    "avg_ctr": 0
  }},
  "insights": [
    "Инсайт 1 с конкретными цифрами",
    "Инсайт 2",
    "Инсайт 3"
  ],
  "next_week_focus": "главный приоритет на следующую неделю"
}}

ТОЛЬКО валидный JSON.
"""
        result = self._call_claude(prompt, max_tokens=2000)
        if "_error" in result:
            return {"trend": "unknown", "insights": [], "key_metrics": {}, "_error": result["_error"]}
        return result

    async def answer_question(self, question: str, context_data: dict = None) -> str:
        """
        Свободный вопрос — агент отвечает как эксперт.
        Если передан context_data (актуальные данные аккаунта), отвечает
        на основе реальных цифр, а не общих рассуждений.
        """
        if context_data:
            prompt = f"""
Вот актуальные данные рекламных аккаунтов (последние 30 дней):

{json.dumps(context_data, ensure_ascii=False, indent=2)}

Вопрос от владельца бизнеса: {question}

Ответь на вопрос, ОПИРАЯСЬ ТОЛЬКО на цифры из данных выше. Если для ответа
не хватает конкретных данных (например, вопрос про ключевые слова, а в
данных есть только уровень кампаний) — честно скажи, каких данных не хватает,
и не выдумывай цифры, которых нет в JSON выше.
"""
        else:
            prompt = f"Вопрос по Google Ads: {question}"

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1200,
                system=self.system_prompt + "\n\nОтвечай кратко и по делу. Используй Markdown форматирование для Telegram.",
                messages=[{"role": "user", "content": prompt}]
            )
            if not response.content:
                log.error("answer_question: пустой content от Claude")
                return "❌ Ошибка: пустой ответ от Claude"
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    return block.text
            return "❌ Ошибка: нет текстового блока в ответе"
        except Exception as e:
            log.error(f"Ошибка ответа на вопрос: {e}")
            return f"❌ Ошибка: {e}"

    # ── АУКЦИОННЫЙ АНАЛИЗ ──────────────────────────

    async def analyze_auction_insights(self, data: dict) -> dict:
        """Анализирует конкурентов из auction insights"""
        prompt = f"""
Проанализируй данные аукциона Google Ads (auction insights) за 30 дней для бизнеса appliance repair в США.

{json.dumps(data, ensure_ascii=False, indent=2)}

Метрики:
- impression_share: доля показов (наши vs конкурент)
- top_is: доля показов в верхней части страницы
- abs_top_is: доля показов на первом месте
- overlap_rate: как часто мы показываемся вместе с конкурентом
- outranking_share: как часто мы показываемся выше конкурента

Верни JSON:
{{
  "competitive_position": "strong|average|weak",
  "position_summary": "2-3 предложения о нашей позиции",
  "main_threats": [
    {{
      "domain": "competitor.com",
      "threat_level": "high|medium|low",
      "reason": "почему угроза и с какими данными",
      "recommended_action": "что делать"
    }}
  ],
  "opportunities": [
    "Возможность 1 — где конкуренты слабее",
    "Возможность 2"
  ],
  "bid_recommendations": [
    {{
      "insight": "конкретный инсайт по ставкам",
      "action": "что изменить"
    }}
  ],
  "summary": "главный вывод для владельца бизнеса"
}}

ТОЛЬКО валидный JSON.
"""
        result = self._call_claude(prompt, max_tokens=2500)
        if "_error" in result:
            return {"competitive_position": "unknown", "main_threats": [], "opportunities": [], "summary": result["_error"]}
        return result

    # ── A/B ТЕСТ ──────────────────────────────────

    async def analyze_ab_test(self, data: dict) -> dict:
        """
        Анализирует производительность объявлений в одной группе.
        Находит победителя и проигравшего после минимум 50+ показов.
        """
        MIN_IMPRESSIONS = 100  # минимум для статистической значимости

        prompt = f"""
Проанализируй объявления Google Ads для A/B теста. Данные за 30 дней:

{json.dumps(data, ensure_ascii=False, indent=2)}

Для каждой ad group где есть 2+ объявления с {MIN_IMPRESSIONS}+ показами:
- Определи победителя (выше CTR + conversion rate)
- Определи проигравшего
- Оцени статистическую значимость разницы

Верни JSON:
{{
  "ab_results": [
    {{
      "ad_group": "название группы",
      "campaign": "название кампании",
      "winner": {{
        "ad_id": "id",
        "headlines": ["заголовок 1", "заголовок 2"],
        "ctr": 5.2,
        "conv_rate": 3.1,
        "impressions": 450,
        "why_winner": "конкретное объяснение с цифрами"
      }},
      "loser": {{
        "ad_id": "id",
        "resource_name": "customers/xxx/adGroupAds/xxx",
        "headlines": ["заголовок 1", "заголовок 2"],
        "ctr": 2.1,
        "conv_rate": 0.8,
        "impressions": 380,
        "action": "pause|keep_testing"
      }},
      "confidence": "high|medium|low",
      "confidence_reason": "почему такая уверенность",
      "ready_for_decision": true
    }}
  ],
  "not_ready": [
    {{
      "ad_group": "название",
      "reason": "почему ещё рано (мало показов / нет чёткого победителя)",
      "impressions_needed": 200
    }}
  ],
  "summary": "общий вывод по A/B тестам"
}}

ТОЛЬКО валидный JSON.
"""
        result = self._call_claude(prompt, max_tokens=3500)
        if "_error" in result:
            return {"ab_results": [], "not_ready": [], "summary": result["_error"]}
        return result

    # ── СЕЗОННЫЕ РЕКОМЕНДАЦИИ ──────────────────────

    async def build_seasonal_action(self, season_data: dict, campaigns: list) -> dict:
        """
        Строит конкретный план сезонных корректировок
        на основе данных о текущем сезоне и списка кампаний.
        """
        prompt = f"""
Бизнес: ремонт бытовой техники (appliance repair) в Нью-Йорке.

Текущий сезон: {season_data['season_name']}
Обоснование: {season_data['reason']}
Ключи для повышения ставок (+{season_data['boost_pct']}%): {season_data['boost_keywords']}
Ключи для снижения ставок ({season_data['reduce_pct']}%): {season_data['reduce_keywords']}
Совет по рекламным текстам: {season_data['ad_copy_tip']}

Текущие кампании:
{json.dumps(campaigns, ensure_ascii=False, indent=2)}

Создай план сезонных корректировок. Сопоставь сезонные ключевые слова с кампаниями.

Верни JSON:
{{
  "season": "{season_data['season_name']}",
  "adjustments": [
    {{
      "campaign_id": "id кампании",
      "campaign_name": "название",
      "budget_id": "id бюджета",
      "current_budget": 50.00,
      "adjustment_pct": 20,
      "direction": "increase|decrease",
      "reason": "конкретное обоснование — какие ключи есть в этой кампании"
    }}
  ],
  "ad_copy_changes": [
    {{
      "campaign": "название",
      "current_headlines": ["заголовок 1"],
      "suggested_headlines": ["новый заголовок 1", "новый заголовок 2"],
      "reason": "почему менять"
    }}
  ],
  "summary": "краткий план на {season_data['season_name']}",
  "expected_impact": "ожидаемый эффект в числах"
}}

ТОЛЬКО валидный JSON.
"""
        result = self._call_claude(prompt, max_tokens=2500)
        if "_error" in result:
            return {"adjustments": [], "summary": result["_error"]}
        return result
