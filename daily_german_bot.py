"""
Daily German Vocabulary Telegram Bot (interactive theme picker)
----------------------------------------------------------------
Flow each day:
  1. Checks whether it's actually 7:00 AM in Ottawa right now (handles
     daylight saving automatically). If not, exits quietly.
  2. Asks Gemini for 4 new daily themes, avoiding ones already used
     (history is kept in themes_history.json in this repo).
  3. Sends the 4 options to Telegram as buttons, plus a "none of these"
     button.
  4. Waits (polling) for you to tap a button.
     - Tap a theme -> generates German adjectives/verbs for it and sends it.
     - Tap "none of these" -> asks Gemini for 4 fresh options and repeats.
     - No response within the timeout -> auto-picks the first suggested
       theme so the day isn't wasted.
  5. Saves the chosen theme into themes_history.json. The GitHub Actions
     workflow commits this file back to the repo so history persists.

Required environment variables (GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  GEMINI_API_KEY

Optional environment variable:
  FORCE_RUN=1   - skip the "is it 7am Ottawa" check (useful for manual testing)
"""

import os
import json
import time
import datetime
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FORCE_RUN = os.environ.get("FORCE_RUN", "").strip() == "1"

HISTORY_FILE = "themes_history.json"
POLL_TIMEOUT_SECONDS = 15 * 60  # how long to wait for a button tap
POLL_INTERVAL_SECONDS = 5

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

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-3.5-flash:generateContent?key={GEMINI_API_KEY}"
)


# ---------------------------------------------------------------------------
# 0. Ottawa 7 AM check (handles EDT/EST automatically)
# ---------------------------------------------------------------------------
def is_seven_am_in_ottawa() -> bool:
    if ZoneInfo is None:
        return True  # fallback: don't block if zoneinfo unavailable
    now_ottawa = datetime.datetime.now(ZoneInfo("America/Toronto"))
    return now_ottawa.hour == 7


# ---------------------------------------------------------------------------
# 1. Theme history (persisted in the repo as JSON)
# ---------------------------------------------------------------------------
def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 2. Ask Gemini for text (generic helper)
# ---------------------------------------------------------------------------
def call_gemini(prompt: str) -> str:
    response = requests.post(
        GEMINI_URL,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def suggest_new_themes(history: list, rejected: list) -> list:
    used_text = "، ".join(history[-40:]) if history else "(هنوز هیچ‌کدام)"
    rejected_text = "، ".join(rejected) if rejected else "(هیچ)"

    prompt = f"""تو داری برای یه ربات یادگیری زبان آلمانی، تم روزانه پیشنهاد می‌دی.
هر تم باید یه موقعیت زندگی روزمره باشه که بشه صفت و فعل آلمانی مرتبط باهاش یاد گرفت
(مثل: بلایی که سر وسایل خونه میاد، احساسات روزانه، اتفاقات مربوط به غذا و ...).

تم‌هایی که قبلاً استفاده شده (تکرار نکن): {used_text}
تم‌هایی که کاربر همین الان رد کرده (اینا رو هم پیشنهاد نده): {rejected_text}

دقیقاً ۴ تم جدید و متفاوت از موارد بالا پیشنهاد بده.
خروجی رو **فقط** به‌صورت یک آرایه‌ی JSON از ۴ رشته‌ی فارسی بده، هیچ متن اضافه‌ای قبل یا بعدش نباشه.
هر رشته کوتاه باشه (حداکثر یک جمله)، مثل: "اتفاقاتی که برای وسایل آشپزخانه می‌افته: زنگ زدن، ترک خوردن، کند شدن تیغه"

فقط آرایه‌ی JSON رو بده."""

    raw = call_gemini(prompt).strip()
    # Gemini sometimes wraps JSON in ```json ... ``` fences; strip those.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        themes = json.loads(raw)
        themes = [str(t).strip() for t in themes if str(t).strip()]
        if len(themes) >= 4:
            return themes[:4]
    except json.JSONDecodeError:
        pass

    # Fallback: if parsing failed, split by lines as a last resort
    lines = [l.strip("-•* ").strip() for l in raw.splitlines() if l.strip()]
    return lines[:4] if len(lines) >= 4 else lines + ["تم پیش‌فرض"] * (4 - len(lines))


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
    return call_gemini(prompt)


# ---------------------------------------------------------------------------
# 3. Telegram helpers
# ---------------------------------------------------------------------------
def send_message(text: str, reply_markup: dict | None = None) -> dict:
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    last_result = None
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "Markdown",
        }
        # Only attach buttons to the last chunk
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=30)
        r.raise_for_status()
        last_result = r.json()
    return last_result


