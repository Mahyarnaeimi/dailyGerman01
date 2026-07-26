"""
Daily German Vocabulary Telegram Bot
------------------------------------
Every day picks a life-situation theme (e.g. "things that happen to household
items: fading, breaking, wearing out...") and asks Claude to generate German
adjectives/verbs for it, with example sentences and English equivalents.
Then sends the result to a Telegram chat.

Required environment variables (set as GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN   - token from @BotFather
  TELEGRAM_CHAT_ID     - your chat id (see README for how to get it)
  GEMINI_API_KEY       - your free Google Gemini API key (aistudio.google.com)
"""

import os
import datetime
import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# بررسی اینکه سکرت‌ها خالی نمونده باشن (تا به‌جای یه 403 گنگ، پیام واضح بدیم)
for _name, _value in [
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
]:
    if not _value or not _value.strip():
        raise SystemExit(
            f"❌ Secret '{_name}' خالیه یا درست تنظیم نشده. "
            f"برو Settings → Secrets and variables → Actions → تب Secrets "
            f"و مطمئن شو '{_name}' با مقدار درست ساخته شده."
        )

# ---------------------------------------------------------------------------
# 1. Daily themes — everyday life situations. Add/remove/edit freely.
#    The theme is picked deterministically from the day of the year, so it
#    won't repeat until the list wraps around.
# ---------------------------------------------------------------------------
THEMES = [
    "چیزهایی که برای وسایل خونه اتفاق می‌افته: رنگ‌ورو رفتن، خراب شدن، کهنه شدن، زنگ زدن",
    "احساسات و حالت‌های روحی در طول روز: خسته شدن، دلتنگ شدن، آروم شدن",
    "اتفاقاتی که برای غذا می‌افته: سوختن، فاسد شدن، سرد شدن، جا افتادن",
    "تغییرات بدن انسان با گذر زمان: پیر شدن، لاغر شدن، خسته شدن",
    "اتفاقات مربوط به آب‌وهوا: ابری شدن، سرد شدن، طوفانی شدن",
    "کارهای روزمره صبح: بیدار شدن، حاضر شدن، عجله کردن",
    "مشکلات فنی و تکنولوژی: هنگ کردن، شارژ تموم شدن، خراب شدن نرم‌افزار",
    "روابط بین آدم‌ها: آشتی کردن، قهر کردن، اعتماد کردن",
    "اتفاقات مربوط به لباس: پاره شدن، تنگ شدن، رنگ پس دادن",
    "کارهای مربوط به کار و اداره: استعفا دادن، ارتقا گرفتن، اخراج شدن",
    "اتفاقات مربوط به گیاهان: پژمرده شدن، رشد کردن، خشک شدن",
    "مشکلات مربوط به ماشین: خراب شدن، ترمز گرفتن، روشن نشدن",
    "اتفاقات مربوط به خواب: خواب موندن، کابوس دیدن، بی‌خواب شدن",
    "تغییرات مالی: پس‌انداز کردن، ورشکست شدن، قرض گرفتن",
    "اتفاقات مربوط به سفر: گم شدن، تاخیر داشتن، جا موندن از پرواز",
]


def get_today_theme() -> str:
    day_of_year = datetime.date.today().timetuple().tm_yday
    return THEMES[day_of_year % len(THEMES)]


# ---------------------------------------------------------------------------
# 2. Ask Claude to generate the vocabulary content for today's theme
# ---------------------------------------------------------------------------
def generate_content(theme: str) -> str:
    prompt = f"""تو یک معلم زبان آلمانی هستی. برای موضوع/تم زیر محتوای یادگیری آلمانی تولید کن:

تم: {theme}

خروجی باید دقیقاً به این ساختار باشه (فارسی برای توضیحات، آلمانی برای لغات):

🎯 تم امروز: [یک خط توضیح تم به فارسی]

📌 صفت‌ها (Adjektive):
برای هر صفت این فرمت رو رعایت کن:
۱. **کلمه‌ی آلمانی** (Artikel در صورت نیاز)
   - مثال آلمانی: ...
   - ترجمه فارسی مثال: ...
   - معادل انگلیسی: ...

حداقل ۴ صفت مرتبط بده.

📌 فعل‌ها (Verben):
همون فرمت بالا رو برای حداقل ۴ فعل مرتبط با تم بده (فعل‌ها رو با پیشوند/زمان حال ساده معرفی کن و بگو منظم هست یا نامنظم).

فقط همین محتوا رو بده، بدون مقدمه یا توضیح اضافه."""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    response = requests.post(
        url,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# 3. Send the message to Telegram
# ---------------------------------------------------------------------------
def send_telegram_message(text: str) -> None:
    # Telegram messages have a ~4096 char limit; split if needed.
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in chunks:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "Markdown",
            },
            timeout=30,
        )
        r.raise_for_status()


def main():
    theme = get_today_theme()
    content = generate_content(theme)
    send_telegram_message(content)
    print("Sent today's German lesson successfully.")


if __name__ == "__main__":
    main()
