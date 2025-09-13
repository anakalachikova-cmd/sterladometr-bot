# bot.py — Бот для литературного клуба

import json
import os
from datetime import datetime, timedelta
from datetime import time as Time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
import logging
import re
import pytz  # pip install pytz

TOKEN = os.getenv("TOKEN")
DATA_FILE = "data/stats.json"
GROUP_CHAT_ID = -1002906845038  # ID группы

THREAD_INPUT = 4      # Тема "Сколько написал"
THREAD_OUTPUT = 5     # Тема "Отчёты"

WEEKLY_REPORT_HOUR = 10
MONTHLY_REPORT_HOUR = 10
REMINDER_HOUR = 20  # Напоминание в 20:00

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Хранение состояний ---
user_states = {}  # user_id → { stage, timer_job, message_id }

# --- Этапы ---
STATE_WAITING_FOR_COUNT = "waiting_for_count"

# --- Проверка админа ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if chat_id > 0:  # ЛС
        return False
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.warning(f"Ошибка проверки админа: {e}")
        return False

# --- Команда /report — кнопки в теме "Отчёты" ---
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    if chat_id != GROUP_CHAT_ID or thread_id != THREAD_INPUT:
        await update.message.reply_text(
            "❗ Команду /report можно использовать только в теме *«Сколько написал»*.",
            parse_mode="Markdown"
        )
        return

    keyboard = [
        [InlineKeyboardButton("💜 Тревожилась/грустила", callback_data="mood_purple")],
        [InlineKeyboardButton("💙 Отдыхала", callback_data="mood_blue")],
        [InlineKeyboardButton("💛 Много работала", callback_data="mood_yellow")],
        [InlineKeyboardButton("💚 Работала с текстом", callback_data="mood_green")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Как прошёл ваш день?", reply_markup=reply_markup)

# --- Обработка кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    username = query.from_user.full_name or query.from_user.username or f"Пользователь"
    chat_id = query.message.chat_id
    thread_id = query.message.message_thread_id

    # Проверяем: только в теме "Отчёты"
    if chat_id != GROUP_CHAT_ID or thread_id != THREAD_INPUT:
        await query.edit_message_text("❌ Действие недоступно здесь.")
        return

    mood_map = {
        "mood_purple": ("💜", "тревожилась/грустила"),
        "mood_blue": ("💙", "отдыхала"),
        "mood_yellow": ("💛", "много работала"),
        "mood_green": ("💚", "работала с текстом")
    }

    if query.data in mood_map:
        emoji, desc = mood_map[query.data]
        today = datetime.now().strftime("%Y-%m-%d")
        stats = load_stats()
        user_key = str(user_id)

        if user_key not in stats:
            stats[user_key] = {"name": username, "entries": {}, "moods": {}}
        stats[user_key]["name"] = username
        stats[user_key]["moods"][today] = query.data.split("_")[1]  # purple/blue/yellow/green
        save_stats(stats)

        if query.data == "mood_green":
            user_states[user_id] = {"stage": STATE_WAITING_FOR_COUNT}

            sent = await query.edit_message_text("📝 Введите количество написанных символов (например: 2500, 5,3к):")
            user_states[user_id]["message_id"] = sent.message_id

            job = context.job_queue.run_once(
                timeout_handler,
                when=300,
                data={"user_id": user_id, "username": username}
            )
            user_states[user_id]["timer_job"] = job
        else:
            await query.edit_message_text(f"✅ Вы отметили: {emoji} Сегодня вы {desc}\nСпасибо за честность! 💖")

    elif query.data in ["stats_week", "stats_month"]:
        stats = load_stats()
        if str(user_id) not in stats:
            await query.edit_message_text("❌ Вы ещё не отправляли отчёты.")
            return

        user_data = stats[str(user_id)]
        entries = user_data["entries"]
        now = datetime.now()
        period_ago = now - (timedelta(days=7) if query.data == "stats_week" else timedelta(days=30))
        title = "📊 Статистика за неделю" if query.data == "stats_week" else "📈 Статистика за месяц"

        total = sum(count for d, count in entries.items() if datetime.strptime(d, "%Y-%m-%d") >= period_ago)
        days = sum(1 for d in entries if datetime.strptime(d, "%Y-%m-%d") >= period_ago)
        avg = total // days if days else 0

        result = (
            f"👤 *{username}*\n{title}\n\n"
            f"🖋 Написано: *{total:,}* зн.\n"
            f"📅 Дней: *{days}*\n"
            f"🎯 Среднее: *{avg:,}* зн./день"
        )

        try:
            await context.bot.send_message(user_id, result, parse_mode="Markdown")
            await query.edit_message_text("✅ Отправлено в ЛС!")
        except:
            await query.edit_message_text("❗ Не удалось отправить в ЛС. Напишите боту: @Sterladometr_bot")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📆 За неделю", callback_data="stats_week"),
            InlineKeyboardButton("📅 За месяц", callback_data="stats_month")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выберите период:",
        reply_markup=reply_markup
    )

# --- Таймаут ввода ---
async def timeout_handler(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    username = context.job.data["username"]

    # Если состояние уже удалено — значит, ввод был сделан
    if user_id not in user_states:
        return  # Уже ввели — ничего не делаем

    del user_states[user_id]

    today = datetime.now().strftime("%Y-%m-%d")
    stats = load_stats()
    user_key = str(user_id)

    if user_key not in stats:
        stats[user_key] = {"name": username, "entries": {}, "moods": {}}

    # ✅ Только если ещё нет записи за сегодня
    if today not in stats[user_key]["entries"]:
        stats[user_key]["entries"][today] = 100
        save_stats(stats)
        logger.info(f"Таймаут: {username} получил 100 символов")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="⏰ Прошло 5 минут без ответа. Вам засчитано *100 символов* за день.",
            parse_mode="Markdown"
        )
    except:
        pass

# --- Ввод количества символов ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    # Проверка: сообщение из темы "Сколько написал"
    if chat_id != GROUP_CHAT_ID or thread_id != THREAD_INPUT:
        return  # Игнорируем

    if user_id not in user_states or user_states[user_id].get("stage") != STATE_WAITING_FOR_COUNT:
        return

    job = user_states[user_id].get("timer_job")
    if job:
        job.schedule_removal()

    count = parse_number(text)
    if count is None:
        await update.message.reply_text("❌ Не удалось распознать число. Примеры: 2500, 5,3к, 2.7k, 1.5 тыс.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    stats = load_stats()
    username = update.effective_user.full_name or f"User_{user_id}"
    user_key = str(user_id)

    if user_key not in stats:
        stats[user_key] = {"name": username, "entries": {}, "moods": {}}
    stats[user_key]["name"] = username
    stats[user_key]["entries"][today] = count  # ✅ Перезаписываем
    save_stats(stats)

    action = "перезаписано" if today in stats[user_key]["entries"] else "засчитано"
    await update.message.reply_text(f"✅ {action}: *{count:,}* символов!", parse_mode="Markdown")

    if user_id in user_states:
        del user_states[user_id]

# --- Парсер чисел ---
def parse_number(text: str) -> int | None:
    text = text.replace(' ', '').replace(',', '.')
    match = re.match(r'(\d+\.?\d*)\s*([кk]|тыс\.?)?$', text.lower())
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2)
    if suffix in ['к', 'k', 'тыс.']:
        number *= 1000
    return int(number)

# --- Работа с данными ---
def load_stats():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for user in data.values():
            if "moods" not in user:
                user["moods"] = {}
        return data
    except Exception as e:
        logger.error(f"Ошибка чтения: {e}")
        return {}

def save_stats(stats):
    os.makedirs("data", exist_ok=True)
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

# --- Отчёты ---
def generate_report(stats, start_date, end_date, title):
    lines = [f"{title} ({start_date} – {end_date})"]
    total_chars = 0
    user_data_list = []

    for user_id, user_data in stats.items():
        name = user_data["name"]
        chars = sum(c for d, c in user_data["entries"].items() if start_date <= d <= end_date)
        green = sum(1 for d, m in user_data["moods"].items() if start_date <= d <= end_date and m == "green")
        blue = sum(1 for d, m in user_data["moods"].items() if start_date <= d <= end_date and m == "blue")
        yellow = sum(1 for d, m in user_data["moods"].items() if start_date <= d <= end_date and m == "yellow")
        purple = sum(1 for d, m in user_data["moods"].items() if start_date <= d <= end_date and m == "purple")

        if any([chars, green, blue, yellow, purple]):
            user_data_list.append({
                "name": name, "chars": chars, "g": green, "b": blue, "y": yellow, "p": purple
            })
            total_chars += chars

    user_data_list.sort(key=lambda x: x["chars"], reverse=True)

    for u in user_data_list:
        line = f"• {u['name']}: {u['chars']:,} зн."
        moods = []
        if u['g'] > 0: moods.append(f"💚{u['g']}")
        if u['b'] > 0: moods.append(f"💙{u['b']}")
        if u['y'] > 0: moods.append(f"💛{u['y']}")
        if u['p'] > 0: moods.append(f"💜{u['p']}")
        if moods:
            line += " (" + " ".join(moods) + ")"
        lines.append(line)

    lines.append(f"\nВсего: *{total_chars:,}* зн.")
    return "\n".join(lines)

def generate_weekly_report(stats):
    week_ago = datetime.now() - timedelta(days=7)
    start = week_ago.strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    return generate_report(stats, start, end, "📝 *Еженедельный отчёт*")

def generate_monthly_report(stats):
    now = datetime.now()
    start = now.replace(day=1).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    return generate_report(stats, start, end, "📅 *Месячный отчёт*")

# --- Отправка отчётов ---
async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    report = generate_weekly_report(load_stats())
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=THREAD_OUTPUT,
        text=report,
        parse_mode="Markdown"
    )

