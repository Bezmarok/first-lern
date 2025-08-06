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
import requests
from oauth2client.service_account import ServiceAccountCredentials

# === НАСТРОЙКИ ===
logging.basicConfig(level=logging.DEBUG)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_CREDENTIALS")
creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
SHEET_NAME = "Cargodeliver"
sheet = client.open(SHEET_NAME).sheet1

ORS_API_KEY = os.environ.get("ORS_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "257300241"))

drivers_data = {}
assigned_requests = defaultdict(list)

# === СТАРТ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Распределить", callback_data="refresh")]])
        await update.message.reply_text("Привет, админ! Нажми кнопку для распределения заявок:", reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "Привет! Укажи параметры машины в формате:\n\n"
            "`2.5, 500`\n\n"
            "где:\n"
            "- 2.5 = объём в м³\n"
            "- 500 = вес в кг",
            parse_mode="Markdown"
        )

# === РЕГИСТРАЦИЯ ВОДИТЕЛЯ ===
async def handle_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str, weight_str = update.message.text.split(",")
        user_id = update.effective_user.id
        username = update.effective_user.username or f"id_{user_id}"
        drivers_data[user_id] = {
            "volume": float(volume_str.strip()),
            "weight": float(weight_str.strip()),
            "username": username
        }
        await update.message.reply_text("✅ Данные сохранены. Ждите назначение заявок.")
    except Exception as e:
        logging.error(f"Ошибка регистрации водителя: {e}")
        await update.message.reply_text("⚠️ Неверный формат. Пример: 2.5, 500", parse_mode="Markdown")

# === КНОПКИ ===
def build_task_keyboard(addr: str, row_index: int):
    ors_link = f"https://maps.openrouteservice.org/directions?n1=0&n2=0&n3=10&a=&b=0&c=0&k1=en-US&k2=km"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
            InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
        ],
        [InlineKeyboardButton("📍 Маршрут", url=ors_link)]
    ])

# === ГЕОКОДИНГ ===
def geocode_address(address):
    try:
        url = f"https://api.openrouteservice.org/geocode/search"
        params = {"api_key": ORS_API_KEY, "text": address}
        response = requests.get(url, params=params)
        coords = response.json()["features"][0]["geometry"]["coordinates"]
        return coords[0], coords[1]
    except Exception as e:
        logging.error(f"Ошибка геокодинга: {e}")
        return None, None

# === РАСПРЕДЕЛЕНИЕ ЗАЯВОК ===
async def distribute_tasks(bot):
    rows = sheet.get_all_records()
    coords_map = {}
    try:
        for idx, row in enumerate(rows, start=2):
            if row.get("Водитель") or row.get("Статус") == "выполняется":
                continue
            addr = row.get("Адрес доставки")
            if addr:
                lon, lat = geocode_address(addr)
                if lon and lat:
                    coords_map[idx] = {"lat": lat, "lon": lon, "row": row}
    except Exception as e:
        logging.error(f"Ошибка при сборе координат: {e}")
        return

    for user_id, driver in drivers_data.items():
        total_vol = 0
        total_weight = 0
        username = driver["username"]
        assigned_requests[user_id] = []

        for idx, data in coords_map.items():
            row = data["row"]
            try:
                vol = float(row.get("Объем заказа", 0))
                weight = float(row.get("Вес заказа", 0))
                if vol + total_vol <= driver["volume"] and weight + total_weight <= driver["weight"]:
                    total_vol += vol
                    total_weight += weight

                    now = datetime.now().strftime("%d.%m.%Y %H:%M")
                    sheet.update(f"J{idx}", "выполняется")
                    sheet.update(f"K{idx}", username)
                    sheet.update(f"L{idx}", now)
                    assigned_requests[user_id].append(idx)

                    text = (
                        f"📦 Заявка №{idx}:\n"
                        f"📍 Адрес: {row.get('Адрес доставки', '')}\n"
                        f"🗓 Время: {row.get('План время дата', '')}\n"
                        f"📦 Товары: {row.get('Наименование', '')} x {row.get('Количество товара', '')}\n"
                        f"💰 Тел: {row.get('Телефон')}"
                    )
                    await bot.send_message(
                        chat_id=user_id,
                        text=text,
                        reply_markup=build_task_keyboard(row.get("Адрес доставки", ""), idx)
                    )
            except Exception as e:
                logging.error(f"Ошибка при распределении заявки {idx}: {e}")
                continue

# === ОТЧЁТ АДМИНУ ===
async def send_daily_report(bot):
    text = "🧾 Отчёт по задачам:\n"
    for user_id, tasks in assigned_requests.items():
        name = f"[{user_id}](tg://user?id={user_id})"
        text += f"\n{name}: {len(tasks)} задач"
    await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown")

# === КНОПКИ: DONE / FAIL ===
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

        addr = sheet.cell(row_index, 12).value or "адрес не указан"
        text = (
            f"📨 Ответ от @{username}:\n"
            f"Заявка по адресу: 📍 {addr}\n"
            f"Статус: {status}"
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
        await query.edit_message_text(f"Статус заявки обновлён: {status}")

# === ЗАПУСК ===
if __name__ == "__main__":
    TOKEN = os.environ.get("BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.run_polling()
