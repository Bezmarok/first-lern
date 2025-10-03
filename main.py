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

def sync_drivers_from_sheet():
    """
    Читает данные о водителях из листа 'Водители' и синхронизирует с drivers_data.
    Структура листа:
      A: Объем (м³)
      B: Вес (кг)
      C: Гос. номер
      D: Принадлежность ('наш' / 'найм')
    Возвращает словарь drivers_data.
    """
    try:
        ws = client.open(SHEET_NAME).worksheet("Водители")
    except Exception as e:
        logger.error(f"Не найден лист 'Водители': {e}")
        return {}

    records = ws.get_all_records()
    drivers = {}
    for idx, row in enumerate(records, start=2):  # строка 2, потому что 1 — заголовки
        try:
            vol = float(str(row.get("Объем", "")).replace(",", ".") or 0)
            wgt = float(str(row.get("Вес", "")).replace(",", ".") or 0)
            plate = str(row.get("Гос номер", "")).strip()
            owner = str(row.get("Принадлежность", "")).strip().lower()

            if not owner:  # если нет принадлежности — игнорируем
                continue

            drivers[idx] = {
                "volume": vol,
                "weight": wgt,
                "car_plate": plate,
                "owner_type": owner,   # 'наш' или 'найм'
                "username": f"id_{idx}"  # заглушка (пока не знаем юзернейм)
            }
        except Exception as e:
            logger.warning(f"Ошибка чтения водителя из строки {idx}: {e}")
            continue

    logger.info(f"Синхронизация: найдено водителей {len(drivers)}")
    return drivers

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

# === Глобальный кэш геокодинга ===
addr_cache = {}

def geocode_address(address: str):
    """
    Геокодирование адреса через OpenRouteService с кэшированием.
    Возвращает (lon, lat) или (None, None).
    """
    if not address:
        return (None, None)

    # чистим индекс и пробелы
    addr = re.sub(r"^\d{5,6},?\s*", "", address.strip())

    # проверяем в кэше
    if addr in addr_cache:
        logger.debug(f"[CACHE] Геокод найден для '{addr}' -> {addr_cache[addr]}")
        return addr_cache[addr]

    try:
        url = "https://api.openrouteservice.org/geocode/search"
        params = {
            "api_key": ORS_API_KEY,
            "text": addr,
            "size": 5,
            "lang": "ru",
            "boundary.country": "RU",
            "focus.point.lon": WAREHOUSE_LON,
            "focus.point.lat": WAREHOUSE_LAT
        }
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        feats = data.get("features", [])
        if not feats:
            logger.warning(f"Геокодинг: нет результатов для '{addr}'")
            return (None, None)

        # выбираем ближайший к складу результат
        best = None
        best_dist = 999999
        for f in feats:
            lon, lat = f["geometry"]["coordinates"]
            dist = _haversine_km(float(WAREHOUSE_LAT), float(WAREHOUSE_LON), lat, lon)
            if dist < best_dist:
                best_dist = dist
                best = (float(lon), float(lat))

        if best:
            addr_cache[addr] = best
            logger.debug(f"[GEOCODE] '{addr}' -> {best} (добавлено в кэш)")
            return best

        return (None, None)

    except Exception as e:
        logger.error(f"Ошибка ORS при геокодировании '{address}': {e}")
        return (None, None)

def build_point_route_url(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "https://maps.google.com"
    return f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f}%2C{lon:.6f}&travelmode=driving"

