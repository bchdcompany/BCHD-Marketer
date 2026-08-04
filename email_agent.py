"""
email_agent.py — BCHD Email Campaign Generator
Место в проекте: добавить в корень bchd-agent рядом с agent.py

Логика:
1. Scheduler вызывает ask_campaign_topic() каждое воскресенье в 9:00
2. Пользователь отвечает текстом — любым, агент понимает
3. generate_campaign(user_input) → генерирует HTML баннер
4. Присылает превью-описание + файл в Telegram
5. Пользователь отвечает ✅ → send_campaign() отправляет через SendGrid
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
OWNER_CHAT_ID     = os.environ.get("OWNER_CHAT_ID")  # Telegram chat ID Чингиса

SENDGRID_FROM_EMAIL = "you@bchdcompany.com"
SENDGRID_FROM_NAME  = "BCHD Appliance Repair"
WORKIZ_BOOKING_URL  = "https://www.bchdcompany.com/#booking-form"

# ── Состояние ожидания (простой in-memory флаг, для Railway достаточно) ──
_pending_campaign: dict | None = None  # хранит сгенерированный HTML до подтверждения


# ══════════════════════════════════════════════════════════
# 1. ВОСКРЕСНЫЙ ВОПРОС
# ══════════════════════════════════════════════════════════

def ask_campaign_topic(bot):
    """Вызывается scheduler'ом каждое воскресенье в 9:00 NY."""
    text = (
        "📧 *Время недельной рассылки!*\n\n"
        "Какую тему выбираем на эту неделю?\n\n"
        "1️⃣ Сезонная акция (скидка на ремонт)\n"
        "2️⃣ Профилактика техники (холодильник, стиралка...)\n"
        "3️⃣ Праздник / поздравление клиентов\n"
        "4️⃣ Своя идея — просто напиши\n\n"
        "_Можешь написать цифру или своими словами_"
    )
    bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text=text,
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════
# 2. ОБРАБОТКА ОТВЕТА ПОЛЬЗОВАТЕЛЯ
# ══════════════════════════════════════════════════════════

def handle_campaign_request(user_text: str, bot) -> None:
    """
    Вызывается из main.py когда пользователь отвечает на воскресный вопрос
    ИЛИ сам пишет что-то про рассылку в любой момент.
    Детектировать можно по ключевым словам: рассылка, баннер, поздравим, акция, письмо клиентам
    """
    bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text="⏳ Генерирую баннер, подожди 30–60 секунд..."
    )

    try:
        # Шаг 1: Claude генерирует контент
        campaign_data = _generate_campaign_content(user_text)

        # Шаг 2: Ideogram генерирует фоновое изображение
        image_url = _generate_ideogram_image(campaign_data["ideogram_prompt"])

        # Шаг 3: Собираем HTML
        html = _build_html(campaign_data, image_url)

        # Шаг 4: Сохраняем в pending
        global _pending_campaign
        _pending_campaign = {
            "html": html,
            "subject": campaign_data["email_subject"],
            "preview_text": campaign_data["preview_text"],
            "generated_at": datetime.now().isoformat()
        }

        # Шаг 5: Отправляем превью в Telegram
        preview_msg = (
            f"✅ *Баннер готов!*\n\n"
            f"📌 *Тема:* {campaign_data['campaign_title']}\n"
            f"📧 *Subject:* {campaign_data['email_subject']}\n"
            f"🎨 *Стиль:* {campaign_data['color_mood']}\n"
            f"💬 *Оффер:* {campaign_data['offer_text']}\n\n"
            f"*Тезисы:*\n"
            + "\n".join([f"• {t['title']}: {t['text']}" for t in campaign_data["cards"]])
            + f"\n\n📨 *Получат:* ~957 клиентов\n\n"
            f"Отправляем? Напиши ✅ или что исправить"
        )
        bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=preview_msg,
            parse_mode="Markdown"
        )

        # Отправляем HTML файл как документ
        html_bytes = html.encode("utf-8")
        bot.send_document(
            chat_id=OWNER_CHAT_ID,
            document=("campaign_preview.html", html_bytes, "text/html"),
            caption="👆 Открой в браузере для просмотра"
        )

    except Exception as e:
        logger.error(f"Campaign generation error: {e}")
        bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"❌ Ошибка генерации: {e}\nПопробуй ещё раз или напиши тему по-другому."
        )


# ══════════════════════════════════════════════════════════
# 3. ПОДТВЕРЖДЕНИЕ И ОТПРАВКА
# ══════════════════════════════════════════════════════════

