#!/usr/bin/env python3
# coding: utf-8

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

# ⚖️ Масштабирование единиц
# Теперь считаем объём прямо в м³, а вес прямо в кг
VOLUME_SCALE = int(os.environ.get("VOLUME_SCALE", "1"))
WEIGHT_SCALE = int(os.environ.get("WEIGHT_SCALE", "1"))

drivers_data = {}
assigned_requests = defaultdict(list)
prev_leg_km_by_row = {}

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
COL_DISTANCE_N   = 14

# === УТИЛИТЫ ===
def order_no_from_col_A(row_idx: int) -> str:
    """
    Возвращает номер заявки по строке Google Sheets.
    Сначала пытается взять из колонки 'номер заявки', если нет — из первой колонки (A).
    """
    try:
        headers = sheet.row_values(1)
        # ищем колонку с названием "номер заявки"
        col_idx = None
        for i, h in enumerate(headers, start=1):
            if h.strip().lower() in ["номер заявки", "номер заявки", "id", "заявка", "номер"]:  
                col_idx = i
                break

        if not col_idx:
            col_idx = 1  # fallback на первую колонку

        val = sheet.cell(row_idx, col_idx).value
        if val:
            return str(val).strip()
        return f"{row_idx}"
    except Exception as e:
        logger.warning(f"Не удалось получить номер заявки из строки {row_idx}: {e}")
        return f"{row_idx}"

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

def geocode_address(address: str):
    """
    Геокодирование адреса через ORS Pelias.
    Возвращает (lon, lat) или (None, None), если не нашли.
    """
    try:
        url = "https://api.openrouteservice.org/geocode/search"
        params = {"api_key": ORS_API_KEY, "text": address, "size": 1, "lang": "ru"}
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        feats = data.get("features", [])
        if feats:
            coords = feats[0]["geometry"]["coordinates"]  # [lon, lat]
            lon, lat = float(coords[0]), float(coords[1])
            return lon, lat
        logger.warning(f"Геокодер ORS не нашёл координаты: {address}")
    except Exception as e:
        logger.error(f"Ошибка ORS при геокодировании '{address}': {e}")
    return (None, None)

# === safe_col для борьбы с дублями ===
def safe_col(df, name):
    col = df[name]
    if isinstance(col, pd.DataFrame):
        logger.warning(f"Колонка '{name}' дублируется, беру первую")
        col = col.iloc[:, 0]
    return col

def read_excel_flex(path: str, filename: str) -> list[pd.DataFrame]:
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
    for i in range(min(120, len(df))):
        row = [("" if pd.isna(x) else str(x)).strip().lower() for x in df.iloc[i].tolist()]
        if any(("номер заяв" in c) or ("№ заяв" in c) for c in row):
            return i
    return 0

