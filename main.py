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

VOLUME_SCALE = int(os.environ.get("VOLUME_SCALE", "1000"))
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
        logger.debug(f"Примеры номеров заявок (raw): {safe_col(df,'Номер заявки').head(5).tolist()}")

    df = df.loc[:, ~(df.isna() | (df.astype(str).str.strip().isin(["", "nan", "None"]))).all(axis=0)]
    df = df.fillna(method="ffill")

    src_cols = {
        "order":  ["Номер заявки"],   # только этот столбец
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

    c_order = pick(src_cols["order"])
    c_date  = pick(src_cols["date"])
    c_time  = pick(src_cols["time"])
    c_items = pick(src_cols["items"])
    c_qty   = pick(src_cols["qty"])
    c_volume= pick(src_cols["volume"])
    c_weight= pick(src_cols["weight"])
    c_addr  = pick(src_cols["addr"])
    c_phone = pick(src_cols["phone"])

    target_cols = [
        "номер заявки", "План время дата", "наименование", "Количество товара",
        "Объем заказа", "Вес заказа", "Адрес доставки", "Телефон"
    ]
    out = pd.DataFrame(columns=target_cols)

    def clean_order(v) -> str:
        s = ("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)).strip()
        s = re.sub(r"[гГ\-]", "", s)
        return re.sub(r"[^0-9A-Za-z]", "", s)

    if c_order:
        out["номер заявки"] = safe_col(df, c_order).map(clean_order)
        logger.debug(f"Примеры номеров заявок (clean): {out['номер заявки'].head(5).tolist()}")
    else:
        out["номер заявки"] = ""

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

    out["наименование"]      = safe_col(df, c_items)   if c_items  else ""
    out["Количество товара"] = safe_col(df, c_qty)     if c_qty    else ""
    out["Объем заказа"]      = safe_col(df, c_volume)  if c_volume else ""
    out["Вес заказа"]        = safe_col(df, c_weight)  if c_weight else ""
    out["Адрес доставки"]    = safe_col(df, c_addr)    if c_addr   else ""
    out["Телефон"]           = safe_col(df, c_phone)   if c_phone  else ""

    if "номер заявки" in out.columns:
        col_vals = safe_col(out, "номер заявки")
        col_vals = col_vals.fillna("").astype(str).str.strip()
        out = out[col_vals != ""]
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

# ... (остальной код, который у тебя был: build_jobs_from_sheet, optimize_and_assign, button_handler и запуск бота остаются без изменений)

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