def build_google_maps_multistop(points: list[tuple[float, float]]) -> str:
    # points: [(lat, lon), ...] где points[0] — склад
    if not points:
        return "https://maps.google.com"
    origin = f"{points[0][0]:.6f},{points[0][1]:.6f}"
    if len(points) == 1:
        return f"https://www.google.com/maps/dir/?api=1&origin={urllib.parse.quote(origin)}&travelmode=driving"
    destination = f"{points[-1][0]:.6f},{points[-1][1]:.6f}"
    waypoints = [f"{lat:.6f},{lon:.6f}" for lat, lon in points[1:-1]]
    url = f"https://www.google.com/maps/dir/?api=1&origin={urllib.parse.quote(origin)}&destination={urllib.parse.quote(destination)}&travelmode=driving"
    if waypoints:
        url += f"&waypoints={urllib.parse.quote('|'.join(waypoints))}"
    return url

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
    """
    Читает один лист Excel и готовит данные для загрузки в Google Sheets бота.
    Делает:
      • Поиск строки заголовков
      • Нормализацию чисел (запятая/точка, пробелы)
      • Округление: Объем -> 2 знака, Вес -> 1 знак
      • Чистку адреса (индекс/кв./офис/лит./строение/дубли города)
      • Группировку по "номер заявки"
      • Извлечение стоимости доставки
    Возвращает DataFrame с колонками:
      ["номер заявки","Вес заказа","Вид перевозки","Телефон","Объем заказа",
       "Адрес доставки","Количество товара","наименование","План время дата",
       "Стоимость доставки (для расчёта)"]
    """
    if df_raw.empty:
        return pd.DataFrame()

    # --- локальные утилиты ---
    def parse_num(val, default=0.0) -> float:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return float(default)
        if isinstance(val, (int, float)):
            try:
                return float(val)
            except Exception:
                return float(default)
        s = str(val).strip()
        s = s.replace("\u00a0", "").replace(" ", "")
        s = s.replace(",", ".")
        m = re.match(r"^-?\d+(\.\d+)?", s)
        try:
            return float(m.group(0)) if m else float(default)
        except Exception:
            return float(default)

    def clean_addr(addr: str) -> str:
        if not addr:
            return ""
        s = str(addr)
        s = re.sub(r"^\s*\d{5,6}\s*,\s*", "", s)  # индекс в начале
        s = s.replace("Россия, Санкт-Петербург, Санкт-Петербург", "Россия, Санкт-Петербург")
        s = re.sub(r"(кв\.?\s*\S+)|(офис\s*\S+)|(пом\.?\s*\S+)|(лит\.?\s*\S+)|(строение\s*\S+)",
                   "", s, flags=re.IGNORECASE)
        parts = [p.strip() for p in s.split(",") if p.strip()]
        dedup = []
        for p in parts:
            if not dedup or p.lower() != dedup[-1].lower():
                dedup.append(p)
        s = ", ".join(dedup)
        s = re.sub(r"\s{2,}", " ", s)
        s = re.sub(r",+", ",", s)
        return s.strip(",; ").strip()

    def norm_text(x) -> str:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
        return re.sub(r"[\r\n\t]+", " ", str(x)).strip()

    # --- определяем шапку ---
    hdr = detect_header_row(df_raw)
    header_row = df_raw.iloc[hdr].tolist()
    headers = _make_headers_from_row(header_row)
    df = df_raw.iloc[hdr + 1:].copy()
    df.columns = headers

    logger.debug(f"Найдены заголовки: {list(df.columns)}")

    df = df.loc[:, ~df.apply(lambda col: col.astype(str).str.strip().isin(["", "nan", "None"]).all())]
    df = df.ffill()

    # --- гибкий поиск колонок ---
    src_cols = {
        "order":    ["Номер заявки", "номер заявки", "Номер", "ID", "Заявка"],
        "date":     ["Дата доставки", "Дата"],
        "time":     ["Время доставки", "Время"],
        "items":    ["Список товаров", "наименование", "Наименование"],
        "qty":      ["Кол-во товара", "Количество товара", "Количество"],
        "volume":   ["Объем заказа", "Обьем заказа", "Объем", "Обьем", "м3", "М3"],
        "weight":   ["Вес заказа", "Вес"],
        "addr":     ["Адрес доставки", "Адрес"],
        "phone":    ["Телефон клиента", "Телефон"],
        "price":    ["Стоимость доставки", "Стоимость доставки, руб.", "стоимость доставки"],  # <── вот она
    }

    def pick(names):
        cand = [c for c in df.columns if any(n.lower() in str(c).strip().lower() for n in names)]
        return cand[0] if cand else None

    c_order  = pick(src_cols["order"])
    c_date   = pick(src_cols["date"])
    c_time   = pick(src_cols["time"])
    c_items  = pick(src_cols["items"])
    c_qty    = pick(src_cols["qty"])
    c_volume = pick(src_cols["volume"])
    c_weight = pick(src_cols["weight"])
    c_addr   = pick(src_cols["addr"])
    c_phone  = pick(src_cols["phone"])
    c_price  = pick(src_cols["price"])

    # --- целевые колонки ---
    target_cols = [
        "номер заявки",
        "Вес заказа",
        "Вид перевозки",
        "Телефон",
        "Объем заказа",
        "Адрес доставки",
        "Количество товара",
        "наименование",
        "План время дата",
        "Стоимость доставки (для расчёта)"
    ]
    out = pd.DataFrame(columns=target_cols)

    # --- наполнение ---
    def clean_order(v) -> str:
        s = "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v).strip()
        s = re.sub(r"[гГ\-]", "", s)
        return re.sub(r"[^0-9A-Za-z]", "", s)

    out["номер заявки"] = safe_col(df, c_order).astype(str).map(clean_order) if c_order else ""

    if c_weight:
        w_series = safe_col(df, c_weight).apply(lambda x: round(parse_num(x, 0.0), 1))
    else:
        w_series = pd.Series([0.0] * len(df))
    out["Вес заказа"] = w_series

    out["Вид перевозки"] = ""

    out["Телефон"] = safe_col(df, c_phone).astype(str).map(norm_text) if c_phone else ""

    if c_volume:
        v_series = safe_col(df, c_volume).apply(lambda x: round(parse_num(x, 0.0), 2))
    else:
        v_series = pd.Series([0.0] * len(df))
    out["Объем заказа"] = v_series

    out["Адрес доставки"] = safe_col(df, c_addr).astype(str).map(clean_addr) if c_addr else ""
    out["Количество товара"] = safe_col(df, c_qty).apply(lambda x: int(parse_num(x, 0))) if c_qty else 0
    out["наименование"] = safe_col(df, c_items).astype(str).map(norm_text) if c_items else ""

    if c_date and c_time:
        date_col = safe_col(df, c_date).astype(str).map(norm_text)
        time_col = safe_col(df, c_time).astype(str).map(norm_text)
        dt_series = pd.to_datetime(
            date_col.str.strip() + " " + time_col.str.strip(),
            dayfirst=True, errors="coerce"
        )
        out["План время дата"] = dt_series.dt.strftime("%d.%m.%Y %H:%M").fillna(
            (date_col + " " + time_col).str.strip()
        )
    elif c_date:
        out["План время дата"] = safe_col(df, c_date).astype(str).map(norm_text)
    elif c_time:
        out["План время дата"] = safe_col(df, c_time).astype(str).map(norm_text)
    else:
        out["План время дата"] = ""

    # стоимость доставки
    if c_price:
        out["Стоимость доставки (для расчёта)"] = safe_col(df, c_price).apply(lambda x: round(parse_num(x, 0.0), 2))
    else:
        out["Стоимость доставки (для расчёта)"] = 0.0

    # группировка
    if not out.empty and "номер заявки" in out.columns:
        agg_funcs = {
            "Вес заказа": "sum",
            "Вид перевозки": "first",
            "Телефон": "first",
            "Объем заказа": "sum",
            "Адрес доставки": "first",
            "Количество товара": "sum",
            "наименование": lambda x: ", ".join([str(v).strip() for v in x if str(v).strip()]),
            "План время дата": "first",
            "Стоимость доставки (для расчёта)": "sum"
        }
        out = out.groupby("номер заявки", as_index=False).agg(agg_funcs)

        out["Вес заказа"] = out["Вес заказа"].apply(lambda x: round(parse_num(x, 0.0), 1))
        out["Объем заказа"] = out["Объем заказа"].apply(lambda x: round(parse_num(x, 0.0), 2))
        out["Количество товара"] = out["Количество товара"].apply(lambda x: int(parse_num(x, 0)))
        out["Стоимость доставки (для расчёта)"] = out["Стоимость доставки (для расчёта)"].apply(lambda x: round(parse_num(x, 0.0), 2))

    out = out.reset_index(drop=True)
    logger.debug(f"Итог к загрузке: строк {len(out)}; примеры стоимостей: {out['Стоимость доставки (для расчёта)'].head(5).tolist()}")
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
                    # округление здесь, чтобы дальше всё шло чистое
                    if "Вес заказа" in df_ready.columns:
                        df_ready["Вес заказа"] = pd.to_numeric(df_ready["Вес заказа"], errors="coerce").fillna(0).round(1)
                    if "Объем заказа" in df_ready.columns:
                        df_ready["Объем заказа"] = pd.to_numeric(df_ready["Объем заказа"], errors="coerce").fillna(0).round(2)
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
    """
    Строит массив jobs для ORS из Google Sheets.
    Объём — м³ (float, округление до 2 знаков), вес — кг (float, до 1 знака).
    Возвращает: jobs, row_index_by_job_id, coords_cache, job_info
    """
    def norm_key(s: str) -> str:
        return re.sub(r"\s+", "", str(s).lower().replace("ё", "е"))

    # Алиасы, чтобы не промахнуться по заголовкам
    aliases = {
        "status":  ["статус"],
        "driver":  ["водитель"],
        "address": ["адресдоставки", "адрес"],
        "order":   ["номерзаявки", "номер", "id", "заявка"],
        "plan_dt": ["планвремядата", "доставкa", "дата", "время"],
        "volume":  ["обьемзаказа", "объемзаказа", "обьем", "объем", "volume", "объемм3", "обьемм3", "м3", "м^3"],
        "weight":  ["весзаказа", "вес", "масса"],
        "qty":     ["количествотовара", "количество"]
    }

    # Предпостроим отображение нормализованных ключей строки -> исходный ключ
    def pick_key(row: dict, keys: list[str]) -> str | None:
        nk = {norm_key(k): k for k in row.keys()}
        for wanted in keys:
            for nkc, orig in nk.items():
                if wanted in nkc:
                    return orig
        return None

    def as_float(val, default=0.0):
        if val is None:
            return float(default)
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        # убираем лишние символы
        s = s.replace(" ", "").replace("\u00a0", "")
        s = s.replace(",", ".")
        # отбрасываем всё, что после второго числа
        m = re.match(r"^-?\d+(\.\d+)?", s)
        try:
            return float(m.group(0)) if m else float(default)
        except Exception:
            return float(default)

    def clean_address_for_geocode(addr: str) -> str:
        if not addr:
            return ""
        s = str(addr)
        # срезаем индекс в начале
        s = re.sub(r"^\s*\d{5,6}\s*,\s*", "", s)
        # убираем повторы города типа "Санкт-Петербург, Санкт-Петербург"
        parts = [p.strip() for p in s.split(",") if p.strip()]
        seen = []
        for p in parts:
            if not seen or norm_key(p) != norm_key(seen[-1]):
                seen.append(p)
        s = ", ".join(seen)

        # удаляем кв/офис/пом/лит/строение — для геокода только улица и дом
        s = re.sub(r"(кв\.?\s*\S+)|(офис\s*\S+)|(пом\.?\s*\S+)|(лит\.?\s*\S+)|(строение\s*\S+)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s{2,}", " ", s).strip(",; ").strip()
        return s

    jobs = []
    row_index_by_job_id = {}
    coords_cache = {}
    job_info = {}

    for idx, row in enumerate(rows, start=start_row_idx):
        if not row or not isinstance(row, dict):
            continue

        # Подхватываем реальные ключи из строки
        k_status  = pick_key(row, aliases["status"])
        k_driver  = pick_key(row, aliases["driver"])
        k_addr    = pick_key(row, aliases["address"])
        k_order   = pick_key(row, aliases["order"])
        k_plan    = pick_key(row, aliases["plan_dt"])
        k_vol     = pick_key(row, aliases["volume"])
        k_wgt     = pick_key(row, aliases["weight"])
        # qty нам не нужен для расчётов, но пригодится для отладки
        k_qty     = pick_key(row, aliases["qty"])

        status = str(row.get(k_status, "")).strip().lower() if k_status else ""
        driver_cell = str(row.get(k_driver, "")).strip() if k_driver else ""
        addr_raw = str(row.get(k_addr, "")).strip() if k_addr else ""

        # Пропускаем уже назначенные/выполненные/без адреса
        if not addr_raw or status in ("выполняется", "выполнено", "не выполнено") or driver_cell:
            continue

        addr_for_geo = clean_address_for_geocode(addr_raw)
        lon, lat = geocode_address(addr_for_geo)
        if not (lon and lat):
            logger.warning(f"Пропущена заявка {idx} — не удалось геокодить: {addr_for_geo}")
            continue

        coords_cache[idx] = (float(lon), float(lat))

        # Время окна
        tw = parse_time_window(row.get(k_plan)) if k_plan else None

        # Сырые значения
        raw_vol = row.get(k_vol) if k_vol else None
        raw_wgt = row.get(k_wgt) if k_wgt else None
        raw_qty = row.get(k_qty) if k_qty else None

        # Нормализация чисел
        vol_m3 = round(max(0.0, as_float(raw_vol, 0.0)), 2)
        wgt_kg = round(max(0.0, as_float(raw_wgt, 0.0)), 1)

        # Защитные ходы против типичных косяков:
        # 1) Если «объём» вдруг равен целому и при этом совпадает с «кол-вом товара» — это почти наверняка подмена столбца.
        try:
            qty_val = as_float(raw_qty, None) if raw_qty is not None else None
            if qty_val is not None and abs(vol_m3 - qty_val) < 1e-9 and vol_m3 > 0 and as_float(raw_vol, None) is None:
                logger.warning(f"[ROW {idx}] Похоже, подхватили 'Количество товара' вместо 'Объем заказа' -> vol={vol_m3}, qty={qty_val}. Ставлю vol=0.00")
                vol_m3 = 0.00
        except Exception:
            pass

        # id и номер заявки
        try:
            order_no_raw = str(row.get(k_order, "")).strip() if k_order else ""
        except Exception:
            order_no_raw = ""
        order_no = order_no_raw if order_no_raw else order_no_from_col_A(idx)

        job_id = idx
        job = {
            "id": job_id,
            "location": [float(lon), float(lat)],
            "service": int(DEFAULT_SERVICE_MIN * 60),
            "amount": [vol_m3, wgt_kg],
            "description": str(order_no),
        }
        if tw:
            job["time_windows"] = [tw]

        jobs.append(job)
        row_index_by_job_id[job_id] = idx
        job_info[job_id] = {
            "addr": addr_raw,
            "addr_geo": addr_for_geo,
            "order_no": order_no,
            "vol_m3": vol_m3,
            "wgt_kg": wgt_kg,
            "tw": job.get("time_windows"),
            "raw": {"vol": raw_vol, "wgt": raw_wgt, "qty": raw_qty}
        }

        logger.debug(f"[JOB row {idx}] order='{order_no}' raw_vol='{raw_vol}' -> vol={vol_m3:.2f} м³; raw_wgt='{raw_wgt}' -> w={wgt_kg:.1f} кг; addr='{addr_for_geo}' loc=[{lon},{lat}]")

    return jobs, row_index_by_job_id, coords_cache, job_info

