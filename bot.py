import logging
import json
import os
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_NAME = "Tradeshow Prints Marshall/Sonos (Sales Sheet)"
PRODUCT_TAB = "COMEX Show 2026"
SALES_TAB = "Sales Tracker"

# ---------- Show-day layout ----------
# Each show day gets its own 6-column block on the Sales Tracker tab, laid out by hand:
#   a day label row, then the header row (S/N, Name, Product (Colour), Time,
#   Delivery?, Pre-order?), then the sales rows beneath it.
SHOW_DAYS = [
    (date(2026, 9, 3), "A"),   # A - F
    (date(2026, 9, 4), "H"),   # H - M
    (date(2026, 9, 5), "O"),   # O - T
    (date(2026, 9, 6), "V"),   # V - AA
]
BLOCK_WIDTH = 6  # S/N, Name, Product (Colour), Time, Delivery?, Pre-order?

logging.basicConfig(level=logging.INFO)

# ---------- Google Sheets (single reused client) ----------
_client = None

def get_client():
    global _client
    if _client is None:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        _client = gspread.authorize(creds)
    return _client

def get_spreadsheet():
    return get_client().open(SPREADSHEET_NAME)

# ---------- Product cache ----------
_products_cache = []
_cache_time = 0
CACHE_TTL = 300  # refresh products every 5 minutes

def get_products(force=False):
    """Return cached products, refreshing from the sheet at most every 5 min."""
    global _products_cache, _cache_time
    now = time.time()
    if force or not _products_cache or (now - _cache_time) > CACHE_TTL:
        ws = get_spreadsheet().worksheet(PRODUCT_TAB)
        rows = ws.get_all_values()
        products = []
        for row in rows[2:]:  # skip two header rows
            name = row[0].strip() if len(row) > 0 else ""      # column A
            category = row[3].strip() if len(row) > 3 else ""  # column D
            brand = row[4].strip() if len(row) > 4 else ""     # column E
            if name:
                products.append({
                    "name": name,
                    "category": category or "Other",
                    "brand": brand or "Other",
                })
        _products_cache = products
        _cache_time = now
    return _products_cache

# ---------- Column helpers ----------
def col_to_index(letter):
    """'A' -> 1, 'H' -> 8, 'AA' -> 27"""
    idx = 0
    for ch in letter.upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx

def index_to_col(idx):
    """1 -> 'A', 8 -> 'H', 27 -> 'AA'"""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

def day_label(d):
    """e.g. '3 SEPT (WED)'"""
    month = d.strftime("%b").upper()
    if month == "SEP":
        month = "SEPT"
    return f"{d.day} {month} ({d.strftime('%a').upper()})"

def block_for(d):
    """Return (show_date, start_column_letter) for the block a given date writes into.

    Dates before the show fall into day 1's block; dates after it fall into day 4's.
    """
    for show_date, col in SHOW_DAYS:
        if d <= show_date:
            return show_date, col
    return SHOW_DAYS[-1]

def block_range(start_letter, row_start, row_end):
    c0 = col_to_index(start_letter)
    return f"{start_letter}{row_start}:{index_to_col(c0 + BLOCK_WIDTH - 1)}{row_end}"

def find_data_start(ws, start_letter):
    """Locate the row where a block's sales rows begin (the row after its header row).

    Scans the top of the block for the header row (the one whose 2nd cell is 'Name'),
    so it works whether the day label sits on one row above the headers or two.
    """
    top = ws.get(block_range(start_letter, 1, 6)) or []
    for i, row in enumerate(top, start=1):
        if len(row) > 1 and row[1].strip().lower().startswith("name"):
            return i + 1
    return 3  # fallback: label row 1, header row 2, data from row 3

