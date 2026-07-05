"""
Google Ads API клиент
v5 — совместимость с google-ads==31.1.0 (API v24)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


class GoogleAdsClient:
    def __init__(self, config):
        self.config = config
        self.customer_id = config.GOOGLE_ADS_CUSTOMER_ID
        self.lsa_customer_id = config.GOOGLE_ADS_LSA_CUSTOMER_ID
        self._client = None

    def _get_client(self):
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

    def _search(self, customer_id: str, query: str) -> list:
        """Универсальный поиск"""
        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")
        response = ga_service.search(customer_id=customer_id, query=query)
        return list(response)

    async def get_both_accounts_summary(self) -> dict:
        ads_data = await self.get_full_audit_data(account="ads")
        lsa_data = await self.get_full_audit_data(account="lsa") if self.lsa_customer_id else None
        total_spend = ads_data['total_spend']
        total_conversions = ads_data['total_conversions']
        if lsa_data and not lsa_data.get('error'):
            total_spend += lsa_data['total_spend']
            total_conversions += lsa_data['total_conversions']
        return {
            'google_ads': ads_data,
            'lsa': lsa_data,
            'combined': {
                'total_spend': round(total_spend, 2),
                'total_conversions': round(total_conversions, 1),
                'avg_cpa': round(total_spend / total_conversions, 2) if total_conversions > 0 else None,
            }
        }

    async def get_full_audit_data(self, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'campaigns': [], 'total_spend': 0, 'total_conversions': 0}

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
                metrics.conversions_from_interactions_rate,
                metrics.search_impression_share,
                metrics.search_rank_lost_impression_share
            FROM campaign
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND campaign.status != 'REMOVED'
            ORDER BY metrics.cost_micros DESC
        """

        try:
            rows = self._search(customer_id, query)
            campaigns = []
            for row in rows:
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
                    'conversion_rate': round(row.metrics.conversions_from_interactions_rate * 100, 2),
                    'impression_share': round(row.metrics.search_impression_share * 100, 1),
                    'rank_lost_is': round(row.metrics.search_rank_lost_impression_share * 100, 1),
                    'account': account,
                })
            return {
                'account': account,
                'customer_id': customer_id,
                'campaigns': campaigns,
                'date_from': date_from,
                'date_to': date_to,
                'total_spend': sum(c['cost'] for c in campaigns),
                'total_conversions': sum(c['conversions'] for c in campaigns),
                'total_clicks': sum(c['clicks'] for c in campaigns),
            }
        except Exception as e:
            log.error(f"get_full_audit_data({account}) error: {e}")
            return {'error': str(e), 'account': account, 'campaigns': [], 'total_spend': 0, 'total_conversions': 0}

    async def get_keywords_analysis(self, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'keywords': []}

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

        try:
            rows = self._search(customer_id, query)
            keywords = []
            for row in rows:
                keywords.append({
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
                })
            return {'keywords': keywords, 'total': len(keywords), 'date_from': date_from, 'date_to': date_to, 'account': account}
        except Exception as e:
            log.error(f"get_keywords_analysis({account}) error: {e}")
            return {'error': str(e), 'keywords': [], 'account': account}

    async def get_search_terms(self, days: int = 30, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'terms': []}

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

        try:
            rows = self._search(customer_id, query)
            terms = []
            for row in rows:
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
            return {'terms': terms, 'days': days, 'account': account}
        except Exception as e:
            log.error(f"get_search_terms({account}) error: {e}")
            return {'error': str(e), 'terms': [], 'account': account}

    async def get_performance_report(self, days: int = 7, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'daily': []}

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

        try:
            rows = self._search(customer_id, query)
            daily = []
            for row in rows:
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
                'daily': daily, 'days': days, 'account': account,
                'total_spend': round(total_spend, 2),
                'total_conversions': round(total_conv, 1),
                'avg_cpa': round(total_spend / total_conv, 2) if total_conv > 0 else None,
                'date_from': date_from, 'date_to': date_to,
            }
        except Exception as e:
            log.error(f"get_performance_report({account}) error: {e}")
            return {'error': str(e), 'daily': [], 'account': account}

    async def get_campaigns(self, account: str = "ads") -> list:
        data = await self.get_full_audit_data(account=account)
        return data.get('campaigns', [])

    async def get_budget_data(self, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'campaigns': []}

        query = """
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign_budget.id,
                campaign_budget.name,
                campaign_budget.amount_micros,
                metrics.cost_micros,
                metrics.conversions
            FROM campaign
            WHERE campaign.status = 'ENABLED'
            ORDER BY campaign_budget.amount_micros DESC
        """

        try:
            rows = self._search(customer_id, query)
            budgets = []
            for row in rows:
                budgets.append({
                    'campaign_id': row.campaign.id,
                    'campaign_name': row.campaign.name,
                    'budget_id': row.campaign_budget.id,
                    'daily_budget': round(row.campaign_budget.amount_micros / 1_000_000, 2),
                    'spend_today': round(row.metrics.cost_micros / 1_000_000, 2),
                    'conversions': round(row.metrics.conversions, 1),
                    'account': account,
                })
            return {
                'campaigns': budgets, 'account': account,
                'total_daily_budget': sum(b['daily_budget'] for b in budgets),
                'total_spend_today': sum(b['spend_today'] for b in budgets),
            }
        except Exception as e:
            log.error(f"get_budget_data({account}) error: {e}")
            return {'error': str(e), 'campaigns': [], 'account': account}

    async def get_auction_insights(self, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'competitors': []}

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

        try:
            rows = self._search(customer_id, query)
            competitors = {}
            for row in rows:
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
                key=lambda x: x['impression_share'], reverse=True
            )
            return {'competitors': result, 'total': len(result), 'date_from': date_from, 'date_to': date_to, 'account': account}
        except Exception as e:
            log.error(f"get_auction_insights({account}) error: {e}")
            return {'error': str(e), 'competitors': [], 'account': account}

    async def get_ad_performance(self, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'ads': []}

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
                metrics.conversions_from_interactions_rate
            FROM ad_group_ad
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND ad_group_ad.status = 'ENABLED'
              AND campaign.status = 'ENABLED'
            ORDER BY metrics.impressions DESC
            LIMIT 50
        """

        try:
            rows = self._search(customer_id, query)
            ads = []
            for row in rows:
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
                    'conv_rate': round(row.metrics.conversions_from_interactions_rate * 100, 2),
                    'account': account,
                })
            by_group = {}
            for ad in ads:
                by_group.setdefault(ad['ad_group'], []).append(ad)
            ab_candidates = {k: v for k, v in by_group.items() if len(v) >= 2}
            return {'ads': ads, 'total': len(ads), 'ab_candidates': ab_candidates, 'date_from': date_from, 'date_to': date_to, 'account': account}
        except Exception as e:
            log.error(f"get_ad_performance({account}) error: {e}")
            return {'error': str(e), 'ads': [], 'account': account}

    def get_current_season_recommendations(self) -> dict:
        month = datetime.now().month
        if month in [12, 1, 2]:
            return {'season': 'winter', 'season_name': 'Зима', 'boost_keywords': ['refrigerator repair', 'fridge repair', 'freezer repair', 'washer repair', 'dryer repair', 'dishwasher repair'], 'reduce_keywords': ['air conditioner repair', 'AC repair', 'HVAC repair'], 'boost_pct': 20, 'reduce_pct': -30, 'reason': 'Зима: высокий спрос на ремонт холодильников и стиральных машин.', 'ad_copy_tip': 'Добавь: "Same-day service", "Available today", "Emergency repair".'}
        elif month in [3, 4, 5]:
            return {'season': 'spring', 'season_name': 'Весна', 'boost_keywords': ['appliance repair', 'refrigerator repair', 'washer repair', 'dryer repair', 'oven repair', 'stove repair'], 'reduce_keywords': [], 'boost_pct': 10, 'reduce_pct': 0, 'reason': 'Весна: равномерный спрос.', 'ad_copy_tip': 'Акцент на "Spring special offer", "Free estimate".'}
        elif month in [6, 7, 8]:
            return {'season': 'summer', 'season_name': 'Лето', 'boost_keywords': ['air conditioner repair', 'AC repair', 'HVAC repair', 'refrigerator repair', 'fridge not cooling', 'freezer repair', 'ice maker repair'], 'reduce_keywords': ['dryer repair', 'washer repair'], 'boost_pct': 30, 'reduce_pct': -15, 'reason': 'Лето: пиковый сезон для ремонта кондиционеров и холодильников.', 'ad_copy_tip': '"Same-day AC repair", "24/7 emergency".'}
        else:
            return {'season': 'fall', 'season_name': 'Осень', 'boost_keywords': ['oven repair', 'stove repair', 'range repair', 'dishwasher repair', 'refrigerator repair', 'washer repair', 'dryer repair'], 'reduce_keywords': ['air conditioner repair', 'AC repair'], 'boost_pct': 15, 'reduce_pct': -25, 'reason': 'Осень: спрос на ремонт духовок и плит растёт перед праздниками.', 'ad_copy_tip': '"Ready for the holidays?"'}

    async def execute_action(self, action: dict) -> dict:
        action_type = action.get('type')
        account = action.get('account', 'ads')
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
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
        return await handler(action, customer_id)

    async def _pause_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        for kw in action.get('keywords', []):
            op = client.get_type("AdGroupCriterionOperation")
            op.update.resource_name = kw['resource_name']
            op.update.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
            op.update_mask.paths.append("status")
            ops.append(op)
        if ops: svc.mutate_ad_group_criteria(customer_id=customer_id, operations=ops)
        return {'summary': f"Поставлено на паузу: {len(ops)} ключей"}

    async def _enable_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        for kw in action.get('keywords', []):
            op = client.get_type("AdGroupCriterionOperation")
            op.update.resource_name = kw['resource_name']
            op.update.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            op.update_mask.paths.append("status")
            ops.append(op)
        if ops: svc.mutate_ad_group_criteria(customer_id=customer_id, operations=ops)
        return {'summary': f"Активировано ключей: {len(ops)}"}

    async def _add_negative_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignCriterionService")
        campaigns = await self.get_campaigns(account="lsa" if customer_id == self.lsa_customer_id else "ads")
        enabled_ids = [c['id'] for c in campaigns if c['status'] == 'ENABLED']
        ops = []
        for neg in action.get('negatives', []):
            for cid in enabled_ids:
                op = client.get_type("CampaignCriterionOperation")
                op.create.campaign = f"customers/{customer_id}/campaigns/{cid}"
                op.create.negative = True
                op.create.keyword.text = neg['term']
                op.create.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
                ops.append(op)
        if ops: svc.mutate_campaign_criteria(customer_id=customer_id, operations=ops)
        return {'summary': f"Добавлено минус-слов: {len(action.get('negatives', []))}"}

    async def _change_budget(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignBudgetService")
        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name = f"customers/{customer_id}/campaignBudgets/{action.get('budget_id')}"
        op.update.amount_micros = int(action.get('proposed_budget', 0) * 1_000_000)
        op.update_mask.paths.append("amount_micros")
        svc.mutate_campaign_budgets(customer_id=customer_id, operations=[op])
        return {'summary': f"Бюджет изменён на ${action.get('proposed_budget'):.2f}/день"}

    async def _update_bid(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.update.resource_name = action.get('resource_name')
        op.update.cpc_bid_micros = int(action.get('new_bid', 0) * 1_000_000)
        op.update_mask.paths.append("cpc_bid_micros")
        svc.mutate_ad_group_criteria(customer_id=customer_id, operations=[op])
        return {'summary': f"Ставка обновлена: ${action.get('new_bid'):.2f}"}

    async def _pause_campaign(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        op.update.resource_name = f"customers/{customer_id}/campaigns/{action['campaign_id']}"
        op.update.status = client.enums.CampaignStatusEnum.PAUSED
        op.update_mask.paths.append("status")
        svc.mutate_campaigns(customer_id=customer_id, operations=[op])
        return {'summary': f"Кампания {action.get('campaign_name')} поставлена на паузу"}

    async def _enable_campaign(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        op.update.resource_name = f"customers/{customer_id}/campaigns/{action['campaign_id']}"
        op.update.status = client.enums.CampaignStatusEnum.ENABLED
        op.update_mask.paths.append("status")
        svc.mutate_campaigns(customer_id=customer_id, operations=[op])
        return {'summary': f"Кампания {action.get('campaign_name')} активирована"}

    async def _apply_seasonal_adjustments(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        applied, errors = 0, []
        for adj in action.get('adjustments', []):
            try:
                svc = client.get_service("CampaignBudgetService")
                op = client.get_type("CampaignBudgetOperation")
                new_budget = adj['current_budget'] * (1 + adj['adjustment_pct'] / 100)
                op.update.resource_name = f"customers/{customer_id}/campaignBudgets/{adj['budget_id']}"
                op.update.amount_micros = int(new_budget * 1_000_000)
                op.update_mask.paths.append("amount_micros")
                svc.mutate_campaign_budgets(customer_id=customer_id, operations=[op])
                applied += 1
            except Exception as e:
                errors.append(f"{adj.get('campaign_name', '?')}: {e}")
        msg = f"Сезонные корректировки: {applied}/{len(action.get('adjustments', []))} применено"
        if errors: msg += f". Ошибки: {'; '.join(errors)}"
        return {'summary': msg}

    async def _pause_ad(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("AdGroupAdService")
        op = client.get_type("AdGroupAdOperation")
        op.update.resource_name = action.get('resource_name')
        op.update.status = client.enums.AdGroupAdStatusEnum.PAUSED
        op.update_mask.paths.append("status")
        svc.mutate_ad_group_ads(customer_id=customer_id, operations=[op])
        return {'summary': f"Объявление поставлено на паузу: {action.get('ad_id')}"}
