import logging
import json
import os
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
    """Read product name, category, and brand live from the COMEX tab."""
    ws = get_client().open(SPREADSHEET_NAME).worksheet(PRODUCT_TAB)
    rows = ws.get_all_values()
    products = []
    for row in rows[2:]:  # skip the two header rows
        name = row[0].strip() if len(row) > 0 else ""      # column A
        category = row[3].strip() if len(row) > 3 else ""  # column D
        brand = row[4].strip() if len(row) > 4 else ""     # column E
        if name:
            products.append({
                "name": name,
                "category": category or "Other",
                "brand": brand or "Other",
            })
    return products

def save_sale(name, product, delivery, preorder):
    ws = get_client().open(SPREADSHEET_NAME).worksheet(SALES_TAB)

    # Find the first empty row based on the Name column (column B)
    names = ws.col_values(2)  # column B = Name
    next_row = len(names) + 1  # first row after the last filled Name

    # S/N = count of existing data rows (excluding header)
    sn = next_row - 1

    values = [
        sn,
        name,
        product,
        datetime.now().strftime("%-I:%M%p"),   # e.g. 3:40PM
        delivery,
        preorder,
    ]
    ws.update(f"A{next_row}:F{next_row}", [values])

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
    brands = sorted(set(p["brand"] for p in products))
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
        cats = sorted(set(p["category"] for p in products if p["brand"] == value))
        await query.edit_message_text(
            f"Brand: {value}\n\nSelect a category:",
            reply_markup=build_keyboard(cats, "cat", per_row=2)
        )

    elif step == "cat":
        sale_data["cat"] = value
        brand = sale_data["brand"]
        models = [p["name"] for p in products
                  if p["brand"] == brand and p["category"] == value]
        context.user_data["models"] = models
        buttons = [InlineKeyboardButton(m, callback_data=f"model:{i}")
                   for i, m in enumerate(models)]
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