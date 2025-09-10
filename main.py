import logging
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)
from datetime import datetime, timedelta
from collections import defaultdict
import os
import json
import requests
from oauth2client.service_account import ServiceAccountCredentials
from math import radians, sin, cos, asin, sqrt
import tempfile
import pandas as pd

# === ЛОГИ ===
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("cargodeliver")

# === GOOGLE SHEETS АВТОРИЗАЦИЯ ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_CREDENTIALS")
if not creds_json:
    raise RuntimeError("Не задана переменная окружения GOOGLE_CREDENTIALS")
creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

SHEET_NAME = "Cargodeliver"
sheet = client.open(SHEET_NAME).sheet1  # sheet1 как и просил

# === ОКРУЖЕНИЕ ===
ORS_API_KEY = os.environ.get("ORS_API_KEY")
if not ORS_API_KEY:
    raise RuntimeError("Не задана переменная окружения ORS_API_KEY")

ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "257300241"))

# 🚚 Координаты склада
WAREHOUSE_LAT = os.environ.get("WAREHOUSE_LAT", "59.780685")
WAREHOUSE_LON = os.environ.get("WAREHOUSE_LON", "30.170815")

TIME_WINDOW_PADDING_MIN = int(os.environ.get("TW_PADDING_MIN", "45"))
DEFAULT_SERVICE_MIN = int(os.environ.get("DEFAULT_SERVICE_MIN", "10"))

# === ЮНИТЫ ДЛЯ ORS (масштабирование до целых) ===
VOLUME_SCALE = int(os.environ.get("VOLUME_SCALE", "1000"))  # м³ -> литры
WEIGHT_SCALE = int(os.environ.get("WEIGHT_SCALE", "1"))     # кг -> кг

# === ПАМЯТЬ БОТА ===
drivers_data = {}  
assigned_requests = defaultdict(list)

# расстояние от предыдущей точки маршрута до текущей по row_idx (км)
prev_leg_km_by_row = {}  

# === КОЛОНКИ ===
HEADERS = sheet.row_values(1)
COL_INDEX = {name.strip(): i + 1 for i, name in enumerate(HEADERS)}

def col(name: str, default=None):
    return COL_INDEX.get(name, default)

COL_STATUS       = col("Статус") or 10
COL_DRIVER       = col("Водитель") or 11
COL_UPDATED      = col("Факт Дата и время") or col("Время обновления") or 12
COL_ETA          = col("ETA")
COL_ADDRESS      = col("Адрес доставки")
COL_ORDER_NUM    = col("НОМЕР заявки") or col("Номер заявки") or col("ID")  
COL_PLAN_DT      = col("План время дата")
COL_ITEM_NAME    = col("Наименование")
COL_ITEM_QTY     = col("Количество товара")
COL_PHONE        = col("Телефон")
COL_VOLUME       = col("Объем заказа")
COL_WEIGHT       = col("Вес заказа")
COL_SERVICE_MIN  = col("Время сервиса (мин)")
COL_CAR_PLATE    = col("Гос номер")
COL_DISTANCE_N   = 14

# === УТИЛИТЫ ===
def now_human():
    return datetime.now().strftime("%d.%m.%Y %H:%M")

# === ОБРАБОТКА ФАЙЛОВ ОТ АДМИНА ===
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⚠️ Загружать файлы может только администратор.")
        return

    document = update.message.document
    if not document.file_name.endswith((".xls", ".xlsx")):
        await update.message.reply_text("⚠️ Пришлите Excel-файл (.xls или .xlsx).")
        return

    # сохраняем временно
    file = await document.get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        # читаем excel в pandas
        df = pd.read_excel(tmp_path)

        # разъединённые ячейки -> заполняем вниз
        df = df.fillna(method="ffill")

        # убираем "Г-" из номеров заявок
        if "Номер заявки" in df.columns:
            df["Номер заявки"] = (
                df["Номер заявки"]
                .astype(str)
                .str.replace("Г-", "", regex=False)
                .str.strip()
            )

        # маппинг колонок (пример — подстроить под ваши поля!)
        mapping = {
            "Номер заявки": "номер заявки",
            "Вес заказа": "Вес заказа",
            "Телефон клиента": "Телефон",
            "Адрес доставки": "Адрес доставки",
            "Список товаров": "Наименование",
            "Кол-во товара": "Количество товара",
            "Объем заказа": "Объем заказа",
            "Дата доставки": "План время дата",
        }
        df = df.rename(columns=mapping)

        # оставляем только нужные
        keep_cols = list(mapping.values())
        df = df[[c for c in keep_cols if c in df.columns]]

        # превращаем в список строк
        rows = df.values.tolist()

        # определяем первую свободную строку
        last_row = len(sheet.get_all_values())
        insert_at = last_row + 1  

        # вставляем новые заявки
        sheet.insert_rows(rows, insert_at)

        await update.message.reply_text("✅ Файл загружен и новые заявки добавлены!\nНажмите 🧭 «Распределить маршруты».")
    except Exception as e:
        logger.error(f"Ошибка обработки файла: {e}")
        await update.message.reply_text("⚠️ Ошибка обработки файла. Проверьте формат.")

# === СТАРТ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("Привет, админ! Пришли Excel-файл с заявками.")
    else:
        await update.message.reply_text(
            "Укажи параметры машины, например:\n`2.5, 500, А123ВС78`\n(объём м³, вес кг, госномер)",
            parse_mode="Markdown"
        )

# === ОБРАБОТКА КНОПОК (кусок с доработками по статусам + уведомлением админа) ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # ... (оптимизация как в вашем коде выше)

    try:
        if data.startswith("done:") or data.startswith("fail:"):
            row_idx = int(data.split(":")[1])
            is_done = data.startswith("done:")
            status_value = "выполнено" if is_done else "не выполнено"

            if COL_STATUS:  sheet.update_cell(row_idx, COL_STATUS, status_value)
            if COL_UPDATED: sheet.update_cell(row_idx, COL_UPDATED, now_human())
            if not is_done:
                if COL_DRIVER: sheet.update_cell(row_idx, COL_DRIVER, "")
                if COL_ETA:    sheet.update_cell(row_idx, COL_ETA, "")

            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            # сообщение водителю
            await query.message.reply_text(f"✅ Статус обновлён: {status_value.upper()} (строка {row_idx})")

            # сообщение админу
            try:
                order_no = sheet.cell(row_idx, 1).value
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"ℹ️ Заявка №{order_no} (строка {row_idx}) → {status_value.upper()}"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить админа: {e}")

            return
    except Exception as e:
        logger.error(f"Ошибка обработки кнопки {data}: {e}")
        await query.message.reply_text("⚠️ Не удалось обновить статус. Сообщите админу.")
        return

# === ЗАПУСК ===
if __name__ == "__main__":
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None))  # заглушка для параметров
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.run_polling()
