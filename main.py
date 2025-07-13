
import logging
import os
import json
from datetime import datetime
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Настройка логов
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Загрузка переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDS_RAW = os.getenv("GOOGLE_CREDENTIALS")

# Парсинг JSON-ключа
google_creds = json.loads(GOOGLE_CREDS_RAW)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(google_creds, scope)
client = gspread.authorize(credentials)

# === НАСТРОЙКА ===
SHEET_NAME = "Cargodeliver"  # ЗАМЕНИТЬ на фактическое имя таблицы
ADMIN_USERNAME = "ник_1"         # ЗАМЕНИТЬ на телеграм-username админа без @

# === СЛОВАРИ СОСТОЯНИЙ ===
drivers_data = {}  # user_id: { "max_weight": ..., "max_volume": ..., "area": ... }
assigned_requests = {}  # user_id: [строки таблицы]

# === ОБРАБОТЧИК СТАРТА ДЛЯ ВОДИТЕЛЕЙ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Укажи максимальные параметры машины (габариты, вес) и район доставки через запятую.\nПример:\n`150х100х80, 500, ЮАО`", parse_mode="Markdown")
    return

# === ПАРСИНГ ДАННЫХ ОТ ВОДИТЕЛЯ ===
async def parse_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        dims_str, weight_str, area = update.message.text.split(",")
        drivers_data[update.effective_user.id] = {
            "max_dims": dims_str.strip(),
            "max_weight": float(weight_str.strip()),
            "area": area.strip(),
        }
        await update.message.reply_text("Спасибо! Теперь жди назначения заявок.")
    except Exception:
        await update.message.reply_text("Неверный формат. Повтори пример: `150х100х80, 500, ЮАО`", parse_mode="Markdown")

# === КНОПКИ В ЗАЯВКЕ ===
def build_task_keyboard(addr: str):
    yandex_url = f"https://yandex.ru/navi/?whatshere[point]={addr}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Выполнено", callback_data="done"),
         InlineKeyboardButton("🔴 Не выполнено", callback_data="fail")],
        [InlineKeyboardButton("🗺 Маршрут", url=yandex_url)]
    ])

# === ПРИМЕР НАЗНАЧЕНИЯ ЗАДАЧ ===
async def assign_tasks(context: ContextTypes.DEFAULT_TYPE):
    sheet = client.open(SHEET_NAME).sheet1
    rows = sheet.get_all_values()[1:]  # без заголовка

    for user_id, driver in drivers_data.items():
        # Здесь фильтрация по весу, объёму и району (упрощено)
        suitable = [r for r in rows if float(r[9]) <= driver["max_weight"] and driver["area"] in r[11]]
        assigned_requests[user_id] = suitable

        for row in suitable:
            text = f"📦 Заявка №{row[1]}\n🚚 Адрес: {row[11]}\n📞 Тел: {row[12]}\n💰 Оплата: {row[13]}"
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=build_task_keyboard(row[11]))
            await asyncio.sleep(1)

# === ОБРАБОТКА КНОПОК ===
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    await update.callback_query.answer()
    if data == "done":
        await update.callback_query.edit_message_text(update.callback_query.message.text + "\n✅ Выполнено")
    elif data == "fail":
        await update.callback_query.edit_message_text(update.callback_query.message.text + "\n❌ Не выполнено")

# === ОТЧЁТ АДМИНУ ===
async def send_admin_report(context: ContextTypes.DEFAULT_TYPE):
    admin_id = (await context.bot.get_chat(ADMIN_USERNAME)).id
    report = "📊 Ежедневный отчёт\n\n"
    for uid, tasks in assigned_requests.items():
        report += f"👤 Водитель {uid}: {len(tasks)} заявок\n"
    await context.bot.send_message(chat_id=admin_id, text=report)

# === ПЛАНИРОВЩИК ===
async def scheduler(app: Application):
    while True:
        now = datetime.now()
        if now.hour == 7 and now.minute == 0:
            await assign_tasks(app)
            await send_admin_report(app)
        await asyncio.sleep(60)

# === ЗАПУСК ===
if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, parse_driver_params))
    app.add_handler(CallbackQueryHandler(handle_callback))

    loop = asyncio.get_event_loop()
    loop.create_task(scheduler(app))
    app.run_polling()
