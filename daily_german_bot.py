"""
Daily German Vocabulary Telegram Bot (multi-user, shared decision)
--------------------------------------------------------------------
Runs every 5 minutes (via GitHub Actions cron). Each run:

  1. Reads any new Telegram updates.
       - If someone sends /start -> adds them as a subscriber and
         welcomes them.
       - If someone taps a theme button -> that choice is recorded and
         will apply to EVERYONE (first valid response wins).
  2. If it's 7:00 AM in Ottawa (handles EDT/EST automatically) and
     today's daily cycle hasn't started yet -> asks Gemini for 4 new
     themes (avoiding history) and broadcasts them to every subscriber
     as buttons.
  3. If a cycle is waiting for a response:
       - Someone picked a theme -> generates the vocab lesson and
         broadcasts it to every subscriber.
       - Someone picked "none of these" -> asks Gemini for 4 fresh
         themes and re-broadcasts.
       - 15 minutes passed with no response -> auto-picks the first
         suggested theme so the day isn't wasted.
  4. Saves all state (subscribers, history, in-progress cycle) into
     bot_state.json. The GitHub Actions workflow commits this file back
     to the repo so it persists between runs.

Required environment variables (GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID     - your own chat id, used to seed the subscriber
                         list the very first time (before anyone else
                         has joined)
  GEMINI_API_KEY

Optional:
  FORCE_DAILY=1   - ignore the "already ran today" / "must be 7am" checks
                    and kick off a fresh daily cycle right now (testing)
"""

import os
import json
import datetime
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FORCE_DAILY = os.environ.get("FORCE_DAILY", "").strip() == "1"

STATE_FILE = "bot_state.json"
RESPONSE_TIMEOUT_MINUTES = 15
MAX_REJECTION_ROUNDS = 5

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
# Time helpers
# ---------------------------------------------------------------------------
def ottawa_now() -> datetime.datetime:
    if ZoneInfo is None:
        return datetime.datetime.utcnow()
    return datetime.datetime.now(ZoneInfo("America/Toronto"))


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def minutes_since(iso_timestamp: str) -> float:
    then = datetime.datetime.fromisoformat(iso_timestamp)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - then).total_seconds() / 60


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "subscribers": [int(TELEGRAM_CHAT_ID)],
        "update_offset": 0,
        "history": [],
        "daily": {"date": None, "status": "idle"},
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Gemini helpers
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
# Telegram helpers
# ---------------------------------------------------------------------------
def send_to_chat(chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    last_message_id = None
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=30)
            r.raise_for_status()
            last_message_id = r.json()["result"]["message_id"]
        except requests.exceptions.RequestException as e:
            print(f"Warning: failed to send message to {chat_id}: {e}")
    return last_message_id


def broadcast(state: dict, text: str, reply_markup: dict | None = None) -> dict:
    """Sends to every subscriber. Returns {chat_id: message_id}."""
    message_ids = {}
    for chat_id in state["subscribers"]:
        mid = send_to_chat(chat_id, text, reply_markup=reply_markup)
        if mid is not None:
            message_ids[str(chat_id)] = mid
    return message_ids


def get_updates(offset: int) -> list:
    params = {"timeout": 0}
    if offset:
        params["offset"] = offset
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("result", [])


def answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=15,
        )
    except requests.exceptions.RequestException:
        pass


def make_theme_keyboard(themes: list) -> dict:
    return {
        "inline_keyboard": (
            [[{"text": f"{i + 1}. {t[:60]}", "callback_data": f"theme_{i}"}] for i, t in enumerate(themes)]
            + [[{"text": "❌ هیچکدام - گزینه‌های جدید بده", "callback_data": "theme_none"}]]
        )
    }


