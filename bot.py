import logging
import json
import os
import time
from datetime import datetime
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

DIVIDER_PREFIX = "──"

def day_label(tz):
    """e.g. '── WED 3 SEPT ──' for the current show day."""
    now = datetime.now(tz)
    return f"{DIVIDER_PREFIX} {now.strftime('%a').upper()} {now.day} {now.strftime('%b').upper()} {DIVIDER_PREFIX}"

def save_sale(name, product, delivery, preorder, qty=1, retries=3):
    """Write qty rows below the header, inserting a day divider when the date rolls over."""
    tz = ZoneInfo("Asia/Singapore")
    timestamp = datetime.now(tz).strftime("%-I:%M%p")  # e.g. 3:40PM, SG time
    today = day_label(tz)

    for attempt in range(retries):
        try:
            ws = get_spreadsheet().worksheet(SALES_TAB)

            # Find first empty row based on the Name column (B), header is row 1
            names = ws.col_values(2)          # column B = Name
            next_row = len(names) + 1
            if next_row < 2:                  # safety: never write on the header
                next_row = 2

            # Last day divider already written to the sheet, if any
            last_divider = None
            for v in reversed(names):
                if v.strip().startswith(DIVIDER_PREFIX):
                    last_divider = v.strip()
                    break

            rows = []
            if last_divider != today:
                if next_row > 2:              # blank spacer row between days
                    rows.append(["", "", "", "", "", ""])
                rows.append(["", today, "", "", "", ""])

            rows += [["", name, product, timestamp, delivery, preorder]
                     for _ in range(qty)]

            end_row = next_row + len(rows) - 1
            ws.update(f"A{next_row}:F{end_row}", rows)
            return True
        except Exception as e:
            logging.warning(f"save_sale attempt {attempt+1} failed: {e}")
            time.sleep(2 * (attempt + 1))
    return False

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

        ok = save_sale(name, sale_data["product"],
                       sale_data["delivery"], sale_data["preorder"], qty=qty)

        if ok:
            await query.edit_message_text(
                "Recorded ✅\n\n"
                f"Promoter: {name}\n"
                f"Product: {sale_data['product']}\n"
                f"Qty: {qty}\n"
                f"Delivery: {sale_data['delivery']}\n"
                f"Pre-order: {sale_data['preorder']}\n\n"
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