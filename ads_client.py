"""
Google Ads API клиент
Все операции чтения и записи в Google Ads
v2 — добавлены: auction insights, A/B тест, сезонные корректировки
"""

import logging
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


class GoogleAdsClient:
    """
    Клиент для работы с Google Ads API v18
    Документация: https://developers.google.com/google-ads/api/docs/start
    """

    def __init__(self, config):
        self.config = config
        self.customer_id = config.GOOGLE_ADS_CUSTOMER_ID
        self._client = None

    def _get_client(self):
        """Ленивая инициализация Google Ads клиента"""
        if self._client is None:
            from google.ads.googleads.client import GoogleAdsClient as GAClient
            self._client = GAClient.load_from_dict({
                "developer_token": self.config.GOOGLE_ADS_DEVELOPER_TOKEN,
                "client_id": self.config.GOOGLE_ADS_CLIENT_ID,
                "client_secret": self.config.GOOGLE_ADS_CLIENT_SECRET,
                "refresh_token": self.config.GOOGLE_ADS_REFRESH_TOKEN,
                "login_customer_id": self.config.GOOGLE_ADS_LOGIN_CUSTOMER_ID,
                "use_proto_plus": True,
            })
        return self._client

    # ── ПОЛУЧЕНИЕ ДАННЫХ ─────────────────────────────────

    async def get_full_audit_data(self) -> dict:
        """Полный аудит: кампании + ключи + расход + конверсии"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign.bidding_strategy_type,
                campaign_budget.amount_micros,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.average_cpc,
                metrics.cost_micros,
                metrics.conversions,
                metrics.cost_per_conversion,
                metrics.conversion_rate,
                metrics.search_impression_share,
                metrics.search_rank_lost_impression_share
            FROM campaign
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND campaign.status != 'REMOVED'
            ORDER BY metrics.cost_micros DESC
        """

        response = ga_service.search(customer_id=self.customer_id, query=query)

        campaigns = []
        for row in response:
            campaigns.append({
                'id': row.campaign.id,
                'name': row.campaign.name,
                'status': row.campaign.status.name,
                'budget_daily': row.campaign_budget.amount_micros / 1_000_000,
                'impressions': row.metrics.impressions,
                'clicks': row.metrics.clicks,
                'ctr': round(row.metrics.ctr * 100, 2),
                'avg_cpc': round(row.metrics.average_cpc / 1_000_000, 2),
                'cost': round(row.metrics.cost_micros / 1_000_000, 2),
                'conversions': round(row.metrics.conversions, 1),
                'cpa': round(row.metrics.cost_per_conversion / 1_000_000, 2) if row.metrics.conversions > 0 else None,
                'conversion_rate': round(row.metrics.conversion_rate * 100, 2),
                'impression_share': round(row.metrics.search_impression_share * 100, 1),
                'rank_lost_is': round(row.metrics.search_rank_lost_impression_share * 100, 1),
            })

        return {
            'campaigns': campaigns,
            'date_from': date_from,
            'date_to': date_to,
            'total_spend': sum(c['cost'] for c in campaigns),
            'total_conversions': sum(c['conversions'] for c in campaigns),
            'total_clicks': sum(c['clicks'] for c in campaigns),
        }

    async def get_keywords_analysis(self) -> dict:
        """Анализ ключевых слов за 30 дней"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.status,
                campaign.name,
                ad_group.name,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.average_cpc,
                metrics.cost_micros,
                metrics.conversions,
                metrics.quality_score
            FROM keyword_view
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND ad_group_criterion.status != 'REMOVED'
            ORDER BY metrics.cost_micros DESC
            LIMIT 200
        """

        response = ga_service.search(customer_id=self.customer_id, query=query)

        keywords = []
        for row in response:
            kw = {
                'keyword': row.ad_group_criterion.keyword.text,
                'match_type': row.ad_group_criterion.keyword.match_type.name,
                'status': row.ad_group_criterion.status.name,
                'campaign': row.campaign.name,
                'ad_group': row.ad_group.name,
                'impressions': row.metrics.impressions,
                'clicks': row.metrics.clicks,
                'ctr': round(row.metrics.ctr * 100, 2),
                'cpc': round(row.metrics.average_cpc / 1_000_000, 2),
                'spend': round(row.metrics.cost_micros / 1_000_000, 2),
                'conversions': round(row.metrics.conversions, 1),
                'quality_score': row.metrics.quality_score,
            }
            keywords.append(kw)

        return {
            'keywords': keywords,
            'total': len(keywords),
            'date_from': date_from,
            'date_to': date_to,
        }

    async def get_search_terms(self, days: int = 30) -> dict:
        """Поисковые запросы — основа для минус-слов"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                search_term_view.search_term,
                search_term_view.status,
                campaign.name,
                ad_group.name,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.cost_micros,
                metrics.conversions
            FROM search_term_view
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND metrics.impressions > 5
            ORDER BY metrics.impressions DESC
            LIMIT 500
        """

        response = ga_service.search(customer_id=self.customer_id, query=query)

        terms = []
        for row in response:
            terms.append({
                'term': row.search_term_view.search_term,
                'status': row.search_term_view.status.name,
                'campaign': row.campaign.name,
                'ad_group': row.ad_group.name,
                'impressions': row.metrics.impressions,
                'clicks': row.metrics.clicks,
                'ctr': round(row.metrics.ctr * 100, 2),
                'spend': round(row.metrics.cost_micros / 1_000_000, 2),
                'conversions': round(row.metrics.conversions, 1),
            })

        return {'terms': terms, 'days': days}

    async def get_performance_report(self, days: int = 7) -> dict:
        """Отчёт о производительности за N дней"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                segments.date,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.cost_micros,
                metrics.conversions,
                metrics.cost_per_conversion
            FROM customer
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
            ORDER BY segments.date
        """

        response = ga_service.search(customer_id=self.customer_id, query=query)

        daily = []
        for row in response:
            daily.append({
                'date': row.segments.date,
                'impressions': row.metrics.impressions,
                'clicks': row.metrics.clicks,
                'ctr': round(row.metrics.ctr * 100, 2),
                'spend': round(row.metrics.cost_micros / 1_000_000, 2),
                'conversions': round(row.metrics.conversions, 1),
                'cpa': round(row.metrics.cost_per_conversion / 1_000_000, 2) if row.metrics.conversions > 0 else None,
            })

        total_spend = sum(d['spend'] for d in daily)
        total_conv = sum(d['conversions'] for d in daily)

        return {
            'daily': daily,
            'days': days,
            'total_spend': round(total_spend, 2),
            'total_conversions': round(total_conv, 1),
            'avg_cpa': round(total_spend / total_conv, 2) if total_conv > 0 else None,
            'date_from': date_from,
            'date_to': date_to,
        }

    async def get_campaigns(self) -> list:
        """Быстрый список кампаний с текущим статусом"""
        data = await self.get_full_audit_data()
        return data['campaigns']

    async def get_budget_data(self) -> dict:
        """Данные о бюджете всех кампаний"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        query = """
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign_budget.id,
                campaign_budget.name,
                campaign_budget.amount_micros,
                campaign_budget.total_amount_micros,
                metrics.cost_micros,
                metrics.conversions
            FROM campaign
            WHERE campaign.status = 'ENABLED'
            ORDER BY campaign_budget.amount_micros DESC
        """

        response = ga_service.search(customer_id=self.customer_id, query=query)

        budgets = []
        for row in response:
            budgets.append({
                'campaign_id': row.campaign.id,
                'campaign_name': row.campaign.name,
                'budget_id': row.campaign_budget.id,
                'daily_budget': round(row.campaign_budget.amount_micros / 1_000_000, 2),
                'spend_today': round(row.metrics.cost_micros / 1_000_000, 2),
                'conversions': round(row.metrics.conversions, 1),
            })

        return {
            'campaigns': budgets,
            'total_daily_budget': sum(b['daily_budget'] for b in budgets),
            'total_spend_today': sum(b['spend_today'] for b in budgets),
        }

    # ── НОВОЕ: AUCTION INSIGHTS ──────────────────────────

    async def get_auction_insights(self) -> dict:
        """Анализ аукциона — конкуренты и их доля показов за 30 дней"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                auction_insight.domain,
                metrics.search_impression_share,
                metrics.search_top_impression_share,
                metrics.search_absolute_top_impression_share,
                metrics.search_overlap_rate,
                metrics.search_outranking_share,
                campaign.name
            FROM auction_insight
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
            ORDER BY metrics.search_impression_share DESC
        """

        response = ga_service.search(customer_id=self.customer_id, query=query)

        competitors = {}
        for row in response:
            domain = row.auction_insight.domain
            if domain not in competitors:
                competitors[domain] = {
                    'domain': domain,
                    'impression_share': round(row.metrics.search_impression_share * 100, 1),
                    'top_is': round(row.metrics.search_top_impression_share * 100, 1),
                    'abs_top_is': round(row.metrics.search_absolute_top_impression_share * 100, 1),
                    'overlap_rate': round(row.metrics.search_overlap_rate * 100, 1),
                    'outranking_share': round(row.metrics.search_outranking_share * 100, 1),
                    'campaigns': set(),
                }
            competitors[domain]['campaigns'].add(row.campaign.name)

        result = sorted(
            [dict(c, campaigns=list(c['campaigns'])) for c in competitors.values()],
            key=lambda x: x['impression_share'],
            reverse=True
        )

        return {
            'competitors': result,
            'total': len(result),
            'date_from': date_from,
            'date_to': date_to,
        }

    # ── НОВОЕ: A/B ТЕСТ ОБЪЯВЛЕНИЙ ──────────────────────

    async def get_ad_performance(self) -> dict:
        """Производительность объявлений — основа для A/B теста"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                ad_group_ad.ad.id,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.status,
                ad_group.name,
                campaign.name,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.average_cpc,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversion_rate
            FROM ad_group_ad
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND ad_group_ad.status = 'ENABLED'
              AND campaign.status = 'ENABLED'
            ORDER BY metrics.impressions DESC
            LIMIT 50
        """

        response = ga_service.search(customer_id=self.customer_id, query=query)

        ads = []
        for row in response:
            rsa = row.ad_group_ad.ad.responsive_search_ad
            headlines = [h.text for h in rsa.headlines] if rsa.headlines else []
            descriptions = [d.text for d in rsa.descriptions] if rsa.descriptions else []

            ads.append({
                'ad_id': row.ad_group_ad.ad.id,
                'ad_group': row.ad_group.name,
                'campaign': row.campaign.name,
                'headlines': headlines[:3],
                'descriptions': descriptions[:2],
                'impressions': row.metrics.impressions,
                'clicks': row.metrics.clicks,
                'ctr': round(row.metrics.ctr * 100, 2),
                'cpc': round(row.metrics.average_cpc / 1_000_000, 2),
                'cost': round(row.metrics.cost_micros / 1_000_000, 2),
                'conversions': round(row.metrics.conversions, 1),
                'conv_rate': round(row.metrics.conversion_rate * 100, 2),
            })

        # Группируем по ad_group для A/B сравнения
        by_group = {}
        for ad in ads:
            key = ad['ad_group']
            if key not in by_group:
                by_group[key] = []
            by_group[key].append(ad)

        ab_candidates = {k: v for k, v in by_group.items() if len(v) >= 2}

        return {
            'ads': ads,
            'total': len(ads),
            'ab_candidates': ab_candidates,
            'date_from': date_from,
            'date_to': date_to,
        }

    # ── НОВОЕ: СЕЗОННЫЕ КОРРЕКТИРОВКИ ───────────────────

    def get_current_season_recommendations(self) -> dict:
        """
        Возвращает сезонные рекомендации по ключевым словам
        на основе текущего месяца.
        Не обращается к API — чистая логика.
        """
        month = datetime.now().month

        # Зима (декабрь, январь, февраль)
        if month in [12, 1, 2]:
            return {
                'season': 'winter',
                'season_name': 'Зима',
                'boost_keywords': [
                    'refrigerator repair', 'fridge repair', 'freezer repair',
                    'washer repair', 'dryer repair', 'dishwasher repair',
                ],
                'reduce_keywords': [
                    'air conditioner repair', 'AC repair', 'HVAC repair',
                ],
                'boost_pct': 20,
                'reduce_pct': -30,
                'reason': 'Зима: высокий спрос на ремонт холодильников и стиральных машин. '
                          'Кондиционеры — низкий сезон.',
                'ad_copy_tip': 'Добавь в объявления: "Same-day service", "Available today", '
                               '"Emergency repair". Люди хотят быстрого ремонта в праздники.',
            }

        # Весна (март, апрель, май)
        elif month in [3, 4, 5]:
            return {
                'season': 'spring',
                'season_name': 'Весна',
                'boost_keywords': [
                    'appliance repair', 'refrigerator repair', 'washer repair',
                    'dryer repair', 'oven repair', 'stove repair',
                ],
                'reduce_keywords': [],
                'boost_pct': 10,
                'reduce_pct': 0,
                'reason': 'Весна: равномерный спрос на все виды ремонта. '
                          'Люди делают "весеннюю уборку" и замечают проблемы с техникой.',
                'ad_copy_tip': 'Акцент на "Spring special offer", "Free estimate". '
                               'Хорошее время для A/B теста новых заголовков.',
            }

        # Лето (июнь, июль, август)
        elif month in [6, 7, 8]:
            return {
                'season': 'summer',
                'season_name': 'Лето',
                'boost_keywords': [
                    'air conditioner repair', 'AC repair', 'HVAC repair',
                    'refrigerator repair', 'fridge not cooling',
                    'freezer repair', 'ice maker repair',
                ],
                'reduce_keywords': [
                    'dryer repair', 'washer repair',
                ],
                'boost_pct': 30,
                'reduce_pct': -15,
                'reason': 'Лето: пиковый сезон для ремонта кондиционеров и холодильников. '
                          'Жара увеличивает нагрузку на эти приборы.',
                'ad_copy_tip': 'Обязательно: "Same-day AC repair", "24/7 emergency". '
                               'Люди не могут ждать когда жарко. Увеличь бюджет на AC-кампании.',
            }

        # Осень (сентябрь, октябрь, ноябрь)
        else:
            return {
                'season': 'fall',
                'season_name': 'Осень',
                'boost_keywords': [
                    'oven repair', 'stove repair', 'range repair',
                    'dishwasher repair', 'refrigerator repair',
                    'washer repair', 'dryer repair',
                ],
                'reduce_keywords': [
                    'air conditioner repair', 'AC repair',
                ],
                'boost_pct': 15,
                'reduce_pct': -25,
                'reason': 'Осень: спрос на ремонт духовок и плит растёт перед праздниками. '
                          'Начало учебного года = больше стирок. AC уходит в низкий сезон.',
                'ad_copy_tip': 'Добавь: "Ready for the holidays?", "Don\'t let a broken appliance '
                               'ruin Thanksgiving". Работает очень хорошо в октябре-ноябре.',
            }

    # ── ПРИМЕНЕНИЕ ИЗМЕНЕНИЙ ─────────────────────────────

    async def execute_action(self, action: dict) -> dict:
        """
        Точка входа для всех изменений.
        Вызывается ТОЛЬКО после одобрения владельца.
        """
        action_type = action.get('type')
        log.info(f"Выполняю одобренное действие: {action_type}")

        handlers = {
            'pause_keywords': self._pause_keywords,
            'enable_keywords': self._enable_keywords,
            'add_negative_keywords': self._add_negative_keywords,
            'budget_change': self._change_budget,
            'update_bid': self._update_bid,
            'pause_campaign': self._pause_campaign,
            'enable_campaign': self._enable_campaign,
            'seasonal_adjustments': self._apply_seasonal_adjustments,
            'pause_ad': self._pause_ad,
        }

        handler = handlers.get(action_type)
        if not handler:
            raise ValueError(f"Неизвестный тип действия: {action_type}")

        return await handler(action)

    async def _pause_keywords(self, action: dict) -> dict:
        client = self._get_client()
        ad_group_criterion_service = client.get_service("AdGroupCriterionService")

        operations = []
        for kw in action.get('keywords', []):
            op = client.get_type("AdGroupCriterionOperation")
            criterion = op.update
            criterion.resource_name = kw['resource_name']
            criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
            op.update_mask.paths.append("status")
            operations.append(op)

        if operations:
            ad_group_criterion_service.mutate_ad_group_criteria(
                customer_id=self.customer_id,
                operations=operations
            )

        return {'summary': f"Поставлено на паузу: {len(operations)} ключей"}

    async def _add_negative_keywords(self, action: dict) -> dict:
        client = self._get_client()
        campaign_criterion_service = client.get_service("CampaignCriterionService")

        campaigns = await self.get_campaigns()
        enabled_campaign_ids = [c['id'] for c in campaigns if c['status'] == 'ENABLED']

        operations = []
        for neg in action.get('negatives', []):
            for campaign_id in enabled_campaign_ids:
                op = client.get_type("CampaignCriterionOperation")
                criterion = op.create
                criterion.campaign = f"customers/{self.customer_id}/campaigns/{campaign_id}"
                criterion.negative = True
                criterion.keyword.text = neg['term']
                criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
                operations.append(op)

        if operations:
            campaign_criterion_service.mutate_campaign_criteria(
                customer_id=self.customer_id,
                operations=operations
            )

        added = len(action.get('negatives', []))
        return {'summary': f"Добавлено минус-слов: {added} в {len(enabled_campaign_ids)} кампаниях"}

    async def _change_budget(self, action: dict) -> dict:
        client = self._get_client()
        campaign_budget_service = client.get_service("CampaignBudgetService")

        new_amount = action.get('proposed_budget', 0)
        budget_id = action.get('budget_id')

        op = client.get_type("CampaignBudgetOperation")
        budget = op.update
        budget.resource_name = f"customers/{self.customer_id}/campaignBudgets/{budget_id}"
        budget.amount_micros = int(new_amount * 1_000_000)
        op.update_mask.paths.append("amount_micros")

        campaign_budget_service.mutate_campaign_budgets(
            customer_id=self.customer_id,
            operations=[op]
        )

        return {'summary': f"Бюджет изменён на ${new_amount:.2f}/день"}

    async def _update_bid(self, action: dict) -> dict:
        client = self._get_client()
        ad_group_criterion_service = client.get_service("AdGroupCriterionService")

        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = action.get('resource_name')
        criterion.cpc_bid_micros = int(action.get('new_bid', 0) * 1_000_000)
        op.update_mask.paths.append("cpc_bid_micros")

        ad_group_criterion_service.mutate_ad_group_criteria(
            customer_id=self.customer_id,
            operations=[op]
        )

        return {'summary': f"Ставка обновлена: ${action.get('new_bid'):.2f}"}

    async def _pause_campaign(self, action: dict) -> dict:
        client = self._get_client()
        campaign_service = client.get_service("CampaignService")

        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{self.customer_id}/campaigns/{action['campaign_id']}"
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        op.update_mask.paths.append("status")

        campaign_service.mutate_campaigns(customer_id=self.customer_id, operations=[op])
        return {'summary': f"Кампания {action.get('campaign_name')} поставлена на паузу"}

    async def _enable_campaign(self, action: dict) -> dict:
        client = self._get_client()
        campaign_service = client.get_service("CampaignService")

        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{self.customer_id}/campaigns/{action['campaign_id']}"
        campaign.status = client.enums.CampaignStatusEnum.ENABLED
        op.update_mask.paths.append("status")

        campaign_service.mutate_campaigns(customer_id=self.customer_id, operations=[op])
        return {'summary': f"Кампания {action.get('campaign_name')} активирована"}

    async def _enable_keywords(self, action: dict) -> dict:
        client = self._get_client()
        ad_group_criterion_service = client.get_service("AdGroupCriterionService")

        operations = []
        for kw in action.get('keywords', []):
            op = client.get_type("AdGroupCriterionOperation")
            criterion = op.update
            criterion.resource_name = kw['resource_name']
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            op.update_mask.paths.append("status")
            operations.append(op)

        if operations:
            ad_group_criterion_service.mutate_ad_group_criteria(
                customer_id=self.customer_id,
                operations=operations
            )

        return {'summary': f"Активировано ключей: {len(operations)}"}

    async def _apply_seasonal_adjustments(self, action: dict) -> dict:
        """Применяет сезонные корректировки ставок (через custom bid adjustments)"""
        adjustments = action.get('adjustments', [])
        applied = 0
        errors = []

        client = self._get_client()
        campaign_service = client.get_service("CampaignService")

        for adj in adjustments:
            try:
                op = client.get_type("CampaignOperation")
                campaign = op.update
                campaign.resource_name = f"customers/{self.customer_id}/campaigns/{adj['campaign_id']}"
                # Устанавливаем enhanced CPC multiplier через target CPA или manual
                # На практике — изменяем бюджет пропорционально сезонному коэффициенту
                new_budget = adj['current_budget'] * (1 + adj['adjustment_pct'] / 100)
                op2 = client.get_type("CampaignBudgetOperation")
                budget = op2.update
                budget.resource_name = f"customers/{self.customer_id}/campaignBudgets/{adj['budget_id']}"
                budget.amount_micros = int(new_budget * 1_000_000)
                op2.update_mask.paths.append("amount_micros")

                budget_service = client.get_service("CampaignBudgetService")
                budget_service.mutate_campaign_budgets(customer_id=self.customer_id, operations=[op2])
                applied += 1
            except Exception as e:
                errors.append(f"{adj.get('campaign_name', '?')}: {e}")

        msg = f"Сезонные корректировки: {applied}/{len(adjustments)} применено"
        if errors:
            msg += f". Ошибки: {'; '.join(errors)}"
        return {'summary': msg}

    async def _pause_ad(self, action: dict) -> dict:
        """Ставит объявление на паузу (проигравший A/B тест)"""
        client = self._get_client()
        ad_group_ad_service = client.get_service("AdGroupAdService")

        op = client.get_type("AdGroupAdOperation")
        ad = op.update
        ad.resource_name = action.get('resource_name')
        ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
        op.update_mask.paths.append("status")

        ad_group_ad_service.mutate_ad_group_ads(
            customer_id=self.customer_id,
            operations=[op]
        )
        return {'summary': f"Объявление поставлено на паузу: {action.get('ad_id')}"}