async def send_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    report = generate_monthly_report(load_stats())
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=THREAD_OUTPUT,
        text=report,
        parse_mode="Markdown"
    )

# --- Ручной вызов отчёта (админы) ---
async def send_weekly_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Только администраторы могут использовать эту команду.")
        return

    report = generate_weekly_report(load_stats())
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=THREAD_OUTPUT,
        text=report,
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ Еженедельный отчёт отправлен вручную.")

# --- Ежедневное напоминание ---
async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        message_thread_id=THREAD_OUTPUT,
        text="🔔 *Напоминание*\nНе забудьте отметить свой день с помощью /report!",
        parse_mode="Markdown"
    )

# --- Команды ---
async def set_commands(application):
    await application.bot.set_my_commands([
        BotCommand("report", "Отметить день"),
        BotCommand("stats", "Личная статистика"),
        BotCommand("top", "ТОП авторов"),
        BotCommand("send_weekly", "Вызвать недельный отчёт (админы)"),
        BotCommand("help", "Помощь")
    ])

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📌 Используйте:\n"
        "• `/report` — как прошёл день\n"
        "• `/stats` — ваша статистика\n"
        "• `/top` — топ участников\n\n"
        "💡 Бот сам засчитает 100 символов, если вы не введёте число за 5 минут."
    )
    await update.message.reply_text(text)

