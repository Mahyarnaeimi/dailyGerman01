import os
import json
import time
import datetime
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FORCE_DAILY = os.environ.get("FORCE_DAILY", "").strip() == "1"
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_ID = os.environ["GIST_ID"]

GIST_FILENAME = "bot_state.json"
MAX_REJECTION_ROUNDS = 5

ACTIVE_POLL_SECONDS = 50
ACTIVE_POLL_STEP = 20

# ساعت شروع ارسال روزانه (به وقت اتاوا) — از این ساعت به بعد ارسال می‌شه
DAILY_SEND_HOUR = 7  # 7 صبح

# --- مقاوم‌سازی در برابر خطاهای موقت Gemini ---
GEMINI_TIMEOUT = 90            # ثانیه (قبلاً ۶۰ بود)
GEMINI_MAX_ATTEMPTS = 4        # تعداد تلاش‌ها با backoff
RETRYABLE_STATUS = {429, 500, 502, 503, 504}  # این‌ها موقتی‌ان، retry کن

# اگه Gemini اصلاً جواب نداد، از این تم‌های آماده استفاده می‌شه تا پیام روز از دست نره
FALLBACK_THEMES = [
    "جلسه‌ی کاری فشرده: مذاکره روی دیدلاین، ارائه‌ی گزارش پیشرفت، اختلاف نظر با همکار",
    "ارائه دادن سمینار دانشگاهی: استرس قبل ارائه، جلب توجه مخاطب، جواب دادن به سوالات سخت",
    "مکاتبات اداری روزمره: نوشتن ایمیل رسمی، پیگیری درخواست، پاسخ دیرهنگام همکار",
    "کار گروهی روی پروژه‌ی دانشگاهی: تقسیم وظایف، جا ماندن از زمان‌بندی، بازنویسی بخش‌ها",
    "مصاحبه‌ی شغلی: معرفی خود، پاسخ به سوال درباره‌ی نقاط ضعف، مذاکره‌ی حقوق",
    "امتحانات پایان‌ترم: آماده شدن شبانه، استرس جلسه‌ی امتحان، فراموش کردن پاسخ",
    "مدیریت زمان در محل کار: عقب افتادن از برنامه، اولویت‌بندی کارها، تمدید مهلت",
    "جلسه با استاد راهنما: ارائه‌ی پیشرفت پایان‌نامه، دریافت انتقاد، اصلاح مسیر تحقیق",
]

for _name, _value in [
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ("GIST_TOKEN", GIST_TOKEN),
    ("GIST_ID", GIST_ID),
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
    f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
)
GIST_API = f"https://api.github.com/gists/{GIST_ID}"