# ---------------------------------------------------------------------------
# Update processing (subscribers + callback answers)
# ---------------------------------------------------------------------------
def process_updates(state: dict) -> None:
    updates = get_updates(offset=state.get("update_offset", 0))
    for update in updates:
        state["update_offset"] = update["update_id"] + 1

        message = update.get("message")
        if message and str(message.get("text", "")).strip().lower().startswith("/start"):
            chat_id = message["chat"]["id"]
            first_name = message["chat"].get("first_name", "دوست عزیز")
            if chat_id not in state["subscribers"]:
                state["subscribers"].append(chat_id)
                send_to_chat(
                    chat_id,
                    f"سلام {first_name}! 👋 خوش اومدی به ربات یادگیری واژگان آلمانی.\n\n"
                    "هر روز صبح یه تم پیشنهاد می‌شه؛ هرکی از اعضا زودتر جواب بده، "
                    "همون برای همه اعمال می‌شه و صفت/فعل‌های آلمانی مرتبط با مثال و "
                    "معادل انگلیسی براتون میاد. فقط کافیه منتظر پیام بعدی بمونی 🇩🇪",
                )
            else:
                send_to_chat(chat_id, "قبلاً عضو بودی، لازم نیست دوباره /start بزنی 😊")

        cq = update.get("callback_query")
        if not cq:
            continue

        daily = state["daily"]
        if daily.get("status") != "awaiting":
            answer_callback(cq["id"], "این گزینه‌ها دیگه فعال نیستن.")
            continue
        if daily.get("pending_choice"):
            # Someone already answered this round; just acknowledge.
            answer_callback(cq["id"], "قبلاً یکی جواب داده بود ✅")
            continue

        msg = cq.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        expected_id = daily.get("message_ids", {}).get(str(chat_id))
        if expected_id != message_id:
            # Stale button from a previous round.
            answer_callback(cq["id"], "این گزینه‌ها دیگه فعال نیستن.")
            continue

        data = cq.get("data", "")
        answer_callback(cq["id"], "گرفتم ✅")
        if data == "theme_none":
            daily["pending_choice"] = "none"
        elif data.startswith("theme_"):
            idx = int(data.split("_")[1])
            if 0 <= idx < len(daily.get("themes", [])):
                daily["pending_choice"] = daily["themes"][idx]


# ---------------------------------------------------------------------------
# Daily cycle
# ---------------------------------------------------------------------------
def start_new_cycle_if_needed(state: dict) -> None:
    now = ottawa_now()
    today_str = now.date().isoformat()
    daily = state["daily"]

    already_ran_today = daily.get("date") == today_str
    is_seven_am = now.hour == 7

    if FORCE_DAILY:
        should_start = daily.get("status", "idle") == "idle"
    else:
        should_start = is_seven_am and not already_ran_today and daily.get("status", "idle") == "idle"

    if not should_start:
        return

    themes = suggest_new_themes(state["history"], [])
    keyboard = make_theme_keyboard(themes)
    text = "🇩🇪 تم‌های پیشنهادی امروز رو انتخاب کنید (هرکی زودتر جواب بده، برای همه اعمال می‌شه):\n\n" + \
        "\n".join(f"{i + 1}. {t}" for i, t in enumerate(themes))
    message_ids = broadcast(state, text, reply_markup=keyboard)

    state["daily"] = {
        "date": today_str,
        "status": "awaiting",
        "themes": themes,
        "message_ids": message_ids,
        "rejected": [],
        "sent_at": utc_now_iso(),
        "pending_choice": None,
        "rounds": 1,
    }


def progress_cycle_if_needed(state: dict) -> None:
    daily = state["daily"]
    if daily.get("status") != "awaiting":
        return

    choice = daily.get("pending_choice")
    timed_out = minutes_since(daily["sent_at"]) >= RESPONSE_TIMEOUT_MINUTES

    if choice is None and not timed_out:
        return  # still waiting, nothing to do this run

    if choice == "none" and daily.get("rounds", 1) < MAX_REJECTION_ROUNDS:
        rejected = daily.get("rejected", []) + daily.get("themes", [])
        new_themes = suggest_new_themes(state["history"], rejected)
        keyboard = make_theme_keyboard(new_themes)
        text = "باشه، بذار گزینه‌های جدید پیشنهاد بدم 🔄\n\n" + \
            "\n".join(f"{i + 1}. {t}" for i, t in enumerate(new_themes))
        message_ids = broadcast(state, text, reply_markup=keyboard)
        daily.update({
            "themes": new_themes,
            "message_ids": message_ids,
            "rejected": rejected,
            "sent_at": utc_now_iso(),
            "pending_choice": None,
            "rounds": daily.get("rounds", 1) + 1,
        })
        return

    # Decide final theme: a real choice, or timeout/too-many-rejections fallback.
    if choice and choice != "none":
        final_theme = choice
    else:
        final_theme = daily["themes"][0]
        broadcast(state, f"جوابی نگرفتم، پس امروز رو خودم انتخاب کردم: *{final_theme}*")

    content = generate_content(final_theme)
    broadcast(state, content)

    state["history"].append(final_theme)
    daily["status"] = "done"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    state = load_state()
    process_updates(state)
    start_new_cycle_if_needed(state)
    progress_cycle_if_needed(state)
    save_state(state)
    print("Run complete. Subscribers:", len(state["subscribers"]), "| Daily status:", state["daily"].get("status"))


if __name__ == "__main__":
    main()
