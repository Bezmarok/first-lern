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

# 🚚 Координаты склада (как просил — через ENV, но по умолчанию выставляем фикс)
WAREHOUSE_LAT = os.environ.get("WAREHOUSE_LAT", "59.780685")
WAREHOUSE_LON = os.environ.get("WAREHOUSE_LON", "30.170815")

TIME_WINDOW_PADDING_MIN = int(os.environ.get("TW_PADDING_MIN", "45"))
DEFAULT_SERVICE_MIN = int(os.environ.get("DEFAULT_SERVICE_MIN", "10"))

# === ПАМЯТЬ БОТА ===
drivers_data = {}  # user_id -> {"volume": float, "weight": float, "username": str}
assigned_requests = defaultdict(list)

# === ЗАГОЛОВКИ И КОЛОНКИ ===
HEADERS = sheet.row_values(1)
COL_INDEX = {name.strip(): i + 1 for i, name in enumerate(HEADERS)}

def col(name: str, default=None):
    return COL_INDEX.get(name, default)

COL_STATUS       = col("Статус") or 10
COL_DRIVER       = col("Водитель") or 11
COL_UPDATED      = col("Время обновления") or 12
COL_ETA          = col("ETA")
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
    if not val:
        return None
    val = str(val).strip()
    fmts = [
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y",
        "%Y-%m-%d",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(val, f)
            if "H" not in f:
                dt = dt.replace(hour=12, minute=0)
            return dt
        except Exception:
            continue
    return None

def parse_time_window(cell_value: str, pad_minutes: int = TIME_WINDOW_PADDING_MIN):
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

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))

# === МАРШРУТНЫЕ ССЫЛКИ ===
def build_point_route_url(lat: float, lon: float):
    try:
        lat = float(lat); lon = float(lon)
    except Exception:
        return "https://www.google.com/maps"
    # если склад известен — ORS directions, иначе Google
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

# === ГЕОКОДИНГ ===
def geocode_address(address):
    """Геокодим адрес только в РФ, с фокусом на склад, чтобы не улетать в США/Европу."""
    try:
        url = "https://api.openrouteservice.org/geocode/search"
        params = {
            "api_key": ORS_API_KEY,
            "text": address,
            "boundary.country": "RU",
            "size": 1
        }
        try:
            if WAREHOUSE_LAT and WAREHOUSE_LON:
                params["focus.point.lat"] = float(WAREHOUSE_LAT)
                params["focus.point.lon"] = float(WAREHOUSE_LON)
        except Exception:
            pass

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        features = response.json().get("features", [])
        if not features:
            logger.warning(f"Геокодер не нашёл адрес: {address}")
            return None, None

        lon, lat = features[0]["geometry"]["coordinates"]  # [lon, lat]
        # Отсечём «улетевшие» координаты
        try:
            dist_km = _haversine_km(float(WAREHOUSE_LAT), float(WAREHOUSE_LON), float(lat), float(lon))
            if dist_km > 500:
                logger.warning(f"Адрес слишком далеко ({dist_km:.0f} км): {address} -> {lat},{lon}")
                return None, None
        except Exception:
            pass

        return lon, lat
    except Exception as e:
        logger.error(f"Ошибка геокодинга: {e}")
        return None, None