def send_theme_options(themes: list) -> dict:
    keyboard = {
        "inline_keyboard": (
            [[{"text": f"{i + 1}. {t[:60]}", "callback_data": f"theme_{i}"}] for i, t in enumerate(themes)]
            + [[{"text": "❌ هیچکدام - گزینه‌های جدید بده", "callback_data": "theme_none"}]]
        )
    }
    text = "🇩🇪 تم‌های پیشنهادی امروز رو انتخاب کن:\n\n" + "\n".join(
        f"{i + 1}. {t}" for i, t in enumerate(themes)
    )
    return send_message(text, reply_markup=keyboard)


def get_updates(offset: int | None = None, timeout: int = 0) -> list:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=timeout + 10)
    r.raise_for_status()
    return r.json().get("result", [])


def answer_callback(callback_query_id: str, text: str = "") -> None:
    requests.post(
        f"{TELEGRAM_API}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id, "text": text},
        timeout=15,
    )


def clear_pending_updates() -> int:
    """Consume any old updates so we don't react to stale button taps."""
    updates = get_updates()
    if not updates:
        return 0
    return updates[-1]["update_id"] + 1


def wait_for_theme_choice(themes: list, sent_message_id: int, start_offset: int):
    """Returns ('chosen', theme_text) or ('none', None) or ('timeout', None)."""
    offset = start_offset
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        updates = get_updates(offset=offset, timeout=POLL_INTERVAL_SECONDS)
        for update in updates:
            offset = update["update_id"] + 1
            cq = update.get("callback_query")
            if not cq:
                continue
            if cq.get("message", {}).get("message_id") != sent_message_id:
                continue
            data = cq.get("data", "")
            answer_callback(cq["id"], text="گرفتم ✅")
            if data == "theme_none":
                return "none", None
            if data.startswith("theme_"):
                idx = int(data.split("_")[1])
                if 0 <= idx < len(themes):
                    return "chosen", themes[idx]
        time.sleep(0.5)
    return "timeout", None


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------
def main():
    if not FORCE_RUN and not is_seven_am_in_ottawa():
        print("Not 7am in Ottawa right now — skipping this run.")
        return

    history = load_history()
    rejected = []
    offset = clear_pending_updates()

    chosen_theme = None
    for _attempt in range(5):  # avoid an infinite loop if user keeps rejecting
        themes = suggest_new_themes(history, rejected)
        sent = send_theme_options(themes)
        message_id = sent["result"]["message_id"]

        status, theme = wait_for_theme_choice(themes, message_id, offset)
        if status == "chosen":
            chosen_theme = theme
            break
        elif status == "none":
            rejected.extend(themes)
            send_message("باشه، بذار گزینه‌های جدید پیشنهاد بدم... 🔄")
            continue
        else:  # timeout
            chosen_theme = themes[0]
            send_message(
                f"جوابی نگرفتم، پس خودم امروز رو انتخاب کردم: *{chosen_theme}*"
            )
            break

    if chosen_theme is None:
        chosen_theme = themes[0]

    content = generate_content(chosen_theme)
    send_message(content)

    history.append(chosen_theme)
    save_history(history)
    print("Done. Theme used:", chosen_theme)


if __name__ == "__main__":
    main()
