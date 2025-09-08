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
sheet = client.open(SHEET_NAME).sheet1

# === ОКРУЖЕНИЕ ===
ORS_API_KEY = os.environ.get("ORS_API_KEY")
if not ORS_API_KEY:
    raise RuntimeError("Не задана переменная окружения ORS_API_KEY")

ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "257300241"))

WAREHOUSE_LAT = os.environ.get("WAREHOUSE_LAT", "59.780685")
WAREHOUSE_LON = os.environ.get("WAREHOUSE_LON", "30.170815")

TIME_WINDOW_PADDING_MIN = int(os.environ.get("TW_PADDING_MIN", "45"))
DEFAULT_SERVICE_MIN = int(os.environ.get("DEFAULT_SERVICE_MIN", "10"))

# === ПАМЯТЬ БОТА ===
drivers_data = {}  # user_id -> {"volume": float, "weight": float, "username": str, "car_plate": str}
assigned_requests = defaultdict(list)

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
        "%d.%м.%Y",
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

# === МАРШРУТНЫЕ ССЫЛКИ (Google Maps) ===
def build_google_maps_multistop(latlon_list):
    """latlon_list = [(lat, lon), ...] — первая точка будет склад."""
    if not latlon_list:
        return "https://www.google.com/maps"
    try:
        origin = f"{latlon_list[0][0]},{latlon_list[0][1]}"
        destination = f"{latlon_list[-1][0]},{latlon_list[-1][1]}"
        waypoints = "|".join([f"{lat},{lon}" for lat, lon in latlon_list[1:-1]]) if len(latlon_list) > 2 else ""
        url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}"
        if waypoints:
            url += f"&waypoints={waypoints}"
        return url
    except Exception:
        return "https://www.google.com/maps"

def build_point_route_url(lat: float, lon: float):
    try:
        return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
    except Exception:
        return "https://www.google.com/maps"

# === ГЕОКОДИНГ ===
def geocode_address(address):
    try:
        url = "https://api.openrouteservice.org/geocode/search"
        params = {
            "api_key": ORS_API_KEY,
            "text": address,
            "boundary.country": "RU",
            "size": 1
        }
        if WAREHOUSE_LAT and WAREHOUSE_LON:
            params["focus.point.lat"] = float(WAREHOUSE_LAT)
            params["focus.point.lon"] = float(WAREHOUSE_LON)

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        features = response.json().get("features", [])
        if not features:
            logger.warning(f"Геокодер не нашёл адрес: {address}")
            return None, None

        lon, lat = features[0]["geometry"]["coordinates"]
        return lon, lat
    except Exception as e:
        logger.error(f"Ошибка геокодинга: {e}")
        return None, None

# === JOBS ===
def build_jobs_from_sheet(rows, start_row_idx=2):
    jobs = []
    row_index_by_job_id = {}
    coords_cache = {}
    job_info = {}

    for idx, row in enumerate(rows, start=start_row_idx):
        status = (row.get("Статус") or "").strip().lower()
        driver_cell = (row.get("Водитель") or "").strip()
        addr = row.get("Адрес доставки")

        # берём только свободные/не назначенные заявки
        if not addr or status in ("выполняется", "выполнено", "не выполнено") or driver_cell:
            continue

        lon, lat = geocode_address(addr)
        if not (lon and lat):
            logger.warning(f"Пропущена заявка {idx} — нет координат: {addr}")
            continue

        coords_cache[idx] = (float(lon), float(lat))

        service_min = DEFAULT_SERVICE_MIN
        if COL_SERVICE_MIN:
            service_min = int(to_float(row.get("Время сервиса (мин)"), DEFAULT_SERVICE_MIN))

        tw = parse_time_window(row.get("План время дата")) if COL_PLAN_DT else None
        vol = to_float(row.get("Объем заказа", 0))
        wgt = to_float(row.get("Вес заказа", 0))

        order_no = row.get("НОМЕР заявки") or row.get("Номер заявки") or row.get("ID") or idx
        job_id = idx  # технический int для ORS

        job = {
            "id": job_id,
            "location": [float(lon), float(lat)],  # [lon, lat]
            "service": int(service_min * 60),
            "amount": [float(vol), float(wgt)],
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

# === VEHICLES ===
def build_vehicles_from_drivers():
    vehicles = []
    s_lat = float(WAREHOUSE_LAT)
    s_lon = float(WAREHOUSE_LON)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today
    end = today + timedelta(days=3, hours=23, minutes=59)

    for user_id, drv in drivers_data.items():
        vol_cap = float(drv["volume"])
        wgt_cap = float(drv["weight"])
        vehicles.append({
            "id": int(user_id),
            "profile": "driving-car",
            "start": [float(s_lon), float(s_lat)],
            "end":   [float(s_lon), float(s_lat)],
            "time_window": [to_unix(start), to_unix(end)],
            "capacity": [float(vol_cap), float(wgt_cap)],
            "description": drv["username"]
        })
    return vehicles

def extract_unassigned_ids(unassigned):
    ids = []
    if isinstance(unassigned, dict) and "jobs" in unassigned:
        arr = unassigned.get("jobs", [])
    else:
        arr = unassigned if isinstance(unassigned, list) else []
    for j in arr:
        if isinstance(j, dict) and "id" in j:
            ids.append(int(j["id"]))
        elif isinstance(j, int):
            ids.append(j)
    return ids

# === ORS ===
def ors_optimize(jobs, vehicles):
    url = "https://api.openrouteservice.org/optimization"
    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {"jobs": jobs, "vehicles": vehicles, "options": {"g": True}}
    logging.debug("📤 Payload в ORS:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=90)
    r.raise_for_status()
    return r.json()

# === TELEGRAM UI ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧭 Распределить маршруты", callback_data="optimize")],
        ])
        await update.message.reply_text("Привет, админ! Нажми кнопку:", reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "Укажи параметры машины, например:\n`2.5, 500, А123ВС78`\n(объём м³, вес кг, госномер)",
            parse_mode="Markdown"
        )

