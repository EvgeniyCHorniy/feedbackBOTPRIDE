"""
Telegram-бот для збору фідбеку від тестувальників мобільного застосунку.
Деплой: Render (web service) + webhook
Залежності: python-telegram-bot==20.x, anthropic, google-api-python-client, google-auth
"""

import os
import json
import time
import logging
import asyncio
from datetime import datetime
from typing import Optional

from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── Логування ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(name)s │ %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Змінні середовища ─────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
SHEETS_ID        = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDS_JSON       = os.environ["GOOGLE_CREDENTIALS_JSON"]
WEBHOOK_URL             = os.environ.get("WEBHOOK_URL", "")
PORT                    = int(os.environ.get("PORT", 8443))
DEFAULT_VERSION_IOS     = os.environ.get("DEFAULT_VERSION_IOS", "")      # напр. "2.1.0"
DEFAULT_VERSION_ANDROID = os.environ.get("DEFAULT_VERSION_ANDROID", "")  # напр. "2.0.5"

# Проста анти-спам конфігурація (можна змінити через env, не обов'язково)
MAX_FEEDBACKS_PER_HOUR = int(os.environ.get("MAX_FEEDBACKS_PER_HOUR", "10"))

def get_default_version(platform: str) -> str:
    """Повертає версію за замовчуванням для платформи."""
    if platform == "iOS":
        return DEFAULT_VERSION_IOS
    if platform == "Android":
        return DEFAULT_VERSION_ANDROID
    return ""

# ─── Стани ConversationHandler ─────────────────────────────────────────────────
(
    CHOOSE_TYPE,
    # Спільне
    ASK_VERSION,
    # Баг
    BUG_SCREEN, BUG_PLATFORM, BUG_VERSION, BUG_DESC, BUG_MEDIA,
    # Загальна оцінка
    GEN_IMPRESSION, GEN_COMFORT, GEN_RECOMMEND, GEN_NPS, GEN_IMPROVE, GEN_PLATFORM,
    # Ідея
    IDEA_DESC, IDEA_PROBLEM, IDEA_MEDIA,
    # Телефон (спільний фінал)
    ASK_PHONE,
    # Перегляд
    MY_FEEDBACK,
) = range(18)

# ─── Google Sheets ─────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheets_service():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)

async def get_telegram_file_url(bot, file_id: str) -> str:
    """Повертає пряме посилання на файл через Telegram Bot API."""
    try:
        tg_file = await bot.get_file(file_id)
        return tg_file.file_path  # вже повне https://api.telegram.org/file/bot.../...
    except Exception as e:
        logger.error(f"get_file error: {e}")
        return f"photo:{file_id}"

