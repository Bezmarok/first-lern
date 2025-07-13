# main.py — Telegram бот для распределения заявок доставки через Google Sheets

import os
import json
import logging
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 🔧 Настройка логов
logging.basicConfig(level=logging.INFO)

# 🛠️ Глобальные переменные
drivers = {}
assignments = {}
ADMIN_USERNAME = "ник_1"  # Заменить на username администратора Telegram

# 🔐 Авторизация через GOOGLE_CREDENTIALS из Railway
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
google_creds = json.loads(os.environ["GOOGLE_CREDENTIALS"])
credentials = ServiceAccountCredentials.from_json_keyfile_dict(google_creds, scope)
client = gspread.authorize(credentials)
sheet = client.open("Cargodeliver").sheet1  # Заменить на имя вашей таблицы

# 🚛 Команда /start — регистрация водителя
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.username == ADMIN_USERNAME:
        await update.message.reply_text("Привет, админ! Заявки будут распределяться автоматически.")
    else:
        await update.message.reply_text(
            "Привет! Введи параметры через запятую:\n"
            "Макс. объем (м³), макс. вес (кг), район доставки (например: ЮЗАО)"
        )

# 🚙 Обработка параметров водителя
async def handle_driver_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text
    try:
        size, weight, district = [x.strip() for x in text.split(",")]
        drivers[chat_id] = {
            "size": float(size),
            "weight": float(weight),
            "district": district,
            "name": update.message.from_user.full_name
        }
        await update.message.reply_text("✅ Данные сохранены. Ждите назначение заявок.")
    except:
        await update.message.reply_text("⚠️ Ошибка. Введите: объем, вес, район (через запятую).")

# 📦 Форматирование текста заявки
def build_order_message(row):
    return (
        f"🚚 Заявка №{row['№']}\n"
        f"Дата: {row['Дата доставки']} {row['Время доставки']}\n"
        f"Адрес: {row['Адрес доставки']}\n"
        f"Клиент: {row['Телефон клиента']}\n"
        f"Объем: {row['Объем заказа']} м³, Вес: {row['Вес заказа']} кг\n"
        f"Оплата: {row['Способ оплаты']}\n"
        f"Комментарий: {row['Комментарий']}"
    )

# 🔎 Поиск подходящего водителя
def match_driver(row):
    for chat_id, driver in drivers.items():
        if driver["district"].lower() in row["Адрес доставки"].lower():
            if float(row["Объем заказа"]) <= driver["size"] and float(row["Вес заказа"]) <= driver["weight"]:
                return chat_id
    return None

# 📤 Распределение заявок
async def assign_tasks(context: ContextTypes.DEFAULT_TYPE):
    rows = sheet.get_all_records()
    for row in rows:
        row_id = str(row["№"])
        if row_id not in assignments:
            chat_id = match_driver(row)
            if chat_id:
                msg = build_order_message(row)
                keyboard = [
                    [InlineKeyboardButton("🟢 Выполнено", callback_data=f"done_{row_id}"),
                     InlineKeyboardButton("🔴 Не выполнено", callback_data=f"fail_{row_id}")],
                    [InlineKeyboardButton("🧭 Маршрут", url=f"https://yandex.ru/maps/?text={row['Адрес доставки']}")]
                ]
                message = await context.bot.send_message(chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard))
                assignments[row_id] = {
                    "driver": chat_id,
                    "status": "assigned",
                    "message_id": message.message_id
                }

# 🖱 Обработка кнопок
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("done_") or data.startswith("fail_"):
        num = data.split("_")[1]
        status = "done" if data.startswith("done_") else "fail"
        assignments[num]["status"] = status
        result = "✅ Выполнена" if status == "done" else "❌ Не выполнена"
        await query.edit_message_text(f"Заявка №{num} — {result}")

# 📊 Ежедневный отчёт администратору
async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    summary = "📊 Ежедневный отчёт:\n"
    for num, a in assignments.items():
        name = drivers[a["driver"]]["name"]
        status = "🟢" if a["status"] == "done" else "🔴" if a["status"] == "fail" else "⏳"
        summary += f"{status} Заявка №{num} — {a['status']} (Водитель: {name})\n"
    for admin in context.application.bot_data.get("admins", []):
        await context.bot.send_message(admin, summary)

# 🧠 Запуск бота
async def main():
    app = ApplicationBuilder().token(os.environ["BOT_TOKEN"]).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_data))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Добавьте Telegram ID админа для получения отчётов
    app.bot_data["admins"] = []

    scheduler = AsyncIOScheduler()
    scheduler.add_job(assign_tasks, "cron", hour=8, minute=0, args=[app])
    scheduler.add_job(daily_report, "cron", hour=21, minute=0, args=[app])
    scheduler.start()

    print("🤖 Бот запущен")
    await app.run_polling()

if __name__ == "__main__":
    app.run_polling()
