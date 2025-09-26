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

def geocode_address(address: str):
    try:
        url = "https://api.openrouteservice.org/geocode/search"
        addr = re.sub(r"^\d{5,6},?\s*", "", address.strip())
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
        return best if best else (None, None)
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
    Возвращает DataFrame с колонками:
      ["номер заявки","Вес заказа","Вид перевозки","Телефон","Объем заказа",
       "Адрес доставки","Количество товара","наименование","План время дата"]
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

        # вырезаем индекс в начале
        s = re.sub(r"^\s*\d{5,6}\s*,\s*", "", s)

        # раздутая конструкция "Россия, Санкт-Петербург, Санкт-Петербург" -> один раз
        s = s.replace("Россия, Санкт-Петербург, Санкт-Петербург", "Россия, Санкт-Петербург")

        # убираем квартиры/офисы/помещения/литеры/строения
        s = re.sub(r"(кв\.?\s*\S+)|(офис\s*\S+)|(пом\.?\s*\S+)|(лит\.?\s*\S+)|(строение\s*\S+)",
                   "", s, flags=re.IGNORECASE)

        # убираем повторяющиеся сегменты типа "Санкт-Петербург, Санкт-Петербург"
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

    # --- определяем шапку и режем пустоту ---
    hdr = detect_header_row(df_raw)
    header_row = df_raw.iloc[hdr].tolist()
    headers = _make_headers_from_row(header_row)
    df = df_raw.iloc[hdr + 1:].copy()
    df.columns = headers

    logger.debug(f"Найдены заголовки: {list(df.columns)}")

    # удаляем полностью пустые колонки
    df = df.loc[:, ~df.apply(lambda col: col.astype(str).str.strip().isin(["", "nan", "None"]).all())]
    # тянем значения сверху (часто шапка/дата растянута)
    df = df.ffill()

    # --- гибкий поиск колонок-источников ---
    src_cols = {
        "order":  ["Номер заявки", "номер заявки", "Номер", "ID", "Заявка"],
        "date":   ["Дата доставки", "Дата"],
        "time":   ["Время доставки", "Время"],
        "items":  ["Список товаров", "наименование", "Наименование"],
        "qty":    ["Кол-во товара", "Количество товара", "Количество"],
        "volume": ["Объем заказа", "Обьем заказа", "Объем", "Обьем", "м3", "М3"],
        "weight": ["Вес заказа", "Вес"],
        "addr":   ["Адрес доставки", "Адрес"],
        "phone":  ["Телефон клиента", "Телефон"],
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

    # --- целевая форма под Google Sheets ---
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

    # --- заполняем поля, сразу чистим и округляем ---
    def clean_order(v) -> str:
        s = "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v).strip()
        s = re.sub(r"[гГ\-]", "", s)
        return re.sub(r"[^0-9A-Za-z]", "", s)

    # номер заявки
    out["номер заявки"] = safe_col(df, c_order).astype(str).map(clean_order) if c_order else ""

    # вес: парсим и округляем до 1 знака (на уровне источника)
    if c_weight:
        w_series = safe_col(df, c_weight).apply(lambda x: round(parse_num(x, 0.0), 1))
    else:
        w_series = pd.Series([0.0] * len(df))
    out["Вес заказа"] = w_series

    out["Вид перевозки"] = ""  # нет такого в исходнике

    # телефон как есть, но нормализуем текст
    out["Телефон"] = safe_col(df, c_phone).astype(str).map(norm_text) if c_phone else ""

    # объем: парсим и округляем до 2 знаков (на уровне источника)
    if c_volume:
        v_series = safe_col(df, c_volume).apply(lambda x: round(parse_num(x, 0.0), 2))
    else:
        v_series = pd.Series([0.0] * len(df))
    out["Объем заказа"] = v_series

    # адрес: чистим под геокод и под человека
    out["Адрес доставки"] = safe_col(df, c_addr).astype(str).map(clean_addr) if c_addr else ""

    # количество и наименование
    out["Количество товара"] = safe_col(df, c_qty).apply(lambda x: int(parse_num(x, 0))) if c_qty else 0
    out["наименование"]      = safe_col(df, c_items).astype(str).map(norm_text) if c_items else ""

    # план-время-дата
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

    # фильтруем мусорные шапки-строки
    out = out[out["наименование"].astype(str).str.strip() != "Доставка товара клиенту"]

    # --- группировка по номеру заявки и итоговое округление после суммирования ---
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
        }
        out = out.groupby("номер заявки", as_index=False).agg(agg_funcs)

        # финальное округление после агрегации
        out["Вес заказа"] = out["Вес заказа"].apply(lambda x: round(parse_num(x, 0.0), 1))
        out["Объем заказа"] = out["Объем заказа"].apply(lambda x: round(parse_num(x, 0.0), 2))
        # Количество товара на всякий случай к int
        out["Количество товара"] = out["Количество товара"].apply(lambda x: int(parse_num(x, 0)))

    out = out.reset_index(drop=True)
    logger.debug(f"Итог к загрузке: строк {len(out)}; примеры объёмов: {out['Объем заказа'].head(5).tolist()}; весов: {out['Вес заказа'].head(5).tolist()}")
    return out

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
    Вместимость теперь в м³ и кг (те же единицы, что и у jobs).
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
            "capacity": [vol_cap_m3, wgt_cap_kg],
            "description": drv.get("username", f"id_{user_id}")
        }
        vehicles.append(vehicle)

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