def save_sale(name, product, delivery, preorder, qty=1, retries=3):
    """Append qty rows to the column block for today's show day."""
    tz = ZoneInfo("Asia/Singapore")
    now = datetime.now(tz)
    timestamp = now.strftime("%-I:%M%p")  # e.g. 3:40PM, SG time
    show_date, start_letter = block_for(now.date())
    name_col = col_to_index(start_letter) + 1  # Name is the 2nd column of the block

    for attempt in range(retries):
        try:
            ws = get_spreadsheet().worksheet(SALES_TAB)

            data_start = find_data_start(ws, start_letter)

            # First free row in this block, based on its Name column
            existing = ws.col_values(name_col)
            next_row = max(len(existing) + 1, data_start)

            # S/N restarts at 1 for each day block
            sn = next_row - data_start + 1
            rows = [[sn + i, name, product, timestamp, delivery, preorder]
                    for i in range(qty)]

            ws.update(block_range(start_letter, next_row, next_row + qty - 1), rows)
            return day_label(show_date)
        except Exception as e:
            logging.warning(f"save_sale attempt {attempt+1} failed: {e}")
            time.sleep(2 * (attempt + 1))
    return None

# ---------- Keyboard builder ----------
def build_keyboard(items, prefix, per_row=1, add_cancel=True, add_back=None):
    buttons = [InlineKeyboardButton(str(i), callback_data=f"{prefix}:{i}"[:64]) for i in items]
    rows = [buttons[i:i+per_row] for i in range(0, len(buttons), per_row)]
    nav = []
    if add_back:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"back:{add_back}"))
    if add_cancel:
        nav.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel:x"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

# ---------- Screen renderers (so Back can redraw any step) ----------
def name_prompt(context):
    recent = context.user_data.get("recent_names", [])
    text = "Who made this sale?\n\nType the promoter's name:"
    rows = [[InlineKeyboardButton(n, callback_data=f"name:{n}"[:64])] for n in recent]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:x")])
    if recent:
        text += "\n\n(or tap a recent name below)"
    return text, InlineKeyboardMarkup(rows)

def remember_name(context, name):
    recent = [n for n in context.user_data.get("recent_names", []) if n.lower() != name.lower()]
    recent.insert(0, name)
    context.user_data["recent_names"] = recent[:5]

def brand_screen(context):
    products = context.user_data["all_products"]
    brands = sorted(set(p["brand"] for p in products))
    promoter = context.user_data.get("sale", {}).get("name", "")
    return (f"Promoter: {promoter}\n\nSelect a brand:",
            build_keyboard(brands, "brand", per_row=2, add_cancel=True, add_back="name"))

def category_screen(context, brand):
    products = context.user_data["all_products"]
    cats = sorted(set(p["category"] for p in products if p["brand"] == brand))
    return (f"Brand: {brand}\n\nSelect a category:",
            build_keyboard(cats, "cat", per_row=2, add_cancel=True, add_back="brand"))

def model_screen(context, brand, cat):
    products = context.user_data["all_products"]
    models = [p["name"] for p in products if p["brand"] == brand and p["category"] == cat]
    context.user_data["models"] = models
    buttons = [InlineKeyboardButton(m, callback_data=f"model:{i}") for i, m in enumerate(models)]
    rows = [[b] for b in buttons]
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data="back:cat"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel:x"),
    ])
    return f"{brand} › {cat}\n\nSelect a model:", InlineKeyboardMarkup(rows)

def qty_screen(context, product):
    return (f"Product: {product}\n\nSelect quantity:",
            build_keyboard([1, 2, 3, 4, 5], "qty", per_row=5,
                           add_cancel=True, add_back="model"))

def delivery_screen(context, product, qty):
    return (f"Product: {product}\nQty: {qty}\n\nDelivery?",
            build_keyboard(["Yes", "No"], "delivery", per_row=2,
                           add_cancel=True, add_back="qty"))

def preorder_screen(context, product, qty, delivery):
    return (f"Product: {product}\nQty: {qty}\nDelivery: {delivery}\n\nPre-order?",
            build_keyboard(["Yes", "No"], "preorder", per_row=2,
                           add_cancel=True, add_back="delivery"))

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tap /sale to log a sale.")

