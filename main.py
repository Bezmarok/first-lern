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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
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
    """
    try:
        ws = client.open(SHEET_NAME).worksheet("Водители")
    except Exception as e:
        logger.error(f"Не найден лист 'Водители': {e}")
        return {}

    records = ws.get_all_records()
    drivers = {}
    for idx, row in enumerate(records, start=2):
        try:
            # нормализуем заголовки
            row_norm = {k.strip().lower(): v for k, v in row.items()}

            vol = float(str(row_norm.get("объем", row_norm.get("обьем", 0))).replace(",", ".") or 0)
            wgt = float(str(row_norm.get("вес", 0)).replace(",", ".") or 0)
            plate = str(row_norm.get("гос номер", "")).strip()
            owner = str(row_norm.get("принадлежность", "")).strip().lower()
            tg_id = str(row_norm.get("telegram id", "")).strip()

            # если вообще нет госномера — строка мусор
            if not plate:
                continue

            drivers[idx] = {
                "volume": vol,
                "weight": wgt,
                "car_plate": plate,
                "owner_type": owner or "не указано",
                "username": f"id_{idx}",
                "telegram_id": tg_id
            }
        except Exception as e:
            logger.warning(f"Ошибка чтения водителя из строки {idx}: {e}")
            continue

    logger.info(f"Синхронизация водителей: {len(drivers)} шт. Примеры: {list(drivers.items())[:2]}")
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
    ВОЗВРАЩАЕТ (lon, lat) — строго в этом порядке, как требует ORS.
    """
    if not address:
        return (None, None)

    # чистим индекс и пробелы
    addr = re.sub(r"^\d{5,6},?\s*", "", address.strip())

    # проверяем в кэше (ключ — очищенный адрес, значение — (lon, lat))
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
        best_dist = float("inf")
        wh_lat = float(WAREHOUSE_LAT)
        wh_lon = float(WAREHOUSE_LON)
        for f in feats:
            lon, lat = f["geometry"]["coordinates"]
            dist = _haversine_km(wh_lat, wh_lon, float(lat), float(lon))
            if dist < best_dist:
                best_dist = dist
                best = (float(lon), float(lat))  # строго (lon, lat)

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

    def join_items(x):
        """Агрегатор списка товаров с ограничением"""
        items = [str(v).strip() for v in x if str(v).strip()]
        if len(items) > 5:
            return ", ".join(items[:5]) + f" …и ещё {len(items)-5} позиций"
        return ", ".join(items)

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
        "price":    ["Стоимость доставки", "Стоимость доставки, руб.", "стоимость доставки"],
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
        s = re.sub(r"[^0-9A-Za-z]", "", s)
        s = s.lstrip("0")  # убираем ведущие нули
        return s.upper()

    out["номер заявки"] = safe_col(df, c_order).astype(str).map(clean_order) if c_order else ""

    if c_weight:
        w_series = safe_col(df, c_weight).apply(lambda x: int(round(parse_num(x, 0.0))))
    else:
        w_series = pd.Series([0.0] * len(df))
    out["Вес заказа"] = w_series

    out["Вид перевозки"] = ""

    out["Телефон"] = safe_col(df, c_phone).astype(str).map(norm_text) if c_phone else ""

    if c_volume:
        v_series = safe_col(df, c_volume).apply(lambda x: int(round(parse_num(x, 0.0))))
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
            "наименование": join_items,
            "План время дата": "first",
            "Стоимость доставки (для расчёта)": "first"
        }
        out = out.groupby("номер заявки", as_index=False).agg(agg_funcs)

        out["Вес заказа"] = out["Вес заказа"].apply(lambda x: round(parse_num(x, 0.0), 1))
        out["Объем заказа"] = out["Объем заказа"].apply(lambda x: round(parse_num(x, 0.0), 2))
        out["Количество товара"] = out["Количество товара"].apply(lambda x: int(parse_num(x, 0)))
        out["Стоимость доставки (для расчёта)"] = out["Стоимость доставки (для расчёта)"].apply(lambda x: round(parse_num(x, 0.0), 2))

        # убираем "мусорные" заявки вроде "доставка товара клиенту"
    mask = ~out["наименование"].str.lower().str.contains("доставка товара клиенту", na=False)
    out = out.loc[mask].reset_index(drop=True)

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
        errors = []
        for df_raw in read_excel_flex(tmp_path, document.file_name):
            try:
                df_ready = build_import_dataframe(df_raw)
                if not df_ready.empty:
                    if "Вес заказа" in df_ready.columns:
                        df_ready["Вес заказа"] = pd.to_numeric(df_ready["Вес заказа"], errors="coerce").fillna(0).round(1)
                    if "Объем заказа" in df_ready.columns:
                        df_ready["Объем заказа"] = pd.to_numeric(df_ready["Объем заказа"], errors="coerce").fillna(0).round(2)
                    frames.append(df_ready)
                else:
                    errors.append("Пустой лист или не найдены номера заявок.")
            except Exception as e:
                logger.warning(f"Лист пропущен: {e}")
                errors.append(f"Ошибка на листе: {str(e)}")

        if not frames:
            msg = "⚠️ В файле не нашёлся ни один номер заявки.\n"
            if errors:
                msg += "\n".join(f"• {e}" for e in errors[:5])
            await update.message.reply_text(msg)
            return

        df_all = pd.concat(frames, ignore_index=True)
        if df_all.empty:
            await update.message.reply_text("⚠️ После обработки файл пуст. Проверьте заголовки или структуру таблицы.")
            return

        # 1) берём заголовки листа и готовим нормализаторы
        sheet_headers = sheet.row_values(1)
        sh_norm = [h.strip() for h in sheet_headers]
        sh_norm_lc = [h.strip().lower() for h in sheet_headers]

        # 2) нормализуем имена колонок df (нижний регистр)
        df_cols = [c.strip() for c in df_all.columns.astype(str)]
        df_cols_lc = [c.lower() for c in df_cols]

        # 3) сопоставление “как называется в листе” → “какая колонка у нас в df”
        # ключевые соответствия по смыслу
        wanted_map = {
            "номер заявки": ["номер заявки", "id", "заявка", "номер"],
            "вес заказа": ["вес заказа", "вес"],
            "вид перевозки": ["вид перевозки"],
            "телефон": ["телефон", "телефон клиента"],
            "объем заказа": ["объем заказа", "обьем заказа", "объем", "обьем", "м3", "м^3"],
            "адрес доставки": ["адрес доставки", "адрес"],
            "количество товара": ["количество товара", "количество"],
            "наименование": ["наименование", "список товаров"],
            "план время дата": ["план время дата", "дата доставки", "время доставки", "дата", "время"],
            "стоимость доставки": ["стоимость доставки", "стоимость доставки (для расчёта)"]
        }

        def find_df_col(aliases: list[str]) -> str | None:
            for a in aliases:
                a_lc = a.lower()
                for i, c in enumerate(df_cols_lc):
                    if a_lc == c or a_lc in c:
                        return df_cols[i]
            return None

        # 4) для каждого заголовка листа вычисляем, из какой df-колонки брать данные
        col_mapping = {}
        for i, sh_name_lc in enumerate(sh_norm_lc):
            # ищем соответствие по словарю смыслов
            match_df_col = None
            for sense_name, aliases in wanted_map.items():
                if sense_name in sh_name_lc:
                    match_df_col = find_df_col(aliases)
                    break
            # если совпадений нет, оставим None (будет пустая ячейка)
            col_mapping[i] = match_df_col

        # 5) собираем строки для append_rows ровно в порядке листа
        aligned_rows = []
        for _, row in df_all.iterrows():
            values = [""] * len(sh_norm)
            for i in range(len(sh_norm)):
                df_col = col_mapping[i]
                if df_col is None:
                    continue
                val = row.get(df_col, "")
                # приятная чистка строк
                if isinstance(val, str):
                    val = val.strip()
                values[i] = val
            aligned_rows.append(values)

        # пишем в лист
        sheet.append_rows(aligned_rows, value_input_option="USER_ENTERED")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧭 Распределить маршруты", callback_data="optimize")]
        ])
        await update.message.reply_text(
            f"✅ Файл обработан. Добавлено заявок: {len(aligned_rows)}.\nНажмите кнопку ниже:",
            reply_markup=keyboard
        )

    except Exception as e:
        logger.error(f"Ошибка обработки файла: {e}")
        await update.message.reply_text(f"⚠️ Ошибка при обработке: {type(e).__name__}: {e}")