def build_task_keyboard(lat: float | None, lon: float | None, row_index: int):
    rows = [[
        InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{row_index}"),
        InlineKeyboardButton("❌ Не выполнено", callback_data=f"fail:{row_index}")
    ]]
    if lat is not None and lon is not None:
        route_url = build_point_route_url(lat, lon)
        rows.append([InlineKeyboardButton("📍 Маршрут", url=route_url)])
    return InlineKeyboardMarkup(rows)

# === ОСНОВНАЯ ОПТИМИЗАЦИЯ ===
async def optimize_and_assign(bot):
    """
    Основная функция: строит задачи из Google Sheets (dict-строки),
    оптимизирует маршруты через ORS и распределяет заявки.
    Работает полностью через get_all_records().
    """
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

    # --- сообщение админу про нераспределённые заявки ---
    if unassigned_ids:
        job_by_id = {j["id"]: j for j in jobs}
        lines = []
        for jid in unassigned_ids:
            idx = row_index_by_job_id.get(jid)
            order_no = job_info.get(jid, {}).get("order_no", str(jid))
            reason = reason_for_unassigned(job_by_id.get(jid, {}), vehicles)
            lines.append(f"• №{order_no} — {reason}")
        msg = "⚠️ Нераспределены заявки:\n" + "\n".join(lines) + \
              "\n\nДобавьте машины или перенесите нераспределённые заявки на другой день."
        await bot.send_message(chat_id=ADMIN_ID, text=msg)

    # --- формируем маршруты по машинам ---
    routes_by_vehicle = {}
    for r in routes:
        vid = int(r["vehicle"])
        steps = [s for s in r.get("steps", []) if s.get("type") == "job"]
        route_dist_km = float(r.get("distance", 0)) / 1000.0
        routes_by_vehicle[vid] = {"steps": steps, "route_km": route_dist_km}
    for v in vehicles:
        routes_by_vehicle.setdefault(v["id"], {"steps": [], "route_km": 0.0})

    # --- обновление статусов в таблице (пакетно) ---
    updates = []
    for vid, data in routes_by_vehicle.items():
        drv = drivers_data.get(vid)
        driver_username = drv["username"] if drv else f"id_{vid}"

        for s in data["steps"]:
            job_id = int(s["job"])
            row_idx = row_index_by_job_id.get(job_id)   # индекс строки из get_all_records() + 2 (заголовки)
            if not row_idx:
                continue
            # Excel-нумерация: реальная строка = row_idx (так как start=2 в build_jobs_from_sheet)
            sheet_row = row_idx

            eta_str = datetime.fromtimestamp(s.get("arrival")).strftime("%H:%M") if s.get("arrival") else ""

            # готовим обновления
            if COL_STATUS:   updates.append((sheet_row, COL_STATUS, "выполняется"))
            if COL_DRIVER:   updates.append((sheet_row, COL_DRIVER, driver_username))
            if COL_UPDATED:  updates.append((sheet_row, COL_UPDATED, now_human()))
            if COL_ETA and eta_str: updates.append((sheet_row, COL_ETA, eta_str))
            if COL_CAR_PLATE and drv: updates.append((sheet_row, COL_CAR_PLATE, drv.get("car_plate", "")))

    # выполняем обновления пачкой
    for row, col_idx, value in updates:
        try:
            sheet.update_cell(row, col_idx, value)
        except Exception as e:
            logger.error(f"Не удалось обновить ячейку ({row},{col_idx}): {e}")

    # --- отправляем маршруты водителям ---
    for vid, data in routes_by_vehicle.items():
        drv = drivers_data.get(vid)
        username = drv["username"] if drv else f"id_{vid}"
        job_steps = data["steps"]

        total_vol_m3 = sum(job_info[int(s["job"])]["vol_m3"] for s in job_steps if int(s["job"]) in job_info)
        total_wgt_kg = sum(job_info[int(s["job"])]["wgt_kg"] for s in job_steps if int(s["job"]) in job_info)

        lines = []
        for s in job_steps:
            jid = int(s["job"])
            info = job_info.get(jid, {})
            eta_str = datetime.fromtimestamp(s.get("arrival")).strftime("%H:%M") if s.get("arrival") else ""
            lines.append(f"• №{info.get('order_no', jid)} — {info.get('addr', '')}" + (f" (ETA {eta_str})" if eta_str else ""))

        route_text = f"🧭 Оптимальный маршрут на сегодня:\n" + ("\n".join(lines) if lines else "Заявок нет")
        if lines:
            route_text += f"\n\nИтого: объём {total_vol_m3:.1f} м³ / вес {total_wgt_kg:.1f} кг"
        route_text += f"\n🛣 Пробег маршрута: ~{data['route_km']:.1f} км"

        try:
            await bot.send_message(chat_id=vid, text=route_text)
        except Exception as e:
            logger.error(f"Не удалось отправить маршрут водителю {vid}: {e}")
            await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Не смогли отправить {username} (id={vid}).")

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

