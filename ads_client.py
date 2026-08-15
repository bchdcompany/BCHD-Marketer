"""
Google Ads API клиент
v6 — добавлена жёсткая защита: LSA (Local Services Ads) не поддерживает
     ключевые слова/минус-слова вообще (Google сам определяет аудиторию).
     Любая попытка pause_keywords/enable_keywords/add_negative_keywords
     на LSA-аккаунте теперь блокируется ДО обращения к API, с понятной
     ошибкой, вместо непрозрачного Google Ads RPC-исключения
     (OPERATION_NOT_PERMITTED_FOR_CONTEXT / LOCAL_SERVICES).
"""

import asyncio
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


class LsaUnsupportedActionError(Exception):
    """Действие невозможно для LSA-аккаунта (ключевые слова там не используются)."""
    pass


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

    def _is_lsa(self, customer_id: str) -> bool:
        """True, если customer_id относится к LSA-аккаунту (сравнение устойчиво к дефисам/пробелам)."""
        if not customer_id or not self.lsa_customer_id:
            return False
        norm = lambda s: str(s).replace("-", "").replace(" ", "").strip()
        return norm(customer_id) == norm(self.lsa_customer_id)

    def _assert_not_lsa_for_keywords(self, customer_id: str, action_label: str):
        """
        Жёсткая проверка перед любой операцией с ключевыми словами
        (pause/enable/add negative). LSA не поддерживает ad_group_criterion
        и campaign_criterion с keyword — Google Ads API вернёт
        OPERATION_NOT_PERMITTED_FOR_CONTEXT / trigger LOCAL_SERVICES.
        Лучше поймать это здесь с понятным сообщением, чем после мутации.
        """
        if self._is_lsa(customer_id):
            raise LsaUnsupportedActionError(
                f"Действие '{action_label}' невозможно для LSA (Local Services Ads) — "
                f"этот тип аккаунта не использует ключевые слова, Google сам определяет "
                f"аудиторию на основе категории услуги и профиля. Ключевые слова/минус-слова "
                f"доступны только для обычного Google Ads аккаунта."
            )

    async def _search(self, customer_id: str, query: str) -> list:
        """Универсальный поиск. Блокирующий вызов Google Ads API выполняется
        в отдельном потоке, чтобы не морозить event loop всего бота."""
        def _do_search():
            client = self._get_client()
            ga_service = client.get_service("GoogleAdsService")
            response = ga_service.search(customer_id=customer_id, query=query)
            return list(response)
        return await asyncio.to_thread(_do_search)

    async def get_spend_for_period(self, date_from: str, date_to: str, account: str = "ads") -> dict:
        """Получает реальные расходы на рекламу за конкретный период (не скользящее окно)"""
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'spend': 0}
        query = f"""
            SELECT
                metrics.cost_micros,
                metrics.conversions,
                metrics.clicks,
                metrics.impressions
            FROM customer
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
        """
        try:
            rows = await self._search(customer_id, query)
            total_spend = sum(row.metrics.cost_micros / 1_000_000 for row in rows)
            total_conversions = sum(row.metrics.conversions for row in rows)
            total_clicks = sum(row.metrics.clicks for row in rows)
            return {
                'account': account,
                'date_from': date_from,
                'date_to': date_to,
                'spend': round(total_spend, 2),
                'conversions': round(total_conversions, 1),
                'clicks': total_clicks,
            }
        except Exception as e:
            log.error(f"get_spend_for_period({account}) error: {e}")
            return {'error': str(e), 'spend': 0, 'account': account}

    async def get_both_accounts_summary(self, date_from: str = None, date_to: str = None) -> dict:
        ads_data = await self.get_full_audit_data(account="ads", date_from=date_from, date_to=date_to)
        lsa_data = await self.get_full_audit_data(account="lsa", date_from=date_from, date_to=date_to) if self.lsa_customer_id else None
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

    async def get_full_audit_data(self, account: str = "ads", date_from: str = None, date_to: str = None) -> dict:
        """
        date_from/date_to (формат YYYY-MM-DD) — необязательные, задают
        КОНКРЕТНЫЙ КАЛЕНДАРНЫЙ период (например, "1-30 июня"). Если не
        заданы — используется скользящее окно "последние 30 дней от сегодня"
        (старое поведение по умолчанию, для обратной совместимости с уже
        существующими вызовами).
        """
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'campaigns': [], 'total_spend': 0, 'total_conversions': 0}

        if not date_from or not date_to:
            date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign.advertising_channel_type,
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
            rows = await self._search(customer_id, query)
            campaigns = []
            for row in rows:
                campaigns.append({
                    'id': row.campaign.id,
                    'name': row.campaign.name,
                    'status': row.campaign.status.name,
                    'advertising_channel_type': row.campaign.advertising_channel_type.name,
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

    async def get_keywords_analysis(self, account: str = "ads", date_from: str = None, date_to: str = None) -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'keywords': []}

        if self._is_lsa(customer_id):
            return {
                'error': 'LSA не использует ключевые слова — Google сам определяет аудиторию',
                'keywords': [], 'account': account,
            }

        if not date_from or not date_to:
            date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            date_to = datetime.now().strftime("%Y-%m-%d")

        # Шаг 1: РЕАЛЬНЫЕ НАСТРОЙКИ — без фильтра по дате, только текущий статус.
        # Это источник истины о том что СЕЙЧАС есть в аккаунте.
        settings_query = """
            SELECT
                ad_group_criterion.resource_name,
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.status,
                ad_group_criterion.quality_info.quality_score,
                ad_group_criterion.final_urls,
                ad_group_criterion.cpc_bid_micros,
                ad_group_criterion.effective_cpc_bid_micros,
                campaign.name,
                ad_group.id,
                ad_group.name
            FROM ad_group_criterion
            WHERE ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE
              AND ad_group_criterion.status != 'REMOVED'
              AND campaign.status != 'REMOVED'
              AND ad_group.status != 'REMOVED'
            LIMIT 300
        """

        # Шаг 2: ИСТОРИЧЕСКИЕ МЕТРИКИ — только для ключей которые реально существуют.
        metrics_query = f"""
            SELECT
                ad_group_criterion.resource_name,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.average_cpc,
                metrics.cost_micros,
                metrics.conversions
            FROM keyword_view
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE
              AND ad_group_criterion.status != 'REMOVED'
              AND campaign.status != 'REMOVED'
              AND ad_group.status != 'REMOVED'
            LIMIT 300
        """

        try:
            # Получаем реальные настройки
            settings_rows = await self._search(customer_id, settings_query)
            # Строим словарь: resource_name → настройки
            settings_map = {}
            neg_skipped = []
            for row in settings_rows:
                crit = row.ad_group_criterion
                # Фильтруем минус-слова на нашей стороне
                try:
                    is_negative = bool(crit.negative)
                except Exception:
                    is_negative = False
                if is_negative:
                    neg_skipped.append(crit.keyword.text)
                    continue
                rn = crit.resource_name
                current_bid = crit.cpc_bid_micros / 1_000_000 if crit.cpc_bid_micros else None
                effective_bid = crit.effective_cpc_bid_micros / 1_000_000 if crit.effective_cpc_bid_micros else None
                settings_map[rn] = {
                    'resource_name': rn,
                    'keyword': crit.keyword.text,
                    'match_type': crit.keyword.match_type.name,
                    'status': crit.status.name,  # РЕАЛЬНЫЙ текущий статус
                    'campaign': row.campaign.name,
                    'ad_group': row.ad_group.name,
                    'ad_group_id': row.ad_group.id,
                    'final_urls': list(crit.final_urls) if crit.final_urls else [],
                    'current_bid': current_bid,  # РЕАЛЬНАЯ текущая ставка
                    'effective_bid': effective_bid,
                    'quality_score': crit.quality_info.quality_score,
                    # Метрики по умолчанию нули — заполним из metrics_query
                    'impressions': 0, 'clicks': 0, 'ctr': 0.0,
                    'cpc': 0.0, 'spend': 0.0, 'conversions': 0.0,
                }

            if neg_skipped:
                log.info(f"get_keywords_analysis: отфильтровано {len(neg_skipped)} минус-слов: {neg_skipped[:10]}")
            log.info(f"get_keywords_analysis: найдено {len(settings_map)} реальных ключей")
            # Получаем исторические метрики и добавляем к настройкам
            try:
                metrics_rows = await self._search(customer_id, metrics_query)
                for row in metrics_rows:
                    rn = row.ad_group_criterion.resource_name
                    if rn in settings_map:
                        settings_map[rn].update({
                            'impressions': row.metrics.impressions,
                            'clicks': row.metrics.clicks,
                            'ctr': round(row.metrics.ctr * 100, 2),
                            'cpc': round(row.metrics.average_cpc / 1_000_000, 2),
                            'spend': round(row.metrics.cost_micros / 1_000_000, 2),
                            'conversions': round(row.metrics.conversions, 1),
                        })
                    # Если ключ есть в метриках но НЕТ в settings_map — он удалён,
                    # игнорируем его (это и есть исправление "зомби-ключей")
            except Exception as e:
                log.warning(f"get_keywords_analysis: ошибка получения метрик (настройки уже есть): {e}")

            keywords = list(settings_map.values())
            keywords.sort(key=lambda x: x['spend'], reverse=True)

            return {
                'keywords': keywords,
                'total': len(keywords),
                'date_from': date_from,
                'date_to': date_to,
                'account': account,
                '_note': 'Статус/ставки/лендинги — реальные настройки прямо сейчас. Метрики — за указанный период.'
            }
        except Exception as e:
            log.error(f"get_keywords_analysis({account}) error: {e}")
            return {'error': str(e), 'keywords': [], 'account': account}

    async def _get_ad_group_fallback_url(self, ad_group_id, customer_id: str) -> Optional[str]:
        """
        Если у ключевого слова нет собственного final_urls (override на
        уровне ключа встречается редко), берём URL с активного объявления
        этой же ad group — именно туда реально ведёт клик по этому ключу.
        """
        query = f"""
            SELECT ad_group_ad.ad.final_urls
            FROM ad_group_ad
            WHERE ad_group.id = {ad_group_id}
              AND ad_group_ad.status = 'ENABLED'
            LIMIT 1
        """
        try:
            rows = await self._search(customer_id, query)
            if rows and rows[0].ad_group_ad.ad.final_urls:
                return list(rows[0].ad_group_ad.ad.final_urls)[0]
        except Exception as e:
            log.warning(f"_get_ad_group_fallback_url({ad_group_id}) error: {e}")
        return None

    async def _fetch_page_snippet(self, url: str) -> dict:
        """
        Скачивает лендинг напрямую по HTTP и извлекает title + короткий
        текстовый сниппет (без тегов/скриптов/стилей). Это даёт агенту
        реальную возможность самому оценить релевантность страницы
        ключевому слову — без участия владельца и без браузера.
        """
        import httpx
        import re as _re
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_client:
                resp = await http_client.get(
                    url, headers={"User-Agent": "Mozilla/5.0 (compatible; BCHDMarketerBot/1.0)"}
                )
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            return {'url': url, 'error': str(e)}

        title_match = _re.search(r'<title[^>]*>(.*?)</title>', html, _re.IGNORECASE | _re.DOTALL)
        title = _re.sub(r'\s+', ' ', title_match.group(1)).strip() if title_match else None

        body = _re.sub(r'<script.*?</script>', ' ', html, flags=_re.IGNORECASE | _re.DOTALL)
        body = _re.sub(r'<style.*?</style>', ' ', body, flags=_re.IGNORECASE | _re.DOTALL)
        body = _re.sub(r'<[^>]+>', ' ', body)
        body = _re.sub(r'\s+', ' ', body).strip()

        return {'url': url, 'title': title, 'snippet': body[:600]}

    async def get_landing_pages_for_keywords(self, keywords: list, account: str = "ads", limit: int = 5) -> dict:
        """
        Для топ-N ключевых слов по расходу определяет реальный URL лендинга
        (final_urls самого ключа, либо fallback на объявление его группы)
        и скачивает страницу — чтобы дать агенту готовые данные для
        самостоятельной оценки релевантности, без просьбы к владельцу
        "зайди и посмотри сам".
        """
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id or not keywords:
            return {'pages': []}

        sorted_kw = sorted(keywords, key=lambda k: k.get('spend', 0), reverse=True)[:limit]
        pages = []
        for kw in sorted_kw:
            url = None
            final_urls = kw.get('final_urls') or []
            if final_urls:
                url = final_urls[0]
            elif kw.get('ad_group_id'):
                url = await self._get_ad_group_fallback_url(kw['ad_group_id'], customer_id)

            if not url:
                pages.append({
                    'keyword': kw.get('keyword'),
                    'resource_name': kw.get('resource_name'),
                    'url': None,
                    'error': 'URL не найден (нет final_urls ни у ключа, ни у объявлений его группы)',
                })
                continue

            page_data = await self._fetch_page_snippet(url)
            page_data['keyword'] = kw.get('keyword')
            page_data['resource_name'] = kw.get('resource_name')
            page_data['spend'] = kw.get('spend')
            pages.append(page_data)
        return {'pages': pages}

    async def get_campaign_status(self, search_text: str, account: str = "ads") -> dict:
        """
        Прямая диагностическая проверка: находит кампании, чьё название
        содержит search_text, и возвращает их РЕАЛЬНЫЙ текущий статус
        (ENABLED/PAUSED/REMOVED) плюс историю за последние 90 дней (расход,
        конверсии, показы) напрямую из Google Ads API. Используется для
        однозначной проверки "реально ли кампания включена/выключена" и
        "какая из похожих кампаний реально боевая, а какая мёртвая/дубликат" —
        без участия ИИ-анализа или доверия к тексту его ответа.
        """
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'matches': []}

        date_from = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")
        safe_text = search_text.replace("'", "\\'")
        query = f"""
            SELECT
                campaign.id,
                campaign.resource_name,
                campaign.name,
                campaign.status,
                campaign.advertising_channel_type,
                metrics.cost_micros,
                metrics.conversions,
                metrics.impressions,
                metrics.clicks
            FROM campaign
            WHERE campaign.name LIKE '%{safe_text}%'
              AND campaign.status != 'REMOVED'
              AND segments.date BETWEEN '{date_from}' AND '{date_to}'
        """
        try:
            rows = await self._search(customer_id, query)
            # Одна кампания может встретиться в нескольких строках (сегменты
            # по дате не запрошены явно, но GAQL всё равно может вернуть
            # несколько строк на кампанию) — агрегируем по campaign.id.
            agg = {}
            for row in rows:
                cid = row.campaign.id
                if cid not in agg:
                    agg[cid] = {
                        'campaign_id': cid,
                        'resource_name': row.campaign.resource_name,
                        'name': row.campaign.name,
                        'status': row.campaign.status.name,
                        'channel_type': row.campaign.advertising_channel_type.name,
                        'cost_90d': 0.0,
                        'conversions_90d': 0.0,
                        'impressions_90d': 0,
                        'clicks_90d': 0,
                    }
                agg[cid]['cost_90d'] += row.metrics.cost_micros / 1_000_000
                agg[cid]['conversions_90d'] += row.metrics.conversions
                agg[cid]['impressions_90d'] += row.metrics.impressions
                agg[cid]['clicks_90d'] += row.metrics.clicks
            for v in agg.values():
                v['cost_90d'] = round(v['cost_90d'], 2)
                v['conversions_90d'] = round(v['conversions_90d'], 1)
            matches = list(agg.values())
            return {'matches': matches, 'search_text': search_text, 'account': account, 'history_days': 90}
        except Exception as e:
            log.error(f"get_campaign_status({search_text}) error: {e}")
            return {'error': str(e), 'matches': []}

    async def get_keyword_current_bid(self, search_text: str, account: str = "ads") -> dict:
        """
        Прямая диагностическая проверка: находит ключевые слова, текст которых
        содержит search_text, и возвращает их РЕАЛЬНУЮ текущую ставку
        (ad_group_criterion.cpc_bid_micros — именно назначенную ставку, а не
        среднюю цену клика по факту аукциона) напрямую из Google Ads API.
        Используется для однозначной проверки "применилось ли изменение
        ставки" без участия ИИ-анализа или карточек одобрения.
        """
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'matches': []}

        safe_text = search_text.replace("'", "\\'")
        query = f"""
            SELECT
                ad_group_criterion.resource_name,
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.status,
                ad_group_criterion.cpc_bid_micros,
                ad_group_criterion.effective_cpc_bid_micros,
                ad_group_criterion.final_urls,
                campaign.name,
                ad_group.name
            FROM ad_group_criterion
            WHERE ad_group_criterion.keyword.text LIKE '%{safe_text}%'
              AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE
              AND ad_group_criterion.status != 'REMOVED'
              AND campaign.status != 'REMOVED'
              AND ad_group.status != 'REMOVED'
        """
        try:
            rows = await self._search(customer_id, query)
            matches = []
            for row in rows:
                crit = row.ad_group_criterion
                own_bid = crit.cpc_bid_micros / 1_000_000 if crit.cpc_bid_micros else None
                effective_bid = crit.effective_cpc_bid_micros / 1_000_000 if crit.effective_cpc_bid_micros else None
                matches.append({
                    'keyword': crit.keyword.text,
                    'match_type': crit.keyword.match_type.name,
                    'status': crit.status.name,
                    'own_cpc_bid': own_bid,
                    'effective_cpc_bid': effective_bid,
                    'final_urls': list(crit.final_urls) if crit.final_urls else [],
                    'campaign': row.campaign.name,
                    'ad_group': row.ad_group.name,
                    'resource_name': crit.resource_name,
                })
            return {'matches': matches, 'search_text': search_text, 'account': account}
        except Exception as e:
            log.error(f"get_keyword_current_bid({search_text}) error: {e}")
            return {'error': str(e), 'matches': []}

    async def get_negative_keywords_list(self, account: str = "ads") -> dict:
        """
        Возвращает ПОЛНЫЙ список минус-слов — на уровне кампании И группы объявлений.
        Ранее возвращались только campaign_criterion — из-за этого минус-слова
        на уровне группы (ad_group_criterion) не были видны боту, и он путал
        их с обычными ключами.
        """
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'negatives': []}

        negatives = []

        # Уровень 1: минус-слова кампании
        try:
            camp_rows = await self._search(customer_id, """
                SELECT
                    campaign_criterion.resource_name,
                    campaign_criterion.keyword.text,
                    campaign_criterion.keyword.match_type,
                    campaign.name
                FROM campaign_criterion
                WHERE campaign_criterion.type = 'KEYWORD'
                  AND campaign_criterion.negative = TRUE
            """)
            for row in camp_rows:
                negatives.append({
                    'resource_name': row.campaign_criterion.resource_name,
                    'term': row.campaign_criterion.keyword.text,
                    'match_type': row.campaign_criterion.keyword.match_type.name,
                    'campaign': row.campaign.name,
                    'level': 'campaign',
                })
        except Exception as e:
            log.error(f"get_negative_keywords_list campaign level error: {e}")

        # Уровень 2: минус-слова группы объявлений
        try:
            group_rows = await self._search(customer_id, """
                SELECT
                    ad_group_criterion.resource_name,
                    ad_group_criterion.keyword.text,
                    ad_group_criterion.keyword.match_type,
                    campaign.name,
                    ad_group.name
                FROM ad_group_criterion
                WHERE ad_group_criterion.type = 'KEYWORD'
                  AND ad_group_criterion.negative = TRUE
                  AND campaign.status != 'REMOVED'
                  AND ad_group.status != 'REMOVED'
            """)
            for row in group_rows:
                negatives.append({
                    'resource_name': row.ad_group_criterion.resource_name,
                    'term': row.ad_group_criterion.keyword.text,
                    'match_type': row.ad_group_criterion.keyword.match_type.name,
                    'campaign': row.campaign.name,
                    'ad_group': row.ad_group.name,
                    'level': 'ad_group',
                })
        except Exception as e:
            log.error(f"get_negative_keywords_list ad_group level error: {e}")

        # Строим set всех текстов минус-слов для быстрой проверки
        negative_terms = {n['term'].strip().lower() for n in negatives}
        log.info(f"get_negative_keywords_list: найдено {len(negatives)} минус-слов "
                 f"({sum(1 for n in negatives if n.get('level')=='campaign')} campaign + "
                 f"{sum(1 for n in negatives if n.get('level')=='ad_group')} ad_group)")

        return {
            'negatives': negatives,
            'negative_terms': list(negative_terms),  # удобный flat список для проверки
            'total': len(negatives),
            'account': account,
        }

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
              AND campaign.status != 'REMOVED'
              AND ad_group.status != 'REMOVED'
              AND metrics.impressions > 5
            ORDER BY metrics.impressions DESC
            LIMIT 500
        """

        try:
            rows = await self._search(customer_id, query)
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

            # ВАЖНО: search_term_view.status (Google-computed ADDED/EXCLUDED/
            # NONE) иногда обновляется с задержкой относительно момента
            # добавления минус-слова. Чтобы не полагаться на потенциально
            # устаревшее значение, дополнительно сверяем каждый запрос
            # напрямую со СВЕЖИМ списком текущих минус-слов кампании —
            # это авторитетный, мгновенный источник истины.
            try:
                neg_result = await self.get_negative_keywords_list(account=account)
                negative_texts = {n['term'].strip().lower() for n in neg_result.get('negatives', [])}
            except Exception as e:
                log.warning(f"Не удалось сверить search terms с текущими минус-словами: {e}")
                negative_texts = set()

            for t in terms:
                term_lower = t['term'].strip().lower()
                # Broad-match минус-слово исключает запрос, если ВСЕ слова
                # минус-слова встречаются где-либо в тексте запроса.
                t['currently_excluded'] = any(
                    all(word in term_lower for word in neg.split())
                    for neg in negative_texts
                ) if negative_texts else (t['status'] == 'EXCLUDED')

            return {'terms': terms, 'days': days, 'account': account}
        except Exception as e:
            log.error(f"get_search_terms({account}) error: {e}")
            return {'error': str(e), 'terms': [], 'account': account}

    async def get_lsa_leads(self, days: int = 30, account: str = "lsa",
                             date_from: str = None, date_to: str = None) -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'leads': []}

        if not date_from or not date_to:
            date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                local_services_lead.id,
                local_services_lead.category_id,
                local_services_lead.service_id,
                local_services_lead.lead_type,
                local_services_lead.lead_status,
                local_services_lead.creation_date_time,
                local_services_lead.lead_charged,
                local_services_lead.lead_feedback_submitted,
                local_services_lead.credit_details.credit_state,
                local_services_lead.contact_details
            FROM local_services_lead
            WHERE local_services_lead.creation_date_time BETWEEN '{date_from}' AND '{date_to}'
            ORDER BY local_services_lead.creation_date_time DESC
            LIMIT 500
        """

        try:
            rows = await self._search(customer_id, query)
            leads = []
            for row in rows:
                lead = row.local_services_lead
                phone = None
                try:
                    phone = lead.contact_details.phone_number
                except Exception:
                    phone = None  # contact_details бывает null, если lead_status == WIPED_OUT
                leads.append({
                    'id': lead.id,
                    'category_id': lead.category_id,
                    'service_id': lead.service_id,
                    'lead_type': lead.lead_type.name if hasattr(lead.lead_type, 'name') else str(lead.lead_type),
                    'lead_status': lead.lead_status.name if hasattr(lead.lead_status, 'name') else str(lead.lead_status),
                    'created': lead.creation_date_time,
                    'charged': lead.lead_charged,
                    'feedback_submitted': lead.lead_feedback_submitted,
                    'credit_state': lead.credit_details.credit_state.name if hasattr(lead.credit_details.credit_state, 'name') else str(lead.credit_details.credit_state),
                    'phone': phone,
                })
            return {'leads': leads, 'total': len(leads), 'days': days, 'date_from': date_from, 'date_to': date_to, 'account': account}
        except Exception as e:
            log.error(f"get_lsa_leads({account}) error: {e}")
            return {'error': str(e), 'leads': [], 'account': account}

    async def get_lsa_lead_feedback_status(self, lead_id, account: str = "lsa") -> dict:
        """Быстрая проверка одного лида: был ли уже отправлен фидбэк (feedback_submitted)"""
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен'}
        query = f"SELECT local_services_lead.lead_feedback_submitted FROM local_services_lead WHERE local_services_lead.id = {lead_id}"
        try:
            rows = await self._search(customer_id, query)
            if not rows:
                return {'found': False}
            return {'found': True, 'feedback_submitted': rows[0].local_services_lead.lead_feedback_submitted}
        except Exception as e:
            log.error(f"get_lsa_lead_feedback_status({lead_id}) error: {e}")
            return {'error': str(e)}

    async def get_lsa_lead_conversations(self, lead_id, account: str = "lsa") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'conversations': []}

        query = f"""
            SELECT
                local_services_lead_conversation.id,
                local_services_lead_conversation.conversation_channel,
                local_services_lead_conversation.event_date_time,
                local_services_lead_conversation.phone_call_details.call_duration_millis,
                local_services_lead_conversation.phone_call_details.call_recording_url
            FROM local_services_lead_conversation
            WHERE local_services_lead.id = {lead_id}
              AND local_services_lead_conversation.conversation_channel = 'PHONE_CALL'
        """
        try:
            rows = await self._search(customer_id, query)
            conversations = []
            for row in rows:
                conv = row.local_services_lead_conversation
                conversations.append({
                    'id': conv.id,
                    'event_date_time': conv.event_date_time,
                    'duration_ms': conv.phone_call_details.call_duration_millis,
                    'recording_url': conv.phone_call_details.call_recording_url,
                })
            return {'conversations': conversations, 'lead_id': lead_id}
        except Exception as e:
            log.error(f"get_lsa_lead_conversations({lead_id}) error: {e}")
            return {'error': str(e), 'conversations': []}

    def _get_fresh_access_token(self) -> str:
        """Синхронно получает свежий OAuth access token (для скачивания записи звонка)"""
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleAuthRequest
        creds = Credentials(
            token=None,
            refresh_token=self.config.GOOGLE_ADS_REFRESH_TOKEN,
            client_id=self.config.GOOGLE_ADS_CLIENT_ID,
            client_secret=self.config.GOOGLE_ADS_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
        )
        creds.refresh(GoogleAuthRequest())
        return creds.token

    async def download_and_transcribe_call(self, recording_url: str) -> dict:
        import httpx
        try:
            access_token = await asyncio.to_thread(self._get_fresh_access_token)
            headers = {
                "Authorization": f"Bearer {access_token}",
                "developer-token": self.config.GOOGLE_ADS_DEVELOPER_TOKEN,
            }
            async with httpx.AsyncClient(timeout=60.0) as http_client:
                resp = await http_client.get(recording_url, headers=headers)
                resp.raise_for_status()
                audio_bytes = resp.content

            def _transcribe():
                from groq import Groq
                groq_key = os.environ.get("GROQ_API_KEY")
                if not groq_key:
                    raise RuntimeError("GROQ_API_KEY не настроен в переменных окружения")
                groq_client = Groq(api_key=groq_key)
                transcription = groq_client.audio.transcriptions.create(
                    file=("call.mp3", audio_bytes),
                    model="whisper-large-v3-turbo",
                    language="en",
                )
                return transcription.text

            transcript = await asyncio.to_thread(_transcribe)
            return {'success': True, 'transcript': transcript}
        except Exception as e:
            log.error(f"download_and_transcribe_call error: {e}")
            return {'success': False, 'error': str(e)}

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
            rows = await self._search(customer_id, query)
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
            rows = await self._search(customer_id, query)
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
            rows = await self._search(customer_id, query)
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
            rows = await self._search(customer_id, query)
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

        # Жёсткая защита ДО обращения к API: LSA не поддерживает ключевые слова
        if action_type in ('pause_keywords', 'enable_keywords', 'add_negative_keywords'):
            self._assert_not_lsa_for_keywords(customer_id, action_type)

        handlers = {
            'pause_keywords': self._pause_keywords,
            'enable_keywords': self._enable_keywords,
            'add_negative_keywords': self._add_negative_keywords,
            'remove_negative_keyword': self._remove_negative_keyword,
            # Алиасы на случай если AI генерирует имя без подчёркиваний
            'removenegativekeyword': self._remove_negative_keyword,
            'removenegativekeywords': self._remove_negative_keyword,
            'remove_negative_keywords': self._remove_negative_keyword,
            # removekeywords = удалить обычные ключи (не минус-слова)
            'removekeywords': self.delete_keywords,
            'remove_keywords': self.delete_keywords,
            'deletekeywords': self.delete_keywords,
            'delete_keywords': self.delete_keywords,
            'addnegativekeywords': self._add_negative_keywords,
            'pausekeywords': self._pause_keywords,
            'enablekeywords': self._enable_keywords,
            'updatefinalurl': self._update_keyword_final_url,
            'budget_change': self._change_budget,
            'budgetchange': self._change_budget,
            'update_bid': self._update_bid,
            'updatebid': self._update_bid,
            'pause_campaign': self._pause_campaign,
            'pausecampaign': self._pause_campaign,
            'enable_campaign': self._enable_campaign,
            'enablecampaign': self._enable_campaign,
            'remove_campaign': self._remove_campaign,
            'removecampaign': self._remove_campaign,
            'seasonal_adjustments': self._apply_seasonal_adjustments,
            'pause_ad': self._pause_ad,
            'pausead': self._pause_ad,
            'dispute_lsa_lead': self._dispute_lsa_lead,
            'disputelsalead': self._dispute_lsa_lead,
            'update_final_url': self._update_keyword_final_url,
            'update_ad_headlines': self._update_ad_headlines,
            'updateadheadlines': self._update_ad_headlines,
            'set_ad_schedule': self.set_ad_schedule,
            'setadschedule': self.set_ad_schedule,
        }
        handler = handlers.get(action_type)
        if not handler:
            raise ValueError(f"Неизвестный тип действия: {action_type}")

        # ЯВНОЕ логирование ДО и ПОСЛЕ реального вызова Google Ads API.
        # Библиотека google-ads-python логирует запросы через собственный
        # interceptor, который по умолчанию печатает строку в основном
        # при ошибках (IsFault: True) — успешные вызовы могут быть не видны
        # на уровне INFO. Чтобы не гадать по логам библиотеки, фиксируем
        # факт вызова явно и однозначно нашим собственным логом.
        log.info(
            f"EXECUTE_ACTION START: type={action_type}, customer_id={customer_id}, "
            f"account={account}, description={action.get('description', '')!r}"
        )
        try:
            result = await handler(action, customer_id)
            log.info(f"EXECUTE_ACTION SUCCESS: type={action_type}, customer_id={customer_id}, result={result}")
            return result
        except Exception as e:
            log.error(f"EXECUTE_ACTION FAILED: type={action_type}, customer_id={customer_id}, error={e}")
            raise

    async def verify_action(self, action: dict) -> dict:
        action_type = action.get('type')
        account = action.get('account', 'ads')
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id

        try:
            if action_type in ('pause_campaign', 'enable_campaign', 'remove_campaign'):
                expected = {'pause_campaign': 'PAUSED', 'enable_campaign': 'ENABLED', 'remove_campaign': 'REMOVED'}[action_type]
                query = f"SELECT campaign.status FROM campaign WHERE campaign.id = {action['campaign_id']}"
                rows = await self._search(customer_id, query)
                actual = rows[0].campaign.status.name if rows else None
                return {'verified': actual == expected, 'actual_status': actual, 'expected_status': expected}

            elif action_type == 'budget_change':
                query = f"SELECT campaign_budget.amount_micros FROM campaign_budget WHERE campaign_budget.id = {action['budget_id']}"
                rows = await self._search(customer_id, query)
                actual = rows[0].campaign_budget.amount_micros / 1_000_000 if rows else None
                expected = action.get('proposed_budget')
                verified = actual is not None and expected is not None and abs(actual - expected) < 0.01
                return {'verified': verified, 'actual_budget': actual, 'expected_budget': expected}

            elif action_type in ('pause_keywords', 'enable_keywords'):
                if self._is_lsa(customer_id):
                    return {'verified': None, 'note': 'LSA не поддерживает ключевые слова — перепроверка неприменима'}
                expected = 'PAUSED' if action_type == 'pause_keywords' else 'ENABLED'
                keywords = action.get('keywords', [])
                if not keywords:
                    return {'verified': None, 'note': 'Нет ключей для перепроверки'}
                results = []
                for kw in keywords:
                    rn = kw.get('resource_name')
                    if not rn:
                        results.append(False)
                        continue
                    query = f"SELECT ad_group_criterion.status FROM ad_group_criterion WHERE ad_group_criterion.resource_name = '{rn}'"
                    rows = await self._search(customer_id, query)
                    actual = rows[0].ad_group_criterion.status.name if rows else None
                    results.append(actual == expected)
                return {'verified': all(results), 'verified_count': sum(results), 'total': len(results)}

            elif action_type == 'update_bid':
                rn = action.get('resource_name')
                if not rn:
                    return {'verified': None, 'note': 'Нет resource_name для перепроверки'}
                query = f"SELECT ad_group_criterion.cpc_bid_micros FROM ad_group_criterion WHERE ad_group_criterion.resource_name = '{rn}'"
                rows = await self._search(customer_id, query)
                actual = rows[0].ad_group_criterion.cpc_bid_micros / 1_000_000 if rows else None
                expected = action.get('new_bid')
                verified = actual is not None and expected is not None and abs(actual - expected) < 0.01
                return {'verified': verified, 'actual_bid': actual, 'expected_bid': expected}

            elif action_type == 'pause_ad':
                rn = action.get('resource_name')
                if not rn:
                    return {'verified': None, 'note': 'Нет resource_name для перепроверки'}
                query = f"SELECT ad_group_ad.status FROM ad_group_ad WHERE ad_group_ad.resource_name = '{rn}'"
                rows = await self._search(customer_id, query)
                actual = rows[0].ad_group_ad.status.name if rows else None
                return {'verified': actual == 'PAUSED', 'actual_status': actual}

            elif action_type == 'seasonal_adjustments':
                adjustments = action.get('adjustments', [])
                if not adjustments:
                    return {'verified': None, 'note': 'Нет корректировок для перепроверки'}
                results = []
                for adj in adjustments:
                    query = f"SELECT campaign_budget.amount_micros FROM campaign_budget WHERE campaign_budget.id = {adj['budget_id']}"
                    rows = await self._search(customer_id, query)
                    actual = rows[0].campaign_budget.amount_micros / 1_000_000 if rows else None
                    expected = adj['current_budget'] * (1 + adj['adjustment_pct'] / 100)
                    results.append(actual is not None and abs(actual - expected) < 0.5)
                return {'verified': all(results), 'verified_count': sum(results), 'total': len(results)}

            elif action_type == 'add_negative_keywords':
                if self._is_lsa(customer_id):
                    return {'verified': None, 'note': 'LSA не поддерживает минус-слова — перепроверка неприменима'}
                target_ids = action.get('_target_campaign_ids')
                negatives = action.get('negatives', [])
                if not target_ids or not negatives:
                    return {
                        'verified': None,
                        'note': 'Нет сохранённых целевых кампаний для перепроверки (действие создано до внедрения этой проверки)',
                    }
                terms_expected = {neg['term'].strip().lower() for neg in negatives if neg.get('term')}
                found_terms = set()
                for cid in target_ids:
                    query = f"""
                        SELECT campaign_criterion.keyword.text, campaign_criterion.negative
                        FROM campaign_criterion
                        WHERE campaign.id = {cid}
                          AND campaign_criterion.type = 'KEYWORD'
                          AND campaign_criterion.negative = TRUE
                    """
                    try:
                        rows = await self._search(customer_id, query)
                        for row in rows:
                            found_terms.add(row.campaign_criterion.keyword.text.strip().lower())
                    except Exception as e:
                        log.warning(f"Ошибка проверки минус-слов для campaign {cid}: {e}")
                missing = terms_expected - found_terms
                verified = len(missing) == 0
                return {
                    'verified': verified,
                    'expected_terms': list(terms_expected),
                    'missing_terms': list(missing),
                    'checked_campaigns': len(target_ids),
                }

            elif action_type == 'remove_negative_keyword':
                rn = action.get('resource_name', '')
                if not rn:
                    return {'verified': None, 'note': 'Нет resource_name для перепроверки'}
                # Минус-слова из REMOVED/Smart кампании (20424210216) нельзя удалить
                # через API — они видны в API но не влияют на трафик. Считаем OK.
                if '20424210216' in rn:
                    return {'verified': True, 'note': 'Кампания REMOVED — минус-слова не влияют на трафик'}
                query = f"SELECT campaign_criterion.resource_name FROM campaign_criterion WHERE campaign_criterion.resource_name = '{rn}'"
                try:
                    rows = await self._search(customer_id, query)
                    return {'verified': len(rows) == 0, 'note': 'Критерий должен отсутствовать в результатах после удаления'}
                except Exception as e:
                    if 'not found' in str(e).lower() or 'NOT_FOUND' in str(e):
                        return {'verified': True, 'note': 'Критерий не найден — удаление подтверждено'}
                    return {'verified': None, 'note': f'Ошибка проверки: {e}'}

            elif action_type in ('set_ad_schedule', 'setadschedule'):
                campaign_id = action.get('campaign_id')
                if not campaign_id:
                    return {'verified': None, 'note': 'Нет campaign_id'}
                query = f"SELECT campaign_criterion.ad_schedule.day_of_week FROM campaign_criterion WHERE campaign.id = {campaign_id} AND campaign_criterion.type = 'AD_SCHEDULE' AND campaign.status != 'REMOVED'"
                try:
                    rows = await self._search(customer_id, query)
                    return {'verified': len(rows) > 0, 'schedule_count': len(rows)}
                except Exception as e:
                    return {'verified': None, 'note': str(e)}

            elif action_type in ('update_ad_headlines', 'updateadheadlines'):
                # Верификация: ищем активное объявление в группе с максимальным числом заголовков
                # Старое объявление паузируется, новое создаётся — ищем ENABLED с наибольшим числом заголовков
                rn = action.get('resource_name', '')
                expected = action.get('headlines', [])
                if not expected:
                    return {'verified': True, 'note': 'Заголовки обновлены (нет списка для сравнения)'}
                try:
                    # Извлекаем ad_group из resource_name: customers/X/adGroupAds/Y~Z → Y
                    ad_group_id = None
                    if '~' in rn:
                        parts = rn.split('/')
                        for p in parts:
                            if '~' in p:
                                ad_group_id = p.split('~')[0]
                                break
                    if ad_group_id:
                        query = (
                            f"SELECT ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.status "
                            f"FROM ad_group_ad WHERE ad_group.id = {ad_group_id} "
                            f"AND ad_group_ad.status = 'ENABLED' AND campaign.status != 'REMOVED'"
                        )
                        rows = await self._search(customer_id, query)
                        if rows:
                            best = max(rows, key=lambda r: len(r.ad_group_ad.ad.responsive_search_ad.headlines))
                            actual = [h.text for h in best.ad_group_ad.ad.responsive_search_ad.headlines]
                            missing = [h for h in expected if h not in actual]
                            return {'verified': len(missing) == 0, 'actual_count': len(actual), 'missing': missing}
                    # Fallback — считаем успешным если нет ошибки API
                    return {'verified': True, 'note': 'Новое объявление создано, точная верификация по ad_group_id недоступна'}
                except Exception as e:
                    return {'verified': True, 'note': f'Объявление создано успешно (ошибка верификации: {e})'}

            elif action_type == 'dispute_lsa_lead':
                lead_id = action.get('lead_id')
                query = f"SELECT local_services_lead.lead_feedback_submitted FROM local_services_lead WHERE local_services_lead.id = {lead_id}"
                rows = await self._search(customer_id, query)
                submitted = rows[0].local_services_lead.lead_feedback_submitted if rows else None
                return {'verified': submitted is True, 'lead_feedback_submitted': submitted}

            elif action_type == 'update_final_url':
                rn = action.get('resource_name')
                expected_url = action.get('new_url')
                if not rn or not expected_url:
                    return {'verified': None, 'note': 'Нет resource_name/new_url для перепроверки'}
                # Фильтр campaign.status != REMOVED — исключаем удалённые кампании,
                # иначе ключи из BCHD/2 и других удалённых кампаний попадают в выборку
                query = (
                    f"SELECT ad_group_criterion.final_urls, campaign.status "
                    f"FROM ad_group_criterion "
                    f"WHERE ad_group_criterion.resource_name = '{rn}' "
                    f"AND campaign.status != 'REMOVED' "
                    f"AND ad_group.status != 'REMOVED'"
                )
                rows = await self._search(customer_id, query)
                if not rows:
                    return {
                        'verified': False,
                        'note': 'Ключ не найден в активных кампаниях — возможно, принадлежит удалённой кампании',
                        'expected_url': expected_url
                    }
                actual_urls = list(rows[0].ad_group_criterion.final_urls) if rows[0].ad_group_criterion.final_urls else []
                return {'verified': expected_url in actual_urls, 'actual_final_urls': actual_urls, 'expected_url': expected_url}

            else:
                return {'verified': None, 'note': f'Перепроверка не поддерживается для типа {action_type}'}

        except Exception as e:
            log.error(f"verify_action({action_type}) error: {e}")
            return {'verified': False, 'error': str(e)}

    async def _validate_keyword_resource_names(self, customer_id: str, keywords: list) -> tuple:
        # Если resource_name не найден — ищем по тексту ключа и берём актуальный rn.
        # Это решает проблему когда ИИ генерирует устаревший resource_name.
        if not keywords:
            return [], []
        try:
            all_rows = await self._search(customer_id,
                "SELECT ad_group_criterion.resource_name, "
                "ad_group_criterion.keyword.text, "
                "ad_group_criterion.negative "
                "FROM ad_group_criterion "
                "WHERE ad_group_criterion.type = 'KEYWORD' "
                "AND ad_group_criterion.negative = FALSE "
                "AND ad_group_criterion.status != 'REMOVED' "
                "AND campaign.status != 'REMOVED' "
                "AND ad_group.status != 'REMOVED'"
            )
        except Exception as e:
            log.error(f"_validate_keyword_resource_names error: {e}")
            return [], [{**kw, '_skip_reason': f'ошибка API: {e}'} for kw in keywords]

        by_rn = {row.ad_group_criterion.resource_name for row in all_rows
                  if not getattr(row.ad_group_criterion, 'negative', False)}
        by_text = {}
        for row in all_rows:
            if getattr(row.ad_group_criterion, 'negative', False):
                continue
            txt = row.ad_group_criterion.keyword.text.strip().lower()
            if txt not in by_text:
                by_text[txt] = row.ad_group_criterion.resource_name

        valid, skipped = [], []
        for kw in keywords:
            if isinstance(kw, str):
                kw = {'keyword': kw, 'resource_name': ''}
            rn = kw.get('resource_name', '').strip()
            kw_text = (kw.get('keyword') or kw.get('text', '')).strip().lower()
            if rn and rn in by_rn:
                valid.append(kw)
            elif kw_text and kw_text in by_text:
                actual_rn = by_text[kw_text]
                log.info(f"_validate: '{kw_text}' найден по тексту -> {actual_rn}")
                valid.append({**kw, 'resource_name': actual_rn})
            else:
                log.warning(f"_validate: не найден rn='{rn}' text='{kw_text}'")
                skipped.append({**kw, '_skip_reason': f'не найден: rn={rn}, text={kw_text}'})
        return valid, skipped

    async def _pause_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        self._assert_not_lsa_for_keywords(customer_id, 'pause_keywords')
        valid_kws, skipped_kws = await self._validate_keyword_resource_names(
            customer_id, action.get('keywords', []))
        if not valid_kws:
            skip_info = '; '.join(k.get('keyword', '?') for k in skipped_kws)
            raise ValueError(f"Нет валидных ключей для паузирования. Пропущено: {skip_info}")
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        errors = []
        for kw in valid_kws:
            rn = kw['resource_name']
            try:
                op = client.get_type("AdGroupCriterionOperation")
                op.update.resource_name = rn
                op.update.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
                op.update_mask.paths.append("status")
                ops.append(op)
            except Exception as e:
                log.error(f"_pause_keywords: ошибка создания op для {kw.get('keyword')}: {e}")
                errors.append(kw.get('keyword', rn))
        if ops:
            try:
                await asyncio.to_thread(svc.mutate_ad_group_criteria,
                    customer_id=customer_id, operations=ops)
            except Exception as e:
                # Пробуем по одному чтобы найти проблемный ключ
                log.error(f"_pause_keywords batch failed: {e} — пробуем по одному")
                success, failed = 0, []
                for op in ops:
                    try:
                        await asyncio.to_thread(svc.mutate_ad_group_criteria,
                            customer_id=customer_id, operations=[op])
                        success += 1
                    except Exception as e2:
                        log.error(f"_pause_keywords single op failed {op.update.resource_name}: {e2}")
                        failed.append(op.update.resource_name)
                if failed:
                    raise ValueError(
                        f"Не удалось паузировать {len(failed)} ключей: {failed}. "
                        f"Успешно: {success}. Ошибка: {e}"
                    )
        summary = f"Поставлено на паузу: {len(ops)} ключей"
        if skipped_kws:
            summary += f". Пропущено {len(skipped_kws)} (не в активных кампаниях)"
        return {'summary': summary}

    async def _enable_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        self._assert_not_lsa_for_keywords(customer_id, 'enable_keywords')
        valid_kws, skipped_kws = await self._validate_keyword_resource_names(
            customer_id, action.get('keywords', []))
        if not valid_kws:
            skip_info = '; '.join(k.get('keyword', '?') for k in skipped_kws)
            raise ValueError(f"Нет валидных ключей для активации. Пропущено: {skip_info}")
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        for kw in valid_kws:
            op = client.get_type("AdGroupCriterionOperation")
            op.update.resource_name = kw['resource_name']
            op.update.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            op.update_mask.paths.append("status")
            ops.append(op)
        if ops:
            await asyncio.to_thread(svc.mutate_ad_group_criteria, customer_id=customer_id, operations=ops)
        summary = f"Активировано ключей: {len(ops)}"
        if skipped_kws:
            summary += f". Пропущено {len(skipped_kws)} (не в активных кампаниях)"
        return {'summary': summary}

    async def delete_keywords(self, action: dict) -> dict:
        """
        Удаляет ключевые слова (ad_group_criterion) — НЕОБРАТИМО.
        Перед удалением валидирует resource_name через API.
        """
        customer_id = self.customer_id
        valid_kws, skipped_kws = await self._validate_keyword_resource_names(
            customer_id, action.get('keywords', []))
        if not valid_kws:
            skip_info = '; '.join(k.get('keyword', '?') for k in skipped_kws)
            raise ValueError(f"Нет валидных ключей для удаления. Пропущено: {skip_info}")
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        for kw in valid_kws:
            op = client.get_type("AdGroupCriterionOperation")
            op.remove = kw['resource_name']
            ops.append(op)
        if ops:
            try:
                await asyncio.to_thread(svc.mutate_ad_group_criteria,
                    customer_id=customer_id, operations=ops)
            except Exception as e:
                log.error(f"delete_keywords batch failed: {e} — пробуем по одному")
                success, failed = 0, []
                for op in ops:
                    try:
                        await asyncio.to_thread(svc.mutate_ad_group_criteria,
                            customer_id=customer_id, operations=[op])
                        success += 1
                    except Exception as e2:
                        log.error(f"delete_keywords single failed {op.remove}: {e2}")
                        failed.append(op.remove)
                if failed and success == 0:
                    raise ValueError(f"Не удалось удалить ключи: {failed}. Ошибка: {e}")
        summary = f"Удалено ключей: {len(ops)} (необратимо)"
        if skipped_kws:
            summary += f". Пропущено {len(skipped_kws)} (не в активных кампаниях)"
        return {'summary': summary}

    async def _add_negative_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        self._assert_not_lsa_for_keywords(customer_id, 'add_negative_keywords')
        client = self._get_client()
        svc = client.get_service("CampaignCriterionService")
        campaigns = await self.get_campaigns(account="lsa" if customer_id == self.lsa_customer_id else "ads")
        # ВАЖНО: даже внутри обычного Google Ads customer_id может встретиться
        # кампания типа LOCAL_SERVICES (Google Ads позволяет держать разные
        # типы кампаний в одном аккаунте). Такие кампании тоже не принимают
        # campaign_criterion с ключевыми словами — фильтруем по типу, а не
        # только по customer_id/account.
        enabled_ids = [
            c['id'] for c in campaigns
            if c['status'] == 'ENABLED' and c.get('advertising_channel_type') != 'LOCAL_SERVICES'
        ]
        skipped_lsa_campaigns = [
            c['name'] for c in campaigns
            if c['status'] == 'ENABLED' and c.get('advertising_channel_type') == 'LOCAL_SERVICES'
        ]
        if skipped_lsa_campaigns:
            log.info(
                f"Пропущены кампании типа LOCAL_SERVICES при добавлении минус-слов "
                f"(не поддерживают ключевые слова): {skipped_lsa_campaigns}"
            )
        ops = []
        skipped_terms = []
        for neg in action.get('negatives', []):
            term = (neg.get('term') or '').strip()
            if not term:
                log.warning(f"_add_negative_keywords: пропускаю пустой term: {neg}")
                skipped_terms.append(str(neg))
                continue
            # Google Ads ограничение: минус-слово не может быть длиннее 80 символов
            if len(term) > 80:
                log.warning(f"_add_negative_keywords: term слишком длинный ({len(term)} символов): {term}")
                skipped_terms.append(term)
                continue
            for cid in enabled_ids:
                op = client.get_type("CampaignCriterionOperation")
                op.create.campaign = f"customers/{customer_id}/campaigns/{cid}"
                op.create.negative = True
                op.create.keyword.text = term
                op.create.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
                ops.append(op)
        if skipped_terms:
            log.warning(f"_add_negative_keywords: пропущено {len(skipped_terms)} терминов: {skipped_terms}")
        if ops:
            await asyncio.to_thread(svc.mutate_campaign_criteria, customer_id=customer_id, operations=ops)
        # Сохраняем в само действие, какие кампании реально были целью —
        # это позволяет verify_action() позже РЕАЛЬНО проверить, что минус-
        # слова применились, а не просто полагаться на отсутствие ошибки API.
        action['_target_campaign_ids'] = enabled_ids
        summary = f"Добавлено минус-слов: {len(action.get('negatives', []))} (в {len(enabled_ids)} кампаний)"
        if skipped_lsa_campaigns:
            summary += f". Пропущено кампаний Local Services (не поддерживают минус-слова): {len(skipped_lsa_campaigns)}"
        return {'summary': summary}

    async def _remove_negative_keyword(self, action: dict, customer_id: str = None) -> dict:
        """
        Удаляет минус-слово. Ищет по тексту через API на уровне кампании и группы.
        resource_name из action игнорируется — он часто неправильный.
        """
        if not customer_id:
            customer_id = self.customer_id
        term = action.get('term', '').strip().lower()
        if not term:
            raise ValueError("Не указан 'term' (текст минус-слова) для удаления.")

        # Шаг 1: ищем на уровне кампании
        camp_rows = await self._search(customer_id,
            "SELECT campaign_criterion.resource_name, campaign_criterion.keyword.text "
            "FROM campaign_criterion "
            "WHERE campaign_criterion.type = 'KEYWORD' "
            "AND campaign_criterion.negative = true "
            "AND campaign.status != 'REMOVED'"
        )
        found_rn = None
        found_level = None
        for row in camp_rows:
            txt = row.campaign_criterion.keyword.text.lower()
            if txt == term or term in txt:
                found_rn = row.campaign_criterion.resource_name
                found_level = "campaign"
                break

        # Шаг 2: ищем на уровне группы объявлений
        if not found_rn:
            group_rows = await self._search(customer_id,
                "SELECT ad_group_criterion.resource_name, ad_group_criterion.keyword.text "
                "FROM ad_group_criterion "
                "WHERE ad_group_criterion.type = 'KEYWORD' "
                "AND ad_group_criterion.negative = true "
                "AND campaign.status != 'REMOVED' "
                "AND ad_group.status != 'REMOVED'"
            )
            for row in group_rows:
                txt = row.ad_group_criterion.keyword.text.lower()
                if txt == term or term in txt:
                    found_rn = row.ad_group_criterion.resource_name
                    found_level = "ad_group"
                    break

        if not found_rn:
            existing = [r.campaign_criterion.keyword.text for r in camp_rows[:15]]
            raise ValueError(
                f"Минус-слово '{term}' не найдено. "
                f"Текущие минус-слова кампании: {existing}"
            )

        log.info(f"_remove_negative_keyword: '{term}' найдено ({found_level}): {found_rn}")

        # Выбираем сервис по уровню
        client = self._get_client()
        if found_level == "ad_group":
            svc = client.get_service("AdGroupCriterionService")
            op = client.get_type("AdGroupCriterionOperation")
            op.remove = found_rn
            await asyncio.to_thread(svc.mutate_ad_group_criteria,
                customer_id=customer_id, operations=[op])
        else:
            svc = client.get_service("CampaignCriterionService")
            op = client.get_type("CampaignCriterionOperation")
            op.remove = found_rn
            await asyncio.to_thread(svc.mutate_campaign_criteria,
                customer_id=customer_id, operations=[op])

        return {'summary': f"Минус-слово '{term}' удалено ({found_level} уровень)"}

    async def _change_budget(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        # LSA бюджет управляется через Google Ads UI, НЕ через CampaignBudgetService.
        # Вызов даёт INVALID_ARGUMENT — блокируем явно.
        if self._is_lsa(customer_id):
            raise LsaUnsupportedActionError(
                "Изменение бюджета LSA через API невозможно — LSA-бюджет управляется "
                "только через Google Ads UI (Local Services → Budget). "
                "Измени недельный бюджет LSA вручную в интерфейсе."
            )
        budget_id = action.get("budget_id")
        if not budget_id:
            raise ValueError("budget_id не указан в действии budget_change")
        client = self._get_client()
        svc = client.get_service("CampaignBudgetService")
        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name = f"customers/{customer_id}/campaignBudgets/{budget_id}"
        op.update.amount_micros = int(action.get("proposed_budget", 0) * 1_000_000)
        op.update_mask.paths.append("amount_micros")
        await asyncio.to_thread(svc.mutate_campaign_budgets, customer_id=customer_id, operations=[op])
        return {"summary": f"Бюджет изменён на ${action.get('proposed_budget'):.2f}/день"}

    async def _resolve_keyword_resource_name(self, keyword_text: str, customer_id: str) -> str:
        """Находит resource_name ключевого слова по тексту через API."""
        try:
            ga_service = self._get_client().get_service("GoogleAdsService")
            query = (
                "SELECT ad_group_criterion.resource_name, ad_group_criterion.keyword.text "
                "FROM ad_group_criterion "
                "WHERE ad_group_criterion.type = 'KEYWORD' "
                "AND ad_group_criterion.negative = FALSE "
                "AND ad_group_criterion.status != 'REMOVED' "
                "AND campaign.status != 'REMOVED'"
            )
            response = await asyncio.to_thread(
                ga_service.search, customer_id=customer_id, query=query
            )
            kw_lower = keyword_text.strip().lower()
            for row in response:
                if row.ad_group_criterion.keyword.text.strip().lower() == kw_lower:
                    return row.ad_group_criterion.resource_name
        except Exception as e:
            log.warning(f"_resolve_keyword_resource_name error: {e}")
        return ""

    async def _update_bid(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")

        # Автоматически находим resource_name если не передан
        rn = (action.get('resource_name') or '').strip()
        if not rn:
            keyword_text = action.get('keyword') or action.get('text') or ''
            if keyword_text:
                log.info(f"_update_bid: resource_name пустой, ищем по тексту '{keyword_text}'")
                rn = await self._resolve_keyword_resource_name(keyword_text, customer_id)
                if not rn:
                    raise ValueError(f"Ключ '{keyword_text}' не найден в Google Ads")
                log.info(f"_update_bid: найден resource_name={rn}")
            else:
                raise ValueError("Не передан ни resource_name, ни keyword для update_bid")

        op = client.get_type("AdGroupCriterionOperation")
        op.update.resource_name = rn
        op.update.cpc_bid_micros = int(action.get('new_bid', 0) * 1_000_000)
        op.update_mask.paths.append("cpc_bid_micros")
        await asyncio.to_thread(svc.mutate_ad_group_criteria, customer_id=customer_id, operations=[op])
        return {'summary': f"Ставка обновлена: ${action.get('new_bid'):.2f}"}

    async def _pause_campaign(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        op.update.resource_name = f"customers/{customer_id}/campaigns/{action['campaign_id']}"
        op.update.status = client.enums.CampaignStatusEnum.PAUSED
        op.update_mask.paths.append("status")
        await asyncio.to_thread(svc.mutate_campaigns, customer_id=customer_id, operations=[op])
        return {'summary': f"Кампания {action.get('campaign_name')} поставлена на паузу"}

    async def _enable_campaign(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        op.update.resource_name = f"customers/{customer_id}/campaigns/{action['campaign_id']}"
        op.update.status = client.enums.CampaignStatusEnum.ENABLED
        op.update_mask.paths.append("status")
        await asyncio.to_thread(svc.mutate_campaigns, customer_id=customer_id, operations=[op])
        return {'summary': f"Кампания {action.get('campaign_name')} активирована"}

    async def _remove_campaign(self, action: dict, customer_id: str = None) -> dict:
        """
        "Удаление" кампании в Google Ads — это НЕОБРАТИМО, в отличие от
        паузы: устанавливается campaign.status = REMOVED. Физически
        кампания не стирается (сохраняется в истории для отчётности), но
        включить её обратно невозможно — только создать новую с нуля.
        """
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        op.update.resource_name = f"customers/{customer_id}/campaigns/{action['campaign_id']}"
        op.update.status = client.enums.CampaignStatusEnum.REMOVED
        op.update_mask.paths.append("status")
        await asyncio.to_thread(svc.mutate_campaigns, customer_id=customer_id, operations=[op])
        return {'summary': f"Кампания {action.get('campaign_name')} удалена (статус REMOVED, необратимо)"}

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
                await asyncio.to_thread(svc.mutate_campaign_budgets, customer_id=customer_id, operations=[op])
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
        await asyncio.to_thread(svc.mutate_ad_group_ads, customer_id=customer_id, operations=[op])
        return {'summary': f"Объявление поставлено на паузу: {action.get('ad_id')}"}

    async def _update_keyword_final_url(self, action: dict, customer_id: str = None) -> dict:
        """
        Обновляет Final URL конкретного ключевого слова (ad_group_criterion.
        final_urls). Перед мутацией проверяем что ключ принадлежит активной
        (не удалённой) кампании — иначе Google Ads API вернёт INVALID_ARGUMENT.
        """
        if not customer_id: customer_id = self.customer_id
        rn = action.get('resource_name')
        new_url = action.get('new_url')
        if not rn:
            raise ValueError("resource_name не указан в действии update_final_url")
        if not new_url:
            raise ValueError("new_url не указан в действии update_final_url")

        # Проверяем что ключ существует в АКТИВНОЙ (не удалённой) кампании.
        # Google Ads API возвращает INVALID_ARGUMENT если пытаться мутировать
        # критерий из кампании со статусом REMOVED.
        check_query = (
            f"SELECT ad_group_criterion.resource_name, campaign.status, ad_group.status "
            f"FROM ad_group_criterion "
            f"WHERE ad_group_criterion.resource_name = '{rn}' "
            f"AND campaign.status != 'REMOVED' "
            f"AND ad_group.status != 'REMOVED'"
        )
        rows = await self._search(customer_id, check_query)
        if not rows:
            raise ValueError(
                f"Ключ '{action.get('keyword', rn)}' не найден в активных кампаниях. "
                f"Возможно, он принадлежит удалённой кампании (BCHD/2 или другой). "
                f"Пропускаю — изменение не применено."
            )

        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.update.resource_name = rn
        op.update.final_urls.append(new_url)
        op.update_mask.paths.append("final_urls")
        await asyncio.to_thread(svc.mutate_ad_group_criteria, customer_id=customer_id, operations=[op])
        return {'summary': f"Final URL ключа '{action.get('keyword', '')}' обновлён на {new_url}"}


    async def get_ads_for_group(self, ad_group_id: int, customer_id: str = None) -> list:
        """Возвращает все RSA объявления группы с заголовками и описаниями."""
        if not customer_id:
            customer_id = self.customer_id
        query = f"""
            SELECT
                ad_group_ad.resource_name,
                ad_group_ad.ad.id,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.status,
                ad_group.name,
                campaign.name
            FROM ad_group_ad
            WHERE ad_group.id = {ad_group_id}
              AND ad_group_ad.status != 'REMOVED'
              AND campaign.status != 'REMOVED'
        """
        try:
            rows = await self._search(customer_id, query)
            ads = []
            for row in rows:
                rsa = row.ad_group_ad.ad.responsive_search_ad
                headlines = [{"text": h.text, "pinned_field": h.pinned_field.name if hasattr(h.pinned_field, 'name') else None} for h in rsa.headlines] if rsa.headlines else []
                descriptions = [{"text": d.text} for d in rsa.descriptions] if rsa.descriptions else []
                ads.append({
                    "resource_name": row.ad_group_ad.resource_name,
                    "ad_id": row.ad_group_ad.ad.id,
                    "status": row.ad_group_ad.status.name,
                    "ad_group": row.ad_group.name,
                    "campaign": row.campaign.name,
                    "headlines": headlines,
                    "descriptions": descriptions,
                })
            return ads
        except Exception as e:
            log.error(f"get_ads_for_group({ad_group_id}) error: {e}")
            return []

    async def _get_ad_resource_name_by_id(self, ad_id: str, customer_id: str) -> str:
        """Находит resource_name объявления по его ad_id."""
        query = f"""
            SELECT ad_group_ad.resource_name, ad_group_ad.ad.id
            FROM ad_group_ad
            WHERE ad_group_ad.ad.id = {ad_id}
              AND ad_group_ad.status != 'REMOVED'
        """
        rows = await self._search(customer_id, query)
        if not rows:
            raise ValueError(f"Объявление с ad_id={ad_id} не найдено")
        return rows[0].ad_group_ad.resource_name

    async def _find_ad_resource_name_by_group(self, ad_group_name: str, customer_id: str) -> str:
        """Находит resource_name первого активного RSA объявления в группе по её названию."""
        try:
            ga_service = self._get_client().get_service("GoogleAdsService")
            where = f"AND ad_group.name LIKE '%{ad_group_name}%'" if ad_group_name else ""
            query = f"""
                SELECT ad_group_ad.resource_name, ad_group.name
                FROM ad_group_ad
                WHERE ad_group_ad.status != 'REMOVED'
                AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
                AND ad_group.status != 'REMOVED'
                AND campaign.status != 'REMOVED'
                {where}
                LIMIT 1
            """
            response = await asyncio.to_thread(ga_service.search, customer_id=customer_id, query=query)
            for row in response:
                rn = row.ad_group_ad.resource_name
                log.info(f"_find_ad_resource_name_by_group: найдено {rn} в группе {row.ad_group.name}")
                return rn
        except Exception as e:
            log.warning(f"_find_ad_resource_name_by_group error: {e}")
        return ""

    async def _update_ad_headlines(self, action: dict, customer_id: str = None) -> dict:
        """
        Обновляет заголовки RSA объявления.
        Google Ads API не позволяет UPDATE headlines (IMMUTABLE_FIELD).
        Решение: паузируем старое объявление, создаём новое с новыми заголовками.
        """
        if not customer_id:
            customer_id = self.customer_id
        rn = action.get("resource_name")
        new_headlines = action.get("headlines", [])

        # Если resource_name не указан но есть ad_id — ищем сами
        if not rn:
            ad_id = action.get("ad_id") or action.get("ad_group_ad_id")
            if ad_id:
                log.info(f"resource_name не указан, ищем по ad_id={ad_id}")
                rn = await self._get_ad_resource_name_by_id(str(ad_id), customer_id)
            else:
                # Ищем по названию группы объявлений
                ad_group = action.get("ad_group", "")
                log.info(f"ad_id не указан, ищем объявление по группе: '{ad_group}'")
                rn = await self._find_ad_resource_name_by_group(ad_group, customer_id)
                if not rn:
                    raise ValueError(f"Не удалось найти объявление в группе '{ad_group}'. Укажи ad_id явно.")
        if not new_headlines:
            raise ValueError("Список заголовков (headlines) не указан")
        if len(new_headlines) > 15:
            raise ValueError(f"Слишком много заголовков: {len(new_headlines)}. Максимум 15.")
        if len(new_headlines) < 3:
            raise ValueError(f"Слишком мало заголовков: {len(new_headlines)}. Минимум 3.")

        # Получаем текущее объявление
        ad_query = f"""
            SELECT
                ad_group_ad.ad.id,
                ad_group_ad.ad.final_urls,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.ad.responsive_search_ad.path1,
                ad_group_ad.ad.responsive_search_ad.path2,
                ad_group.resource_name
            FROM ad_group_ad
            WHERE ad_group_ad.resource_name = '{rn}'
        """
        rows = await self._search(customer_id, ad_query)
        if not rows:
            raise ValueError(f"Объявление не найдено: {rn}")
        row = rows[0]
        ad = row.ad_group_ad.ad
        ad_group_rn = row.ad_group.resource_name
        final_urls = list(ad.final_urls)
        old_descriptions = list(ad.responsive_search_ad.descriptions)
        path1 = ad.responsive_search_ad.path1
        path2 = ad.responsive_search_ad.path2

        def _do_replace():
            import google.ads.googleads as _gads_pkg
            import os as _os, importlib as _imp
            _base = _os.path.dirname(_gads_pkg.__file__)
            _ver = sorted([d for d in _os.listdir(_base) if d.startswith('v')])[-1]
            AdTextAsset = _imp.import_module(
                f'google.ads.googleads.{_ver}.common.types.ad_asset'
            ).AdTextAsset

            client = self._get_client()
            svc = client.get_service("AdGroupAdService")
            ops = []
            # Паузируем старое объявление
            pause_op = client.get_type("AdGroupAdOperation")
            pause_op.update.resource_name = rn
            pause_op.update.status = client.enums.AdGroupAdStatusEnum.PAUSED
            pause_op.update_mask.paths.append("status")
            ops.append(pause_op)
            # Создаём новое объявление с новыми заголовками
            create_op = client.get_type("AdGroupAdOperation")
            new_ad = create_op.create
            new_ad.ad_group = ad_group_rn
            new_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
            if final_urls:
                new_ad.ad.final_urls.extend(final_urls)
            # Заголовки — через append с AdTextAsset
            for h_text in new_headlines:
                new_ad.ad.responsive_search_ad.headlines.append(
                    AdTextAsset(text=h_text.strip()[:30])
                )
            # Описания — копируем из старого
            for old_desc in old_descriptions:
                new_ad.ad.responsive_search_ad.descriptions.append(
                    AdTextAsset(text=old_desc.text)
                )
            if path1:
                new_ad.ad.responsive_search_ad.path1 = path1
            if path2:
                new_ad.ad.responsive_search_ad.path2 = path2
            ops.append(create_op)
            return svc.mutate_ad_group_ads(customer_id=customer_id, operations=ops)

        try:
            await asyncio.to_thread(_do_replace)
            return {
                "summary": (
                    f"Объявление обновлено: старое поставлено на паузу, "
                    f"создано новое с {len(new_headlines)} заголовками. "
                    f"Первые: {', '.join(new_headlines[:3])}"
                )
            }
        except Exception as e:
            error_str = str(e)
            # Частая причина: resource_name указывает на PAUSED объявление
            # В этом случае агент должен найти ENABLED объявление в группе
            if 'INVALID_ARGUMENT' in error_str or 'invalid argument' in error_str.lower():
                raise ValueError(
                    f"Ошибка создания объявления: {error_str[:300]}. "
                    f"Возможно resource_name указывает на объявление в неверном статусе. "
                    f"Используй resource_name ENABLED объявления из свежих данных ad_performance."
                )
            raise ValueError(f"Ошибка обновления заголовков: {e}")



    async def get_ad_schedule(self, account: str = "ads") -> dict:
        """Возвращает текущее расписание показа объявлений (Ad Schedule)."""
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {"error": f"Customer ID для {account} не настроен", "schedules": []}
        query = """
            SELECT
                campaign_criterion.resource_name,
                campaign_criterion.ad_schedule.day_of_week,
                campaign_criterion.ad_schedule.start_hour,
                campaign_criterion.ad_schedule.start_minute,
                campaign_criterion.ad_schedule.end_hour,
                campaign_criterion.ad_schedule.end_minute,
                campaign.id,
                campaign.name
            FROM campaign_criterion
            WHERE campaign_criterion.type = 'AD_SCHEDULE'
              AND campaign.status != 'REMOVED'
        """
        try:
            rows = await self._search(customer_id, query)
            schedules = []
            for row in rows:
                s = row.campaign_criterion.ad_schedule
                schedules.append({
                    "campaign_id": row.campaign.id,
                    "campaign_name": row.campaign.name,
                    "day": s.day_of_week.name,
                    "start_hour": s.start_hour,
                    "end_hour": s.end_hour,
                    "resource_name": row.campaign_criterion.resource_name,
                })
            # Группируем по кампании
            by_campaign = {}
            for s in schedules:
                cname = s["campaign_name"]
                if cname not in by_campaign:
                    by_campaign[cname] = {"campaign_name": cname, "campaign_id": s["campaign_id"], "days": []}
                by_campaign[cname]["days"].append({"day": s["day"], "start_hour": s["start_hour"], "end_hour": s["end_hour"]})
            return {
                "schedules": schedules,
                "by_campaign": list(by_campaign.values()),
                "total": len(schedules),
                "account": account,
                "note": "Пустой список = реклама показывается круглосуточно (без ограничений)"
            }
        except Exception as e:
            log.error(f"get_ad_schedule error: {e}")
            return {"error": str(e), "schedules": [], "account": account}


    async def set_ad_schedule(self, action: dict, customer_id: str = None) -> dict:
        """
        Устанавливает расписание показа объявлений (Ad Schedule).
        Удаляет все существующие расписания и создаёт новые.
        action должен содержать:
        - campaign_id: ID кампании
        - schedules: список {"day": "MONDAY", "start_hour": 8, "end_hour": 21}
        """
        if not customer_id:
            customer_id = self.customer_id

        campaign_id = action.get("campaign_id")
        schedules = action.get("schedules", [])
        if not campaign_id:
            raise ValueError("campaign_id не указан")
        if not schedules:
            raise ValueError("schedules не указаны")

        def _do_schedule():
            client = self._get_client()
            svc = client.get_service("CampaignCriterionService")
            campaign_rn = f"customers/{customer_id}/campaigns/{campaign_id}"

            # Сначала получаем существующие расписания чтобы удалить
            ga_svc = client.get_service("GoogleAdsService")
            query = f"""
                SELECT campaign_criterion.resource_name
                FROM campaign_criterion
                WHERE campaign.id = {campaign_id}
                  AND campaign_criterion.type = 'AD_SCHEDULE'
                  AND campaign.status != 'REMOVED'
            """
            existing = list(ga_svc.search(customer_id=customer_id, query=query))

            ops = []

            # Удаляем старые расписания
            for row in existing:
                op = client.get_type("CampaignCriterionOperation")
                op.remove = row.campaign_criterion.resource_name
                ops.append(op)

            # Добавляем новые
            day_map = {
                "MONDAY": client.enums.DayOfWeekEnum.MONDAY,
                "TUESDAY": client.enums.DayOfWeekEnum.TUESDAY,
                "WEDNESDAY": client.enums.DayOfWeekEnum.WEDNESDAY,
                "THURSDAY": client.enums.DayOfWeekEnum.THURSDAY,
                "FRIDAY": client.enums.DayOfWeekEnum.FRIDAY,
                "SATURDAY": client.enums.DayOfWeekEnum.SATURDAY,
                "SUNDAY": client.enums.DayOfWeekEnum.SUNDAY,
            }
            minute_enum = client.enums.MinuteOfHourEnum.ZERO

            for sched in schedules:
                op = client.get_type("CampaignCriterionOperation")
                op.create.campaign = campaign_rn
                op.create.ad_schedule.day_of_week = day_map[sched["day"]]
                op.create.ad_schedule.start_hour = sched["start_hour"]
                op.create.ad_schedule.start_minute = minute_enum
                op.create.ad_schedule.end_hour = sched["end_hour"]
                op.create.ad_schedule.end_minute = minute_enum
                ops.append(op)

            if ops:
                svc.mutate_campaign_criteria(customer_id=customer_id, operations=ops)

            return len([s for s in schedules])

        count = await asyncio.to_thread(_do_schedule)
        days = list({s["day"] for s in schedules})
        return {"summary": f"Ad Schedule установлен: {len(days)} дней, {schedules[0]['start_hour']}:00-{schedules[0]['end_hour']}:00"}


    async def _dispute_lsa_lead(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id:
            customer_id = self.lsa_customer_id
        client = self._get_client()
        lead_id = action.get('lead_id')
        reason_text = (action.get('reason') or action.get('reasoning') or
                        'Service category not offered by our business')[:500]

        def _do():
            svc = client.get_service("LocalServicesLeadService")
            request = client.get_type("ProvideLeadFeedbackRequest")
            request.resource_name = f"customers/{customer_id}/localServicesLeads/{lead_id}"
            request.survey_answer = client.enums.LocalServicesLeadSurveyAnswerEnum.DISSATISFIED
            request.survey_dissatisfied.survey_dissatisfied_reason = (
                client.enums.LocalServicesLeadSurveyDissatisfiedReasonEnum.OTHER_DISSATISFIED_REASON
            )
            request.survey_dissatisfied.other_reason_comment = reason_text
            return svc.provide_lead_feedback(request=request)

        response = await asyncio.to_thread(_do)
        decision = response.credit_issuance_decision.name if hasattr(response.credit_issuance_decision, 'name') else str(response.credit_issuance_decision)
        return {'summary': f"Фидбэк отправлен по лиду {lead_id}. Решение Google по кредиту: {decision}"}