# ─── Захист від formula injection у Google Sheets ─────────────────────────────
def sanitize_cell(value):
    """Екранує значення, що починається з =, +, -, @ — щоб Sheets не сприймав
    їх як формулу (актуально при valueInputOption=USER_ENTERED)."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value

def sanitize_row(row: list) -> list:
    return [sanitize_cell(v) for v in row]

# ─── Синхронні "ядра" викликів до Google Sheets (виконуються в окремому потоці) ─

def _append_row_sync(sheet_name: str, row: list):
    try:
        svc = get_sheets_service()
        svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID,
            range=f"{sheet_name}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [sanitize_row(row)]}
        ).execute()
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        _log_to_sheets_sync("ERROR", f"append_row({sheet_name}): {e}")

def _log_to_sheets_sync(level: str, message: str):
    """Записує лог помилки в аркуш Logs. Не кидає виняток."""
    try:
        svc = get_sheets_service()
        svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID,
            range="Logs!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[now_str(), level, message]]}
        ).execute()
    except Exception:
        pass  # логування не має ламати бота

def _check_duplicate_bug_sync(screen: str, description: str) -> Optional[str]:
    """Перевіряє чи є схожий баг в Sheets. Повертає дату дубліката або None."""
    try:
        svc = get_sheets_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID,
            range="Bugs!A:G"
        ).execute()
        rows = result.get("values", [])[1:]  # пропускаємо заголовок

        screen_norm = screen.lower().strip()
        desc_words  = set(description.lower().split())

        for row in rows:
            if len(row) < 7:
                continue
            existing_screen = row[5].lower().strip()
            existing_desc   = row[6].lower()

            # Збіг екрану + більше 50% слів опису
            existing_words = set(existing_desc.split())
            if existing_screen == screen_norm and len(desc_words) > 0:
                overlap = len(desc_words & existing_words) / len(desc_words)
                if overlap >= 0.5:
                    return row[0]  # дата дубліката
        return None
    except Exception as e:
        logger.error(f"Duplicate check error: {e}")
        return None

def _get_user_rows_sync(sheet_name: str, telegram_id: str) -> list:
    try:
        svc = get_sheets_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID,
            range=f"{sheet_name}!A:Z"
        ).execute()
        rows = result.get("values", [])
        # Колонка B — telegram_id
        return [r for r in rows[1:] if len(r) > 1 and r[1] == telegram_id]
    except Exception as e:
        logger.error(f"Sheets read error: {e}")
        return []

def _get_user_phone_sync(telegram_id: int) -> Optional[str]:
    """Шукає збережений телефон користувача в аркуші Users."""
    try:
        svc = get_sheets_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID,
            range="Users!A:B"
        ).execute()
        rows = result.get("values", [])
        uid = str(telegram_id)
        for row in rows[1:]:  # пропускаємо заголовок
            if len(row) >= 2 and row[0] == uid and row[1]:
                return row[1]
        return None
    except Exception as e:
        logger.error(f"Users read error: {e}")
        return None

def _save_user_phone_sync(telegram_id: int, phone: str, name: str = ""):
    """Зберігає або оновлює телефон користувача в аркуші Users."""
    try:
        svc = get_sheets_service()
        uid = str(telegram_id)
        ts  = now_str()

        # Перевіряємо чи існує вже запис
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID,
            range="Users!A:A"
        ).execute()
        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if row and row[0] == uid:
                # Оновлюємо телефон у колонці B (рядок i+1, нумерація з 1)
                svc.spreadsheets().values().update(
                    spreadsheetId=SHEETS_ID,
                    range=f"Users!B{i+1}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[sanitize_cell(phone)]]}
                ).execute()
                return

        # Новий користувач — додаємо рядок
        svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID,
            range="Users!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [sanitize_row([uid, phone, name, ts])]}
        ).execute()
    except Exception as e:
        logger.error(f"Users write error: {e}")

def _generate_ai_summary_sync(feedback_type: str, data: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"""Ти аналітик продукту. Проаналізуй фідбек тестувальника мобільного застосунку та дай короткий висновок (2-3 речення) українською мовою.

Тип фідбеку: {feedback_type}
Дані: {json.dumps(data, ensure_ascii=False)}

Висновок має бути конкретним, actionable, без зайвих слів."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return "AI-аналіз недоступний"

# ─── Async-обгортки — те, що реально викликається з хендлерів ─────────────────
# Кожна з них виконує блокуючий виклик у окремому потоці, щоб НЕ зупиняти
# event loop бота для інших користувачів під час звернення до Sheets/Claude.

async def append_row(sheet_name: str, row: list):
    await asyncio.to_thread(_append_row_sync, sheet_name, row)

async def log_to_sheets(level: str, message: str):
    await asyncio.to_thread(_log_to_sheets_sync, level, message)

async def check_duplicate_bug(screen: str, description: str) -> Optional[str]:
    return await asyncio.to_thread(_check_duplicate_bug_sync, screen, description)

async def get_user_rows(sheet_name: str, telegram_id: str) -> list:
    return await asyncio.to_thread(_get_user_rows_sync, sheet_name, telegram_id)

async def get_user_phone(telegram_id: int) -> Optional[str]:
    return await asyncio.to_thread(_get_user_phone_sync, telegram_id)

async def save_user_phone(telegram_id: int, phone: str, name: str = ""):
    await asyncio.to_thread(_save_user_phone_sync, telegram_id, phone, name)

async def generate_ai_summary(feedback_type: str, data: dict) -> str:
    return await asyncio.to_thread(_generate_ai_summary_sync, feedback_type, data)

# ─── Допоміжні функції ─────────────────────────────────────────────────────────
def now_str() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")

def parse_ts(ts: str) -> datetime:
    """Парсить дату у форматі now_str() для коректного сортування (не як текст)."""
    try:
        return datetime.strptime(ts, "%d.%m.%Y %H:%M")
    except Exception:
        return datetime.min

def make_keyboard(options: list, one_time=True) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(options, one_time_keyboard=one_time, resize_keyboard=True)

