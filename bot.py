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

BOT_TOKEN = os.environ["8693678368:AAGGdz-rX9yy6WcV0ytpcizKey9R0nZ6WSE"]
SHEET_NAME = "Tradeshow Prints Marshall/Sonos (Sales Sheet)"

logging.basicConfig(level=logging.INFO)

# ---------- CONFIG: edit for your event ----------
PRODUCTS = ["Wireless Earbuds", "Power Bank", "Smart Watch", "Bluetooth Speaker"]
COLOURS  = ["Black", "White", "Blue", "Red"]
QUANTITIES = [1, 2, 3, 4, 5]
# -------------------------------------------------

# ---------- Google Sheets ----------
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).sheet1

def save_sale(promoter, product, colour, qty):
    sheet = get_sheet()
    sheet.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      promoter, product, colour, str(qty)])

# ---------- Keyboards ----------
def build_keyboard(items, prefix):
    buttons = [
        InlineKeyboardButton(str(item), callback_data=f"{prefix}:{item}")
        for item in items
    ]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tap /sale to log a sale.")

async def sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sale"] = {}
    await update.message.reply_text(
        "Select a product:",
        reply_markup=build_keyboard(PRODUCTS, "product")
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    step, value = query.data.split(":", 1)
    sale_data = context.user_data.setdefault("sale", {})

    if step == "product":
        sale_data["product"] = value
        await query.edit_message_text(
            f"Product: {value}\n\nSelect a colour:",
            reply_markup=build_keyboard(COLOURS, "colour")
        )

    elif step == "colour":
        sale_data["colour"] = value
        await query.edit_message_text(
            f"Product: {sale_data['product']}\nColour: {value}\n\nSelect quantity:",
            reply_markup=build_keyboard(QUANTITIES, "qty")
        )

    elif step == "qty":
        sale_data["qty"] = value
        promoter = query.from_user.first_name
        save_sale(promoter, sale_data["product"], sale_data["colour"], sale_data["qty"])
        await query.edit_message_text(
            "Recorded ✅\n\n"
            f"Promoter: {promoter}\n"
            f"Product: {sale_data['product']}\n"
            f"Colour: {sale_data['colour']}\n"
            f"Quantity: {sale_data['qty']}\n\n"
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