def ottawa_now() -> datetime.datetime:
    if ZoneInfo is None:
        return datetime.datetime.utcnow()
    return datetime.datetime.now(ZoneInfo("America/Toronto"))


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_state() -> dict:
    try:
        r = requests.get(
            GIST_API,
            headers={"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        r.raise_for_status()
        content = r.json()["files"][GIST_FILENAME]["content"]
        return json.loads(content)
    except (requests.exceptions.RequestException, KeyError, json.JSONDecodeError):
        pass
    return {
        "subscribers": [int(TELEGRAM_CHAT_ID)],
        "update_offset": 0,
        "history": [],
        "daily": {"date": None, "status": "idle"},
    }


def save_state(state: dict) -> None:
    try:
        requests.patch(
            GIST_API,
            headers={"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"files": {GIST_FILENAME: {"content": json.dumps(state, ensure_ascii=False, indent=2)}}},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        print(f"Warning: failed to save state to gist: {e}")


def call_gemini(prompt: str) -> str:
    """
    Gemini رو با retry و backoff صدا می‌زنه.
    خطاهای موقت (429/5xx و تایم‌اوت/قطعی شبکه) رو چند بار دوباره تلاش می‌کنه.
    خطاهای دائمی (مثل ۴۰۰/۴۰۳/۴۰۴) رو بلافاصله raise می‌کنه چون retry بی‌فایده‌ست.
    """
    last_err = None
    for attempt in range(GEMINI_MAX_ATTEMPTS):
        try:
            response = requests.post(
                GEMINI_URL,
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=GEMINI_TIMEOUT,
            )
            if response.status_code in RETRYABLE_STATUS:
                print(f"Gemini API {response.status_code} (attempt {attempt + 1}/{GEMINI_MAX_ATTEMPTS}): {response.text[:200]}")
                last_err = requests.exceptions.HTTPError(
                    f"{response.status_code} retryable", response=response
                )
            else:
                if not response.ok:
                    print(f"Gemini API error {response.status_code}: {response.text[:500]}")
                    response.raise_for_status()  # خطای دائمی — بدون retry بالا می‌ره
                data = response.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            print(f"Gemini network error (attempt {attempt + 1}/{GEMINI_MAX_ATTEMPTS}): {e}")
            last_err = e

        if attempt < GEMINI_MAX_ATTEMPTS - 1:
            wait = 5 * (3 ** attempt)  # ۵، ۱۵، ۴۵ ثانیه
            print(f"Retrying Gemini in {wait}s...")
            time.sleep(wait)

    raise last_err if last_err is not None else RuntimeError("Gemini call failed")


def _pick_fallback_themes(history: list, rejected: list) -> list:
    """۴ تم از لیست آماده که تو history و rejected نیستن."""
    used = set(history) | set(rejected)
    pool = [t for t in FALLBACK_THEMES if t not in used]
    if len(pool) < 4:
        pool = pool + [t for t in FALLBACK_THEMES if t not in pool]
    return pool[:4]


def suggest_new_themes(history: list, rejected: list) -> list:
    used_text = "، ".join(history[-40:]) if history else "(هنوز هیچ‌کدام)"
    rejected_text = "، ".join(rejected) if rejected else "(هیچ)"

    prompt = f"""تو داری برای یه ربات یادگیری زبان آلمانی، تم روزانه پیشنهاد می‌دی.
هر تم باید یه موقعیت مرتبط با محیط کار/شغل یا محیط آکادمیک/دانشگاه باشه که بشه صفت و فعل
آلمانی مرتبط باهاش یاد گرفت. تم‌ها رو از این دو دسته انتخاب کن:

۱) محیط کار و شغل: مکالمات کاری، جلسات، ایمیل‌های اداری، مذاکره با همکار/مدیر، مصاحبه‌ی
   شغلی، دیدلاین و فشار کاری، همکاری تیمی، مشکلات و اتفاقات روزمره‌ی محل کار.
۲) محیط آکادمیک و دانشگاه: کلاس درس، سمینار و ارائه، تحقیق و پروژه‌ی دانشگاهی، پایان‌نامه،
   مکالمه با استاد یا هم‌کلاسی، امتحانات، کار گروهی روی پروژه، کنفرانس علمی.

تم‌ها رو بین این دو دسته متنوع کن (نه فقط یکی از دو تا).

تم‌هایی که قبلاً استفاده شده (تکرار نکن): {used_text}
تم‌هایی که کاربر همین الان رد کرده (اینا رو هم پیشنهاد نده): {rejected_text}

دقیقاً ۴ تم جدید و متفاوت از موارد بالا پیشنهاد بده.
خروجی رو **فقط** به‌صورت یک آرایه‌ی JSON از ۴ رشته‌ی فارسی بده، هیچ متن اضافه‌ای قبل یا بعدش نباشه.
هر رشته کوتاه باشه (حداکثر یک جمله)، مثل: "ارائه دادن سمینار دانشگاهی: استرس قبلش، جلب توجه مخاطب، جواب دادن به سوالات" یا "جلسه‌ی کاری فشرده: مذاکره روی دیدلاین، ارائه‌ی گزارش پیشرفت، اختلاف نظر با همکار"

فقط آرایه‌ی JSON رو بده."""

    try:
        raw = call_gemini(prompt).strip()
    except Exception as e:
        print(f"Gemini failed for theme suggestion, using fallback themes: {e}")
        return _pick_fallback_themes(history, rejected)

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
    if len(lines) >= 4:
        return lines[:4]
    # اگه حتی خروجی هم خراب بود، به fallback برگرد به‌جای تم پیش‌فرض بی‌معنی
    return _pick_fallback_themes(history, rejected)


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


def send_to_chat(chat_id: int, text: str, reply_markup=None):
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


def broadcast(state: dict, text: str, reply_markup=None) -> dict:
    message_ids = {}
    for chat_id in state["subscribers"]:
        mid = send_to_chat(chat_id, text, reply_markup=reply_markup)
        if mid is not None:
            message_ids[str(chat_id)] = mid
    return message_ids


def get_updates(offset: int, timeout: int = 0) -> list:
    params = {"timeout": timeout}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=timeout + 15)
        r.raise_for_status()
        return r.json().get("result", [])
    except requests.exceptions.RequestException as e:
        print(f"Warning: getUpdates failed: {e}")
        return []


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


def handle_single_update(state: dict, update: dict) -> None:
    message = update.get("message") or {}
    text = str(message.get("text") or "").strip()
    if text.lower().startswith("/start"):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        first_name = chat.get("first_name") or "دوست عزیز"
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
        return

    cq = update.get("callback_query")
    if not cq:
        return

    daily = state["daily"]
    if daily.get("status") != "awaiting":
        answer_callback(cq.get("id", ""), "این گزینه‌ها دیگه فعال نیستن.")
        return
    if daily.get("pending_choice"):
        answer_callback(cq.get("id", ""), "قبلاً یکی جواب داده بود ✅")
        return

    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    expected_id = daily.get("message_ids", {}).get(str(chat_id))
    if expected_id != message_id:
        answer_callback(cq.get("id", ""), "این گزینه‌ها دیگه فعال نیستن.")
        return

    data = cq.get("data", "")
    answer_callback(cq.get("id", ""), "گرفتم ✅")
    if data == "theme_none":
        daily["pending_choice"] = "none"
    elif data.startswith("theme_"):
        try:
            idx = int(data.split("_")[1])
        except (IndexError, ValueError):
            return
        if 0 <= idx < len(daily.get("themes", [])):
            daily["pending_choice"] = daily["themes"][idx]


def process_updates(state: dict) -> None:
    deadline = time.monotonic() + ACTIVE_POLL_SECONDS
    while True:
        updates = get_updates(offset=state.get("update_offset", 0), timeout=ACTIVE_POLL_STEP)
        for update in updates:
            state["update_offset"] = update["update_id"] + 1
            try:
                handle_single_update(state, update)
            except Exception as e:
                print(f"Warning: failed to process update {update.get('update_id')}: {e}")

        if state["daily"].get("pending_choice"):
            return
        if time.monotonic() >= deadline:
            return


def reset_daily_if_new_day(state: dict) -> None:
    """
    FIX: اگه روز جدیده و status روی done یا awaiting مونده، ریست کن.
    این جلوی بلاک شدن روزهای بعدی رو می‌گیره.
    """
    now = ottawa_now()
    today_str = now.date().isoformat()
    daily = state["daily"]
    last_date = daily.get("date")

    if last_date and last_date != today_str:
        print(f"New day detected ({last_date} -> {today_str}), resetting daily state.")
        state["daily"] = {"date": None, "status": "idle"}


def start_new_cycle_if_needed(state: dict) -> None:
    now = ottawa_now()
    today_str = now.date().isoformat()
    daily = state["daily"]

    already_ran_today = daily.get("date") == today_str
    is_after_send_hour = now.hour >= DAILY_SEND_HOUR

    if FORCE_DAILY:
        should_start = daily.get("status", "idle") == "idle"
    else:
        should_start = is_after_send_hour and not already_ran_today and daily.get("status", "idle") == "idle"

    if not should_start:
        print(f"No new cycle needed. status={daily.get('status')}, already_ran={already_ran_today}, hour={now.hour}")
        return

    print(f"Starting new daily cycle for {today_str}...")
    themes = suggest_new_themes(state["history"], [])
    keyboard = make_theme_keyboard(themes)
    text = "🇩🇪 تم‌های پیشنهادی امروز رو انتخاب کنید (هرکی زودتر جواب بده، برای همه اعمال می‌شه):\n\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(themes))
    message_ids = broadcast(state, text, reply_markup=keyboard)

    # FIX: اگه هیچ پیامی موفق ارسال نشد، وضعیت رو قفل نکن (awaiting) —
    # idle نگه دار تا ران بعدی دوباره تلاش کنه.
    if not message_ids:
        print("Broadcast delivered no messages; keeping cycle idle to retry next run.")
        state["daily"] = {"date": None, "status": "idle"}
        return

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
    if choice is None:
        return

    if choice == "none" and daily.get("rounds", 1) < MAX_REJECTION_ROUNDS:
        rejected = daily.get("rejected", []) + daily.get("themes", [])
        new_themes = suggest_new_themes(state["history"], rejected)
        keyboard = make_theme_keyboard(new_themes)
        text = "باشه، بذار گزینه‌های جدید پیشنهاد بدم 🔄\n\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(new_themes))
        message_ids = broadcast(state, text, reply_markup=keyboard)
        if not message_ids:
            # ارسال گزینه‌های جدید موفق نبود؛ pending_choice رو نگه دار تا ران بعدی دوباره تلاش کنه
            print("Failed to broadcast new theme options; will retry next run.")
            return
        daily.update({
            "themes": new_themes,
            "message_ids": message_ids,
            "rejected": rejected,
            "sent_at": utc_now_iso(),
            "pending_choice": None,
            "rounds": daily.get("rounds", 1) + 1,
        })
        return

    # FIX: اگه کاربر تا سقف دفعات ❌ زده، به‌جای themes[0] (که همین الان ردش کرده)
    # یه تم آماده‌ی ردنشده انتخاب کن.
    if choice != "none":
        final_theme = choice
    else:
        exhausted = daily.get("rejected", []) + daily.get("themes", [])
        final_theme = _pick_fallback_themes(state["history"], exhausted)[0]

    try:
        content = generate_content(final_theme)
    except Exception as e:
        # تولید محتوا موقتاً نشد — به‌جای کرش و سکوت، یه بار اطلاع بده و
        # pending_choice/awaiting رو نگه دار تا ران ساعتی بعدی خودش دوباره تلاش کنه.
        print(f"Gemini failed to generate content, will retry next run: {e}")
        if not daily.get("content_failed_notified"):
            broadcast(
                state,
                "⚠️ تم امروز انتخاب شد ولی سرویس تولید محتوا موقتاً شلوغه. "
                "خودکار دوباره تلاش می‌شه و محتوا به‌زودی میاد.",
            )
            daily["content_failed_notified"] = True
        return

    broadcast(state, content)
    state["history"].append(final_theme)
    daily["status"] = "done"


def main():
    state = load_state()
    # FIX: اول چک کن روز جدید شده یا نه — اگه آره، daily رو ریست کن
    reset_daily_if_new_day(state)
    process_updates(state)
    start_new_cycle_if_needed(state)
    progress_cycle_if_needed(state)
    save_state(state)
    print("Run complete. Subscribers:", len(state["subscribers"]), "| Daily status:", state["daily"].get("status"))


if __name__ == "__main__":
    main()