# === ОБРАБОТКА ЗАЯВОК ===
def build_jobs_from_sheet(rows, start_row_idx=2):
    jobs = []
    row_index_by_job_id = {}
    coords_cache = {}
    job_info = {}

    for idx, row in enumerate(rows, start=start_row_idx):
        status = (row.get("Статус") or "").strip().lower()
        driver_cell = (row.get("Водитель") or "").strip()
        addr = row.get("Адрес доставки")

        if not addr or status in ("выполняется", "выполнено") or driver_cell:
            continue

        lon, lat = geocode_address(addr)
        if not (lon and lat):
            logger.warning(f"Пропущена заявка {idx} — нет координат: {addr}")
            continue

        coords_cache[idx] = (lon, lat)

        service_min = DEFAULT_SERVICE_MIN
        if COL_SERVICE_MIN:
            service_min = int(to_float(row.get("Время сервиса (мин)"), DEFAULT_SERVICE_MIN))

        tw = parse_time_window(row.get("План время дата")) if COL_PLAN_DT else None
        vol = to_float(row.get("Объем заказа", 0))
        wgt = to_float(row.get("Вес заказа", 0))
        order_no = (row.get("Номер заявки") if COL_ORDER_NUM else None) or (row.get("ID") or idx)

        job_id = idx  # связываем с индексом строки
        job = {
            "id": job_id,
            "location": [lon, lat],           # [lon, lat]
            "service": int(service_min * 60), # сек
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

# === МАШИНЫ ===
def build_vehicles_from_drivers():
    vehicles = []
    s_lat = float(WAREHOUSE_LAT)
    s_lon = float(WAREHOUSE_LON)

    # Широкое окно: сегодня 00:00 → +3 суток 23:59
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today
    end = today + timedelta(days=3, hours=23, minutes=59)

    for user_id, drv in drivers_data.items():
        vol_cap = float(drv["volume"])
        wgt_cap = float(drv["weight"])
        vehicles.append({
            "id": int(user_id),
            "profile": "driving-car",
            "start": [s_lon, s_lat],
            "end":   [s_lon, s_lat],
            "time_window": [to_unix(start), to_unix(end)],
            "capacity": [vol_cap, wgt_cap],
            "description": drv["username"]
        })
    return vehicles

# === ЗАПРОС В ORS ===
def ors_optimize(jobs, vehicles):
    url = "https://api.openrouteservice.org/optimization"
    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "jobs": jobs,
        "vehicles": vehicles,
        "options": {"g": True}
    }

    logger.info("🚀 Отправка запроса в ORS: %d заявок, %d водителей", len(jobs), len(vehicles))
    if jobs:     logger.debug("📦 Пример job:\n%s", json.dumps(jobs[0], indent=2, ensure_ascii=False))
    if vehicles: logger.debug("🚐 Пример vehicle:\n%s", json.dumps(vehicles[0], indent=2, ensure_ascii=False))
    logger.debug("📤 Полный payload:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=90)
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ HTTP ошибка ORS: {r.status_code} - {r.text}")
        raise
    return r.json()

# === TELEGRAM UI ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧭 Оптимизировать маршруты", callback_data="optimize")],
        ])
        await update.message.reply_text("Привет, админ! Нажми кнопку:", reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "Укажи параметры машины, например:\n`2.5, 500`\n(объём м³, вес кг)",
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
        await update.message.reply_text("⚠️ Неверный формат. Пример: `2.5, 500`", parse_mode="Markdown")

def build_task_keyboard(lat: float, lon: float, row_index: int):
    route_url = build_point_route_url(lat, lon)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
            InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
        ],
        [InlineKeyboardButton("📍 Маршрут", url=route_url)]
    ])

