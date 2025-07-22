import logging
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)
from datetime import datetime
from collections import defaultdict
import os
import json
from oauth2client.service_account import ServiceAccountCredentials

# === НАСТРОЙКИ ===
logging.basicConfig(level=logging.INFO)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_CREDENTIALS")
creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

SHEET_NAME = "Cargodeliver"
sheet = client.open(SHEET_NAME).sheet1

drivers_data = {}
assigned_requests = defaultdict(list)
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "257300241"))

# === /start ===
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

# === Ввод параметров водителя ===
async def handle_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str, weight_str, zone = update.message.text.split(",")
        user_id = update.effective_user.id
        username = update.effective_user.username or f"id_{user_id}"
        drivers_data[user_id] = {
            "volume": float(volume_str.strip()),
            "weight": float(weight_str.strip()),
            "zone": zone.strip().lower(),
            "username": username
        }
        await update.message.reply_text("✅ Данные сохранены. Ждите назначение заявок.")
    except Exception:
        await update.message.reply_text("⚠️ Неверный формат. Пример: 2.5, 500, СПБ", parse_mode="Markdown")

# === Кнопки ===
def build_task_keyboard(addr: str, row_index: int):
    yandex_url = f"https://yandex.ru/maps/?text={addr}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
            InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
        ],
        [InlineKeyboardButton("📍 Маршрут", url=yandex_url)]
    ])

# === Распределение заявок ===
async def distribute_tasks(bot):
    rows = sheet.get_all_records()
    for user_id, driver in drivers_data.items():
        total_vol = 0
        total_weight = 0
        username = driver["username"]
        assigned_requests[user_id] = []

        for idx, row in enumerate(rows, start=2):
            if row.get("Водитель") or row.get("Статус") == "выполняется":
                continue

            try:
                vol = float(row.get("Объем заказа", 0))
                weight = float(row.get("Вес заказа", 0))
                zone = row.get("Вид перевозки", "").strip().lower()

                logging.info(f"[DEBUG] Проверка строки {idx}: заявка зона={zone} | водитель зона={driver['zone']}")

                if vol + total_vol <= driver["volume"] and weight + total_weight <= driver["weight"] and zone in driver["zone"]:
                    total_vol += vol
                    total_weight += weight

                    sheet.update(f"J{idx}", "выполняется")
                    sheet.update(f"K{idx}", username)
                    assigned_requests[user_id].append(idx)

                    addr = row.get("Адрес доставки", "Москва")
                    text = (
                        f"📦 Заявка:\n"
                        f"📍 Адрес: {addr}\n"
                        f"🗓 Дата/время: {row.get('План время дата')}\n"
                        f"📦 Товары: {row.get('наименование')} x {row.get('Количество товара')}\n"
                        f"💰 Сумма: {row.get('Стоимость заказа, руб.', '—')}₽\n"
                    )
                    await bot.send_message(
                        chat_id=user_id,
                        text=text,
                        reply_markup=build_task_keyboard(addr, idx)
                    )
            except Exception as e:
                logging.error(f"[ERROR] Ошибка при распределении строки {idx}: {e}")
                continue

# === Отчёт админу ===
async def send_daily_report(bot):
    text = "🧾 Отчёт по задачам:\n"
    for user_id, tasks in assigned_requests.items():
        name = f"[{user_id}](tg://user?id={user_id})"
        text += f"\n{name}: {len(tasks)} задач"
    await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown")

# === Обработка кнопок ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    username = user.username or f"id_{user.id}"

    if query.data == "refresh":
        await distribute_tasks(context.bot)
        await query.edit_message_text("🔄 Заявки перераспределены!")
        await send_daily_report(context.bot)

    elif query.data.startswith("done") or query.data.startswith("fail"):
        action, row_index = query.data.split(":")
        row_index = int(row_index)
        status = "выполнено" if action == "done" else "не выполнено"
        now = datetime.now().strftime("%d.%m.%Y %H:%M")

        sheet.update(f"J{row_index}", status)
        sheet.update(f"L{row_index}", now)

        addr = sheet.cell(row_index, 6).value or "адрес не указан"
        text = (
            f"📨 Ответ от @{username}:\n"
            f"Заявка по адресу: 📍 {addr}\n"
            f"Статус: {status}\n"
            f"Маршрут: https://yandex.ru/maps/?text={addr}"
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
        await query.edit_message_text(f"Статус заявки обновлён: {status}")

# === Запуск бота ===
if __name__ == "__main__":
    TOKEN = os.environ.get("BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.run_polling()
