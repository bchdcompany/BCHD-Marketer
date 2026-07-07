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

    async def get_keywords_analysis(self, account: str = "ads") -> dict:
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id
        if not customer_id:
            return {'error': f'Customer ID для {account} не настроен', 'keywords': []}

        if self._is_lsa(customer_id):
            return {
                'error': 'LSA не использует ключевые слова — Google сам определяет аудиторию',
                'keywords': [], 'account': account,
            }

        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        query = f"""
            SELECT
                ad_group_criterion.resource_name,
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.status,
                ad_group_criterion.quality_info.quality_score,
                ad_group_criterion.final_urls,
                campaign.name,
                ad_group.id,
                ad_group.name,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr,
                metrics.average_cpc,
                metrics.cost_micros,
                metrics.conversions
            FROM keyword_view
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND ad_group_criterion.status != 'REMOVED'
            ORDER BY metrics.cost_micros DESC
            LIMIT 200
        """

        try:
            rows = await self._search(customer_id, query)
            keywords = []
            for row in rows:
                keywords.append({
                    'resource_name': row.ad_group_criterion.resource_name,
                    'keyword': row.ad_group_criterion.keyword.text,
                    'match_type': row.ad_group_criterion.keyword.match_type.name,
                    'status': row.ad_group_criterion.status.name,
                    'campaign': row.campaign.name,
                    'ad_group': row.ad_group.name,
                    'ad_group_id': row.ad_group.id,
                    'final_urls': list(row.ad_group_criterion.final_urls) if row.ad_group_criterion.final_urls else [],
                    'impressions': row.metrics.impressions,
                    'clicks': row.metrics.clicks,
                    'ctr': round(row.metrics.ctr * 100, 2),
                    'cpc': round(row.metrics.average_cpc / 1_000_000, 2),
                    'spend': round(row.metrics.cost_micros / 1_000_000, 2),
                    'conversions': round(row.metrics.conversions, 1),
                    'quality_score': row.ad_group_criterion.quality_info.quality_score,
                })
            return {'keywords': keywords, 'total': len(keywords), 'date_from': date_from, 'date_to': date_to, 'account': account}
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
                    'url': None,
                    'error': 'URL не найден (нет final_urls ни у ключа, ни у объявлений его группы)',
                })
                continue

            page_data = await self._fetch_page_snippet(url)
            page_data['keyword'] = kw.get('keyword')
            page_data['spend'] = kw.get('spend')
            pages.append(page_data)
        return {'pages': pages}

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
            'budget_change': self._change_budget,
            'update_bid': self._update_bid,
            'pause_campaign': self._pause_campaign,
            'enable_campaign': self._enable_campaign,
            'seasonal_adjustments': self._apply_seasonal_adjustments,
            'pause_ad': self._pause_ad,
            'dispute_lsa_lead': self._dispute_lsa_lead,
        }
        handler = handlers.get(action_type)
        if not handler:
            raise ValueError(f"Неизвестный тип действия: {action_type}")
        return await handler(action, customer_id)

    async def verify_action(self, action: dict) -> dict:
        action_type = action.get('type')
        account = action.get('account', 'ads')
        customer_id = self.lsa_customer_id if account == "lsa" else self.customer_id

        try:
            if action_type in ('pause_campaign', 'enable_campaign'):
                expected = 'PAUSED' if action_type == 'pause_campaign' else 'ENABLED'
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

            elif action_type == 'dispute_lsa_lead':
                lead_id = action.get('lead_id')
                query = f"SELECT local_services_lead.lead_feedback_submitted FROM local_services_lead WHERE local_services_lead.id = {lead_id}"
                rows = await self._search(customer_id, query)
                submitted = rows[0].local_services_lead.lead_feedback_submitted if rows else None
                return {'verified': submitted is True, 'lead_feedback_submitted': submitted}

            else:
                return {'verified': None, 'note': f'Перепроверка не поддерживается для типа {action_type}'}

        except Exception as e:
            log.error(f"verify_action({action_type}) error: {e}")
            return {'verified': False, 'error': str(e)}

    async def _pause_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        self._assert_not_lsa_for_keywords(customer_id, 'pause_keywords')
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        for kw in action.get('keywords', []):
            op = client.get_type("AdGroupCriterionOperation")
            op.update.resource_name = kw['resource_name']
            op.update.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
            op.update_mask.paths.append("status")
            ops.append(op)
        if ops: await asyncio.to_thread(svc.mutate_ad_group_criteria, customer_id=customer_id, operations=ops)
        return {'summary': f"Поставлено на паузу: {len(ops)} ключей"}

    async def _enable_keywords(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        self._assert_not_lsa_for_keywords(customer_id, 'enable_keywords')
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        for kw in action.get('keywords', []):
            op = client.get_type("AdGroupCriterionOperation")
            op.update.resource_name = kw['resource_name']
            op.update.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            op.update_mask.paths.append("status")
            ops.append(op)
        if ops: await asyncio.to_thread(svc.mutate_ad_group_criteria, customer_id=customer_id, operations=ops)
        return {'summary': f"Активировано ключей: {len(ops)}"}

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
        for neg in action.get('negatives', []):
            for cid in enabled_ids:
                op = client.get_type("CampaignCriterionOperation")
                op.create.campaign = f"customers/{customer_id}/campaigns/{cid}"
                op.create.negative = True
                op.create.keyword.text = neg['term']
                op.create.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
                ops.append(op)
        if ops: await asyncio.to_thread(svc.mutate_campaign_criteria, customer_id=customer_id, operations=ops)
        # Сохраняем в само действие, какие кампании реально были целью —
        # это позволяет verify_action() позже РЕАЛЬНО проверить, что минус-
        # слова применились, а не просто полагаться на отсутствие ошибки API.
        action['_target_campaign_ids'] = enabled_ids
        summary = f"Добавлено минус-слов: {len(action.get('negatives', []))} (в {len(enabled_ids)} кампаний)"
        if skipped_lsa_campaigns:
            summary += f". Пропущено кампаний Local Services (не поддерживают минус-слова): {len(skipped_lsa_campaigns)}"
        return {'summary': summary}

    async def _change_budget(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("CampaignBudgetService")
        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name = f"customers/{customer_id}/campaignBudgets/{action.get('budget_id')}"
        op.update.amount_micros = int(action.get('proposed_budget', 0) * 1_000_000)
        op.update_mask.paths.append("amount_micros")
        await asyncio.to_thread(svc.mutate_campaign_budgets, customer_id=customer_id, operations=[op])
        return {'summary': f"Бюджет изменён на ${action.get('proposed_budget'):.2f}/день"}

    async def _update_bid(self, action: dict, customer_id: str = None) -> dict:
        if not customer_id: customer_id = self.customer_id
        client = self._get_client()
        svc = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.update.resource_name = action.get('resource_name')
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
