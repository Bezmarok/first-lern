import logging
import os
import time
import gspread
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler, ContextTypes

# Настройка логирования
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Настройки окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ORS_API_KEY = os.getenv("ORS_API_KEY")
SPREADSHEET_NAME = "Cargodeliver"

# Подключение к Google Sheets
gc = gspread.service_account(filename='creds.json')
sheet = gc.open(SPREADSHEET_NAME).sheet1

# Словарь с зарегистрированными водителями
drivers = {}

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "Привет! Укажи параметры машины в формате:\n"
        "вес (кг), объём (м3), например: 100 2.5"
    )

async def register_driver(update: Update, context: CallbackContext):
    try:
        chat_id = update.message.chat_id
        text = update.message.text
        parts = text.strip().split()
        if len(parts) != 2:
            raise ValueError("Неверный формат")
        weight, volume = map(float, parts)
        drivers[chat_id] = {
            "weight": weight,
            "volume": volume,
            "assignments": []
        }
        await update.message.reply_text("Вы зарегистрированы как водитель.")
    except Exception as e:
        logger.exception("Ошибка при регистрации водителя")
        await update.message.reply_text("Ошибка. Укажите вес и объём через пробел, например: 100 2.5")

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    action, row = data[0], int(data[1])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    if action == "done":
        sheet.update_cell(row + 2, 12, "выполнено")
    elif action == "fail":
        sheet.update_cell(row + 2, 12, "не выполнено")
    sheet.update_cell(row + 2, 13, now_str)
    await query.edit_message_text(text=f"Заявка №{row+1} отмечена как '{action}'")

async def distribute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        all_rows = sheet.get_all_values()[1:]
        tasks = []
        for i, row in enumerate(all_rows):
            if not row[9]:  # Водитель не назначен
                address = row[4]
                try:
                    weight = float(row[0])
                    volume = float(row[3])
                except ValueError:
                    logger.warning(f"Невалидные данные веса или объема в строке {i}")
                    continue
                tasks.append({
                    "row": i,
                    "address": address,
                    "weight": weight,
                    "volume": volume,
                    "qty": row[5],
                    "title": row[6],
                    "plan_time": row[7]
                })

        logger.debug(f"Найдено заявок для распределения: {len(tasks)}")
        if not tasks:
            await update.message.reply_text("Нет новых заявок для распределения.")
            return

        for chat_id, driver in drivers.items():
            cap_weight = driver["weight"]
            cap_volume = driver["volume"]
            assigned = []

            for task in tasks[:]:
                if task["weight"] <= cap_weight and task["volume"] <= cap_volume:
                    logger.debug(f"Назначаем заявку {task['row']} водителю {chat_id}")
                    cap_weight -= task["weight"]
                    cap_volume -= task["volume"]
                    assigned.append(task)
                    tasks.remove(task)

                    # Построение маршрута
                    route_url = f"https://www.openstreetmap.org/search?query={task['address'].replace(' ', '%20')}"

                    # Обновление таблицы
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                    sheet.update_cell(task["row"] + 2, 10, "выполняется")
                    sheet.update_cell(task["row"] + 2, 11, str(chat_id))
                    sheet.update_cell(task["row"] + 2, 13, now_str)

                    # Отправка сообщения
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Выполнено", callback_data=f"done|{task['row']}"),
                            InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail|{task['row']}"),
                            InlineKeyboardButton("📍 Маршрут", url=route_url)
                        ]
                    ])
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"📦 Новая заявка:\n"
                             f"Адрес: {task['address']}\n"
                             f"План: {task['plan_time']}\n"
                             f"Товар: {task['title']}\n"
                             f"Кол-во: {task['qty']}",
                        reply_markup=keyboard
                    )
    except Exception as e:
        logger.exception("Ошибка при распределении заявок")
        await update.message.reply_text(f"Ошибка при распределении: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, register_driver))
    app.add_handler(CommandHandler("распределить", distribute))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