async def handle_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str, weight_str, plate_str = update.message.text.split(",")
        user_id = update.effective_user.id
        username = update.effective_user.username or f"id_{user_id}"
        drivers_data[user_id] = {
            "volume": float(volume_str.strip().replace(",", ".")),
            "weight": float(weight_str.strip().replace(",", ".")),
            "username": username,
            "car_plate": plate_str.strip()
        }
        await update.message.reply_text("✅ Данные сохранены. Ждите назначение заявок.")
    except Exception as e:
        logger.error(f"Ошибка регистрации водителя: {e}")
        await update.message.reply_text("⚠️ Неверный формат. Пример: `2.5, 500, А123ВС78`", parse_mode="Markdown")

def build_task_keyboard(lat: float, lon: float, row_index: int):
    route_url = build_point_route_url(lat, lon)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
            InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
        ],
        [InlineKeyboardButton("📍 Маршрут", url=route_url)]
    ])

# === ОСНОВНАЯ ОПТИМИЗАЦИЯ (итерации 1→2→3 машины) ===
async def optimize_and_assign(bot):
    rows = sheet.get_all_records()
    if not drivers_data:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет данных от водителей.")
        return

    jobs, row_index_by_job_id, coords_cache, job_info = build_jobs_from_sheet(rows)
    if not jobs:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет заявок для маршрутизации.")
        return

    vehicles_all = build_vehicles_from_drivers()
    if not vehicles_all:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет водителей.")
        return

    # Сортируем по вместимости (объём, вес) убыв.
    vehicles_sorted = sorted(vehicles_all, key=lambda v: (v["capacity"][0], v["capacity"][1]), reverse=True)

    best = None  # (count_unassigned, k, solution, used_vehicles)
    max_k = min(len(vehicles_sorted), 3)
    for k in range(1, max_k + 1):
        used = vehicles_sorted[:k]
        try:
            sol = ors_optimize(jobs, used)
        except Exception as e:
            await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка оптимизации (k={k}): {e}")
            return
        unas_ids = extract_unassigned_ids(sol.get("unassigned", []))
        cand = (len(unas_ids), k, sol, used)
        # Лучшая — где unassigned меньше. Если 0 — сразу берём
        if best is None or cand[0] < best[0]:
            best = cand
        if len(unas_ids) == 0:
            break

    unassigned_cnt, used_k, solution, used_vehicles = best
    routes = solution.get("routes", [])
    unassigned_raw = solution.get("unassigned", [])
    unassigned_ids = extract_unassigned_ids(unassigned_raw)

    # Сообщаем админу, какие номера остались нераспределены
    if unassigned_ids:
        unas_numbers = []
        for jid in unassigned_ids:
            idx = row_index_by_job_id.get(jid)
            if idx:
                unas_numbers.append(str(job_info[jid]["order_no"]))
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Нераспределены: {', '.join(unas_numbers)}")
    else:
        await bot.send_message(chat_id=ADMIN_ID, text=f"🧠 ORS разложил всё на {used_k} машине(ах).")

    # Собираем map vehicle -> job steps
    routes_by_vehicle = {int(r["vehicle"]): [s for s in r.get("steps", []) if s.get("type") == "job"] for r in routes}
    # Если каких-то машин нет в ответе — добавим с пустыми задачами (чтобы отправить «заявок нет»)
    for v in used_vehicles:
        routes_by_vehicle.setdefault(v["id"], [])

    # === Рассылка водителям и обновление таблицы ТОЛЬКО для назначенных заявок ===
    for vid, job_steps in routes_by_vehicle.items():
        drv = drivers_data.get(vid)
        driver_username = drv["username"] if drv else f"id_{vid}"

        waypoints = []  # (lat, lon)
        lines = []
        total_vol = 0.0
        total_wgt = 0.0
        per_stop_msgs = []

        for s in job_steps:
            job_id = int(s["job"])
            row_idx = row_index_by_job_id.get(job_id)
            if not row_idx:
                continue

            info = job_info[job_id]
            addr = info["addr"]
            order_no = info["order_no"]
            vol = info["vol"]; wgt = info["wgt"]
            total_vol += vol; total_wgt += wgt

            arrival = s.get("arrival")
            eta_str = datetime.fromtimestamp(arrival).strftime("%H:%M") if arrival else ""

            lon, lat = coords_cache.get(row_idx, (None, None))
            if lon and lat:
                waypoints.append((float(lat), float(lon)))

            # Обновления — только назначенным
            if COL_STATUS:  sheet.update_cell(row_idx, COL_STATUS, "выполняется")
            if COL_DRIVER:  sheet.update_cell(row_idx, COL_DRIVER, driver_username)
            if COL_UPDATED: sheet.update_cell(row_idx, COL_UPDATED, now_human())
            if COL_ETA and arrival: sheet.update_cell(row_idx, COL_ETA, eta_str)
            if COL_CAR_PLATE and drv: sheet.update_cell(row_idx, COL_CAR_PLATE, drv.get("car_plate", ""))

            lines.append(f"• №{order_no} — {addr}" + (f" (ETA {eta_str})" if eta_str else ""))

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
                f"📞 Тел: {phone}\n"
                f"🚘 Госномер: {drv.get('car_plate', '') if drv else ''}"
            )
            per_stop_msgs.append((point_text, float(lat) if lat else None, float(lon) if lon else None, row_idx))

        # Стартуем маршрут со склада
        try:
            s_lat = float(WAREHOUSE_LAT); s_lon = float(WAREHOUSE_LON)
            full_latlon = [(s_lat, s_lon)] + waypoints if waypoints else [(s_lat, s_lon)]
        except Exception:
            full_latlon = waypoints[:]

        route_text = "🧭 Оптимальный маршрут на сегодня:\n"
        route_text += ("\n".join(lines) if lines else "На сегодня заявок нет")
        if lines:
            route_text += f"\n\nИтого погрузка: объём {total_vol:.1f} / вес {total_wgt:.1f}"
        route_text += f"\n📍 Открыть маршрут: {build_google_maps_multistop(full_latlon)}"

        try:
            await bot.send_message(chat_id=vid, text=route_text)
        except Exception as e:
            logger.error(f"Не удалось отправить маршрут водителю {vid}: {e}")
            await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Не смогли отправить {driver_username} (id={vid}).")

        for pt_text, lat, lon, row_idx in per_stop_msgs:
            try:
                await bot.send_message(chat_id=vid, text=pt_text,
                                       reply_markup=build_task_keyboard(lat, lon, row_idx))
            except Exception as e:
                logger.error(f"Не удалось отправить карточку точки {row_idx} водителю {vid}: {e}")

    # Итог админу + кнопка «распределить повторно»
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Распределить повторно", callback_data="optimize_again")]])
    await bot.send_message(chat_id=ADMIN_ID,
                           text=f"✅ Готово. Маршрутов: {len(routes)}. Нераспределены: {len(unassigned_ids)}.",
                           reply_markup=kb)

# === КНОПКИ ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data in ("optimize", "optimize_again"):
        await optimize_and_assign(context.bot)
        try:
            await query.edit_message_text("🔄 Маршруты построены и разосланы!")
        except Exception:
            pass
        return

    # Обработка статусов
    try:
        if data.startswith("done:") or data.startswith("fail:"):
            row_idx = int(data.split(":")[1])
            is_done = data.startswith("done:")
            status_value = "выполнено" if is_done else "не выполнено"

            if COL_STATUS:  sheet.update_cell(row_idx, COL_STATUS, status_value)
            if COL_UPDATED: sheet.update_cell(row_idx, COL_UPDATED, now_human())
            if not is_done:
                # очистим водителя и ETA, чтобы заявка снова попала в распределение
                if COL_DRIVER: sheet.update_cell(row_idx, COL_DRIVER, "")
                if COL_ETA:    sheet.update_cell(row_idx, COL_ETA, "")

            # снимаем клавиатуру у сообщения
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            await query.message.reply_text(f"✅ Статус обновлён: {status_value.upper()} (строка {row_idx})")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.run_polling()
