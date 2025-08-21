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

# === ЛОГИ ===
logging.basicConfig(level=logging.INFO)
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
sheet = client.open(SHEET_NAME).sheet1

# === ОКРУЖЕНИЕ ===
ORS_API_KEY = os.environ.get("ORS_API_KEY")
if not ORS_API_KEY:
    raise RuntimeError("Не задана переменная окружения ORS_API_KEY")

ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "257300241"))

WAREHOUSE_LAT = os.environ.get("WAREHOUSE_LAT")  # строка или None
WAREHOUSE_LON = os.environ.get("WAREHOUSE_LON")

# Настройка дефолтной «подушки» для временных окон (минуты)
TIME_WINDOW_PADDING_MIN = int(os.environ.get("TW_PADDING_MIN", "45"))
DEFAULT_SERVICE_MIN = int(os.environ.get("DEFAULT_SERVICE_MIN", "10"))

# === ПАМЯТЬ БОТА ===
drivers_data = {}  # user_id -> {"volume": float, "weight": float, "username": str}
assigned_requests = defaultdict(list)

# === КАРТА ИНДЕКСОВ КОЛОНОК ===
HEADERS = sheet.row_values(1)
COL_INDEX = {name.strip(): i + 1 for i, name in enumerate(HEADERS)}

def col(name: str, default=None):
    return COL_INDEX.get(name, default)

COL_STATUS       = col("Статус") or 10           # J
COL_DRIVER       = col("Водитель") or 11         # K
COL_UPDATED      = col("Время обновления") or 12 # L
COL_ETA          = col("ETA")                     # (опционально)
COL_ADDRESS      = col("Адрес доставки")
COL_ORDER_NUM    = col("Номер заявки") or col("ID")
COL_PLAN_DT      = col("План время дата")
COL_ITEM_NAME    = col("Наименование")
COL_ITEM_QTY     = col("Количество товара")
COL_PHONE        = col("Телефон")
COL_VOLUME       = col("Объем заказа")
COL_WEIGHT       = col("Вес заказа")
COL_SERVICE_MIN  = col("Время сервиса (мин)")

# === УТИЛИТЫ ===
def now_human():
    return datetime.now().strftime("%d.%m.%Y %H:%M")

def to_unix(dt: datetime) -> int:
    return int(dt.timestamp())