def confirm_and_send(bot) -> None:
    """Вызывается когда пользователь написал ✅"""
    global _pending_campaign
    if not _pending_campaign:
        bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text="⚠️ Нет готового баннера. Сначала попроси создать рассылку."
        )
        return

    bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text="📤 Отправляю рассылку по 957 клиентам..."
    )

    try:
        sent_count = _send_via_sendgrid(
            html_content=_pending_campaign["html"],
            subject=_pending_campaign["subject"],
            preview_text=_pending_campaign["preview_text"]
        )
        _pending_campaign = None
        bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"✅ *Рассылка отправлена!*\nДоставлено: {sent_count} писем",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"SendGrid error: {e}")
        bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"❌ Ошибка отправки: {e}"
        )


def has_pending_campaign() -> bool:
    return _pending_campaign is not None


# ══════════════════════════════════════════════════════════
# 4. ГЕНЕРАЦИЯ КОНТЕНТА ЧЕРЕЗ CLAUDE
# ══════════════════════════════════════════════════════════

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
  "ideogram_prompt": "Detailed Ideogram image generation prompt for a professional email banner background. Describe: mood, colors, objects, lighting, style. NO text in image. Photorealistic or cinematic. Max 200 chars."
}}

Rules:
- All content in English
- Tone: energetic and professional, never pushy
- For holidays/celebrations: warm and sincere, focus on gratitude not sales
- For repair campaigns: urgency + reassurance
- ideogram_prompt must match the color_mood and campaign theme
"""

def _generate_campaign_content(user_input: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": CONTENT_PROMPT.format(user_input=user_input)
        }]
    )
    raw = response.content[0].text.strip()
    # Убираем markdown-обёртку если вдруг есть
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ══════════════════════════════════════════════════════════
# 5. ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ ЧЕРЕЗ IDEOGRAM
# ══════════════════════════════════════════════════════════

def _generate_ideogram_image(prompt: str) -> str:
    """Возвращает URL сгенерированного изображения."""
    if not IDEOGRAM_API_KEY:
        # Fallback — пустой фон если нет ключа
        return ""

    resp = requests.post(
        "https://api.ideogram.ai/generate",
        headers={
            "Api-Key": IDEOGRAM_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "image_request": {
                "prompt": prompt,
                "aspect_ratio": "ASPECT_16_9",
                "model": "V_2",
                "magic_prompt_option": "AUTO",
                "style_type": "REALISTIC"
            }
        },
        timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"][0]["url"]


# ══════════════════════════════════════════════════════════
# 6. СБОРКА HTML
# ══════════════════════════════════════════════════════════

# Цветовые схемы под каждое настроение
COLOR_SCHEMES = {
    "red_urgent": {
        "hero_bg": "#1a0000",
        "accent": "#cc1f1f",
        "accent_light": "#ff5c5c",
        "card_border": "#cc1f1f",
        "card_title_color": "#cc1f1f",
        "cta_bg": "#cc1f1f",
        "cta_btn_bg": "#ffffff",
        "cta_btn_color": "#cc1f1f",
        "eyebrow_bg": "#cc1f1f",
        "eyebrow_color": "#ffffff",
        "badge_check": "#ff5c5c",
    },
    "blue_cold": {
        "hero_bg": "#0a1628",
        "accent": "#1a6bcc",
        "accent_light": "#5bc8ff",
        "card_border": "#cc1f1f",
        "card_title_color": "#cc1f1f",
        "cta_bg": "#cc1f1f",
        "cta_btn_bg": "#ffffff",
        "cta_btn_color": "#cc1f1f",
        "eyebrow_bg": "#1a6bcc",
        "eyebrow_color": "#ffffff",
        "badge_check": "#5bc8ff",
    },
    "gold_festive": {
        "hero_bg": "#0d1b2e",
        "accent": "#c8a84b",
        "accent_light": "#e8c96a",
        "card_border": "#c8a84b",
        "card_title_color": "#8a6a1a",
        "cta_bg": "#0d1b2e",
        "cta_btn_bg": "#c8a84b",
        "cta_btn_color": "#0d1b2e",
        "eyebrow_bg": "#c8a84b",
        "eyebrow_color": "#0d1b2e",
        "badge_check": "#e8c96a",
    },
    "green_fresh": {
        "hero_bg": "#0a1f0a",
        "accent": "#2e7d32",
        "accent_light": "#66bb6a",
        "card_border": "#2e7d32",
        "card_title_color": "#2e7d32",
        "cta_bg": "#2e7d32",
        "cta_btn_bg": "#ffffff",
        "cta_btn_color": "#2e7d32",
        "eyebrow_bg": "#2e7d32",
        "eyebrow_color": "#ffffff",
        "badge_check": "#66bb6a",
    },
    "dark_professional": {
        "hero_bg": "#111111",
        "accent": "#444444",
        "accent_light": "#aaaaaa",
        "card_border": "#cc1f1f",
        "card_title_color": "#cc1f1f",
        "cta_bg": "#1a1a1a",
        "cta_btn_bg": "#cc1f1f",
        "cta_btn_color": "#ffffff",
        "eyebrow_bg": "#333333",
        "eyebrow_color": "#ffffff",
        "badge_check": "#aaaaaa",
    },
}

# SVG иконки для карточек
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
    return (
        f'<svg viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'style="width:18px;height:18px;flex-shrink:0">{path}</svg>'
    )

def _build_html(data: dict, image_url: str) -> str:
    c = COLOR_SCHEMES.get(data.get("color_mood", "red_urgent"), COLOR_SCHEMES["red_urgent"])

    # Section title с подсветкой
    section_title_html = data["section_title"].replace(
        "[HIGHLIGHT]", f'<span style="color:{c["accent"]}">'
    ) + ("</span>" if "[HIGHLIGHT]" in data["section_title"] else "")

    # Карточки
    cards_html = ""
    for card in data["cards"]:
        icon_svg = _svg_icon(card["icon"], c["card_title_color"])
        cards_html += f"""
        <div style="border:1px solid #e8e8e8;border-left:4px solid {c['card_border']};
                    border-radius:6px;padding:16px;background:#fafafa;">
          <div style="font-size:13px;font-weight:700;color:{c['card_title_color']};
                      text-transform:uppercase;letter-spacing:0.5px;margin-bottom:7px;
                      display:flex;align-items:center;gap:7px;">
            {icon_svg}{card['title']}
          </div>
          <p style="font-size:13px;color:#444;line-height:1.55;">{card['text']}</p>
        </div>"""

    # Badges
    badges_html = "".join([
        f'<span style="font-size:13px;color:#cccccc;display:flex;align-items:center;gap:5px;">'
        f'<span style="color:{c["badge_check"]};font-weight:700;">✓</span>{b}</span>'
        for b in data.get("hero_badges", [])
    ])

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
  <div style="background:#1a1a1a;padding:20px 28px;display:flex;align-items:center;justify-content:space-between;">
    <a href="https://bchdcompany.com" target="_blank" style="text-decoration:none;display:flex;align-items:center;gap:14px;">
      <div style="height:56px;flex-shrink:0;">
        <img src="http://cdn.mcauto-images-production.sendgrid.net/6e6226165269f28e/def6291d-dfc8-4dcd-8744-a195f135a22c/2697x1448.png"
             alt="BCHD Appliance Repair"
             style="height:56px;width:auto;display:block;"
        />
      </div>
      <div style="line-height:1.15;">
        <span style="font-size:22px;font-weight:900;color:#ffffff;letter-spacing:1px;display:block;">BCHD</span>
        <span style="font-size:12px;color:#aaaaaa;display:block;">Appliance Repair Service &amp; HVAC</span>
        <span style="font-size:10px;color:#cc1f1f;font-style:italic;display:block;margin-top:2px;">Fast Quality Guaranteed</span>
      </div>
    </a>
    <a href="tel:9179354553" style="text-decoration:none;background:#cc1f1f;color:#fff;font-size:15px;font-weight:700;padding:10px 16px;border-radius:6px;white-space:nowrap;margin-left:12px;">
      📞 (917) 935-4553
    </a>
  </div>

  <!-- HERO -->
  <div style="position:relative;background:{c['hero_bg']};min-height:280px;overflow:hidden;">
    {'<img src="' + image_url + '" alt="" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;opacity:0.35;display:block;" />' if image_url else ''}
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
    <div style="font-size:22px;font-weight:800;color:#1a1a1a;margin-bottom:22px;">
      {section_title_html}
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
      {cards_html}
    </div>
  </div>

  <!-- CTA -->
  <div style="background:{c['cta_bg']};padding:36px 28px;text-align:center;">
    <h2 style="font-size:26px;font-weight:900;color:#fff;margin-bottom:6px;">{data['cta_headline']}</h2>
    <p style="font-size:14px;color:rgba(255,255,255,0.8);margin-bottom:24px;">{data['cta_subtext']}</p>
    <a href="{WORKIZ_BOOKING_URL}" target="_blank"
       style="display:inline-block;background:{c['cta_btn_bg']};color:{c['cta_btn_color']};font-size:16px;font-weight:800;padding:15px 36px;border-radius:6px;text-decoration:none;margin-bottom:22px;">
      {data['cta_button']} →
    </a>
    <div style="display:flex;justify-content:center;gap:32px;flex-wrap:wrap;">
      <a href="tel:9179354553" style="color:rgba(255,255,255,0.9);font-size:15px;font-weight:600;text-decoration:none;">📞 (917) 935-4553</a>
      <a href="https://bchdcompany.com" target="_blank" style="color:rgba(255,255,255,0.9);font-size:15px;font-weight:600;text-decoration:none;">🌐 bchdcompany.com</a>
    </div>
  </div>

  <!-- SERVICES -->
  <div style="background:#1a1a1a;padding:16px 28px;display:flex;justify-content:center;flex-wrap:wrap;">
    <a href="https://bchdcompany.com/refrigerator" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:6px 14px;border-right:1px solid #333;">❄️ Refrigerator</a>
    <a href="https://bchdcompany.com/washer" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:6px 14px;border-right:1px solid #333;">🫧 Washer</a>
    <a href="https://bchdcompany.com/dryer" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:6px 14px;border-right:1px solid #333;">🌀 Dryer</a>
    <a href="https://bchdcompany.com/dishwasher" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:6px 14px;border-right:1px solid #333;">🍽️ Dishwasher</a>
    <a href="https://bchdcompany.com/oven" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:6px 14px;border-right:1px solid #333;">🔥 Oven</a>
    <a href="https://bchdcompany.com/ac" target="_blank" style="font-size:12px;color:#999;text-decoration:none;padding:6px 14px;">🌬️ AC &amp; HVAC</a>
  </div>

  <!-- FOOTER -->
  <div style="background:#0f0f0f;padding:18px 28px;text-align:center;">
    <p style="font-size:11px;color:#555;line-height:1.7;">
      BCHD Appliance Repair &amp; HVAC · 501A Surf Ave, Brooklyn, NY 11224<br>
      <a href="https://bchdcompany.com" style="color:#777;text-decoration:none;">bchdcompany.com</a> ·
      <a href="mailto:you@bchdcompany.com" style="color:#777;text-decoration:none;">you@bchdcompany.com</a><br>
      <a href="#" style="color:#777;text-decoration:none;">Unsubscribe</a>
    </p>
  </div>

</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════
# 7. ОТПРАВКА ЧЕРЕЗ SENDGRID
# ══════════════════════════════════════════════════════════

def _get_client_emails() -> list:
    """Получает список email клиентов из Postgres или clients_clean.txt."""
    try:
        import asyncpg, asyncio
        DATABASE_URL = os.environ.get("DATABASE_URL", "")
        if DATABASE_URL:
            async def _fetch():
                pool = await asyncpg.create_pool(DATABASE_URL)
                rows = await pool.fetch(
                    "SELECT email FROM email_clients WHERE unsubscribed = FALSE"
                )
                await pool.close()
                return [r["email"] for r in rows]
            loop = asyncio.new_event_loop()
            emails = loop.run_until_complete(_fetch())
            loop.close()
            if emails:
                owner = "you@bchdcompany.com"
                if owner not in emails:
                    emails = [owner] + emails
                logger.info(f"Клиентов из Postgres: {len(emails)}")
                return emails
    except Exception as e:
        logger.warning(f"Postgres недоступен: {e}")
    clients_file = Path("clients_clean.txt")
    if clients_file.exists():
        emails = [line.strip() for line in clients_file.read_text().splitlines() if "@" in line]
        logger.info(f"Клиентов из файла: {len(emails)}")
        return emails
    return []


def _send_via_sendgrid(html_content: str, subject: str, preview_text: str) -> int:
    """Отправляет письмо каждому клиенту индивидуально. Возвращает кол-во отправленных."""
    emails = _get_client_emails()
    if not emails:
        raise RuntimeError("Список клиентов пуст")
    headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"}
    sent = 0
    failed = 0
    for email in emails:
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
            else:
                failed += 1
                logger.error(f"SendGrid {email}: {resp.status_code}")
        except Exception as e:
            failed += 1
            logger.error(f"SendGrid error {email}: {e}")
    logger.info(f"Рассылка: отправлено {sent}, ошибок {failed}")
    return sent
