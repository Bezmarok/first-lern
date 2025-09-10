import logging
import os
import json
import re
import tempfile
from datetime import datetime, timedelta
from collections import defaultdict
from math import radians, sin, cos, asin, sqrt

import gspread
import pandas as pd
import requests
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)

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
sheet = client.open(SHEET_NAME).sheet1  # sheet1 как и просили

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
drivers_data = {}            # user_id -> {"volume": float, "weight": float, "username": str, "car_plate": str}
assigned_requests = defaultdict(list)
prev_leg_km_by_row = {}      # row_idx -> float (сегментный пробег)

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
COL_ITEM_NAME    = col("наименование") or col("Наименование")
COL_ITEM_QTY     = col("Количество товара")
COL_PHONE        = col("Телефон")
COL_VOLUME       = col("Объем заказа")
COL_WEIGHT       = col("Вес заказа")
COL_SERVICE_MIN  = col("Время сервиса (мин)")
COL_CAR_PLATE    = col("Гос номер")
COL_DISTANCE_N   = 14  # колонка «Километры»

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
        "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y", "%Y-%m-%d",
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
    dlat = radians(lat2 - lat1); dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))

def scale_volume_m3_to_units(vol_m3: float) -> int:
    try: v = float(vol_m3)
    except Exception: v = 0.0
    return max(0, int(round(v * VOLUME_SCALE)))

def scale_weight_kg_to_units(w_kg: float) -> int:
    try: w = float(w_kg)
    except Exception: w = 0.0
    return max(0, int(round(w * WEIGHT_SCALE)))

# === МАРШРУТНЫЕ ССЫЛКИ ===
def build_google_maps_multistop(latlon_list):
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
        params = {"api_key": ORS_API_KEY, "text": address, "boundary.country": "RU", "size": 1}
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

# === Номер заявки строго из колонки A ===
def order_no_from_col_A(row_idx: int) -> str:
    try:
        val = sheet.cell(row_idx, 1).value  # Колонка A
        return str(val).strip() if val is not None else str(row_idx)
    except Exception as e:
        logger.error(f"Не смогли прочитать колонку A для строки {row_idx}: {e}")
        return str(row_idx)

# === ЧТЕНИЕ EXCEL (любой зоопарк) ===
def read_excel_flex(path: str, filename: str) -> list[pd.DataFrame]:
    """
    Возвращает список DataFrame (по каждому листу), прочитанных без заголовка.
    Никаких .str у Series — только чистые списки и Index.
    """
    ext = os.path.splitext(filename.lower())[1]
    engine = "openpyxl" if ext == ".xlsx" else None
    try:
        xls = pd.ExcelFile(path, engine=engine)
    except Exception:
        xls = pd.ExcelFile(path)

    dfs = []
    for sheet_name in xls.sheet_names:
        try:
            df = xls.parse(sheet_name, header=None, dtype=object)
            dfs.append(df)
        except Exception as e:
            logger.warning(f"Лист '{sheet_name}' пропущен: {e}")
    return dfs

def detect_header_row(df: pd.DataFrame) -> int:
    """
    Находим строку, где реально лежат заголовки.
    Критерий: ≥2 совпадений по известным названиям (без .str — работаем с Python-строками).
    """
    keys = {
        "Номер заявки", "Номер документа продажи", "Адрес доставки",
        "Телефон клиента", "Список товаров", "Кол-во товара", "Количество",
        "Дата доставки", "Вид перевозки"
    }
    # Прямое совпадение
    for i in range(min(80, len(df))):
        row = [("" if pd.isna(x) else str(x)).strip() for x in df.iloc[i].tolist()]
        if sum(1 for c in row if c in keys) >= 2:
            return i
    # Эвристика по словам
    for i in range(min(160, len(df))):
        row = [("" if pd.isna(x) else str(x)).strip().lower() for x in df.iloc[i].tolist()]
        if any(("номер" in c and "заяв" in c) for c in row):
            return i
    return 0

