import logging
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)
from datetime import datetime
from collections import defaultdict
import asyncio
import os
import json
from oauth2client.service_account import ServiceAccountCredentials

# --- ЛОГИКА GOOGLE SHEETS ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_CREDENTIALS")
creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
SHEET_NAME = "Cargodeliver" 

# --- ХРАНИЛИЩЕ ---
drivers_data = {}
assigned_requests = defaultdict(list)
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "257300241"))  # ← замени на ID админа

# === СТАРТ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="refresh")]])
        await update.message.reply_text("Привет, админ! Нажми кнопку для распределения заявок:", reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "Привет! Укажи параметры машины в формате:\n\n"
            "`2.5, 500, СПБ`\n\n"
            "где:\n"
            "- 2.5 = объём в м³\n"
            "- 500 = вес в кг\n"
            "- СПБ или СПБ + Область = зона доставки",
            parse_mode="Markdown"
        )

# === ПАРСИНГ ВОДИТЕЛЯ ===
async def handle_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str, weight_str, zone = update.message.text.split(",")
        user_id = update.effective_user.id
        drivers_data[user_id] = {
            "volume": float(volume_str.strip()),
            "weight": float(weight_str.strip()),
            "zone": zone.strip().lower()
        }
        await update.message.reply_text("✅ Данные сохранены. Ждите назначение заявок.")
    except Exception:
        await update.message.reply_text(
            "⚠️ Неверный формат. Пример: 2.5, 500, СПБ",
            parse_mode="Markdown"
        )

# === КНОПКИ ЗАЯВКИ ===
def build_task_keyboard(addr: str):
    yandex_url = f"https://yandex.ru/maps/?text={addr}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выполнено", callback_data="done"),
         InlineKeyboardButton("❌ Не выполнено", callback_data="fail")],
        [InlineKeyboardButton("📍 Маршрут", url=yandex_url)]
    ])

# === РАСПРЕДЕЛЕНИЕ ЗАЯВОК ===
async def distribute_tasks(bot):
    sheet = client.open(SHEET_NAME).sheet1
    rows = sheet.get_all_records()

    for user_id, driver in drivers_data.items():
        suitable = []
        for row in rows:
            try:
                vol = float(row.get("Объем заказа", 0))
                weight = float(row.get("Вес заказа", 0))
                zone = row.get("Вид перевозки", "").lower()
                if vol <= driver["volume"] and weight <= driver["weight"] and driver["zone"] in zone:
                    suitable.append(row)
            except Exception:
                continue
        assigned_requests[user_id] = suitable
        for task in suitable:
            addr = task.get("Адрес доставки", "Без адреса")
            await bot.send_message(
                chat_id=user_id,
                text=f"📦 Заявка:\n{task}",
                reply_markup=build_task_keyboard(addr)
            )

# === ОТЧЁТ АДМИНИСТРАТОРУ ===
async def send_daily_report(bot):
    text = "🧾 Отчёт по задачам:\n"
    for user_id, tasks in assigned_requests.items():
        name = f"[{user_id}](tg://user?id={user_id})"
        text += f"\n{name}: {len(tasks)} задач"
    await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown")

# === ОБРАБОТКА CALLBACK ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "refresh":
        await distribute_tasks(context.bot)
        await query.edit_message_text("🔄 Заявки перераспределены!")
        await send_daily_report(context.bot)

# === ЗАПУСК ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    TOKEN = os.environ.get("BOT_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))

    app.run_polling()