# === ORS / РАСПРЕДЕЛЕНИЕ ===
def build_jobs_from_sheet(rows, start_row_idx=2):
    """
    Строит массив jobs для ORS из Google Sheets.
    Объём — м³ (float), вес — кг (float) внутри кода,
    НО в ORS уходит в целых числах с масштабами VOLUME_SCALE/WEIGHT_SCALE.
    Возвращает: jobs, row_index_by_job_id, coords_cache, job_info
    """
    def norm_key(s: str) -> str:
        return re.sub(r"\s+", "", str(s).lower().replace("ё", "е"))

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
        s = str(val).strip().replace(" ", "").replace("\u00a0", "")
        s = s.replace(",", ".")
        m = re.match(r"^-?\d+(\.\d+)?", s)
        try:
            return float(m.group(0)) if m else float(default)
        except Exception:
            return float(default)

    def clean_address_for_geocode(addr: str) -> str:
        if not addr:
            return ""
        s = str(addr)
        s = re.sub(r"^\s*\d{5,6}\s*,\s*", "", s)
        parts = [p.strip() for p in s.split(",") if p.strip()]
        seen = []
        for p in parts:
            if not seen or norm_key(p) != norm_key(seen[-1]):
                seen.append(p)
        s = ", ".join(seen)
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

        k_status  = pick_key(row, aliases["status"])
        k_driver  = pick_key(row, aliases["driver"])
        k_addr    = pick_key(row, aliases["address"])
        k_order   = pick_key(row, aliases["order"])
        k_plan    = pick_key(row, aliases["plan_dt"])
        k_vol     = pick_key(row, aliases["volume"])
        k_wgt     = pick_key(row, aliases["weight"])
        k_qty     = pick_key(row, aliases["qty"])

        status = str(row.get(k_status, "")).strip().lower() if k_status else ""
        driver_cell = str(row.get(k_driver, "")).strip() if k_driver else ""
        addr_raw = str(row.get(k_addr, "")).strip() if k_addr else ""

        # пропускаем уже занятые/готовые/без адреса
        if not addr_raw or status in ("выполняется", "выполнено", "не выполнено") or driver_cell:
            continue

        addr_for_geo = clean_address_for_geocode(addr_raw)
        lon, lat = geocode_address(addr_for_geo)  # <- (lon, lat)
        if not (lon and lat):
            logger.warning(f"Пропущена заявка {idx} — не удалось геокодить: {addr_for_geo}")
            continue
        coords_cache[idx] = (float(lon), float(lat))

        tw = parse_time_window(row.get(k_plan)) if k_plan else None

        raw_vol = row.get(k_vol) if k_vol else None
        raw_wgt = row.get(k_wgt) if k_wgt else None
        raw_qty = row.get(k_qty) if k_qty else None

        vol_m3 = round(max(0.0, as_float(raw_vol, 0.0)), 2)
        wgt_kg = round(max(0.0, as_float(raw_wgt, 0.0)), 1)

        try:
            qty_val = as_float(raw_qty, None) if raw_qty is not None else None
            if qty_val is not None and abs(vol_m3 - qty_val) < 1e-9 and vol_m3 > 0 and as_float(raw_vol, None) is None:
                logger.warning(f"[ROW {idx}] Похоже, подхватили 'Количество товара' вместо 'Объем заказа' -> vol={vol_m3}, qty={qty_val}. Ставлю vol=0.00")
                vol_m3 = 0.00
        except Exception:
            pass

        try:
            order_no_raw = str(row.get(k_order, "")).strip() if k_order else ""
        except Exception:
            order_no_raw = ""
        order_no = order_no_raw if order_no_raw else order_no_from_col_A(idx)

        # ВАЖНО: ORS требует целые числа в amount
        amount_int = [
            int(round(vol_m3 * VOLUME_SCALE)),  # объём
            int(round(wgt_kg * WEIGHT_SCALE))   # вес
        ]

        job_id = idx
        job = {
            "id": job_id,
            "location": [float(lon), float(lat)],  # (lon, lat)
            "service": int(DEFAULT_SERVICE_MIN * 60),
            "amount": amount_int,
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

        logger.debug(f"[JOB row {idx}] order='{order_no}' raw_vol='{raw_vol}' -> vol={vol_m3:.2f} м³; raw_wgt='{raw_wgt}' -> w={wgt_kg:.1f} кг; addr='{addr_for_geo}' loc=[{lon},{lat}] amount={amount_int}")

    return jobs, row_index_by_job_id, coords_cache, job_info

def build_vehicles_from_drivers():
    """
    Собирает список машин для ORS.
    В capacity передаём ТОЛЬКО целые числа с масштабами VOLUME_SCALE/WEIGHT_SCALE.
    Источник данных — лист "Водители" в Google Sheets.
    """
    vehicles = []
    try:
        s_lat = float(WAREHOUSE_LAT)
        s_lon = float(WAREHOUSE_LON)
    except Exception:
        logger.error("⚠️ Неверные координаты склада. Проверь WAREHOUSE_LAT/LON.")
        return vehicles

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

            capacity_int = [
                int(round(vol_cap_m3 * VOLUME_SCALE)),
                int(round(wgt_cap_kg * WEIGHT_SCALE))
            ]

            vehicle = {
                "id": int(user_id),
                "profile": "driving-car",
                "start": [s_lon, s_lat],   # (lon, lat)
                "end":   [s_lon, s_lat],   # (lon, lat)
                "time_window": [to_unix(start), to_unix(end)],
                "capacity": capacity_int,
                "description": drv.get("username", f"id_{user_id}"),
                "car_plate": drv.get("car_plate", ""),
                "owner_type": owner_type
            }
            vehicles.append(vehicle)
        except Exception as e:
            logger.warning(f"⚠️ У водителя {user_id} некорректные параметры: {e}")
            continue

    for v in vehicles:
        if not isinstance(v, dict) or "id" not in v:
            logger.error(f"🚨 В vehicles затесался мусор: {v}")
            raise ValueError("Сломанный объект в списке машин.")

    logger.info(f"Собрано машин: {len(vehicles)}; пример capacity: {vehicles[0]['capacity'] if vehicles else '—'} (scale V={VOLUME_SCALE}, W={WEIGHT_SCALE})")
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
    Отправляет корректный JSON в ORS. Перед отправкой валидируем,
    что amount/capacity состоят из ЦЕЛЫХ чисел.
    """
    # валидация целочисленности
    for j in jobs:
        if not isinstance(j.get("amount"), list) or len(j["amount"]) != 2:
            raise ValueError(f"Job {j.get('id')} имеет некорректный amount={j.get('amount')}")
        if any((not isinstance(x, int)) for x in j["amount"]):
            raise ValueError(f"Job {j.get('id')} amount должен быть целым: {j['amount']}")
        if not isinstance(j.get("location"), list) or len(j["location"]) != 2:
            raise ValueError(f"Job {j.get('id')} некорректная location={j.get('location')}")

    for v in vehicles:
        cap = v.get("capacity")
        if not isinstance(cap, list) or len(cap) != 2 or any((not isinstance(x, int)) for x in cap):
            raise ValueError(f"Vehicle {v.get('id')} capacity должен быть целым: {cap}")
        loc_ok = isinstance(v.get("start"), list) and isinstance(v.get("end"), list) and len(v["start"]) == 2 and len(v["end"]) == 2
        if not loc_ok:
            raise ValueError(f"Vehicle {v.get('id')} некорректные start/end")

    url = "https://api.openrouteservice.org/optimization"
    headers = {"Authorization": ORS_API_KEY}
    payload = {"jobs": jobs, "vehicles": vehicles, "options": {"g": True}}

    try:
        sample = {
            "jobs": [{k: j[k] for k in ("id", "location", "amount")} for j in jobs[:5]],
            "vehicles": [{k: v[k] for k in ("id", "capacity")} for v in vehicles[:3]]
        }
        logger.debug("📤 ORS payload (sample): %s", json.dumps(sample, ensure_ascii=False))
    except Exception:
        pass

    logger.debug("📤 Payload в ORS:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))

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
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"

    if user_id == ADMIN_ID:
        # Постоянное меню для админа (ReplyKeyboard внизу чата)
        reply_keyboard = [
            ["🧭 Оптимизация маршрутов", "📊 Итоги дня"],
            ["📥 Загрузить Excel"]
        ]
        markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"Привет, админ {username}!\nВыберите действие ниже:",
            reply_markup=markup
        )
    else:
        # Водителю оставляем прежнюю логику (инлайн-кнопка)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Мой заработок", callback_data="earnings")]
        ])
        await update.message.reply_text(
            "Укажи параметры машины, например:\n`2.5, 500, А123ВС78`\n(объём м³, вес кг, госномер)\n\n"
            "После назначения маршрута вы сможете смотреть свой доход.",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на постоянное меню для администратора."""
    if update.effective_user.id != ADMIN_ID:
        return

    text = (update.message.text or "").strip().lower()

    if "оптимизац" in text:
        ok = await optimize_and_assign(context.bot, context)
        if ok:
            await update.message.reply_text("🚀 Оптимизация выполнена. Результаты отправлены.")
        else:
            await update.message.reply_text("⚠️ Оптимизация не выполнена. См. сообщение(я) выше.")
        return

    elif "итог" in text:
        await daily_summary(update, context)
        return

    elif "excel" in text or "загруз" in text:
        await update.message.reply_text("📂 Пришлите Excel-файл (.xlsx или .xls).")
        return

    await update.message.reply_text("Команда из меню не распознана. Тыкните кнопку ниже ещё раз.")

async def handle_driver_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str, weight_str, *rest = [x.strip() for x in update.message.text.split(",")]
        plate_str = rest[0] if rest else ""
        user_id = update.effective_user.id
        username = update.effective_user.username or f"id_{user_id}"

        vol = float(volume_str.replace(",", "."))
        wgt = float(weight_str.replace(",", "."))
        plate = plate_str.strip()

        # обновляем данные в оперативке
        drivers_data[user_id] = {
            "volume": vol,
            "weight": wgt,
            "username": username,
            "car_plate": plate,
            "owner_type": "",
            "telegram_id": user_id
        }

        # записываем в таблицу
        try:
            ws = client.open(SHEET_NAME).worksheet("Водители")
            ws.append_row([wgt, vol, plate, "", str(user_id)])  # записываем telegram_id в 5-й столбец
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
    """
    Строит маршруты и шлёт администратору сводки.
    Данные складываются в application.bot_data.
    Возвращает True при успехе, False при любой проблеме.
    """
    try:
        rows = sheet.get_all_records()
    except Exception as e:
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Не удалось прочитать лист заявок: {e}")
        return False

    vehicles = build_vehicles_from_drivers()
    if not vehicles:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет водителей в листе «Водители».")
        return False

    try:
        jobs, row_index_by_job_id, coords_cache, job_info = build_jobs_from_sheet(rows)
    except Exception as e:
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка подготовки заявок: {e}")
        return False

    if not jobs:
        await bot.send_message(chat_id=ADMIN_ID, text="❗ Нет заявок для маршрутизации (все назначены/выполнены или без адреса).")
        return False

    # Мягкие предупреждения, но без остановки
    warn_msgs = []
    if len(jobs) > 200:
        warn_msgs.append(f"много заявок: {len(jobs)}")
    if len(vehicles) > 20:
        warn_msgs.append(f"много машин: {len(vehicles)}")
    if warn_msgs:
        await bot.send_message(chat_id=ADMIN_ID, text="⚠️ Внимание: " + ", ".join(warn_msgs) + ". Считаю.")

    # Оптимизация
    try:
        solution = ors_optimize(jobs, vehicles)
    except Exception as e:
        await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка оптимизации: {e}")
        return False

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

    # Сохранение в application.bot_data
    if context:
        store = context.application.bot_data
        store["routes_by_vehicle"] = routes_by_vehicle
        store["job_info"] = job_info
        store["rows"] = rows
        store["row_index_by_job_id"] = row_index_by_job_id

    # Цена из строки (поддержка двух возможных заголовков)
    def _price_from_row(r: dict) -> float:
        for key in ("Стоимость доставки (для расчёта)", "Стоимость доставки"):
            if key in r and r[key] not in (None, ""):
                try:
                    return float(str(r[key]).replace(",", "."))
                except Exception:
                    pass
        return 0.0

    # Сообщение админу по каждому водителю
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
                row_idx = store["row_index_by_job_id"].get(jid)
                if row_idx:
                    price_val = _price_from_row(rows[row_idx - 2])
            except Exception:
                price_val = 0.0
            total_price += price_val
            lines.append(
                f"• №{info.get('order_no', jid)} — {info.get('addr', '')}"
                + (f" (ETA {eta_str})" if eta_str else "")
                + (f" | {price_val:.2f} ₽" if price_val else "")
            )

        route_text  = f"🚚 Маршрут для {drv.get('description','')}\n"
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

    if unassigned_ids:
        await bot.send_message(chat_id=ADMIN_ID,
            text=f"ℹ️ Нераспределено заявок: {len(unassigned_ids)} "
                 f"({', '.join(map(str, unassigned_ids[:10]))}{'…' if len(unassigned_ids) > 10 else ''})")
    return True

# === ОБРАБОТЧИК КНОПОК ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # === Оптимизация маршрутов ===
    if data in ("optimize", "optimize_again"):
        ok = await optimize_and_assign(context.bot, context)
        try:
            if ok:
                await query.edit_message_text("🔄 Маршруты построены и разосланы админу!")
            else:
                await query.edit_message_text("⚠️ Оптимизация не выполнена. См. сообщения выше.")
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
        pending = context.application.bot_data.setdefault("pending_routes", {})
        routes_by_vehicle = context.application.bot_data.get("routes_by_vehicle", {})
        if vid in routes_by_vehicle:
            pending[vid] = routes_by_vehicle[vid]
            await query.edit_message_text(f"⏸ Маршрут для водителя {vid} отложен и сохранён.")
        else:
            await query.edit_message_text(f"⚠️ Не найден маршрут для {vid}.")
        return

    # === Назначение маршрута ===
    if data.startswith("assign:"):
        vid = int(data.split(":")[1])
        await send_route_to_driver(context, vid)
        await query.edit_message_text(f"✅ Маршрут назначен водителю {vid}")
        return

    # === Редактирование маршрута ===
    if data.startswith("edit:"):
        vid = int(data.split(":")[1])
        await start_editing_route(update, context, vid)
        return

    # === Удаление заявки из маршрута ===
    if data.startswith("remove:"):
        try:
            _, vid_str, jid_str = data.split(":")
            vid, jid = int(vid_str), int(jid_str)
            routes = context.application.bot_data.get("routes_by_vehicle", {})
            if vid not in routes:
                await query.edit_message_text(f"⚠️ Не найден маршрут {vid}.")
                return
            steps = routes[vid]["steps"]
            routes[vid]["steps"] = [s for s in steps if int(s["job"]) != jid]
            await query.edit_message_text(f"🗑 Заказ №{jid} убран из маршрута водителя {vid}.")
            context.application.bot_data["routes_by_vehicle"] = routes
        except Exception as e:
            logger.error(f"Ошибка удаления заказа: {e}")
            await query.edit_message_text("⚠️ Не удалось удалить заказ.")
        return

    # === Пересчёт маршрута ===
    if data.startswith("recalc:"):
        vid = int(data.split(":")[1])
        await query.edit_message_text(f"♻️ Пересчёт маршрута {vid} пока не реализован.")
        return

    # === Отмена редактирования ===
    if data == "cancel_edit":
        await query.edit_message_text("↩️ Редактирование отменено.")
        return

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ КНОПОК ===
routes_by_vehicle = {}   # глобально храним маршруты после оптимизации
job_info = {}
row_index_by_job_id = {}
rows = []

async def send_route_to_driver(context: ContextTypes.DEFAULT_TYPE, vid: int):
    """
    Отправляет маршрут конкретному водителю.
    Данные берём из application.bot_data, а сообщения шлём через context.bot.
    """
    bot = context.bot
    store = context.application.bot_data

    routes_by_vehicle = store.get("routes_by_vehicle", {})
    job_info = store.get("job_info", {})

    data = routes_by_vehicle.get(vid)
    if not data:
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Нет маршрута для водителя {vid}.")
        return

    # данные о водителе
    drv = drivers_data.get(vid)
    tg_id = None
    if drv and drv.get("telegram_id"):
        try:
            tg_id = int(drv["telegram_id"])
        except Exception:
            tg_id = None

    if not tg_id:
        try:
            ws = client.open(SHEET_NAME).worksheet("Водители")
            records = ws.get_all_records()
            # ищем по госномеру из оперативки, если он есть
            match = None
            if drv and drv.get("car_plate"):
                match = next((r for r in records if str(r.get("гос номер","")).strip() == drv.get("car_plate","")), None)
            if not match:
                # запасной поиск: по id водителя, если совпадает с номером строки/ключом
                match = next((r for r in records if str(r.get("telegram id","")).strip()), None)
            if match:
                try:
                    tg_id = int(str(match.get("telegram id") or match.get("Telegram ID") or "0"))
                except Exception:
                    tg_id = None
        except Exception as e:
            logger.error(f"Не удалось найти Telegram ID для водителя {vid}: {e}")

    if not tg_id:
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ У водителя {vid} нет Telegram ID.")
        return

    username = (drv.get("username") if drv else None) or f"id_{vid}"
    job_steps = data["steps"]
    total_vol_m3 = sum(job_info[int(s["job"])]["vol_m3"] for s in job_steps if int(s["job"]) in job_info)
    total_wgt_kg = sum(job_info[int(s["job"])]["wgt_kg"] for s in job_steps if int(s["job"]) in job_info)

    lines = []
    for s in job_steps:
        jid = int(s["job"])
        info = job_info.get(jid, {})
        eta_str = datetime.fromtimestamp(s.get("arrival")).strftime("%H:%M") if s.get("arrival") else ""
        lines.append(f"• №{info.get('order_no', jid)} — {info.get('addr', '')}" + (f" (ETA {eta_str})" if eta_str else ""))

    route_text = "🧭 Ваш маршрут на сегодня:\n" + ("\n".join(lines) if lines else "Заявок нет")
    if lines:
        route_text += f"\n\nИтого: объём {total_vol_m3} м³ / вес {total_wgt_kg} кг"
    route_text += f"\n🛣 Пробег маршрута: ~{data['route_km']:.1f} км"

    try:
        await bot.send_message(chat_id=tg_id, text=route_text)
        await bot.send_message(chat_id=ADMIN_ID, text=f"✅ Маршрут отправлен водителю {username} (id={tg_id}).")
    except Exception as e:
        logger.error(f"Не удалось отправить маршрут водителю {vid}: {e}")
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Не смогли отправить маршрут водителю {username}.")

async def start_editing_route(update: Update, context: ContextTypes.DEFAULT_TYPE, vid: int):
    """Режим редактирования маршрута: можно убирать заказы из маршрута."""
    routes_by_vehicle = context.bot_data.get("routes_by_vehicle", {})
    job_info = context.bot_data.get("job_info", {})

    data = routes_by_vehicle.get(vid, {})
    job_steps = data.get("steps", [])
    if not job_steps:
        await update.effective_message.reply_text("⚠️ У этого маршрута нет заказов.")
        return

    buttons = []
    for s in job_steps:
        jid = int(s["job"])
        order_no = job_info.get(jid, {}).get("order_no", str(jid))
        addr = job_info.get(jid, {}).get("addr", "")
        short_addr = addr.split(",")[0] if addr else ""
        buttons.append([InlineKeyboardButton(f"🗑 Убрать №{order_no} ({short_addr})", callback_data=f"remove:{vid}:{jid}")])

    kb = InlineKeyboardMarkup(buttons + [
        [InlineKeyboardButton("♻️ Пересчитать маршрут", callback_data=f"recalc:{vid}")],
        [InlineKeyboardButton("↩️ Отмена", callback_data="cancel_edit")]
    ])
    await update.effective_message.reply_text("✏️ Выберите заказы, которые нужно убрать:", reply_markup=kb)

# === ЗАПУСК ===
if __name__ == "__main__":
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("earnings", earnings))
    app.add_handler(CommandHandler("summary", daily_summary))

    # Кнопки (callback_data)
    app.add_handler(CallbackQueryHandler(button_handler))

    # Файлы Excel
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    # Меню админа (ДОЛЖНО идти раньше общего текстового)
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_admin_menu))

    # Общий текст: регистрация водителей. ВАЖНО исключить админа
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.User(ADMIN_ID), handle_driver_params))

    app.run_polling()