def build_vehicles_from_drivers():
    """
    Собирает список машин для ORS.
    Вместимость в м³ и кг (те же единицы, что и у jobs).
    Источник данных — лист "Водители" в Google Sheets.
    """
    vehicles = []
    try:
        s_lat = float(WAREHOUSE_LAT)
        s_lon = float(WAREHOUSE_LON)
    except Exception:
        logger.error("⚠️ Неверные координаты склада. Проверь WAREHOUSE_LAT/LON.")
        return vehicles

    # подтягиваем водителей из листа
    drivers = sync_drivers_from_sheet()

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today
    end = today + timedelta(days=3, hours=23, minutes=59)

    for user_id, drv in drivers.items():
        try:
            vol_cap_m3 = float(drv["volume"])
            wgt_cap_kg = float(drv["weight"])
            owner_type = drv.get("owner_type", "").strip().lower()

            if not owner_type:
                logger.info(f"Водитель {user_id} пропущен (нет принадлежности).")
                continue

            vehicle = {
                "id": int(user_id),
                "profile": "driving-car",
                "start": [s_lon, s_lat],
                "end": [s_lon, s_lat],
                "time_window": [to_unix(start), to_unix(end)],
                "capacity": [vol_cap_m3, wgt_cap_kg],
                "description": drv.get("username", f"id_{user_id}"),
                "car_plate": drv.get("car_plate", ""),
                "owner_type": owner_type  # <── теперь хранится здесь
            }
            vehicles.append(vehicle)
        except Exception as e:
            logger.warning(f"⚠️ У водителя {user_id} некорректные параметры: {e}")
            continue

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
    """
    Отправляет корректный JSON в ORS. Используем json=payload,
    чтобы не словить «кривую» сериализацию и случайные мусорные куски.
    """
    url = "https://api.openrouteservice.org/optimization"
    headers = {"Authorization": ORS_API_KEY}
    payload = {"jobs": jobs, "vehicles": vehicles, "options": {"g": True}}

    # компактный лог + полный для отладки
    try:
        sample = {
            "jobs": [{k: j[k] for k in ("id", "location", "amount")} for j in jobs[:5]],
            "vehicles": [{k: v[k] for k in ("id", "capacity")} for v in vehicles[:3]]
        }
        logger.debug("📤 ORS payload (sample): %s", json.dumps(sample, ensure_ascii=False))
    except Exception:
        pass
    logger.debug("📤 Payload в ORS:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))

    # важно: именно json=payload, не data=json.dumps(...)
    r = requests.post(url, headers=headers, json=payload, timeout=90)
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧭 Оптимизация", callback_data="optimize")],
            [InlineKeyboardButton("📊 Итоги дня", callback_data="summary")]
        ])
        await update.message.reply_text("Привет, админ! Выберите действие:", reply_markup=keyboard)
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Мой заработок", callback_data="earnings")]
        ])
        await update.message.reply_text(
            "Укажи параметры машины, например:\n`2.5, 500, А123ВС78`\n(объём м³, вес кг, госномер)\n\n"
            "После назначения маршрута вы сможете смотреть свой доход.",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

async def handle_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str, weight_str, *rest = [x for x in update.message.text.split(",")]
        plate_str = rest[0] if rest else ""
        user_id = update.effective_user.id
        username = update.effective_user.username or f"id_{user_id}"

        vol = float(volume_str.strip().replace(",", "."))
        wgt = float(weight_str.strip().replace(",", "."))
        plate = plate_str.strip()

        # обновляем RAM
        drivers_data[user_id] = {
            "volume": vol,
            "weight": wgt,
            "username": username,
            "car_plate": plate,
            "owner_type": ""  # будет заполнено админом вручную в таблице
        }

        # пишем в лист "Водители"
        try:
            ws = client.open(SHEET_NAME).worksheet("Водители")
            ws.append_row([vol, wgt, plate, ""])  # принадлежность админ поставит вручную
        except Exception as e:
            logger.error(f"Не удалось записать водителя в лист 'Водители': {e}")

        await update.message.reply_text("✅ Данные сохранены. Ждите назначение заявок.")
    except Exception as e:
        logger.error(f"Ошибка регистрации водителя: {e}")
        await update.message.reply_text("⚠️ Неверный формат. Пример: `2.5, 500, А123ВС78`", parse_mode="Markdown")

async def earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ws = client.open(SHEET_NAME).sheet1
    records = ws.get_all_records()

    ws_drivers = client.open(SHEET_NAME).worksheet("Водители")
    drivers = ws_drivers.get_all_records()

    driver_info = next((d for d in drivers if str(d.get("id","")) == str(user_id)), None)
    if not driver_info:
        await update.message.reply_text("⚠️ Вы не зарегистрированы в системе.")
        return

    owner_type = driver_info.get("Принадлежность","").strip().lower()
    percent = 0.6 if owner_type == "найм" else 0.4

    total_today = total_week = total_month = 0
    now = datetime.now()

    for row in records:
        if str(row.get("Водитель","")) == update.effective_user.username:
            price = float(row.get("Стоимость доставки (для расчёта)",0) or 0)
            dt = try_parse_datetime(row.get("Факт Дата и время"))
            if not dt:
                continue
            if dt.date() == now.date():
                total_today += price*percent
            if dt.isocalendar()[1] == now.isocalendar()[1]:
                total_week += price*percent
            if dt.month == now.month and dt.year == now.year:
                total_month += price*percent

    msg = f"💰 Ваш заработок:\n" \
          f"Сегодня: {total_today:.2f} ₽\n" \
          f"Эта неделя: {total_week:.2f} ₽\n" \
          f"Этот месяц: {total_month:.2f} ₽"
    await update.message.reply_text(msg)

def build_task_keyboard(lat: float | None, lon: float | None, row_index: int):
    rows = [[
        InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
        InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
    ]]
    if lat is not None and lon is not None:
        route_url = build_point_route_url(lat, lon)
        rows.append([InlineKeyboardButton("📍 Маршрут", url=route_url)])
    return InlineKeyboardMarkup(rows)

async def daily_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    ws = client.open(SHEET_NAME).sheet1
    records = ws.get_all_records()
    ws_drivers = client.open(SHEET_NAME).worksheet("Водители")
    drivers = ws_drivers.get_all_records()

    summary = {}
    now = datetime.now()

    for row in records:
        if str(row.get("Статус","")).lower() != "выполнено":
            continue
        driver_name = str(row.get("Водитель","")).strip()
        if not driver_name:
            continue
        price = float(row.get("Стоимость доставки (для расчёта)",0) or 0)

        driver_info = next((d for d in drivers if d.get("Гос номер","") == row.get("Гос номер","")), None)
        owner_type = driver_info.get("Принадлежность","").strip().lower() if driver_info else ""
        percent = 0.6 if owner_type == "найм" else 0.4

        dt = try_parse_datetime(row.get("Факт Дата и время"))
        if not dt or dt.date() != now.date():
            continue

        summary.setdefault(driver_name, 0)
        summary[driver_name] += price * percent

    lines = [f"{d}: {s:.2f} ₽" for d, s in summary.items()]
    total = sum(summary.values())
    msg = "📊 Итоги дня:\n" + "\n".join(lines) + f"\n\nИтого по всем: {total:.2f} ₽"
    await update.message.reply_text(msg)

# === ОСНОВНАЯ ОПТИМИЗАЦИЯ ===
async def optimize_and_assign(bot, context=None):
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
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Лимит ORS: jobs={len(jobs)} (≤50), vehicles={len(vehicles)} (≤3)."
        )
        return

    try:
        solution = ors_optimize(jobs, vehicles)
    except Exception as e:
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка оптимизации: {e}")
        return

    routes = solution.get("routes", [])
    unassigned_raw = solution.get("unassigned", [])
    unassigned_ids = extract_unassigned_ids(unassigned_raw)

    routes_by_vehicle = {}
    for r in routes:
        vid = int(r["vehicle"])
        steps = [s for s in r.get("steps", []) if s.get("type") == "job"]
        route_dist_km = float(r.get("distance", 0)) / 1000.0
        routes_by_vehicle[vid] = {"steps": steps, "route_km": route_dist_km}
    for v in vehicles:
        routes_by_vehicle.setdefault(v["id"], {"steps": [], "route_km": 0.0})

    # сохраняем в bot_data
    if context:
        context.bot_data["routes_by_vehicle"] = routes_by_vehicle
        context.bot_data["job_info"] = job_info
        context.bot_data["rows"] = rows
        context.bot_data["row_index_by_job_id"] = row_index_by_job_id

    # сообщение админу
    for vid, data in routes_by_vehicle.items():
        drv = next((d for d in vehicles if d["id"] == vid), None)
        if not drv:
            continue

        job_steps = data["steps"]
        total_vol_m3 = sum(job_info[int(s["job"])]["vol_m3"] for s in job_steps if int(s["job"]) in job_info)
        total_wgt_kg = sum(job_info[int(s["job"])]["wgt_kg"] for s in job_steps if int(s["job"]) in job_info)

        total_price = 0.0
        lines = []
        for s in job_steps:
            jid = int(s["job"])
            info = job_info.get(jid, {})
            eta_str = datetime.fromtimestamp(s.get("arrival")).strftime("%H:%M") if s.get("arrival") else ""
            price_val = 0.0
            try:
                row_idx = row_index_by_job_id.get(jid)
                if row_idx and "Стоимость доставки (для расчёта)" in rows[row_idx-2]:
                    price_val = float(rows[row_idx-2].get("Стоимость доставки (для расчёта)", 0) or 0)
            except Exception:
                price_val = 0.0
            total_price += price_val
            lines.append(
                f"• №{info.get('order_no', jid)} — {info.get('addr', '')}"
                + (f" (ETA {eta_str})" if eta_str else "")
                + (f" | {price_val:.2f} ₽" if price_val else "")
            )

        route_text = f"🚚 Маршрут для {drv.get('description','')}\n"
        route_text += f"Заявок: {len(job_steps)}\n"
        route_text += f"Итого: объём {total_vol_m3:.1f} м³ / вес {total_wgt_kg:.1f} кг\n"
        route_text += f"Пробег: ~{data['route_km']:.1f} км\n"
        route_text += f"Сумма заказов: {total_price:.2f} ₽\n\n"
        route_text += "\n".join(lines) if lines else "Заявок нет"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Назначить водителю", callback_data=f"assign:{vid}")],
            [InlineKeyboardButton("✏️ Редактировать маршрут", callback_data=f"edit:{vid}")],
            [InlineKeyboardButton("⏸ Отложить", callback_data=f"skip:{vid}")]
        ])

        await bot.send_message(chat_id=ADMIN_ID, text=route_text, reply_markup=kb)

