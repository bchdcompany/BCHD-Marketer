"""
Email рассылка через SendGrid.
Генерирует HTML баннер в фирменном стиле BCHD,
отправляет по базе клиентов из Workiz.
Каждый клиент получает письмо индивидуально (BCC не используется —
каждое письмо отправляется отдельно чтобы можно было персонализировать).
"""
import logging
import os
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import io
import base64

log = logging.getLogger(__name__)

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL = "you@bchdcompany.com"
FROM_NAME = "BCHD Appliance Repair & HVAC"
UNSUBSCRIBE_GROUP_ID = None  # Заполнить после создания группы в SendGrid


def generate_banner_base64(
    headline: str,
    subheadline: str,
    offer: str = "",
) -> str:
    """
    Генерирует PNG баннер в фирменном стиле BCHD и возвращает base64.
    Встраивается прямо в HTML письмо.
    """
    W, H = 600, 280
    img = Image.new("RGB", (W, H), "#0A1628")
    draw = ImageDraw.Draw(img)

    # Градиентный фон
    for y in range(H):
        ratio = y / H
        r = int(10 + 15 * ratio)
        g = int(22 + 20 * ratio)
        b = int(40 + 50 * ratio)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Левая акцентная полоса
    draw.rectangle([0, 0, 5, H], fill="#E8A020")

    # Шрифты
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/"
        font_bold = ImageFont.truetype(font_path + "DejaVuSans-Bold.ttf", 20)
        font_logo = ImageFont.truetype(font_path + "DejaVuSans-Bold.ttf", 18)
        font_title = ImageFont.truetype(font_path + "DejaVuSans-Bold.ttf", 30)
        font_sub = ImageFont.truetype(font_path + "DejaVuSans.ttf", 15)
        font_small = ImageFont.truetype(font_path + "DejaVuSans.ttf", 12)
        font_btn = ImageFont.truetype(font_path + "DejaVuSans-Bold.ttf", 14)
    except Exception:
        font_bold = font_logo = font_title = font_sub = font_small = font_btn = ImageFont.load_default()

    # Логотип
    draw.rectangle([25, 18, 155, 48], fill="#E8A020")
    draw.text((33, 24), "BCHD", font=font_logo, fill="#0A1628")
    draw.text((165, 26), "Appliance Repair & HVAC", font=font_small, fill="#A0B4C8")

    # Заголовок
    draw.text((25, 65), headline, font=font_title, fill="#FFFFFF")

    # Подзаголовок
    draw.text((25, 108), subheadline, font=font_sub, fill="#E8A020")

    # Оффер
    if offer:
        draw.text((25, 135), offer, font=font_small, fill="#C0D0E0")

    # Декоративные круги справа
    for cx, cy, cr, alpha in [(510, 70, 55, 25), (545, 160, 70, 18), (480, 230, 35, 22)]:
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(overlay)
        d2.ellipse([cx-cr, cy-cr, cx+cr, cy+cr], fill=(232, 160, 32, alpha))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

    # CTA кнопка
    draw.rounded_rectangle([25, 165, 210, 200], radius=7, fill="#E8A020")
    draw.text((45, 175), "Book Repair Now", font=font_btn, fill="#0A1628")

    # Телефон
    draw.text((225, 176), "(917) 935-4553", font=font_sub, fill="#FFFFFF")

    # Нижняя полоса
    draw.rectangle([0, 255, W, H], fill="#E8A020")
    draw.text((25, 260), "bchdcompany.com  |  Brooklyn, Queens, Manhattan  |  Licensed & Insured", font=font_small, fill="#0A1628")

    # Конвертируем в base64
    buf = io.BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def build_html_email(
    client_name: str,
    headline: str,
    subheadline: str,
    body_text: str,
    offer: str = "",
    cta_url: str = "https://www.bchdcompany.com/#booking-form",
    banner_b64: str = "",
) -> str:
    """Собирает HTML письмо с встроенным баннером."""

    banner_html = ""
    if banner_b64:
        banner_html = f'''
        <tr>
            <td style="padding:0;">
                <img src="data:image/png;base64,{banner_b64}"
                     width="600" alt="BCHD Appliance Repair"
                     style="display:block;width:100%;max-width:600px;"/>
            </td>
        </tr>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{headline}</title>
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:20px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  {banner_html}

  <!-- BODY -->
  <tr>
    <td style="background:#ffffff;padding:30px;border-left:5px solid #E8A020;">
      <p style="color:#0A1628;font-size:16px;margin:0 0 16px;">Hi {client_name},</p>
      <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 16px;">{body_text}</p>
      <div style="text-align:center;margin:24px 0;">
        <a href="{cta_url}"
           style="background:#E8A020;color:#0A1628;font-weight:bold;font-size:16px;
                  padding:14px 36px;text-decoration:none;border-radius:6px;display:inline-block;">
          Book Your Repair →
        </a>
      </div>
    </td>
  </tr>

  <!-- SERVICES -->
  <tr>
    <td style="background:#f8f9fa;padding:20px 30px;border-left:5px solid #E8A020;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="33%" style="text-align:center;padding:8px;">
            <a href="https://www.bchdcompany.com/refrigerator" style="color:#0A1628;text-decoration:none;font-size:13px;font-weight:bold;">❄️ Refrigerators</a>
          </td>
          <td width="33%" style="text-align:center;padding:8px;">
            <a href="https://www.bchdcompany.com/hvac" style="color:#0A1628;text-decoration:none;font-size:13px;font-weight:bold;">🌡️ AC & HVAC</a>
          </td>
          <td width="33%" style="text-align:center;padding:8px;">
            <a href="https://www.bchdcompany.com/washer" style="color:#0A1628;text-decoration:none;font-size:13px;font-weight:bold;">👕 Washers & Dryers</a>
          </td>
        </tr>
        <tr>
          <td width="33%" style="text-align:center;padding:8px;">
            <a href="https://www.bchdcompany.com/dishwasher" style="color:#0A1628;text-decoration:none;font-size:13px;font-weight:bold;">🍽️ Dishwashers</a>
          </td>
          <td width="33%" style="text-align:center;padding:8px;">
            <a href="https://www.bchdcompany.com/oven" style="color:#0A1628;text-decoration:none;font-size:13px;font-weight:bold;">🔥 Ovens & Stoves</a>
          </td>
          <td width="33%" style="text-align:center;padding:8px;">
            <a href="https://www.bchdcompany.com/other-appliances" style="color:#0A1628;text-decoration:none;font-size:13px;font-weight:bold;">🔧 Other</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- CONTACT -->
  <tr>
    <td style="background:#0A1628;padding:20px 30px;border-left:5px solid #E8A020;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td>
            <a href="tel:9179354553" style="color:#E8A020;font-size:16px;font-weight:bold;text-decoration:none;display:block;margin-bottom:6px;">📞 (917) 935-4553</a>
            <a href="mailto:you@bchdcompany.com" style="color:#A0B4C8;font-size:13px;text-decoration:none;display:block;margin-bottom:4px;">✉️ you@bchdcompany.com</a>
            <span style="color:#A0B4C8;font-size:12px;">📍 501A Surf Ave, Brooklyn, NY 11224</span>
          </td>
          <td align="right">
            <a href="{cta_url}"
               style="background:#E8A020;color:#0A1628;font-weight:bold;font-size:13px;
                      padding:10px 20px;text-decoration:none;border-radius:5px;display:inline-block;">
              Book Now
            </a>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="background:#071020;padding:12px 30px;text-align:center;">
      <p style="color:#506070;font-size:11px;margin:0 0 4px;">
        BCHD Appliance Repair & HVAC | 501A Surf Ave, Brooklyn, NY 11224
      </p>
      <p style="color:#506070;font-size:11px;margin:0;">
        You received this because you used our services.
        <a href="{{{{unsubscribe_url}}}}" style="color:#E8A020;text-decoration:none;">Unsubscribe</a>
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""


async def send_campaign(
    clients: list,
    subject: str,
    headline: str,
    subheadline: str,
    body_text: str,
    offer: str = "",
) -> dict:
    """
    Отправляет рассылку через SendGrid.
    clients — список словарей {"email": ..., "name": ..., "first_name": ...}
    Возвращает статистику отправки.
    """
    if not SENDGRID_API_KEY:
        return {"success": False, "error": "SENDGRID_API_KEY не настроен"}

    try:
        import httpx
    except ImportError:
        return {"success": False, "error": "httpx не установлен"}

    # Генерируем баннер один раз для всей рассылки
    try:
        banner_b64 = generate_banner_base64(headline, subheadline, offer)
    except Exception as e:
        log.warning(f"Ошибка генерации баннера: {e}")
        banner_b64 = ""

    sent = 0
    failed = 0
    errors = []

    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        for c in clients:
            email = c.get("email", "")
            name = c.get("name", "Valued Customer")
            first_name = c.get("first_name", "there")

            if not email:
                continue

            html = build_html_email(
                client_name=first_name,
                headline=headline,
                subheadline=subheadline,
                body_text=body_text,
                offer=offer,
                banner_b64=banner_b64,
            )

            payload = {
                "personalizations": [{
                    "to": [{"email": email, "name": name}],
                }],
                "from": {"email": FROM_EMAIL, "name": FROM_NAME},
                "subject": subject,
                "content": [{"type": "text/html", "value": html}],
                "tracking_settings": {
                    "click_tracking": {"enable": True},
                    "open_tracking": {"enable": True},
                },
            }

            try:
                resp = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 202:
                    sent += 1
                else:
                    failed += 1
                    errors.append(f"{email}: {resp.status_code} {resp.text[:100]}")
                    log.error(f"SendGrid error {email}: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                failed += 1
                errors.append(f"{email}: {e}")
                log.error(f"SendGrid exception {email}: {e}")

    return {
        "success": True,
        "sent": sent,
        "failed": failed,
        "total": len(clients),
        "errors": errors[:5],  # Первые 5 ошибок для диагностики
    }