SKIP_KB = make_keyboard([["⏭ Пропустити"]])
PLATFORM_KB = make_keyboard([["iOS", "Android"]])
RECOMMEND_KB = make_keyboard([["✅ Так", "❌ Ні"]])

IMPRESSION_KB = make_keyboard([[
    "😡 Дуже погано", "😕 Погано"
], [
    "🙂 Можна краще", "😊 Добре", "🤩 Супер"
]])

COMFORT_KB = make_keyboard([
    ["1", "2", "3"],
    ["4", "5", "6"],
    ["7", "8", "9"]
])

NPS_KB = make_keyboard([
    ["1", "2", "3", "4", "5"],
    ["6", "7", "8", "9", "10"]
])

# ─── Проста антиспам-перевірка (in-memory, скидається при рестарті) ───────────
def check_rate_limit(ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    """Повертає True, якщо користувачу можна продовжити (ліміт не вичерпано)."""
    bucket = ctx.application.bot_data.setdefault("feedback_times", {})
    now = time.time()
    times = [t for t in bucket.get(uid, []) if now - t < 3600]
    bucket[uid] = times
    return len(times) < MAX_FEEDBACKS_PER_HOUR

def register_feedback_sent(ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    bucket = ctx.application.bot_data.setdefault("feedback_times", {})
    bucket.setdefault(uid, []).append(time.time())

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Привіт, {name}!\n\n"
        "Я бот для збору фідбеку про мобільний застосунок.\n"
        "Твій відгук допоможе зробити продукт кращим 🚀\n\n"
        "Натисни /feedback щоб залишити фідбек\n"
        "Натисни /myfeedback щоб переглянути свої останні відгуки",
        reply_markup=ReplyKeyboardRemove()
    )

# ─── /feedback — вибір типу ───────────────────────────────────────────────────
async def feedback_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not check_rate_limit(ctx, uid):
        await update.message.reply_text(
            "⏳ Ти вже залишив декілька відгуків нещодавно. "
            "Спробуй, будь ласка, трохи пізніше — це допомагає нам обробляти фідбек якісно 🙏"
        )
        return ConversationHandler.END

    ctx.user_data.clear()
    await update.message.reply_text(
        "📋 Який тип фідбеку хочеш залишити?",
        reply_markup=make_keyboard([
            ["🐛 Баг"],
            ["⭐ Загальна оцінка"],
            ["💡 Ідея / Пропозиція"]
        ])
    )
    return CHOOSE_TYPE

async def choose_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    if "Баг" in choice:
        ctx.user_data["type"] = "bug"
        # Для багу версія питається після платформи — одразу на екран
        await update.message.reply_text(
            "🖥 На якому екрані / в якій функції виникла проблема?",
            reply_markup=ReplyKeyboardRemove()
        )
        return BUG_SCREEN
    elif "Загальна" in choice:
        ctx.user_data["type"] = "general"
    elif "Ідея" in choice:
        ctx.user_data["type"] = "idea"
    else:
        await update.message.reply_text("Будь ласка, обери один із варіантів 👆")
        return CHOOSE_TYPE

    # General і Idea — питаємо версію через DEFAULT якщо не задано
    if DEFAULT_VERSION_IOS and DEFAULT_VERSION_ANDROID:
        # Для General/Idea версія залежить від платформи — поки що пропускаємо
        ctx.user_data["version"] = ""
    return await route_after_version(update, ctx)

async def ask_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    ctx.user_data["version"] = "" if text == "⏭ Пропустити" else text
    return await route_after_version(update, ctx)

async def route_after_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ftype = ctx.user_data["type"]
    if ftype == "general":
        await update.message.reply_text(
            "📱 Яку платформу використовуєш?",
            reply_markup=PLATFORM_KB
        )
        return GEN_PLATFORM
    else:  # idea
        await update.message.reply_text(
            "💡 Опиши свою ідею або пропозицію:",
            reply_markup=ReplyKeyboardRemove()
        )
        return IDEA_DESC

# ─── БАГ флоу ─────────────────────────────────────────────────────────────────
async def bug_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["screen"] = update.message.text
    await update.message.reply_text(
        "📱 Яку платформу використовуєш?",
        reply_markup=PLATFORM_KB
    )
    return BUG_PLATFORM

async def bug_platform(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ["iOS", "Android"]:
        await update.message.reply_text("Будь ласка, обери iOS або Android 👆")
        return BUG_PLATFORM
    ctx.user_data["platform"] = text

    # Версія: автоматично якщо задана для цієї платформи
    default_ver = get_default_version(text)
    if default_ver:
        ctx.user_data["version"] = default_ver
        await update.message.reply_text(
            "📝 Опиши баг — що саме відбулося?",
            reply_markup=ReplyKeyboardRemove()
        )
        return BUG_DESC
    else:
        await update.message.reply_text(
            "📱 Яку версію застосунку тестуєш?\n_(необов'язково — натисни Пропустити)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=SKIP_KB
        )
        return BUG_VERSION

async def bug_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    ctx.user_data["version"] = "" if text == "⏭ Пропустити" else text
    await update.message.reply_text(
        "📝 Опиши баг — що саме відбулося?",
        reply_markup=ReplyKeyboardRemove()
    )
    return BUG_DESC

async def bug_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["description"] = update.message.text
    await update.message.reply_text(
        "📎 Додай скриншот якщо є\n_(необов'язково — натисни Пропустити)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=SKIP_KB
    )
    return BUG_MEDIA

async def bug_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⏭ Пропустити":
        ctx.user_data["media_url"] = ""
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        url = await get_telegram_file_url(update.get_bot(), file_id)
        ctx.user_data["media_url"] = url
    elif update.message.document:
        file_id = update.message.document.file_id
        url = await get_telegram_file_url(update.get_bot(), file_id)
        ctx.user_data["media_url"] = url
    else:
        ctx.user_data["media_url"] = update.message.text

    return await ask_phone_or_finish(update, ctx)

# ─── ЗАГАЛЬНА ОЦІНКА флоу ─────────────────────────────────────────────────────
async def gen_platform(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ["iOS", "Android"]:
        await update.message.reply_text("Будь ласка, обери iOS або Android 👆")
        return GEN_PLATFORM
    ctx.user_data["platform"] = text
    # Версія автоматично по платформі
    default_ver = get_default_version(text)
    ctx.user_data["version"] = default_ver if default_ver else ctx.user_data.get("version", "")
    await update.message.reply_text(
        "🎭 Яке твоє загальне враження від застосунку?",
        reply_markup=IMPRESSION_KB
    )
    return GEN_IMPRESSION

async def gen_impression(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    valid = ["😡 Дуже погано", "😕 Погано", "🙂 Можна краще", "😊 Добре", "🤩 Супер"]
    if update.message.text not in valid:
        await update.message.reply_text("Обери один із варіантів 👆", reply_markup=IMPRESSION_KB)
        return GEN_IMPRESSION
    ctx.user_data["impression"] = update.message.text
    await update.message.reply_text(
        "🕹 Наскільки зручно користуватись застосунком?\n_(1 — дуже незручно, 9 — дуже зручно)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=COMFORT_KB
    )
    return GEN_COMFORT

async def gen_comfort(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in [str(i) for i in range(1, 10)]:
        await update.message.reply_text("Обери число від 1 до 9 👆", reply_markup=COMFORT_KB)
        return GEN_COMFORT
    ctx.user_data["comfort"] = update.message.text
    await update.message.reply_text(
        "👥 Чи порадив би ти оновлений застосунок друзям?",
        reply_markup=RECOMMEND_KB
    )
    return GEN_RECOMMEND

async def gen_recommend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in ["✅ Так", "❌ Ні"]:
        await update.message.reply_text("Обери Так або Ні 👆", reply_markup=RECOMMEND_KB)
        return GEN_RECOMMEND
    ctx.user_data["recommend"] = update.message.text
    await update.message.reply_text(
        "📊 Як би ти оцінив застосунок за шкалою NPS?\n_(1 — точно не порекомендую, 10 — точно порекомендую)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=NPS_KB
    )
    return GEN_NPS

async def gen_nps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in [str(i) for i in range(1, 11)]:
        await update.message.reply_text("Обери число від 1 до 10 👆", reply_markup=NPS_KB)
        return GEN_NPS
    ctx.user_data["nps"] = update.message.text
    await update.message.reply_text(
        "✏️ Що варто покращити, щоб ти поставив вищу оцінку?",
        reply_markup=SKIP_KB
    )
    return GEN_IMPROVE

async def gen_improve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    ctx.user_data["improve"] = "" if text == "⏭ Пропустити" else text
    return await ask_phone_or_finish(update, ctx)

# ─── ІДЕЯ флоу ────────────────────────────────────────────────────────────────
async def idea_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["description"] = update.message.text
    await update.message.reply_text("🎯 Яку проблему це вирішує або що покращує?")
    return IDEA_PROBLEM

async def idea_problem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["problem"] = update.message.text
    await update.message.reply_text(
        "📎 Додай скриншот або макет якщо є\n_(необов'язково — натисни Пропустити)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=SKIP_KB
    )
    return IDEA_MEDIA

async def idea_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "⏭ Пропустити":
        ctx.user_data["media_url"] = ""
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        url = await get_telegram_file_url(update.get_bot(), file_id)
        ctx.user_data["media_url"] = url
    elif update.message.document:
        file_id = update.message.document.file_id
        url = await get_telegram_file_url(update.get_bot(), file_id)
        ctx.user_data["media_url"] = url
    else:
        ctx.user_data["media_url"] = update.message.text

    return await ask_phone_or_finish(update, ctx)

# ─── Телефон ───────────────────────────────────────────────────────────────────
async def ask_phone_or_finish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone = await get_user_phone(uid)
    if phone:
        ctx.user_data["phone"] = phone
        await finish_feedback(update, ctx)
        return ConversationHandler.END
    else:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Поділитись номером", request_contact=True)],
             ["⏭ Пропустити"]],
            one_time_keyboard=True, resize_keyboard=True
        )
        await update.message.reply_text(
            "📱 Поділись номером телефону — ми збережемо його і більше не питатимемо\n"
            "_(необов'язково)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
        return ASK_PHONE

async def receive_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name or ""
    if update.message.contact:
        phone = update.message.contact.phone_number
        await save_user_phone(uid, phone, name)
        ctx.user_data["phone"] = phone
    elif update.message.text == "⏭ Пропустити":
        ctx.user_data["phone"] = ""
    else:
        phone = update.message.text
        await save_user_phone(uid, phone, name)
        ctx.user_data["phone"] = phone

    await finish_feedback(update, ctx)
    return ConversationHandler.END

# ─── Збереження та підтвердження ──────────────────────────────────────────────
async def finish_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.user_data.copy()
    uid  = str(update.effective_user.id)
    user = update.effective_user
    ts   = now_str()
    ftype = data.get("type", "unknown")

    # AI-висновок
    ai_summary = await generate_ai_summary(ftype, data)

    if ftype == "bug":
        # Перевірка дублікатів
        duplicate_date = await check_duplicate_bug(
            data.get("screen", ""),
            data.get("description", "")
        )
        row = [
            ts, uid, user.username or "", user.first_name or "",
            data.get("version", ""),
            data.get("screen", ""),
            data.get("description", ""),
            data.get("platform", ""),
            data.get("media_url", ""),
            data.get("phone", ""),
            ai_summary
        ]
        await append_row("Bugs", row)
        dup_note = f"\n⚠️ _Схожий баг вже є від {duplicate_date}_" if duplicate_date else ""
        confirm = (
            f"✅ *Баг записано* | {ts}{dup_note}\n\n"
            f"📍 Екран: {data.get('screen','—')}\n"
            f"📝 Опис: {data.get('description','—')}\n"
            f"📱 Платформа: {data.get('platform','—')}"
        )

    elif ftype == "general":
        row = [
            ts, uid, user.username or "", user.first_name or "",
            data.get("version", ""),
            data.get("platform", ""),
            data.get("impression", ""),
            data.get("comfort", ""),
            data.get("recommend", ""),
            data.get("nps", ""),
            data.get("improve", ""),
            data.get("phone", ""),
            ai_summary
        ]
        await append_row("General", row)
        confirm = (
            f"✅ *Оцінку записано* | {ts}\n\n"
            f"📱 Платформа: {data.get('platform','—')}\n"
            f"🎭 Враження: {data.get('impression','—')}\n"
            f"📊 NPS: {data.get('nps','—')}/10"
        )

    else:  # idea
        row = [
            ts, uid, user.username or "", user.first_name or "",
            data.get("version", ""),
            data.get("description", ""),
            data.get("problem", ""),
            data.get("media_url", ""),
            data.get("phone", ""),
            ai_summary
        ]
        await append_row("Ideas", row)
        confirm = (
            f"✅ *Ідею записано* | {ts}\n\n"
            f"💡 Ідея: {data.get('description','—')[:80]}...\n"
            f"🎯 Навіщо: {data.get('problem','—')[:80]}..."
        )

    register_feedback_sent(ctx, update.effective_user.id)

    after_kb = ReplyKeyboardMarkup(
        [["📋 Залишити ще один фідбек"], ["📁 Мої відгуки"]],
        one_time_keyboard=False,
        resize_keyboard=True
    )
    await update.message.reply_text(
        f"{confirm}\n\nДякуємо! Твій фідбек дуже важливий для нас 🙏",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=after_kb
    )

# ─── /myfeedback ──────────────────────────────────────────────────────────────
async def my_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    all_rows = []

    for sheet in ["Bugs", "Ideas", "General"]:
        rows = await get_user_rows(sheet, uid)
        for r in rows:
            all_rows.append((sheet, r))

    if not all_rows:
        await update.message.reply_text(
            "У тебе ще немає записаного фідбеку. Натисни /feedback щоб залишити перший! 🚀"
        )
        return

    # Останні 5 — сортуємо за реальною датою, а не як текст
    all_rows.sort(key=lambda x: parse_ts(x[1][0]) if x[1] else datetime.min, reverse=True)
    recent = all_rows[:5]

    text = "📋 *Твої останні відгуки:*\n\n"
    icons = {"Bugs": "🐛", "Ideas": "💡", "General": "⭐"}
    # Індекс поля з описом для кожного аркуша: Bugs→col6(Опис), Ideas→col5(Опис), General→col6(Враження)
    desc_idx = {"Bugs": 6, "Ideas": 5, "General": 6}
    for sheet, row in recent:
        date = row[0] if row else "—"
        idx  = desc_idx.get(sheet, 5)
        desc = row[idx] if len(row) > idx else "—"
        text += f"{icons.get(sheet,'📝')} *{sheet}* | {date}\n_{desc[:60]}..._\n\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ─── Скасування ───────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Скасовано. Натисни /feedback коли будеш готовий.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Не розумію цю команду 🤔\n"
        "/feedback — залишити фідбек\n"
        "/myfeedback — мої відгуки"
    )

# ─── Глобальний обробник помилок ───────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Ловить будь-які необроблені винятки в хендлерах, щоб юзер не зависав
    без відповіді і щоб бот не падав повністю через один поганий запит."""
    logger.error("Необроблений виняток під час обробки update", exc_info=ctx.error)

    try:
        await log_to_sheets("ERROR", f"Unhandled exception: {ctx.error}")
    except Exception:
        pass

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Сталася технічна помилка. Спробуй, будь ласка, ще раз — /feedback",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception:
            pass

# ─── Головна функція ───────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("feedback", feedback_start),
            MessageHandler(filters.Regex("^/feedback$"), feedback_start),
            MessageHandler(filters.Regex("^📋 Залишити ще один фідбек$"), feedback_start),
        ],
        states={
            CHOOSE_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_type)],
            ASK_VERSION:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_version)],
            # Баг
            BUG_SCREEN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, bug_screen)],
            BUG_PLATFORM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bug_platform)],
            BUG_VERSION:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bug_version)],
            BUG_DESC:       [MessageHandler(filters.TEXT & ~filters.COMMAND, bug_desc)],
            BUG_MEDIA:      [MessageHandler(
                (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL,
                bug_media
            )],
            # Загальна
            GEN_PLATFORM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_platform)],
            GEN_IMPRESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_impression)],
            GEN_COMFORT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_comfort)],
            GEN_RECOMMEND:  [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_recommend)],
            GEN_NPS:        [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_nps)],
            GEN_IMPROVE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_improve)],
            # Ідея
            IDEA_DESC:      [MessageHandler(filters.TEXT & ~filters.COMMAND, idea_desc)],
            IDEA_PROBLEM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, idea_problem)],
            IDEA_MEDIA:     [MessageHandler(
                (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL,
                idea_media
            )],
            # Телефон
            ASK_PHONE:      [
                MessageHandler(filters.CONTACT, receive_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myfeedback", my_feedback))
    app.add_handler(MessageHandler(filters.Regex("^📁 Мої відгуки$"), my_feedback))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    app.add_error_handler(error_handler)

    if WEBHOOK_URL:
        logger.info(f"Запуск через webhook: {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{WEBHOOK_URL}/webhook",
            url_path="webhook",
        )
    else:
        logger.info("Запуск через polling (локально)")
        app.run_polling()

if __name__ == "__main__":
    main()
