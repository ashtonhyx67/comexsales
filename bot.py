import logging
import json
import os
import re
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_NAME = "Tradeshow Prints Marshall/Sonos (Sales Sheet)"
PRODUCT_TAB = "COMEX Show 2026"
SALES_TAB = "Sales Tracker"

logging.basicConfig(level=logging.INFO)

# ---------- Google Sheets ----------
def get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

def get_products():
    """Read item_desc column live from the COMEX tab."""
    ws = get_client().open(SPREADSHEET_NAME).worksheet(PRODUCT_TAB)
    col = ws.col_values(1)  # column A = item_desc
    # skip header rows, drop blanks
    products = [p.strip() for p in col[2:] if p.strip()]
    return products

def save_sale(name, product, delivery, preorder):
    ws = get_client().open(SPREADSHEET_NAME).worksheet(SALES_TAB)
    # S/N = number of existing data rows (minus header) + 1
    existing = ws.col_values(1)
    sn = len([v for v in existing[1:] if v.strip()]) + 1
    ws.append_row([
        sn,
        name,
        product,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        delivery,
        preorder,
    ])

# ---------- Categorisation ----------
def categorise(product):
    p = product.lower()
    if "soundbar" in p or "arc" in p or "beam" in p or "heston" in p:
        return "Soundbars"
    if "sub" in p:
        return "Subwoofers"
    if any(k in p for k in ["monitor", "major", "minor", "motif", "ace", "mode"]):
        return "Headphones"
    if any(k in p for k in ["emberton", "willen", "kilburn", "middleton", "roam", "move"]):
        return "Portable Speakers"
    return "Home Speakers"

def get_brand(product):
    return "Marshall" if product.lower().startswith("marshall") else "Sonos"

# ---------- Keyboard builder ----------
def build_keyboard(items, prefix, per_row=1):
    buttons = [InlineKeyboardButton(str(i), callback_data=f"{prefix}:{i}"[:64]) for i in items]
    rows = [buttons[i:i+per_row] for i in range(0, len(buttons), per_row)]
    return InlineKeyboardMarkup(rows)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tap /sale to log a sale.")

async def sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sale"] = {}
    products = get_products()
    context.user_data["all_products"] = products
    brands = sorted(set(get_brand(p) for p in products))
    await update.message.reply_text(
        "Select a brand:",
        reply_markup=build_keyboard(brands, "brand", per_row=2)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    step, value = query.data.split(":", 1)
    sale_data = context.user_data.setdefault("sale", {})
    products = context.user_data.get("all_products", [])

    if step == "brand":
        sale_data["brand"] = value
        cats = sorted(set(categorise(p) for p in products if get_brand(p) == value))
        await query.edit_message_text(
            f"Brand: {value}\n\nSelect a category:",
            reply_markup=build_keyboard(cats, "cat", per_row=2)
        )

    elif step == "cat":
        sale_data["cat"] = value
        brand = sale_data["brand"]
        models = [p for p in products if get_brand(p) == brand and categorise(p) == value]
        # store models indexed, since names are long (callback_data max 64 bytes)
        context.user_data["models"] = models
        buttons = [InlineKeyboardButton(m, callback_data=f"model:{i}") for i, m in enumerate(models)]
        rows = [[b] for b in buttons]
        await query.edit_message_text(
            f"{sale_data['brand']} › {value}\n\nSelect a model:",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif step == "model":
        models = context.user_data.get("models", [])
        sale_data["product"] = models[int(value)]
        await query.edit_message_text(
            f"Product: {sale_data['product']}\n\nDelivery?",
            reply_markup=build_keyboard(["Yes", "No"], "delivery", per_row=2)
        )

    elif step == "delivery":
        sale_data["delivery"] = value
        await query.edit_message_text(
            f"Product: {sale_data['product']}\nDelivery: {value}\n\nPre-order?",
            reply_markup=build_keyboard(["Yes", "No"], "preorder", per_row=2)
        )

    elif step == "preorder":
        sale_data["preorder"] = value
        name = query.from_user.first_name
        save_sale(name, sale_data["product"], sale_data["delivery"], sale_data["preorder"])
        await query.edit_message_text(
            "Recorded ✅\n\n"
            f"Name: {name}\n"
            f"Product: {sale_data['product']}\n"
            f"Delivery: {sale_data['delivery']}\n"
            f"Pre-order: {sale_data['preorder']}\n\n"
            "Tap /sale to log another."
        )
        context.user_data["sale"] = {}

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sale", sale))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()