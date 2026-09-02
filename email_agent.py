"""
email_agent.py — BCHD Email Campaign Generator
"""
import os
import re
import json
import logging
import anthropic
import requests
from datetime import datetime
from pathlib import Path
logger = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
IDEOGRAM_API_KEY  = os.environ.get("IDEOGRAM_API_KEY")
SENDGRID_API_KEY  = os.environ.get("SENDGRID_API_KEY")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
OWNER_CHAT_ID     = os.environ.get("OWNER_CHAT_ID")
SENDGRID_FROM_EMAIL = "you@bchdcompany.com"
SENDGRID_FROM_NAME  = "BCHD Appliance Repair"
WORKIZ_BOOKING_URL  = "https://www.bchdcompany.com/#booking-form"
LOGO_URL = "http://cdn.mcauto-images-production.sendgrid.net/6e6226165269f28e/49a5a9a0-80f2-432d-9af0-972544bd432d/400x400.png"
_pending_campaign = None

def ask_campaign_topic(bot):
    text = (
        "📧 *Время недельной рассылки!*\n\n"
        "Какую тему выбираем на эту неделю?\n\n"
        "1️⃣ Сезонная акция (скидка на ремонт)\n"
        "2️⃣ Профилактика техники (холодильник, стиралка...)\n"
        "3️⃣ Праздник / поздравление клиентов\n"
        "4️⃣ Своя идея — просто напиши\n\n"
        "_Можешь написать цифру или своими словами_"
    )
    bot.send_message(chat_id=OWNER_CHAT_ID, text=text, parse_mode="Markdown")

def handle_campaign_request(user_text: str, bot) -> None:
    bot.send_message(chat_id=OWNER_CHAT_ID, text="⏳ Генерирую баннер, подожди 30–60 секунд...")
    try:
        campaign_data = _generate_campaign_content(user_text)
        image_url = _generate_ideogram_image(campaign_data["ideogram_prompt"])
        html = _build_html(campaign_data, image_url)
        global _pending_campaign
        _pending_campaign = {
            "html": html,
            "subject": campaign_data["email_subject"],
            "preview_text": campaign_data["preview_text"],
            "generated_at": datetime.now().isoformat()
        }
        preview_msg = (
            f"✅ *Баннер готов!*\n\n"
            f"📌 *Тема:* {campaign_data['campaign_title']}\n"
            f"📧 *Subject:* {campaign_data['email_subject']}\n"
            f"💬 *Оффер:* {campaign_data['offer_text']}\n\n"
            f"*Тезисы:*\n"
            + "\n".join([f"• {t['title']}: {t['text']}" for t in campaign_data["cards"]])
            + f"\n\n📨 *Получат:* ~958 клиентов\n\n"
            f"Напиши *отправляй* или что исправить"
        )
        bot.send_message(chat_id=OWNER_CHAT_ID, text=preview_msg, parse_mode="Markdown")
        html_bytes = html.encode("utf-8")
        bot.send_document(
            chat_id=OWNER_CHAT_ID,
            document=("campaign_preview.html", html_bytes, "text/html"),
            caption="👆 Открой в браузере для просмотра"
        )
    except Exception as e:
        logger.error(f"Campaign generation error: {e}")
        bot.send_message(chat_id=OWNER_CHAT_ID, text=f"❌ Ошибка генерации: {e}")

def confirm_and_send(bot) -> None:
    global _pending_campaign
    if not _pending_campaign:
        bot.send_message(chat_id=OWNER_CHAT_ID, text="⚠️ Нет готового баннера.")
        return
    bot.send_message(chat_id=OWNER_CHAT_ID, text="📤 Отправляю рассылку...")
    try:
        sent_count = _send_via_sendgrid(
            html_content=_pending_campaign["html"],
            subject=_pending_campaign["subject"],
            preview_text=_pending_campaign["preview_text"]
        )
        _pending_campaign = None
        bot.send_message(chat_id=OWNER_CHAT_ID, text=f"✅ Рассылка отправлена! Доставлено: {sent_count} писем", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"SendGrid error: {e}")
        bot.send_message(chat_id=OWNER_CHAT_ID, text=f"❌ Ошибка отправки: {e}")