def _make_headers_from_row(row_list: list[str]) -> pd.Index:
    """Делаем индекс заголовков из списка строк + обеспечиваем уникальность имён."""
    cleaned = []
    for x in row_list:
        s = ("" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x))
        s = re.sub(r"[\r\n\t]+", " ", s).strip()
        cleaned.append(s)
    # уникальность
    seen = {}
    unique = []
    for name in cleaned:
        base = name or "col"
        if base not in seen:
            seen[base] = 1
            unique.append(base)
        else:
            seen[base] += 1
            unique.append(f"{base}_{seen[base]}")
    return pd.Index(unique)

def build_import_dataframe(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    1) Находит строку заголовков.
    2) Делает fill down (вместо объединённых ячеек).
    3) Чистит номер заявки (оставляет только цифры).
    4) Приводит к колонкам Google-таблицы бота.
    """
    if df_raw.empty:
        return pd.DataFrame()

    hdr = detect_header_row(df_raw)
    header_row = df_raw.iloc[hdr].tolist()
    headers = _make_headers_from_row(header_row)
    df = df_raw.iloc[hdr + 1:].copy()
    df.columns = headers

    # Убираем полностью пустые столбцы
    df = df.loc[:, ~(df.isna() | (df.astype(str).str.strip().isin(["", "nan", "None"]))).all(axis=0)]

    # Fill-down
    df = df.fillna(method="ffill")

    # кандидаты имён
    src_cols = {
        "order":  ["Номер заявки", "№ заявки", "Номер документа продажи"],
        "weight": ["Вес заказа", "Вес, кг", "Вес"],
        "volume": ["Объем заказа", "Объем, м3", "Объем м3", "Объем"],
        "addr":   ["Адрес доставки", "Адрес"],
        "phone":  ["Телефон клиента", "Телефон", "Контактный телефон"],
        "items":  ["Список товаров", "Наименование товара", "Товар", "Наименование"],
        "qty":    ["Кол-во товара", "Кол-во", "Количество"],
        "plan":   ["Дата доставки", "План время дата", "Дата и время доставки", "Дата отгрузки"],
        "mode":   ["Вид перевозки", "Тип доставки"],
    }
    def pick(names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_order = pick(src_cols["order"])
    c_weight = pick(src_cols["weight"])
    c_volume = pick(src_cols["volume"])
    c_addr   = pick(src_cols["addr"])
    c_phone  = pick(src_cols["phone"])
    c_items  = pick(src_cols["items"])
    c_qty    = pick(src_cols["qty"])
    c_plan   = pick(src_cols["plan"])
    c_mode   = pick(src_cols["mode"])

    logger.debug(f"Импорт: найдены колонки -> "
                 f"order={c_order}, weight={c_weight}, volume={c_volume}, addr={c_addr}, phone={c_phone}, "
                 f"items={c_items}, qty={c_qty}, plan={c_plan}, mode={c_mode}")

    target_cols = [
        "номер заявки", "Вес заказа", "Вид перевозки", "Телефон", "Объем заказа",
        "Адрес доставки", "Количество товара", "наименование", "План время дата"
    ]
    out = pd.DataFrame(columns=target_cols)

    if not c_order:
        return pd.DataFrame()  # без номера заявки — ничего не пишем

    def clean_order(v) -> str:
        s = ("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)).strip()
        digits = re.sub(r"[^\d]", "", s)  # оставим только цифры
        return digits

    out["номер заявки"] = df[c_order].map(clean_order)

    if c_weight: out["Вес заказа"]        = df[c_weight]
    out["Вид перевозки"]                  = df[c_mode] if c_mode else ""
    if c_phone:  out["Телефон"]           = df[c_phone]
    if c_volume: out["Объем заказа"]      = df[c_volume]
    if c_addr:   out["Адрес доставки"]    = df[c_addr]
    if c_qty:    out["Количество товара"] = df[c_qty]
    if c_items:  out["наименование"]      = df[c_items]

    if c_plan:
        try:
            plan_series = pd.to_datetime(df[c_plan], dayfirst=True, errors="coerce")
            out["План время дата"] = plan_series.dt.strftime("%d.%m.%Y %H:%M").fillna(df[c_plan].astype(str))
        except Exception:
            out["План время дата"] = df[c_plan].astype(str)
    else:
        out["План время дата"] = ""

    # убираем строки, где после чистки номера пусто
    out["номер заявки"] = out["номер заявки"].astype(str).str.strip()
    out = out[out["номер заявки"] != ""]
    out = out.reset_index(drop=True)
    return out

# === ОБРАБОТКА ФАЙЛОВ ОТ АДМИНА ===
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⚠️ Загружать файлы может только администратор.")
        return

    document = update.message.document
    if not document or not document.file_name.lower().endswith((".xls", ".xlsx")):
        await update.message.reply_text("⚠️ Пришлите Excel-файл (.xlsx или .xls).")
        return

    tg_file = await document.get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(document.file_name)[1]) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        frames = []
        for df_raw in read_excel_flex(tmp_path, document.file_name):
            try:
                df_ready = build_import_dataframe(df_raw)
                if not df_ready.empty:
                    frames.append(df_ready)
            except Exception as e:
                logger.warning(f"Лист пропущен: {e}")

        if not frames:
            await update.message.reply_text("⚠️ В файле не нашёлся ни один номер заявки. Проверьте заголовки/лист.")
            return

        df_all = pd.concat(frames, ignore_index=True)
        if df_all.empty:
            await update.message.reply_text("⚠️ После обработки файл пуст. Проверьте формат.")
            return

        rows = df_all.values.tolist()
        sheet.append_rows(rows, value_input_option="USER_ENTERED")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧭 Распределить маршруты", callback_data="optimize")]
        ])
        await update.message.reply_text(
            f"✅ Файл обработан. Добавлено заявок: {len(rows)}.\nНажмите кнопку ниже:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка обработки файла: {e}")
        await update.message.reply_text("⚠️ Ошибка обработки файла. Проверьте формат/заголовки.")

# === ORS / РАСПРЕДЕЛЕНИЕ ===
def build_jobs_from_sheet(rows, start_row_idx=2):
    jobs = []
    row_index_by_job_id = {}
    coords_cache = {}
    job_info = {}

    for idx, row in enumerate(rows, start=start_row_idx):
        status = (row.get("Статус") or "").strip().lower()
        driver_cell = (row.get("Водитель") or "").strip()
        addr = row.get("Адрес доставки")

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

        vol_m3 = to_float(row.get("Объем заказа", 0))
        wgt_kg = to_float(row.get("Вес заказа", 0))

        vol_units = scale_volume_m3_to_units(vol_m3)
        wgt_units = scale_weight_kg_to_units(wgt_kg)

        order_no = order_no_from_col_A(idx)
        job_id = idx  # технический int для ORS

        job = {
            "id": job_id,
            "location": [float(lon), float(lat)],
            "service": int(service_min * 60),
            "amount": [vol_units, wgt_units],
            "description": str(order_no)
        }
        if tw:
            job["time_windows"] = [tw]

        jobs.append(job)
        row_index_by_job_id[job_id] = idx
        job_info[job_id] = {
            "addr": addr,
            "order_no": order_no,
            "vol_m3": vol_m3,
            "wgt_kg": wgt_kg,
            "tw": job.get("time_windows", None)
        }

    return jobs, row_index_by_job_id, coords_cache, job_info

def build_vehicles_from_drivers():
    vehicles = []
    s_lat = float(WAREHOUSE_LAT); s_lon = float(WAREHOUSE_LON)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today
    end = today + timedelta(days=3, hours=23, minutes=59)
    for user_id, drv in drivers_data.items():
        vol_cap_m3 = float(drv["volume"])
        wgt_cap_kg = float(drv["weight"])
        vehicles.append({
            "id": int(user_id),
            "profile": "driving-car",
            "start": [float(s_lon), float(s_lat)],
            "end":   [float(s_lon), float(s_lat)],
            "time_window": [to_unix(start), to_unix(end)],
            "capacity": [scale_volume_m3_to_units(vol_cap_m3), scale_weight_kg_to_units(wgt_cap_kg)],
            "description": drv["username"]
        })
    return vehicles

def reason_for_unassigned(job, vehicles):
    reasons = []
    amount = job.get("amount", [0, 0])
    tws = job.get("time_windows", None)
    fits_capacity_any = False
    fits_time_any = False
    for v in vehicles:
        cap = v.get("capacity", [0, 0])
        if amount[0] <= cap[0] and amount[1] <= cap[1]:
            fits_capacity_any = True
        vw = v.get("time_window", None)
        if not tws or not vw:
            fits_time_any = True
        else:
            for tw in tws:
                if not tw or len(tw) != 2:
                    continue
                a1, a2 = tw; b1, b2 = vw
                if max(a1, b1) <= min(a2, b2):
                    fits_time_any = True
    if not fits_capacity_any: reasons.append("превышение объёма/веса машины")
    if not fits_time_any:     reasons.append("вне временного окна водителей")
    if not reasons:           reasons.append("маршрутные/временные ограничения")
    return ", ".join(reasons)

def ors_optimize(jobs, vehicles):
    url = "https://api.openrouteservice.org/optimization"
    headers = {"Authorization": ORS_API_KEY, "Content-Type": "application/json"}
    payload = {"jobs": jobs, "vehicles": vehicles, "options": {"g": True}}
    logger.debug("📤 Payload в ORS:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=90)
    r.raise_for_status()
    return r.json()

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

# === TELEGRAM UI ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("Привет, админ! Пришли Excel-файл с заявками (.xlsx/.xls).")
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

# === ОСНОВНАЯ ОПТИМИЗАЦИЯ ===
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

    if len(jobs) > 50 or len(vehicles) > 3:
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Лимит ORS: jobs={len(jobs)} (≤50), vehicles={len(vehicles)} (≤3).")
        return

    try:
        solution = ors_optimize(jobs, vehicles)
    except Exception as e:
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка оптимизации: {e}")
        return

    routes = solution.get("routes", [])
    unassigned_raw = solution.get("unassigned", [])
    unassigned_ids = extract_unassigned_ids(unassigned_raw)

    if unassigned_ids:
        job_by_id = {j["id"]: j for j in jobs}
        lines = []
        for jid in unassigned_ids:
            idx = row_index_by_job_id.get(jid)
            order_no = order_no_from_col_A(idx) if idx else str(jid)
            reason = reason_for_unassigned(job_by_id.get(jid, {}), vehicles)
            lines.append(f"• №{order_no} — {reason}")
        msg = "⚠️ Нераспределены заявки:\n" + "\n".join(lines) + \
              "\n\nДобавьте машины или перенесите нераспределённые заявки на другой день."
        await bot.send_message(chat_id=ADMIN_ID, text=msg)

    routes_by_vehicle = {}
    for r in routes:
        vid = int(r["vehicle"])
        steps = [s for s in r.get("steps", []) if s.get("type") == "job"]
        route_dist_km = 0.0
        if "distance" in r:
            try:
                route_dist_km = float(r["distance"]) / 1000.0
            except Exception:
                route_dist_km = 0.0
        routes_by_vehicle[vid] = {"steps": steps, "route_km": route_dist_km}
    for v in vehicles:
        routes_by_vehicle.setdefault(v["id"], {"steps": [], "route_km": 0.0})

    for vid, data in routes_by_vehicle.items():
        drv = drivers_data.get(vid)
        username = drv["username"] if drv else f"id_{vid}"
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📦 Машина {vid} ({username}): задач {len(data['steps'])}, пробег ~{data['route_km']:.1f} км"
        )

    prev_leg_km_by_row.clear()

    for vid, data in routes_by_vehicle.items():
        drv = drivers_data.get(vid)
        driver_username = drv["username"] if drv else f"id_{vid}"
        job_steps = data["steps"]

        waypoints_latlon = []
        total_vol_m3 = 0.0
        total_wgt_kg = 0.0
        lines = []
        per_stop_msgs = []

        try:
            prev_lat = float(WAREHOUSE_LAT)
            prev_lon = float(WAREHOUSE_LON)
        except Exception:
            prev_lat = prev_lon = None

        route_km_computed = 0.0

        for s in job_steps:
            job_id = int(s["job"])
            row_idx = row_index_by_job_id.get(job_id)
            if not row_idx:
                continue

            info = job_info[job_id]
            addr = info["addr"]
            order_no = info["order_no"]
            vol_m3 = info["vol_m3"]; wgt_kg = info["wgt_kg"]
            total_vol_m3 += vol_m3; total_wgt_kg += wgt_kg

            arrival = s.get("arrival")
            eta_str = datetime.fromtimestamp(arrival).strftime("%H:%M") if arrival else ""

            lon, lat = coords_cache.get(row_idx, (None, None))
            if lon and lat:
                waypoints_latlon.append((float(lat), float(lon)))
                seg_km = 0.0
                if prev_lat is not None and prev_lon is not None:
                    seg_km = _haversine_km(prev_lat, prev_lon, float(lat), float(lon))
                    route_km_computed += seg_km
                prev_lat, prev_lon = float(lat), float(lon)
                prev_leg_km_by_row[row_idx] = seg_km

            if COL_STATUS:  sheet.update_cell(row_idx, COL_STATUS, "выполняется")
            if COL_DRIVER:  sheet.update_cell(row_idx, COL_DRIVER, driver_username)
            if COL_UPDATED: sheet.update_cell(row_idx, COL_UPDATED, now_human())
            if COL_ETA and arrival: sheet.update_cell(row_idx, COL_ETA, eta_str)
            if COL_CAR_PLATE and drv: sheet.update_cell(row_idx, COL_CAR_PLATE, drv.get("car_plate", ""))

            lines.append(f"• №{order_no} — {addr}" + (f" (ETA {eta_str})" if eta_str else ""))

            row_dict = rows[row_idx - 2]
            plan_dt   = row_dict.get("План время дата", "")
            item_name = row_dict.get("наименование", "") or row_dict.get("Наименование", "")
            item_qty  = row_dict.get("Количество товара", "")
            phone     = row_dict.get("Телефон", "")
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

        try:
            s_lat = float(WAREHOUSE_LAT); s_lon = float(WAREHOUSE_LON)
            full_latlon = [(s_lat, s_lon)] + waypoints_latlon if waypoints_latlon else [(s_lat, s_lon)]
        except Exception:
            full_latlon = waypoints_latlon[:]

        route_text = "🧭 Оптимальный маршрут на сегодня:\n"
        route_text += ("\n".join(lines) if lines else "На сегодня заявок нет")
        if lines:
            route_text += f"\n\nИтого погрузка: объём {total_vol_m3:.1f} м³ / вес {total_wgt_kg:.1f} кг"

        route_total_km = data["route_km"] if data["route_km"] > 0 else route_km_computed
        route_text += f"\n🛣 Пробег маршрута: ~{route_total_km:.1f} км"
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

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Распределить повторно", callback_data="optimize_again")]])
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Готово. Маршрутов: {len(routes)}. Нераспределены: {len(unassigned_ids)}.",
        reply_markup=kb
    )

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

    try:
        if data.startswith("done:") or data.startswith("fail:"):
            row_idx = int(data.split(":")[1])
            is_done = data.startswith("done:")
            status_value = "выполнено" if is_done else "не выполнено"

            seg_km = prev_leg_km_by_row.get(row_idx)
            if seg_km is not None and COL_DISTANCE_N:
                try:
                    sheet.update_cell(row_idx, COL_DISTANCE_N, f"{seg_km:.1f}")
                except Exception as e:
                    logger.error(f"Не удалось записать сегментный пробег в строку {row_idx}: {e}")

            if COL_STATUS:  sheet.update_cell(row_idx, COL_STATUS, status_value)
            if COL_UPDATED: sheet.update_cell(row_idx, COL_UPDATED, now_human())
            if not is_done:
                if COL_DRIVER: sheet.update_cell(row_idx, COL_DRIVER, "")
                if COL_ETA:    sheet.update_cell(row_idx, COL_ETA, "")

            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            await query.message.reply_text(f"✅ Статус обновлён: {status_value.upper()} (строка {row_idx})")

            try:
                order_no = order_no_from_col_A(row_idx)
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"ℹ️ Заявка №{order_no} ({row_idx}) → {status_value.upper()}"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить админа об изменении статуса: {e}")
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
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_driver_params))
    app.run_polling()