def try_parse_datetime(val: str):
    """
    Пытаемся понять 'План время дата' в нескольких форматах.
    Вернём None, если не получилось.
    """
    if not val:
        return None
    val = str(val).strip()
    fmts = [
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y",  # если без времени — возьмём 12:00
        "%Y-%m-%d",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(val, f)
            if "H" not in f:
                dt = dt.replace(hour=12, minute=0)  # середина дня
            return dt
        except Exception:
            continue
    return None

def parse_time_window(cell_value: str, pad_minutes: int = TIME_WINDOW_PADDING_MIN):
    """
    '21.08.2025 12:00' -> [11:15, 12:45] (в секундах epoch), если pad_minutes=45
    """
    dt = try_parse_datetime(cell_value)
    if not dt:
        return None
    start = dt - timedelta(minutes=pad_minutes)
    end = dt + timedelta(minutes=pad_minutes)
    return [to_unix(start), to_unix(end)]

def to_float(val, default=0.0):
    if val is None:
        return default
    try:
        if isinstance(val, (int, float)):
            return float(val)
        return float(str(val).replace(",", ".").strip() or default)
    except Exception:
        return default

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
        lon, lat = features[0]["geometry"]["coordinates"]  # [lon, lat]
        return lon, lat
    except Exception as e:
        logger.error(f"Ошибка геокодинга: {e}")
        return None, None

# === ССЫЛКИ НА МАРШРУТ ===
def build_point_route_url(lat: float, lon: float):
    """
    Для карточки единичной точки — удобнее Google (едет «от текущего местоположения»).
    Если задан склад — отдадим ORS start->end.
    """
    try:
        lat = float(lat); lon = float(lon)
    except Exception:
        return "https://www.google.com/maps"

    if WAREHOUSE_LAT and WAREHOUSE_LON:
        try:
            slat = float(WAREHOUSE_LAT); slon = float(WAREHOUSE_LON)
            return (
                "https://maps.openrouteservice.org/directions"
                f"?a={slon},{slat},{lon},{lat}&b=0&c=0&k1=ru-RU&k2=km&n1={lat}&n2={lon}&n3=14"
            )
        except Exception:
            pass
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"

def build_multistop_ors_url(latlon_list):
    """
    Много остановок: ORS принимает a=lon,lat,lon,lat,...
    ВНИМАНИЕ: слишком длинные URL могут не открываться в некоторых клиентах.
    """
    try:
        pairs = []
        for (lat, lon) in latlon_list:
            pairs.append(f"{lon},{lat}")
        a_param = ",".join(pairs)
        center_lat = latlon_list[0][0]
        center_lon = latlon_list[0][1]
        return (
            "https://maps.openrouteservice.org/directions"
            f"?a={a_param}&b=0&c=0&k1=ru-RU&k2=km&n1={center_lat}&n2={center_lon}&n3=12"
        )
    except Exception:
        return "https://maps.openrouteservice.org"

# === ORS OPTIMIZATION ===
def build_jobs_from_sheet(rows, start_row_idx=2):
    jobs = []
    row_index_by_job_id = {}   # job_id -> индекс строки в листе
    coords_cache = {}          # row_idx -> (lon, lat)
    job_info = {}              # job_id -> полезные данные для сообщений

    for idx, row in enumerate(rows, start=start_row_idx):
        status = (row.get("Статус") or "").strip().lower()
        driver_cell = (row.get("Водитель") or "").strip()
        addr = row.get("Адрес доставки")

        if not addr:
            continue
        if status in ("выполняется", "выполнено") or driver_cell:
            continue

        lon, lat = geocode_address(addr)
        if not (lon and lat):
            logger.warning(f"Не удалось геокодировать строку {idx}: {addr}")
            continue

        coords_cache[idx] = (lon, lat)

        service_min = DEFAULT_SERVICE_MIN
        if COL_SERVICE_MIN:
            service_min = int(to_float(row.get("Время сервиса (мин)"), DEFAULT_SERVICE_MIN))

        tw = parse_time_window(row.get("План время дата")) if COL_PLAN_DT else None

        vol = to_float(row.get("Объем заказа", 0))
        wgt = to_float(row.get("Вес заказа", 0))

        order_no = (row.get("Номер заявки") if COL_ORDER_NUM else None) or \
                   (row.get("ID") if col("ID") else None) or idx

        job_id = idx  # связываем задачу с номером строки
        job = {
            "id": job_id,
            "location": [lon, lat],     # [lon, lat]
            "service": int(service_min * 60),  # в секундах
            "amount": [vol, wgt],
            "description": str(order_no)
        }
        if tw:
            job["time_windows"] = [tw]

        jobs.append(job)
        row_index_by_job_id[job_id] = idx
        job_info[job_id] = {
            "addr": addr,
            "order_no": order_no,
            "vol": vol,
            "wgt": wgt
        }

    return jobs, row_index_by_job_id, coords_cache, job_info

def build_vehicles_from_drivers():
    vehicles = []
    if WAREHOUSE_LAT and WAREHOUSE_LON:
        s_lat = float(WAREHOUSE_LAT)
        s_lon = float(WAREHOUSE_LON)
    else:
        # Без склада — не идеально, но пусть будет (0,0)
        s_lat = 0.0
        s_lon = 0.0

    # Рабочее окно по умолчанию: сегодня 09:00–21:00
    today = datetime.now()
    start = today.replace(hour=9, minute=0, second=0, microsecond=0)
    end = today.replace(hour=21, minute=0, second=0, microsecond=0)
    start_unix = to_unix(start)
    end_unix = to_unix(end)

    for user_id, drv in drivers_data.items():
        vol_cap = float(drv["volume"])
        wgt_cap = float(drv["weight"])
        vehicles.append({
            "id": int(user_id),
            "profile": "driving-car",
            "start": [s_lon, s_lat],
            "end": [s_lon, s_lat],
            "time_window": [start_unix, end_unix],
            "capacity": [vol_cap, wgt_cap],
            "description": drv["username"]
        })
    return vehicles

def ors_optimize(jobs, vehicles):
    url = "https://api.openrouteservice.org/optimization"
    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "jobs": jobs,
        "vehicles": vehicles,
        "options": {"g": True}  # попросим геометрию (на будущее)
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=90)
    r.raise_for_status()
    return r.json()