# === ОБРАБОТЧИК КНОПОК ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # === Оптимизация маршрутов ===
    if data in ("optimize", "optimize_again"):
        await optimize_and_assign(context.bot, context)
        try:
            await query.edit_message_text("🔄 Маршруты построены и разосланы админу!")
        except Exception:
            pass
        return

    # === Итоги дня (админ) ===
    if data == "summary":
        await daily_summary(update, context)
        return

    # === Заработок (водитель) ===
    if data == "earnings":
        await earnings(update, context)
        return

    # === Отложить маршрут ===
    if data.startswith("skip:"):
        vid = int(data.split(":")[1])
        await query.edit_message_text(f"⏸ Маршрут для водителя {vid} отложен.")
        return

    # === Назначение маршрута ===
    if data.startswith("assign:"):
        vid = int(data.split(":")[1])
        await send_route_to_driver(context.bot, vid)
        await query.edit_message_text(f"✅ Маршрут назначен водителю {vid}")
        return

    # === Редактирование маршрута (пока простая заглушка) ===
    if data.startswith("edit:"):
        vid = int(data.split(":")[1])
        await query.edit_message_text(f"✏️ Редактирование маршрута для водителя {vid} (в разработке)")
        return

    # === Завершение заявок (done/fail) ===
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

            # === Запись в историю ===
            try:
                ws_history = client.open(SHEET_NAME).worksheet("История")
                ws_history.append_row([
                    now_human(),                       # дата/время
                    order_no_from_col_A(row_idx),      # номер заявки
                    query.from_user.username,          # кто выполнял
                    status_value,                      # статус
                    sheet.cell(row_idx, col("Стоимость доставки (для расчёта)")).value or 0
                ])
                logger.debug(f"История: записана строка для {row_idx}")
            except Exception as e:
                logger.error(f"Не удалось записать историю: {e}")

            return
    except Exception as e:
        logger.error(f"Ошибка обработки кнопки {data}: {e}")
        await query.message.reply_text("⚠️ Не удалось обновить статус. Сообщите админу.")
        return


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ КНОПОК ===
routes_by_vehicle = {}   # глобально храним маршруты после оптимизации
job_info = {}
row_index_by_job_id = {}
rows = []