# --- ТОП ---
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Только администраторы могут использовать эту команду.")
        return

    stats = load_stats()
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    start = week_ago.strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    user_data = []
    for uid, data in stats.items():
        name = data["name"]
        chars = sum(c for d, c in data["entries"].items() if start <= d <= end)
        days = sum(1 for d in data["entries"] if start <= d <= end)
        if chars > 0:
            user_data.append((name, chars, days))

    user_data.sort(key=lambda x: x[1], reverse=True)
    lines = ["🏆 *ТОП-5 за неделю*"]
    for i, (name, chars, days) in enumerate(user_data[:5], 1):
        lines.append(f"{i}. {name} — *{chars:,}* зн. (*{days}* дн.)")
    lines.append("\n📌 Данные обновлены")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# --- main ---
def main():
    app = Application.builder().token(TOKEN).build()

    if app.job_queue is None:
        logger.error("JobQueue не создана.")
        return

    # Команды
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("send_weekly", send_weekly_now))
    app.add_handler(CommandHandler("help", help_command))

    # Обработчики
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Установка команд
    app.job_queue.run_once(set_commands, when=2)

    # --- Расписание ---
    moscow = pytz.timezone("Europe/Moscow")

    daily_time = Time(hour=REMINDER_HOUR, minute=0, second=0, tzinfo=moscow)
    weekly_time = Time(hour=WEEKLY_REPORT_HOUR, minute=0, second=0, tzinfo=moscow)
    monthly_time = Time(hour=MONTHLY_REPORT_HOUR, minute=0, second=0, tzinfo=moscow)

    app.job_queue.run_daily(daily_reminder, time=daily_time)
    app.job_queue.run_daily(send_weekly_report, time=weekly_time, days=(1,))  # ✅ Вторник
    app.job_queue.run_monthly(send_monthly_report, when=monthly_time, day=1)

    logger.info("✅ Бот запущен и слушает...")
    app.run_polling()

if __name__ == "__main__":
    main()