def has_pending_campaign() -> bool:
    return _pending_campaign is not None

CONTENT_PROMPT = """You are a professional email marketing specialist for BCHD Appliance Repair & HVAC, 
a licensed appliance repair company in NYC (Brooklyn, Queens, Manhattan).
The user wants to send an email campaign. Their request: "{user_input}"
Generate email campaign content. Respond ONLY with valid JSON, no markdown, no extra text.
JSON structure:
{{
  "campaign_title": "Short campaign name (3-5 words)",
  "email_subject": "Email subject line (max 50 chars, compelling)",
  "preview_text": "Preview text shown in inbox (max 90 chars)",
  "color_mood": "One of: red_urgent / blue_cold / gold_festive / green_fresh / dark_professional",
  "hero_eyebrow": "Small label above title with emoji (e.g. '❄️ Preventive Maintenance')",
  "hero_title": "Main headline (2-4 words, max 2 lines)",
  "offer_text": "Discount or value proposition (e.g. '30% OFF Your Repair')",
  "hero_subtitle": "1-2 sentences explaining the offer (max 150 chars)",
  "hero_badges": ["badge1", "badge2", "badge3"],
  "section_title": "Section heading with placeholder [HIGHLIGHT] for colored word",
  "cards": [
    {{"icon": "thermometer|lightning|dollar|shield|heart|clock|home|star", "title": "Card title", "text": "2-3 sentence explanation"}},
    {{"icon": "thermometer|lightning|dollar|shield|heart|clock|home|star", "title": "Card title", "text": "2-3 sentence explanation"}},
    {{"icon": "thermometer|lightning|dollar|shield|heart|clock|home|star", "title": "Card title", "text": "2-3 sentence explanation"}},
    {{"icon": "thermometer|lightning|dollar|shield|heart|clock|home|star", "title": "Card title", "text": "2-3 sentence explanation"}}
  ],
  "cta_headline": "CTA block headline (punchy, max 8 words)",
  "cta_subtext": "Supporting line under headline (location, dates, conditions)",
  "cta_button": "Button text (max 5 words, includes offer)",
  "ideogram_prompt": "Ideogram prompt for email banner. RULES: (1) Focus on RESULT and ATMOSPHERE, never the process or equipment. (2) Use interior/lifestyle scenes: bright modern home, kitchen, living room. (3) NEVER mention: water, faucet, pipe, drain, filter, wrench, tools, repair process. (4) Always end with: photorealistic, commercial photography style, soft natural lighting, no text, no logos, high quality. (5) For AC/cooling: cozy bright room, summer light. (6) For water: glass of water on countertop, clean kitchen. (7) For appliance repair: modern kitchen interior, professional atmosphere. Max 300 chars.",
  "partner_name": "Partner company name if recommendation email, else empty string",
  "partner_phone": "Partner phone as XXX-XXX-XXXX, else empty string",
  "partner_site": "Partner website domain only e.g. elite-aquacare.com, else empty string",
  "partner_contact": "Partner contact person first name if mentioned, else empty string"
}}
Rules:
- All content in English
- Tone: energetic and professional, never pushy
- For holidays/celebrations: warm and sincere, focus on gratitude not sales
- For repair campaigns: urgency + reassurance
- For partner recommendations: emphasize trust, quality, exclusive offer
- ideogram_prompt must be UNIQUE to each campaign — no generic prompts
- ideogram_prompt must match the color_mood and campaign theme exactly
- If user mentions specific colors, visuals, or style — use them in ideogram_prompt
"""