async def send_route_to_driver(bot, vid: int):
    """
    Отправляет маршрут конкретному водителю после того,
    как админ нажал кнопку "Назначить водителю".
    """
    routes_by_vehicle = bot.bot_data.get("routes_by_vehicle", {})
    job_info = bot.bot_data.get("job_info", {})

    data = routes_by_vehicle.get(vid)
    if not data:
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Нет маршрута для водителя {vid}.")
        return

    drv = drivers_data.get(vid, {})
    username = drv.get("username", f"id_{vid}")
    job_steps = data["steps"]

    total_vol_m3 = sum(job_info[int(s["job"])]["vol_m3"] for s in job_steps if int(s["job"]) in job_info)
    total_wgt_kg = sum(job_info[int(s["job"])]["wgt_kg"] for s in job_steps if int(s["job"]) in job_info)

    lines = []
    for s in job_steps:
        jid = int(s["job"])
        info = job_info.get(jid, {})
        eta_str = datetime.fromtimestamp(s.get("arrival")).strftime("%H:%M") if s.get("arrival") else ""
        lines.append(
            f"• №{info.get('order_no', jid)} — {info.get('addr', '')}"
            + (f" (ETA {eta_str})" if eta_str else "")
        )

    route_text = f"🧭 Ваш маршрут на сегодня:\n" + ("\n".join(lines) if lines else "Заявок нет")
    if lines:
        route_text += f"\n\nИтого: объём {total_vol_m3:.1f} м³ / вес {total_wgt_kg:.1f} кг"
    route_text += f"\n🛣 Пробег маршрута: ~{data['route_km']:.1f} км"

    try:
        await bot.send_message(chat_id=vid, text=route_text)
        await bot.send_message(chat_id=ADMIN_ID, text=f"✅ Маршрут отправлен водителю {username} (id={vid}).")
    except Exception as e:
        logger.error(f"Не удалось отправить маршрут водителю {vid}: {e}")
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Не смогли отправить {username} (id={vid}).")

