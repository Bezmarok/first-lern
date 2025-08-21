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

# Опционально: если знаете координаты склада — укажите.
WAREHOUSE_LAT = os.environ.get("WAREHOUSE_LAT")  # строка или None
WAREHOUSE_LON = os.environ.get("WAREHOUSE_LON")  # строка или None

drivers_data = {}
assigned_requests = defaultdict(list)

# ===== ВСПОМОГАТЕЛЬНОЕ: заголовки и индексы столбцов =====
# Достаём первую строку (заголовки) и строим карту "имя колонки" -> её индекс (1-based)
HEADERS = sheet.row_values(1)
COL_INDEX = {name.strip(): i + 1 for i, name in enumerate(HEADERS)}

def col(name: str, default=None):
    """Безопасно вернуть индекс столбца по имени."""
    return COL_INDEX.get(name, default)

# Обязательные/ожидаемые колонки (под ваши заголовки)
COL_STATUS = col("Статус") or 10     # J по умолчанию
COL_DRIVER = col("Водитель") or 11   # K по умолчанию
COL_UPDATED = col("Время обновления") or 12  # L по умолчанию (можете переименовать в таблице)
COL_ADDRESS = col("Адрес доставки")
COL_ORDER_NUM = col("Номер заявки") or col("ID")  # авто-поиск номера заявки
COL_PLAN_DATETIME = col("План время дата")
COL_ITEM_NAME = col("Наименование")
COL_ITEM_QTY = col("Количество товара")
COL_PHONE = col("Телефон")
COL_VOLUME = col("Объем заказа")
COL_WEIGHT = col("Вес заказа")

def now_human():
    return datetime.now().strftime("%d.%m.%Y %H:%M")

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

# === МАРШРУТ ===
def build_route_url(lat: float, lon: float):
    """
    Если известны координаты склада — строим ORS-ссылку (start -> end).
    Иначе — отдаём Google Maps на точку назначения (от текущего местоположения водителя).
    """
    try:
        lat = float(lat); lon = float(lon)
    except Exception:
        return None

    if WAREHOUSE_LAT and WAREHOUSE_LON:
        try:
            slat = float(WAREHOUSE_LAT); slon = float(WAREHOUSE_LON)
            # ORS ожидает lon,lat
            return (
                "https://maps.openrouteservice.org/directions"
                f"?a={slon},{slat},{lon},{lat}&b=0&c=0&k1=ru-RU&k2=km&n1={lat}&n2={lon}&n3=14"
            )
        except Exception as e:
            logging.warning(f"WAREHOUSE coords некорректны: {e}")

    # Фолбэк: Google Maps
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"

def build_task_keyboard(lat: float, lon: float, row_index: int):
    route_url = build_route_url(lat, lon) or "https://www.google.com/maps"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
            InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
        ],
        [InlineKeyboardButton("📍 Маршрут", url=route_url)]
    ])

# === ГЕОКОДИНГ ===
def geocode_address(address):
    try:
        url = "https://api.openrouteservice.org/geocode/search"
        params = {"api_key": ORS_API_KEY, "text": address}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        features = response.json().get("features", [])
        if not features:
            return None, None
        coords = features[0]["geometry"]["coordinates"]  # [lon, lat]
        return coords[0], coords[1]
    except Exception as e:
        logging.error(f"Ошибка геокодинга: {e}")
        return None, None