# === ОСНОВНАЯ ОПТИМИЗАЦИЯ И РАССЫЛКА ===
async def optimize_and_assign(bot):
    rows = sheet.get_all_records()
    if not drivers_data:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет данных от водителей.")
        return

    jobs, row_index_by_job_id, coords_cache, job_info = build_jobs_from_sheet(rows)
    if not jobs:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет заявок для маршрутизации.")
        return

    vehicles = build_vehicles_from_drivers()
    if not vehicles:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет водителей.")
        return

    # Ограничения бесплатного ORS
    if len(jobs) > 50 or len(vehicles) > 3:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Превышен лимит ORS: jobs={len(jobs)} (≤50), vehicles={len(vehicles)} (≤3)."
        )
        return

    try:
        solution = ors_optimize(jobs, vehicles)
    except Exception as e:
        logger.exception("Ошибка ORS Optimization")
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка оптимизации: {e}")
        return

    routes = solution.get("routes", [])
    unassigned = solution.get("unassigned", [])
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🧠 ORS ответ: маршрутов {len(routes)}, нераспределены: {len(unassigned)}"
    )

    if not routes:
        await bot.send_message(chat_id=ADMIN_ID, text="⚠️ Маршрутов нет (все заявки могли попасть в unassigned).")
        return

    # === РАССЫЛКА ВОДИТЕЛЯМ ===
    for r in routes:
        vid = int(r["vehicle"])  # на всякий случай
        steps = r.get("steps", [])
        drv = drivers_data.get(vid)
        driver_username = drv["username"] if drv else f"id_{vid}"

        job_steps = [s for s in steps if s.get("type") == "job"]
        logger.info("📦 vehicle.id=%s, driver=%s, job_steps=%d", vid, driver_username, len(job_steps))
        await bot.send_message(chat_id=ADMIN_ID, text=f"📦 vehicle {vid} ({driver_username}): задач {len(job_steps)}")

        waypoints_latlon = []
        lines = []
        total_vol = 0.0
        total_wgt = 0.0
        per_stop_msgs = []  # (text, lat, lon, row_idx)

        for s in job_steps:
            job_id = s["job"]
            row_idx = row_index_by_job_id[job_id]
            info = job_info[job_id]
            addr = info["addr"]; order_no = info["order_no"]; vol = info["vol"]; wgt = info["wgt"]
            total_vol += vol; total_wgt += wgt

            arrival = s.get("arrival")
            eta_str = datetime.fromtimestamp(arrival).strftime("%H:%M") if arrival else ""

            lon, lat = coords_cache.get(row_idx, (None, None))
            if lon and lat:
                waypoints_latlon.append((lat, lon))

            # апдейты таблицы, если строка ещё не занята
            live_status = (sheet.cell(row_idx, COL_STATUS).value or "").strip().lower() if COL_STATUS else ""
            live_driver = (sheet.cell(row_idx, COL_DRIVER).value or "").strip() if COL_DRIVER else ""
            if not (live_status in ("выполняется", "выполнено") or live_driver):
                when = now_human()
                if COL_STATUS:  sheet.update_cell(row_idx, COL_STATUS, "выполняется")
                if COL_DRIVER:  sheet.update_cell(row_idx, COL_DRIVER, driver_username)
                if COL_UPDATED: sheet.update_cell(row_idx, COL_UPDATED, when)
                if COL_ETA and arrival: sheet.update_cell(row_idx, COL_ETA, eta_str)

            lines.append(f"• №{order_no} — {addr} (ETA {eta_str})")

            row = rows[row_idx - 2]
            plan_dt   = row.get("План время дата", "")
            item_name = row.get("Наименование", "")
            item_qty  = row.get("Количество товара", "")
            phone     = row.get("Телефон", "")
            point_text = (
                f"📦 Заявка №{order_no}\n"
                f"📍 Адрес: {addr}\n"
                f"🗓 Время: {plan_dt}\n"
                f"⏱ ETA: {eta_str}\n"
                f"📦 Товары: {item_name} x {item_qty}\n"
                f"📞 Тел: {phone}"
            )
            per_stop_msgs.append((point_text, lat, lon, row_idx))

        # отправка
        if len(job_steps) == 0:
            # водителю всё равно пишем "пусто"
            try:
                await bot.send_message(chat_id=vid, text="🧭 На сегодня заявок не назначено.")
                await bot.send_message(chat_id=ADMIN_ID, text=f"ℹ️ {driver_username} (id={vid}) — 0 задач.")
            except Exception as e:
                logger.error(f"Не удалось написать водителю {vid}: {e}")
                await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Не смогли написать {driver_username} (id={vid}). Ошибка: {e}")
        else:
            # сводка + маршрут
            route_text = (
                "🧭 Оптимальный маршрут на сегодня:\n" +
                "\n".join(lines) +
                f"\n\nИтого погрузка: объём {total_vol:.1f} / вес {total_wgt:.1f}"
            )
            route_link = build_multistop_ors_url(waypoints_latlon) if waypoints_latlon else "https://maps.openrouteservice.org"
            route_text += f"\n📍 Открыть маршрут: {route_link}"

            try:
                await bot.send_message(chat_id=vid, text=route_text)
                await bot.send_message(chat_id=ADMIN_ID, text=f"✅ Сводка отправлена {driver_username} (id={vid})")
            except Exception as e:
                logger.error(f"Не удалось отправить маршрут водителю {vid}: {e}")
                await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Не удалось написать {driver_username} (id={vid}). Ошибка: {e}\n\n{route_text}")

            for pt_text, lat, lon, row_idx in per_stop_msgs:
                try:
                    await bot.send_message(chat_id=vid, text=pt_text,
                                           reply_markup=build_task_keyboard(lat, lon, row_idx))
                except Exception as e:
                    logger.error(f"Не удалось отправить карточку точки {row_idx} водителю {vid}: {e}")
                    await bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"⚠️ Карточка {row_idx} не доставлена {driver_username} (id={vid}). Ошибка: {e}\n\n{pt_text}"
                    )

    # Итог админу
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Оптимизация завершена. Маршруты: {len(routes)}. Не распределены: {len(unassigned)}."
    )

# === КНОПКИ ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "optimize":
        await optimize_and_assign(context.bot)
        await query.edit_message_text("🔄 Маршруты построены и разосланы!")

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