# === TELEGRAM UI ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧭 Оптимизировать маршруты", callback_data="optimize")],
        ])
        await update.message.reply_text(
            "Привет, админ! Нажми кнопку, чтобы построить маршрут(ы) и раздать заявки:",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            "Привет! Укажи параметры машины в формате:\n\n"
            "`2.5, 500`\n\n"
            "где:\n"
            "- 2.5 = объём в м³\n"
            "- 500 = вес в кг",
            parse_mode="Markdown"
        )

async def handle_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str, weight_str = update.message.text.split(",")
        user_id = update.effective_user.id
        username = update.effective_user.username or f"id_{user_id}"
        drivers_data[user_id] = {
            "volume": float(volume_str.strip().replace(",", ".")),
            "weight": float(weight_str.strip().replace(",", ".")),
            "username": username
        }
        await update.message.reply_text("✅ Данные сохранены. Ждите назначение заявок.")
    except Exception as e:
        logger.error(f"Ошибка регистрации водителя: {e}")
        await update.message.reply_text("⚠️ Неверный формат. Пример: 2.5, 500", parse_mode="Markdown")

def build_task_keyboard(lat: float, lon: float, row_index: int):
    route_url = build_point_route_url(lat, lon)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
            InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
        ],
        [InlineKeyboardButton("📍 Маршрут", url=route_url)]
    ])