def _generate_campaign_content(user_input: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        messages=[{"role": "user", "content": CONTENT_PROMPT.format(user_input=user_input)}]
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Пробуем починить незакрытый JSON
        if not raw.endswith("}"):
            raw = raw + "}"
        try:
            data = json.loads(raw)
        except Exception:
            # Минимальный fallback
            data = {"email_subject": "BCHD Recommendation", "preview_text": "", "hero_headline": "Special Offer", "hero_subline": "", "cards": [], "cta_headline": "Contact Us", "cta_subtext": "", "cta_button": "Learn More", "color_mood": "blue_trust", "ideogram_prompt": "Professional blue water purification banner", "partner": {}}
    # Убеждаемся что partner есть
    if "partner" not in data:
        data["partner"] = {}
    return data

def _generate_ideogram_image(prompt: str) -> str:
    if not IDEOGRAM_API_KEY:
        return ""
    resp = requests.post(
        "https://api.ideogram.ai/generate",
        headers={"Api-Key": IDEOGRAM_API_KEY, "Content-Type": "application/json"},
        json={"image_request": {"prompt": prompt, "aspect_ratio": "ASPECT_16_9", "model": "V_2", "magic_prompt_option": "AUTO", "style_type": "REALISTIC"}},
        timeout=60
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["url"]

COLOR_SCHEMES = {
    "red_urgent": {"hero_bg": "#1a0000", "accent": "#cc1f1f", "accent_light": "#ff5c5c", "card_border": "#cc1f1f", "card_title_color": "#cc1f1f", "cta_bg": "#cc1f1f", "cta_btn_bg": "#ffffff", "cta_btn_color": "#cc1f1f", "eyebrow_bg": "#cc1f1f", "eyebrow_color": "#ffffff", "badge_check": "#ff5c5c"},
    "blue_cold": {"hero_bg": "#0a1628", "accent": "#1a6bcc", "accent_light": "#5bc8ff", "card_border": "#cc1f1f", "card_title_color": "#cc1f1f", "cta_bg": "#cc1f1f", "cta_btn_bg": "#ffffff", "cta_btn_color": "#cc1f1f", "eyebrow_bg": "#1a6bcc", "eyebrow_color": "#ffffff", "badge_check": "#5bc8ff"},
    "gold_festive": {"hero_bg": "#0d1b2e", "accent": "#c8a84b", "accent_light": "#e8c96a", "card_border": "#c8a84b", "card_title_color": "#8a6a1a", "cta_bg": "#0d1b2e", "cta_btn_bg": "#c8a84b", "cta_btn_color": "#0d1b2e", "eyebrow_bg": "#c8a84b", "eyebrow_color": "#0d1b2e", "badge_check": "#e8c96a"},
    "green_fresh": {"hero_bg": "#0a1f0a", "accent": "#2e7d32", "accent_light": "#66bb6a", "card_border": "#2e7d32", "card_title_color": "#2e7d32", "cta_bg": "#2e7d32", "cta_btn_bg": "#ffffff", "cta_btn_color": "#2e7d32", "eyebrow_bg": "#2e7d32", "eyebrow_color": "#ffffff", "badge_check": "#66bb6a"},
    "dark_professional": {"hero_bg": "#111111", "accent": "#444444", "accent_light": "#aaaaaa", "card_border": "#cc1f1f", "card_title_color": "#cc1f1f", "cta_bg": "#1a1a1a", "cta_btn_bg": "#cc1f1f", "cta_btn_color": "#ffffff", "eyebrow_bg": "#333333", "eyebrow_color": "#ffffff", "badge_check": "#aaaaaa"},
}

ICONS = {
    "thermometer": '<path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/>',
    "lightning":   '<polyline points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "dollar":      '<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    "shield":      '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "heart":       '<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>',
    "clock":       '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "home":        '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
    "star":        '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
}

def _svg_icon(name: str, color: str) -> str:
    path = ICONS.get(name, ICONS["star"])
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px;flex-shrink:0">{path}</svg>')

def _build_html(data: dict, image_url: str) -> str:
    c = COLOR_SCHEMES.get(data.get("color_mood", "red_urgent"), COLOR_SCHEMES["red_urgent"])
    # Partner data
    p_name    = data.get("partner_name", "").strip()
    p_phone   = data.get("partner_phone", "").strip()
    p_digits  = p_phone.replace("-","").replace(" ","").replace("(","").replace(")","")
    p_site    = data.get("partner_site", "").strip()
    p_contact = data.get("partner_contact", "").strip()
    has_partner = bool(p_name)
    # CTA target
    if has_partner and p_site:
        cta_href = "https://" + p_site
    elif has_partner and p_phone:
        cta_href = "tel:" + p_digits
    else:
        cta_href = WORKIZ_BOOKING_URL
    # Partner contact block HTML
    if has_partner:
        partner_block = (
            '<div style="margin-bottom:24px;padding:16px;background:rgba(255,255,255,0.12);border-radius:10px;">'
            + ('<div style="font-size:30px;font-weight:900;color:#fff;margin-bottom:6px;">&#128222; <a href="tel:' + p_digits + '" style="color:#fff;text-decoration:none;">' + p_phone + '</a></div>' if p_phone else '')
            + ('<div style="font-size:18px;font-weight:700;color:rgba(255,255,255,0.9);margin-bottom:4px;">Contact: ' + (p_contact or p_name) + '</div>' if (p_contact or p_name) else '')
            + ('<div style="font-size:17px;color:rgba(255,255,255,0.85);">&#127760; <a href="https://' + p_site + '" style="color:#fff;text-decoration:none;">' + p_site + '</a></div>' if p_site else '')
            + '</div>'
        )
        bchd_note = '<p style="font-size:12px;color:rgba(255,255,255,0.5);margin-top:8px;">Mention BCHD when booking to receive your discount</p>'
    else:
        partner_block = '<p style="font-size:14px;color:rgba(255,255,255,0.8);margin-bottom:24px;">' + data.get("cta_subtext","") + '</p>'
        bchd_note = ""
    section_title_html = data["section_title"].replace("[HIGHLIGHT]", f'<span style="color:{c["accent"]}">') + ("</span>" if "[HIGHLIGHT]" in data["section_title"] else "")
    cards_html = ""
    for card in data["cards"]:
        icon_svg = _svg_icon(card["icon"], c["card_title_color"])
        cards_html += f"""
        <div style="border:1px solid #e8e8e8;border-left:4px solid {c['card_border']};border-radius:6px;padding:16px;background:#fafafa;">
          <div style="font-size:13px;font-weight:700;color:{c['card_title_color']};text-transform:uppercase;letter-spacing:0.5px;margin-bottom:7px;display:flex;align-items:center;gap:7px;">
            {icon_svg}{card['title']}
          </div>
          <p style="font-size:13px;color:#444;line-height:1.55;">{card['text']}</p>
        </div>"""
    badges_html = "".join([
        f'<span style="font-size:13px;color:#cccccc;display:flex;align-items:center;gap:5px;"><span style="color:{c["badge_check"]};font-weight:700;">✓</span>{b}</span>'
        for b in data.get("hero_badges", [])
    ])
    hero_img = f'<img src="{image_url}" alt="" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;opacity:0.35;display:block;" />' if image_url else ''
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{data['email_subject']}</title>
</head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f0f0f0;">
<div style="max-width:620px;margin:0 auto;background:#ffffff;">
  <!-- HEADER -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#111111;padding:16px 24px;">
    <tr>
      <td>
        <a href="https://www.bchdcompany.com" target="_blank" style="text-decoration:none;display:flex;align-items:center;gap:12px;">
          <img src="{LOGO_URL}" alt="BCHD" style="height:70px;width:70px;display:block;object-fit:contain;" />
          <div>
            <div style="font-size:22px;font-weight:900;color:#ffffff;letter-spacing:1px;line-height:1.1;">BCHD</div>
            <div style="font-size:11px;color:#aaaaaa;line-height:1.2;">Appliance Repair Service &amp; HVAC</div>
            <div style="font-size:10px;color:#cc1f1f;font-style:italic;">Fast Quality Guaranteed</div>
          </div>
        </a>
      </td>
      <td align="right" style="white-space:nowrap;">
        <a href="tel:9179354553" style="text-decoration:none;background:#cc1f1f;color:#fff;font-size:15px;font-weight:700;padding:10px 16px;border-radius:6px;display:inline-block;">
          📞 (917) 935-4553
        </a>
      </td>
    </tr>
  </table>
  <!-- HERO -->
  <div style="position:relative;background:{c['hero_bg']};min-height:280px;overflow:hidden;">
    {hero_img}
    <div style="position:relative;padding:40px 36px;z-index:2;">
      <span style="display:inline-block;background:{c['eyebrow_bg']};color:{c['eyebrow_color']};font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;padding:5px 12px;border-radius:3px;margin-bottom:16px;">
        {data['hero_eyebrow']}
      </span>
      <div style="font-size:44px;font-weight:900;color:#ffffff;line-height:1.05;margin-bottom:10px;text-shadow:0 2px 8px rgba(0,0,0,0.5);">
        {data['hero_title'].replace(chr(10), '<br>')}
      </div>
      <div style="font-size:28px;font-weight:800;color:{c['accent_light']};margin-bottom:16px;">
        {data['offer_text']}
      </div>
      <p style="font-size:15px;color:#dddddd;line-height:1.5;max-width:380px;margin-bottom:20px;">
        {data['hero_subtitle']}
      </p>
      <div style="display:flex;gap:18px;flex-wrap:wrap;">{badges_html}</div>
    </div>
  </div>
  <!-- WHY SECTION -->
  <div style="padding:36px 28px;background:#fff;">
    <div style="font-size:22px;font-weight:800;color:#1a1a1a;margin-bottom:22px;">{section_title_html}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">{cards_html}</div>
  </div>
  <!-- CTA -->
  <div style="background:{c['cta_bg']};padding:36px 28px;text-align:center;">
    <h2 style="font-size:26px;font-weight:900;color:#fff;margin-bottom:10px;">{data['cta_headline']}</h2>
    {partner_block}
    <a href="{cta_href}" target="_blank"
       style="display:inline-block;background:{c['cta_btn_bg']};color:{c['cta_btn_color']};font-size:18px;font-weight:800;padding:16px 40px;border-radius:6px;text-decoration:none;margin-bottom:16px;">
      {data['cta_button']} →
    </a>
    {bchd_note}
  </div>
  <!-- SERVICES -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#1a1a1a;">
    <tr>
      <td align="center" style="padding:14px;">
        <a href="https://www.bchdcompany.com/refrigerator" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:0 10px;">❄️ Refrigerator</a>
        <a href="https://www.bchdcompany.com/washer" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:0 10px;">🫧 Washer</a>
        <a href="https://www.bchdcompany.com/dryer" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:0 10px;">🌀 Dryer</a>
        <a href="https://www.bchdcompany.com/dishwasher" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:0 10px;">🍽️ Dishwasher</a>
        <a href="https://www.bchdcompany.com/oven" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:0 10px;">🔥 Oven</a>
        <a href="https://www.bchdcompany.com/hvac" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:0 10px;">🌬️ AC &amp; HVAC</a>
      </td>
    </tr>
  </table>
  <!-- FOOTER -->
  <div style="background:#0f0f0f;padding:18px 28px;text-align:center;">
    <p style="font-size:11px;color:#555;line-height:1.7;">
      BCHD Appliance Repair &amp; HVAC · 501A Surf Ave, Brooklyn, NY 11224<br>
      <a href="https://www.bchdcompany.com" style="color:#777;text-decoration:none;">bchdcompany.com</a> ·
      <a href="mailto:you@bchdcompany.com" style="color:#777;text-decoration:none;">you@bchdcompany.com</a><br>
      <a href="#" style="color:#777;text-decoration:none;">Unsubscribe</a>
    </p>
  </div>
</div>
</body>
</html>"""

def _get_client_emails(unsent_only: bool = False) -> list:
    """Получает список email клиентов из Postgres."""
    try:
        import asyncpg, asyncio
        DATABASE_URL = os.environ.get("DATABASE_URL", "")
        if DATABASE_URL:
            async def _fetch():
                pool = await asyncpg.create_pool(DATABASE_URL)
                if unsent_only:
                    rows = await pool.fetch(
                        "SELECT email FROM email_clients WHERE unsubscribed = FALSE AND last_sent_at IS NULL ORDER BY id"
                    )
                else:
                    rows = await pool.fetch(
                        "SELECT email FROM email_clients WHERE unsubscribed = FALSE ORDER BY id"
                    )
                await pool.close()
                return [r["email"] for r in rows]
            loop = asyncio.new_event_loop()
            emails = loop.run_until_complete(_fetch())
            loop.close()
            logger.info(f"Клиентов{'(не получали)' if unsent_only else ''}: {len(emails)}")
            return emails
    except Exception as e:
        logger.warning(f"Postgres недоступен: {e}")
    clients_file = Path("clients_clean.txt")
    if clients_file.exists():
        return [line.strip() for line in clients_file.read_text().splitlines() if "@" in line]
    return []

def _mark_emails_sent(emails: list) -> None:
    """Помечает клиентов как получивших рассылку."""
    try:
        import asyncpg, asyncio
        DATABASE_URL = os.environ.get("DATABASE_URL", "")
        if not DATABASE_URL:
            return
        async def _update():
            pool = await asyncpg.create_pool(DATABASE_URL)
            await pool.execute(
                "UPDATE email_clients SET last_sent_at = NOW() WHERE email = ANY($1::text[])",
                emails
            )
            await pool.close()
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_update())
        loop.close()
    except Exception as e:
        logger.warning(f"Ошибка пометки отправленных: {e}")

def _send_via_sendgrid(html_content: str, subject: str, preview_text: str) -> int:
    """Отправляет только тем кто ещё не получал рассылку."""
    # Сначала всегда добавляем владельца
    all_emails = _get_client_emails(unsent_only=False)
    unsent = _get_client_emails(unsent_only=True)

    # Владелец всегда первым если не в unsent
    owner = "you@bchdcompany.com"
    if owner not in unsent and owner in all_emails:
        unsent = [owner] + [e for e in unsent if e != owner]

    if not unsent:
        # Все уже получили — сбрасываем и начинаем заново
        logger.info("Все клиенты получили рассылку — сбрасываем историю")
        try:
            import asyncpg, asyncio
            DATABASE_URL = os.environ.get("DATABASE_URL", "")
            async def _reset():
                pool = await asyncpg.create_pool(DATABASE_URL)
                await pool.execute("UPDATE email_clients SET last_sent_at = NULL")
                await pool.close()
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_reset())
            loop.close()
        except Exception as e:
            logger.warning(f"Ошибка сброса: {e}")
        unsent = all_emails

    if not unsent:
        raise RuntimeError("Список клиентов пуст")

    headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"}
    sent = 0
    failed = 0
    sent_emails = []

    for email in unsent:
        payload = {
            "personalizations": [{"to": [{"email": email}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": SENDGRID_FROM_NAME},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_content}],
            "tracking_settings": {"click_tracking": {"enable": True}, "open_tracking": {"enable": True}}
        }
        try:
            resp = requests.post("https://api.sendgrid.com/v3/mail/send", headers=headers, json=payload, timeout=30)
            if resp.status_code == 202:
                sent += 1
                sent_emails.append(email)
            else:
                failed += 1
                logger.error(f"SendGrid {email}: {resp.status_code}")
        except Exception as e:
            failed += 1
            logger.error(f"SendGrid error {email}: {e}")

    # Помечаем отправленных
    if sent_emails:
        _mark_emails_sent(sent_emails)

    remaining = len(unsent) - sent
    logger.info(f"Рассылка: отправлено {sent}, ошибок {failed}, осталось {remaining}")
    return sent