async def sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sale"] = {}
    context.user_data["all_products"] = get_products()  # cached, fast
    context.user_data["awaiting_name"] = True
    text, markup = name_prompt(context)
    await update.message.reply_text(text, reply_markup=markup)

async def name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manager types the promoter's name."""
    if not context.user_data.get("awaiting_name"):
        await update.message.reply_text("Tap /sale to log a sale.")
        return
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please type the promoter's name.")
        return
    context.user_data["awaiting_name"] = False
    context.user_data.setdefault("sale", {})["name"] = name
    remember_name(context, name)
    if not context.user_data.get("all_products"):
        context.user_data["all_products"] = get_products()
    text, markup = brand_screen(context)
    await update.message.reply_text(text, reply_markup=markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    step, value = query.data.split(":", 1)
    sale_data = context.user_data.setdefault("sale", {})

    # ----- Cancel -----
    if step == "cancel":
        context.user_data["sale"] = {}
        context.user_data["awaiting_name"] = False
        await query.edit_message_text("Cancelled. Tap /sale to start again.")
        return

    # ----- Back -----
    if step == "back":
        if value == "name":
            context.user_data["awaiting_name"] = True
            text, markup = name_prompt(context)
            await query.edit_message_text(text, reply_markup=markup)
            return
        if value == "brand":
            text, markup = brand_screen(context)
        elif value == "cat":
            text, markup = category_screen(context, sale_data["brand"])
        elif value == "model":
            text, markup = model_screen(context, sale_data["brand"], sale_data["cat"])
        elif value == "qty":
            text, markup = qty_screen(context, sale_data["product"])
        elif value == "delivery":
            text, markup = delivery_screen(context, sale_data["product"], sale_data["qty"])
        await query.edit_message_text(text, reply_markup=markup)
        return

    # ----- Forward flow -----
    if step == "name":
        sale_data["name"] = value
        context.user_data["awaiting_name"] = False
        remember_name(context, sale_data["name"])
        text, markup = brand_screen(context)
        await query.edit_message_text(text, reply_markup=markup)

    elif step == "brand":
        sale_data["brand"] = value
        text, markup = category_screen(context, value)
        await query.edit_message_text(text, reply_markup=markup)

    elif step == "cat":
        sale_data["cat"] = value
        text, markup = model_screen(context, sale_data["brand"], value)
        await query.edit_message_text(text, reply_markup=markup)

    elif step == "model":
        models = context.user_data.get("models", [])
        sale_data["product"] = models[int(value)]
        text, markup = qty_screen(context, sale_data["product"])
        await query.edit_message_text(text, reply_markup=markup)

    elif step == "qty":
        sale_data["qty"] = value
        text, markup = delivery_screen(context, sale_data["product"], value)
        await query.edit_message_text(text, reply_markup=markup)

    elif step == "delivery":
        sale_data["delivery"] = value
        text, markup = preorder_screen(context, sale_data["product"],
                                       sale_data["qty"], value)
        await query.edit_message_text(text, reply_markup=markup)

    elif step == "preorder":
        sale_data["preorder"] = value
        name = sale_data.get("name") or query.from_user.first_name
        qty = int(sale_data["qty"])

        logged_day = save_sale(name, sale_data["product"],
                               sale_data["delivery"], sale_data["preorder"], qty=qty)

        if logged_day:
            await query.edit_message_text(
                "Recorded ✅\n\n"
                f"Promoter: {name}\n"
                f"Product: {sale_data['product']}\n"
                f"Qty: {qty}\n"
                f"Delivery: {sale_data['delivery']}\n"
                f"Pre-order: {sale_data['preorder']}\n"
                f"Logged to: {logged_day}\n\n"
                "Tap /sale to log another."
            )
        else:
            await query.edit_message_text(
                "⚠️ Something went wrong saving. Please try /sale again."
            )
        context.user_data["sale"] = {}

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sale", sale))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, name_input))
    app.run_polling()

if __name__ == "__main__":
    main()