async def optimize_and_assign(bot):
    rows = sheet.get_all_records()
    if not drivers_data:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Сначала пусть водители пришлют параметры машины (например: 2.5, 500).")
        return

    jobs, row_index_by_job_id, coords_cache, job_info = build_jobs_from_sheet(rows)
    if not jobs:
        await bot.send_message(chat_id=ADMIN_ID, text="Нет доступных заявок для оптимизации.")
        return

    vehicles = build_vehicles_from_drivers()
    if not vehicles:
        await bot.send_message(chat_id=ADMIN_ID, text="Нет зарегистрированных водителей.")
        return

    # Запрос в ORS Optimization
    try:
        solution = ors_optimize(jobs, vehicles)
    except Exception as e:
        logger.exception("Ошибка ORS Optimization")
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка оптимизации: {e}")
        return

    routes = solution.get("routes", [])
    unassigned = solution.get("unassigned", [])
    unassigned_count = len(unassigned)

    # Для каждого маршрута → водителю сводка + карточки задач. Таблицу обновляем.
    for r in routes:
        vid = r["vehicle"]
        steps = r.get("steps", [])
        drv = drivers_data.get(vid)
        driver_username = drv["username"] if drv else f"id_{vid}"

        # Список точек для ссылки ORS
        waypoints_latlon = []

        # Сбор текста и апдейты таблицы
        lines = []
        total_vol = 0.0
        total_wgt = 0.0
        per_stop_msgs = []  # (text, lat, lon, row_idx)

        for s in steps:
            if s.get("type") != "job":
                continue

            job_id = s["job"]
            row_idx = row_index_by_job_id[job_id]
            info = job_info[job_id]
            addr = info["addr"]
            order_no = info["order_no"]
            vol = info["vol"]
            wgt = info["wgt"]

            total_vol += vol
            total_wgt += wgt

            arrival = s.get("arrival")
            eta_str = datetime.fromtimestamp(arrival).strftime("%H:%M") if arrival else ""

            # координаты
            lon, lat = coords_cache.get(row_idx, (None, None))
            if lon and lat:
                waypoints_latlon.append((lat, lon))

            # Апдейт таблицы (повторная проверка статуса перед записью)
            live_status = (sheet.cell(row_idx, COL_STATUS).value or "").strip().lower() if COL_STATUS else ""
            live_driver = (sheet.cell(row_idx, COL_DRIVER).value or "").strip() if COL_DRIVER else ""
            if live_status in ("выполняется", "выполнено") or live_driver:
                # Уже кем-то занято — пропускаем эту строку
                continue

            when = now_human()
            if COL_STATUS:
                sheet.update_cell(row_idx, COL_STATUS, "выполняется")
            if COL_DRIVER:
                sheet.update_cell(row_idx, COL_DRIVER, driver_username)
            if COL_UPDATED:
                sheet.update_cell(row_idx, COL_UPDATED, when)
            if COL_ETA and arrival:
                sheet.update_cell(row_idx, COL_ETA, eta_str)

            # Строка для сводки
            lines.append(f"• №{order_no} — {addr} (ETA {eta_str})")

            # Текст карточки для отдельной точки
            row = rows[row_idx - 2]  # потому что rows начинается со 2-й строки
            plan_dt = row.get("План время дата", "")
            item_name = row.get("Наименование", "")
            item_qty = row.get("Количество товара", "")
            phone = row.get("Телефон", "")

            point_text = (
                f"📦 Заявка №{order_no}\n"
                f"📍 Адрес: {addr}\n"
                f"🗓 Время: {plan_dt}\n"
                f"⏱ ETA: {eta_str}\n"
                f"📦 Товары: {item_name} x {item_qty}\n"
                f"📞 Тел: {phone}"
            )
            per_stop_msgs.append((point_text, lat, lon, row_idx))

        # Сводное сообщение водителю
        if lines:
            route_text = (
                "🧭 Оптимальный маршрут на сегодня:\n"
                + "\n".join(lines) +
                f"\n\nИтого погрузка: объём {total_vol:.1f} / вес {total_wgt:.1f}"
            )

            route_link = build_multistop_ors_url(waypoints_latlon) if waypoints_latlon else "https://maps.openrouteservice.org"
            route_text += f"\n📍 Открыть маршрут: {route_link}"

            try:
                await bot.send_message(chat_id=vid, text=route_text)
            except Exception as e:
                logger.error(f"Не удалось отправить маршрут водителю {vid}: {e}")
                # если водителя нет в чате — отправим админу
                await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Не удалось написать водителю {driver_username}. Маршрут отправлен админу.\n\n{route_text}")

            # Отдельные карточки на точки с кнопками
            for pt_text, lat, lon, row_idx in per_stop_msgs:
                try:
                    await bot.send_message(
                        chat_id=vid,
                        text=pt_text,
                        reply_markup=build_task_keyboard(lat, lon, row_idx)
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить карточку точки {row_idx} водителю {vid}: {e}")

    # Итог администратору
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Оптимизация завершена. Маршруты: {len(routes)}. Не распределены: {unassigned_count}."
    )

# === ОТЧЁТ АДМИНУ (простая статистика) ===
async def send_daily_report(bot):
    text = "🧾 Отчёт по задачам:\n"
    for user_id, tasks in assigned_requests.items():
        name = f"[{user_id}](tg://user?id={user_id})"
        text += f"\n{name}: {len(tasks)} задач"
    await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown")

# === КНОПКИ ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    username = user.username or f"id_{user.id}"

    try:
        if query.data == "optimize":
            await optimize_and_assign(context.bot)
            await query.edit_message_text("🔄 Маршруты построены и разосланы!")
            return

        if query.data.startswith("done") or query.data.startswith("fail"):
            action, row_index = query.data.split(":")
            row_index = int(row_index)
            status = "выполнено" if action == "done" else "не выполнено"
            when = now_human()

            if COL_STATUS:
                sheet.update_cell(row_index, COL_STATUS, status)
            if COL_UPDATED:
                sheet.update_cell(row_index, COL_UPDATED, when)

            # Безопасно достанем адрес и номер заявки для уведомления
            addr_val = sheet.cell(row_index, COL_ADDRESS).value if COL_ADDRESS else "адрес не указан"
            order_no_val = None
            if COL_ORDER_NUM:
                order_no_val = sheet.cell(row_index, COL_ORDER_NUM).value
            if not order_no_val:
                order_no_val = row_index

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
        logger.error(f"Ошибка в обработчике кнопок: {e}")
        await query.edit_message_text("⚠️ Что-то пошло не так. Попробуйте ещё раз.")

# === ЗАПУСК ===
if __name__ == "__main__":
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.run_polling()