# === РАСПРЕДЕЛЕНИЕ ЗАЯВОК ===
async def distribute_tasks(bot):
    rows = sheet.get_all_records()  # список словарей, строки начиная со 2-й
    coords_cache = {}  # row_idx -> (lon, lat)
    taken_local = set()  # чтобы в пределах одного запуска не дублировать

    # Предрасчёт координат (только для строк, где статус ещё не назначен)
    for idx, row in enumerate(rows, start=2):
        try:
            status = (row.get("Статус") or "").strip().lower()
            driver_cell = (row.get("Водитель") or "").strip()
            addr = row.get("Адрес доставки")
            if not addr:
                continue
            if status in ("выполняется", "выполнено") or driver_cell:
                continue
            lon, lat = geocode_address(addr)
            if lon and lat:
                coords_cache[idx] = (lon, lat)
        except Exception as e:
            logging.error(f"Ошибка при подготовке координат для строки {idx}: {e}")

    # Назначение по каждому водителю, с ПОВТОРНОЙ ПРОВЕРКОЙ статуса перед апдейтом
    for user_id, driver in drivers_data.items():
        total_vol = 0.0
        total_weight = 0.0
        username = driver["username"]
        assigned_requests[user_id] = []

        for idx, row in enumerate(rows, start=2):
            if idx in taken_local:
                continue

            try:
                # Быстрые геттеры из словаря (для номера заявки и параметров)
                order_no = (
                    (row.get("Номер заявки") if COL_ORDER_NUM else None)
                    or (row.get("ID") if col("ID") else None)
                    or idx  # фолбэк: номер строки
                )

                vol = float(row.get("Объем заказа", 0) or 0)
                weight = float(row.get("Вес заказа", 0) or 0)
                addr = row.get("Адрес доставки")

                if not addr:
                    continue

                # Проверка лимитов машины
                if vol + total_vol > driver["volume"] or weight + total_weight > driver["weight"]:
                    continue

                # 1) ПОВТОРНО читаем «Статус» И «Водитель» из самой таблицы (актуально на момент апдейта)
                live_status = (sheet.cell(idx, COL_STATUS).value or "").strip().lower() if COL_STATUS else ""
                live_driver = (sheet.cell(idx, COL_DRIVER).value or "").strip() if COL_DRIVER else ""

                if live_status in ("выполняется", "выполнено") or live_driver:
                    # Уже кем-то взято/выполнено
                    continue

                # 2) Нужны координаты — смотрим кэш, при отсутствии — геокодим на лету
                if idx in coords_cache:
                    lon, lat = coords_cache[idx]
                else:
                    lon, lat = geocode_address(addr)

                if not (lon and lat):
                    logging.warning(f"Не удалось геокодировать адрес строки {idx}: {addr}")
                    continue

                # 3) Обновляем таблицу (фиксируем назначение)
                when = now_human()
                if COL_STATUS:
                    sheet.update_cell(idx, COL_STATUS, "выполняется")
                if COL_DRIVER:
                    sheet.update_cell(idx, COL_DRIVER, username)
                if COL_UPDATED:
                    sheet.update_cell(idx, COL_UPDATED, when)

                total_vol += vol
                total_weight += weight
                assigned_requests[user_id].append(idx)
                taken_local.add(idx)  # не отдавать эту строку другим в текущем прогоне

                # 4) Отправляем карточку водителю
                plan_dt = row.get("План время дата", "")
                item_name = row.get("Наименование", "")
                item_qty = row.get("Количество товара", "")
                phone = row.get("Телефон", "")

                text = (
                    f"📦 Заявка №{order_no}\n"
                    f"📍 Адрес: {addr}\n"
                    f"🗓 Время: {plan_dt}\n"
                    f"📦 Товары: {item_name} x {item_qty}\n"
                    f"📞 Тел: {phone}"
                )

                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=build_task_keyboard(lat=float(lat), lon=float(lon), row_index=idx)
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

    try:
        if query.data == "refresh":
            await distribute_tasks(context.bot)
            await query.edit_message_text("🔄 Заявки перераспределены!")
            await send_daily_report(context.bot)
            return

        if query.data.startswith("done") or query.data.startswith("fail"):
            action, row_index = query.data.split(":")
            row_index = int(row_index)
            status = "выполнено" if action == "done" else "не выполнено"
            when = now_human()

            # Обновляем таблицу
            if COL_STATUS:
                sheet.update_cell(row_index, COL_STATUS, status)
            if COL_UPDATED:
                sheet.update_cell(row_index, COL_UPDATED, when)

            # Берём корректный адрес для уведомления админа
            addr_val = sheet.cell(row_index, COL_ADDRESS).value if COL_ADDRESS else "адрес не указан"

            # Номер заявки для уведомления
            order_no_val = None
            if COL_ORDER_NUM:
                order_no_val = sheet.cell(row_index, COL_ORDER_NUM).value
            if not order_no_val:
                order_no_val = row_index  # фолбэк

            text = (
                f"📨 Ответ от @{username}:\n"
                f"Заявка №{order_no_val}\n"
                f"Адрес: 📍 {addr_val}\n"
                f"Статус: {status}\n"
                f"Время: {when}"
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=text)
            await query.edit_message_text(f"Статус заявки обновлён: {status}")
    except Exception as e:
        logging.error(f"Ошибка в обработчике кнопок: {e}")
        await query.edit_message_text("⚠️ Что-то пошло не так. Попробуйте ещё раз.")

# === ЗАПУСК ===
if __name__ == "__main__":
    TOKEN = os.environ.get("BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.run_polling()