async def start_editing_route(update: Update, context: ContextTypes.DEFAULT_TYPE, vid: int):
    """
    Заготовка: вход в режим редактирования маршрута.
    Показывает список заказов с кнопками "убрать".
    """
    global routes_by_vehicle, job_info
    data = routes_by_vehicle.get(vid, {})
    job_steps = data.get("steps", [])
    if not job_steps:
        await update.effective_message.reply_text("⚠️ У этого маршрута нет заказов.")
        return

    # строим кнопки для удаления заказов
    buttons = []
    for s in job_steps:
        jid = int(s["job"])
        order_no = job_info.get(jid, {}).get("order_no", str(jid))
        buttons.append([InlineKeyboardButton(f"🗑 Убрать заказ №{order_no}", callback_data=f"remove:{vid}:{jid}")])

    kb = InlineKeyboardMarkup(buttons + [
        [InlineKeyboardButton("♻️ Пересчитать маршрут", callback_data=f"recalc:{vid}")],
        [InlineKeyboardButton("↩️ Отмена", callback_data="cancel_edit")]
    ])
    await update.effective_message.reply_text("Выберите заказы для удаления:", reply_markup=kb)

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
    app.add_handler(CommandHandler("earnings", earnings))
    app.add_handler(CommandHandler("summary", daily_summary))
    app.run_polling()
