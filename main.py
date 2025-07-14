
import logging
import os
import json
import asyncio
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDS_RAW = os.getenv("GOOGLE_CREDENTIALS")
SHEET_NAME = "Cargodeliver"
ADMIN_USERNAME = "ник_1"

# === ЛОГИ ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# === GOOGLE SHEETS АВТОРИЗАЦИЯ ===
google_creds = json.loads(GOOGLE_CREDS_RAW)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(google_creds, scope)
client = gspread.authorize(credentials)

# === ХРАНИЛИЩЕ ===
drivers_data = {}        # user_id: { max_volume, max_weight, zone }
assigned_requests = {}   # user_id: [rows]

# === КОМАНДА /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username == ADMIN_USERNAME:
        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data='refresh_tasks')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Привет, админ! Нажми кнопку для распределения заявок:", reply_markup=reply_markup)
    else:
        await update.message.reply_text(
    "Привет! Укажи параметры машины в формате:\n\n"
    "`2.5, 500, СПБ`\n\n"
    "*где:*\n"
    "- 2.5 = объём в м³\n"
    "- 500 = макс. вес в кг\n"
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
        await update.message.reply_text("⚠️ Неверный формат. Пример:
`2.5, 500, СПБ + Область`", parse_mode="Markdown")

# === КНОПКИ ЗАЯВКИ ===
def build_task_keyboard(addr: str):
    yandex_url = f"https://yandex.ru/maps/?text={addr}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Выполнено", callback_data="done"),
         InlineKeyboardButton("🔴 Не выполнено", callback_data="fail")],
        [InlineKeyboardButton("🧭 Маршрут", url=yandex_url)]
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

                if vol <= driver["volume"] and weight <= driver["weight"]:
                    if driver["zone"] in zone:
                        suitable.append(row)
            except Exception:
                continue

        assigned_requests[user_id] = suitable

        for row in suitable:
            msg = (
                f"📦 Заявка №{row['№']}
"
                f"🚚 Адрес: {row['Адрес доставки']}
"
                f"🕒 Время: {row['Дата доставки']} {row['Время доставки']}
"
                f"💼 Вес: {row['Вес заказа']} кг | Объём: {row['Объем заказа']} м³
"
                f"📞 Тел: {row['Телефон клиента']}
"
                f"💰 Оплата: {row['Способ оплаты']}"
            )
            await bot.send_message(chat_id=user_id, text=msg, reply_markup=build_task_keyboard(row['Адрес доставки']))
            await asyncio.sleep(0.3)

# === ОБРАБОТКА КНОПОК ===
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    result = "✅ Выполнено" if query.data == "done" else "❌ Не выполнено"
    await query.edit_message_text(query.message.text + f"
{result}")

# === ОБНОВИТЬ (для админа) ===
async def refresh_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🚚 Распределяю заявки...")
    await distribute_tasks(context.bot)
    await query.edit_message_text("✅ Распределение завершено!")


# === ОТЧЁТ АДМИНУ ===
async def send_admin_report(bot):
    try:
        admin_chat = await bot.get_chat(f"@{ADMIN_USERNAME}")
        report = "📊 Ежедневный отчёт по выполненным заявкам:\n\n"
        for user_id, tasks in assigned_requests.items():
            driver_name = f"ID {user_id}"
            report += f"👤 {driver_name}: {len(tasks)} заявок отправлено\n"
        await bot.send_message(chat_id=admin_chat.id, text=report)
    except Exception as e:
        logging.error(f"Ошибка при отправке отчета админу: {e}")

# === ЕЖЕДНЕВНЫЙ ОТЧЁТ В 19:00 ===
async def schedule_admin_report(bot):
    while True:
        now = datetime.now()
        if now.hour == 19 and now.minute == 0:
            await send_admin_report(bot)
            await asyncio.sleep(60)
        await asyncio.sleep(30)


# === ЗАПУСК ===
if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.add_handler(CallbackQueryHandler(refresh_button, pattern="^refresh_tasks$"))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^(done|fail)$"))

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.add_handler(CallbackQueryHandler(refresh_button, pattern="^refresh_tasks$"))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^(done|fail)$"))

    # Запуск фоновой задачи для отчёта админу
    loop = asyncio.get_event_loop()
    loop.create_task(schedule_admin_report(app.bot))

    app.run_polling()