def _make_headers_from_row(row_list: list[str]) -> pd.Index:
    cleaned = []
    for x in row_list:
        s = ("" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x))
        s = re.sub(r"[\r\n\t]+", " ", s).strip()
        cleaned.append(s)
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
    if df_raw.empty:
        return pd.DataFrame()
    hdr = detect_header_row(df_raw)
    header_row = df_raw.iloc[hdr].tolist()
    headers = _make_headers_from_row(header_row)
    df = df_raw.iloc[hdr + 1:].copy()
    df.columns = headers

    logger.debug(f"Найдены заголовки: {list(df.columns)}")
    if "Номер заявки" in df.columns:
        sample_col = safe_col(df, "Номер заявки")
        logger.debug(f"Примеры номеров заявок (raw): {sample_col.astype(str).head(5).tolist()}")

    # удаляем полностью пустые колонки
    df = df.loc[:, ~df.apply(lambda col: col.astype(str).str.strip().isin(["", "nan", "None"]).all())]
    df = df.ffill()

    src_cols = {
        "order":  ["Номер заявки"],
        "date":   ["Дата доставки"],
        "time":   ["Время доставки"],
        "items":  ["Список товаров"],
        "qty":    ["Кол-во товара"],
        "volume": ["Объем заказа"],
        "weight": ["Вес заказа"],
        "addr":   ["Адрес доставки"],
        "phone":  ["Телефон клиента"],
    }

    def pick(names):
        for col in df.columns:
            col_norm = str(col).strip().lower()
            for target in names:
                if target.lower() in col_norm:
                    return col
        return None

    c_order  = pick(src_cols["order"])
    c_date   = pick(src_cols["date"])
    c_time   = pick(src_cols["time"])
    c_items  = pick(src_cols["items"])
    c_qty    = pick(src_cols["qty"])
    c_volume = pick(src_cols["volume"])
    c_weight = pick(src_cols["weight"])
    c_addr   = pick(src_cols["addr"])
    c_phone  = pick(src_cols["phone"])

    # порядок колонок под Google Sheets
    target_cols = [
        "номер заявки",        # A
        "Вес заказа",          # B
        "Вид перевозки",       # C
        "Телефон",             # D
        "Объем заказа",        # E
        "Адрес доставки",      # F
        "Количество товара",   # G
        "наименование",        # H
        "План время дата"      # I
    ]
    out = pd.DataFrame(columns=target_cols)

    def clean_order(v) -> str:
        s = "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v).strip()
        s = re.sub(r"[гГ\-]", "", s)
        return re.sub(r"[^0-9A-Za-z]", "", s)

    if c_order:
        out["номер заявки"] = safe_col(df, c_order).astype(str).map(clean_order)
        logger.debug(f"Примеры номеров заявок (clean): {out['номер заявки'].head(5).tolist()}")
    else:
        out["номер заявки"] = ""

    out["Вес заказа"]        = safe_col(df, c_weight) if c_weight else ""
    out["Вид перевозки"]     = ""  # в файле нет, оставляем пустым
    out["Телефон"]           = safe_col(df, c_phone) if c_phone else ""
    out["Объем заказа"]      = safe_col(df, c_volume) if c_volume else ""
    out["Адрес доставки"]    = safe_col(df, c_addr)  if c_addr   else ""
    out["Количество товара"] = safe_col(df, c_qty)   if c_qty    else ""
    out["наименование"]      = safe_col(df, c_items) if c_items  else ""

    if c_date and c_time:
        date_col = safe_col(df, c_date)
        time_col = safe_col(df, c_time)
        dt_series = pd.to_datetime(
            date_col.astype(str).str.strip() + " " + time_col.astype(str).str.strip(),
            dayfirst=True, errors="coerce"
        )
        out["План время дата"] = dt_series.dt.strftime("%d.%m.%Y %H:%M").fillna(
            (date_col.astype(str) + " " + time_col.astype(str)).str.strip()
        )
    elif c_date:
        out["План время дата"] = safe_col(df, c_date).astype(str)
    elif c_time:
        out["План время дата"] = safe_col(df, c_time).astype(str)
    else:
        out["План время дата"] = ""

    # фильтрация мусора (шапки типа "Доставка товара клиенту")
    out = out[out["наименование"].astype(str).str.strip() != "Доставка товара клиенту"]

    # --- группировка по номеру заявки ---
    if not out.empty and "номер заявки" in out.columns:
        agg_funcs = {
            "Вес заказа": "sum",
            "Вид перевозки": "first",
            "Телефон": "first",
            "Объем заказа": "sum",
            "Адрес доставки": "first",
            "Количество товара": "sum",
            "наименование": lambda x: ", ".join([str(v).strip() for v in x if v and v.strip() != ""]),
            "План время дата": "first",
        }
        out = out.groupby("номер заявки", as_index=False).agg(agg_funcs)

    out = out.reset_index(drop=True)
    logger.debug(f"Итог к загрузке: строк {len(out)}")
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

    # убираем лишние хвосты типа "кв. 99" или "офис 12" для геокодинга
    def clean_address_for_geocode(addr: str) -> str:
        if not addr:
            return ""
        return re.sub(r"(кв\.?\s*\d+.*)|(офис\s*\d+.*)", "", addr, flags=re.IGNORECASE).strip()

    for idx, row in enumerate(rows, start=start_row_idx):
        status = str(row.get("Статус", "")).strip().lower()
        driver_cell = str(row.get("Водитель", "")).strip()
        addr_raw = str(row.get("Адрес доставки", "")).strip()

        # пропускаем пустые адреса и уже назначенные/закрытые заявки
        if not addr_raw or status in ("выполняется", "выполнено", "не выполнено") or driver_cell:
            continue

        addr_for_geo = clean_address_for_geocode(addr_raw)
        lon, lat = geocode_address(addr_for_geo)
        if not (lon and lat):
            logger.warning(f"Пропущена заявка {idx} — не удалось геокодить: {addr_for_geo}")
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
        job_id = idx  # технический id для ORS

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
            "addr": addr_raw,  # тут сохраняем полный адрес (с кв/офис)
            "addr_geo": addr_for_geo,  # адрес для геокодинга
            "order_no": order_no,
            "vol_m3": vol_m3,
            "wgt_kg": wgt_kg,
            "tw": job.get("time_windows", None)
        }

    return jobs, row_index_by_job_id, coords_cache, job_info

def build_vehicles_from_drivers():
    """
    Собирает список машин для ORS. 
    Возвращает список словарей с id, профилем, старт/финиш, окном доступности и грузоподъёмностью.
    """
    vehicles = []
    try:
        s_lat = float(WAREHOUSE_LAT)
        s_lon = float(WAREHOUSE_LON)
    except Exception:
        logger.error("⚠️ Неверные координаты склада. Проверь WAREHOUSE_LAT/LON.")
        return vehicles

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today
    end = today + timedelta(days=3, hours=23, minutes=59)

    for user_id, drv in drivers_data.items():
        try:
            vol_cap_m3 = float(drv["volume"])
            wgt_cap_kg = float(drv["weight"])
        except Exception as e:
            logger.warning(f"⚠️ У водителя {user_id} некорректные параметры: {e}")
            continue

        vehicle = {
            "id": int(user_id),
            "profile": "driving-car",
            "start": [s_lon, s_lat],
            "end": [s_lon, s_lat],
            "time_window": [to_unix(start), to_unix(end)],
            "capacity": [
                scale_volume_m3_to_units(vol_cap_m3),
                scale_weight_kg_to_units(wgt_cap_kg)
            ],
            "description": drv.get("username", f"id_{user_id}")
        }
        vehicles.append(vehicle)

    # sanity check
    for v in vehicles:
        if not isinstance(v, dict) or "id" not in v:
            logger.error(f"🚨 В vehicles затесался мусор: {v}")
            raise ValueError("Сломанный объект в списке машин.")

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
        volume_str, weight_str, *rest = [x for x in update.message.text.split(",")]
        plate_str = rest[0] if rest else ""
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

