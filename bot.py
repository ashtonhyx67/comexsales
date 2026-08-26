import asyncio
import logging
import json
import os
import re
import threading
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_NAME = "Tradeshow Prints Marshall/Sonos (Sales Sheet)"
PRODUCT_TAB = "COMEX Show 2026"
SALES_TAB = "Sales Tracker"

# ---------- Show-day layout ----------
# Each show day gets its own 6-column block on the Sales Tracker tab:
#   a day label row ("DAY 1 (3 Sept)"), then the header row (S/N, Name,
#   Product (Colour), Time, Delivery?, Pre-order?), then the sales rows beneath it.
# The layout is read off the sheet rather than hardcoded, so an inserted column
# or an extra day block can't silently send sales to the wrong place.
SHOW_YEAR = 2026  # the day labels carry no year
BLOCK_WIDTH = 6   # S/N, Name, Product (Colour), Time, Delivery?, Pre-order?
EXPECTED_HEADERS = ["s/n", "name", "product", "time", "delivery", "pre-order"]
DAY_LABEL_RE = re.compile(r"^\s*DAY\s*\d+\s*\(\s*(\d{1,2})\s*([A-Za-z]+)", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]

# ---------- Throughput tuning ----------
# The show is the stress test: ~8 promoters logging several items each at once.
# Everything below exists to keep one slow Google call from stalling everyone.
LAYOUT_TTL = 300      # re-read the day-block map at most every 5 min
PRODUCT_TTL = 600     # products don't change mid-show; serve stale, refresh behind
FLUSH_WINDOW = 0.25   # coalesce sales arriving within this window into one write
SHEET_TIMEOUT = 20    # don't let a hung Google request wedge the writer
WRITE_RETRIES = 3     # attempts within one round
MAX_ROUNDS = 3        # rounds before a sale is declared lost and reported
REQUEUE_DELAY = 5     # seconds before retrying a failed round (grows per round)
MAX_BATCH = 50
DRAIN_TIMEOUT = 60    # how long to keep writing after shutdown is requested


class LayoutError(Exception):
    """The Sales Tracker tab doesn't look the way the bot expects."""


logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------- Google Sheets ----------
# Every gspread call is blocking HTTP, so it only ever runs inside a worker
# thread (never on the event loop) and under _sheet_lock (one shared session).
_sheet_lock = threading.RLock()
_client = None
_spreadsheet = None
_worksheets = {}


def get_client():
    global _client
    if _client is None:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        _client = gspread.authorize(creds)
        try:
            _client.set_timeout(SHEET_TIMEOUT)
        except Exception:  # older gspread
            pass
    return _client


def get_spreadsheet():
    """Cached. open() by name is a Drive search - far too slow to repeat per sale."""
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = get_client().open(SPREADSHEET_NAME)
    return _spreadsheet


def get_worksheet(title):
    """Cached. worksheet() refetches spreadsheet metadata on every call otherwise."""
    ws = _worksheets.get(title)
    if ws is None:
        ws = get_spreadsheet().worksheet(title)
        _worksheets[title] = ws
    return ws


def reset_sheet_cache():
    """Drop cached handles so the next call reconnects (used after hard failures)."""
    global _client, _spreadsheet
    _client = None
    _spreadsheet = None
    _worksheets.clear()
    _layout["blocks"] = None


# ---------- Product cache ----------
_products_cache = []
_cache_time = 0.0
_product_refreshing = False


def refresh_products():
    """Blocking - call from a thread only."""
    global _products_cache, _cache_time
    with _sheet_lock:
        rows = get_worksheet(PRODUCT_TAB).get_all_values()
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
    if products:
        _products_cache = products
        _cache_time = time.time()
    return _products_cache


async def get_products():
    """Never blocks a promoter: serve the cache, refresh in the background when stale.

    Only the very first call (cold cache) actually waits on Google.
    """
    global _product_refreshing
    if not _products_cache:
        return await asyncio.to_thread(refresh_products)
    if time.time() - _cache_time > PRODUCT_TTL and not _product_refreshing:
        _product_refreshing = True

        async def _bg():
            global _product_refreshing
            try:
                await asyncio.to_thread(refresh_products)
            except Exception as e:
                logging.warning(f"product refresh failed, keeping cache: {e}")
            finally:
                _product_refreshing = False

        asyncio.create_task(_bg())
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


def block_range(start_letter, row_start, row_end):
    c0 = col_to_index(start_letter)
    return f"{start_letter}{row_start}:{index_to_col(c0 + BLOCK_WIDTH - 1)}{row_end}"


def clock(now):
    """'3:40PM'. %-I is glibc-only, so build the 12-hour time by hand."""
    return f"{now.hour % 12 or 12}:{now.minute:02d}{now.strftime('%p')}"


# ---------- Layout discovery ----------
def _norm(text):
    return " ".join(text.split()).strip().lower()


def parse_day_label(text):
    """'DAY 1 (3 Sept)' -> date(SHOW_YEAR, 9, 3). None if it isn't a day label."""
    m = DAY_LABEL_RE.match(text or "")
    if not m:
        return None
    day, month = int(m.group(1)), m.group(2)[:3].lower()
    if month not in MONTHS:
        return None
    try:
        return date(SHOW_YEAR, MONTHS.index(month) + 1, day)
    except ValueError:
        return None


def header_at(row, c0):
    """True if the BLOCK_WIDTH cells starting at 0-based column c0 are the block headers."""
    cells = [row[i] if i < len(row) else "" for i in range(c0, c0 + BLOCK_WIDTH)]
    return all(_norm(c).startswith(h) for c, h in zip(cells, EXPECTED_HEADERS))


def discover_blocks(ws):
    """Read the day blocks off the sheet: [(show_date, start_letter, data_start_row), ...].

    Raises LayoutError rather than guessing, so a reshuffled sheet stops the bot
    instead of dropping sales into the wrong day's columns.
    """
    top = ws.get(f"A1:{index_to_col(ws.col_count)}8") or []
    label_row = top[0] if top else []

    blocks, problems = [], []
    for c0, text in enumerate(label_row):
        show_date = parse_day_label(text)
        if show_date is None:
            continue
        letter = index_to_col(c0 + 1)
        for r in range(1, min(len(top), 6)):  # header sits a row or two below the label
            if header_at(top[r], c0):
                blocks.append((show_date, letter, r + 2))  # sheet row r+1 holds the headers
                break
        else:
            problems.append(f"{_norm(text)!r} at column {letter} has no "
                            f"'S/N | Name | Product...' header row under it")

    if problems:
        raise LayoutError("; ".join(problems))
    if not blocks:
        raise LayoutError(f"no 'DAY n (d Mon)' labels found in row 1 of '{SALES_TAB}'")

    by_col = sorted(blocks, key=lambda b: col_to_index(b[1]))
    for (_, l1, _), (_, l2, _) in zip(by_col, by_col[1:]):
        if col_to_index(l2) - col_to_index(l1) < BLOCK_WIDTH:
            raise LayoutError(f"the day blocks at columns {l1} and {l2} overlap "
                              f"(each needs {BLOCK_WIDTH} columns)")

    blocks.sort(key=lambda b: b[0])
    return blocks


def block_for(d, blocks):
    """The block a given date writes into.

    Dates before the show fall into the first day's block; dates after it, the last.
    """
    for b in blocks:
        if d <= b[0]:
            return b
    return blocks[-1]


# ---------- Layout + row cursors ----------
# The old code re-read the layout AND scanned a whole Name column on every single
# sale - four extra Google round-trips per save, and it still raced: two promoters
# saving at once both read the same "next free row", so one overwrote the other.
# Now the layout is cached and the next free row is tracked in memory, handed out
# by the single writer, so concurrent sales can't collide.
_layout = {"blocks": None, "read_at": 0.0, "cursors": {}}


def _sync_cursor(ws, letter, data_start):
    """Point the cursor at the first free row, never moving it backwards."""
    name_col = col_to_index(letter) + 1  # Name is the block's 2nd column
    existing = ws.col_values(name_col)
    from_sheet = max(len(existing) + 1, data_start)
    _layout["cursors"][letter] = max(_layout["cursors"].get(letter, 0), from_sheet)
    return _layout["cursors"][letter]


def get_blocks(force=False):
    """Blocking - call from a thread only."""
    with _sheet_lock:
        stale = (time.time() - _layout["read_at"]) > LAYOUT_TTL
        if force or _layout["blocks"] is None or stale:
            ws = get_worksheet(SALES_TAB)
            _layout["blocks"] = discover_blocks(ws)
            _layout["read_at"] = time.time()
            # Re-sync cursors in case rows were added to the sheet by hand.
            for _, letter, data_start in _layout["blocks"]:
                _sync_cursor(ws, letter, data_start)
        return _layout["blocks"]


def get_cursor(ws, letter, data_start):
    if letter not in _layout["cursors"]:
        _sync_cursor(ws, letter, data_start)
    return _layout["cursors"][letter]


# ---------- Background writer ----------
# Sales queue up here and the promoter is confirmed immediately - nobody waits on
# Google. One worker drains the queue, groups everything bound for the same day
# block into a single contiguous range, and writes the whole burst in ONE API call.
# 8 promoters x 5 items was ~200 Google round-trips; now it's a handful.
_write_queue = None
_app = None
_pending = 0  # sales accepted but not yet on the sheet


class _Sale:
    __slots__ = ("name", "product", "delivery", "preorder", "qty",
                 "day", "clock", "chat_id", "tries")

    def __init__(self, name, product, delivery, preorder, qty, now, chat_id):
        self.name, self.product = name, product
        self.delivery, self.preorder = delivery, preorder
        self.qty = qty
        self.day = now.date()
        self.clock = clock(now)
        self.chat_id = chat_id
        self.tries = 0

    def describe(self):
        """Everything needed to write this row by hand."""
        return (f"Promoter: {self.name}\n"
                f"Product: {self.product}\n"
                f"Qty: {self.qty}\n"
                f"Delivery: {self.delivery}\n"
                f"Pre-order: {self.preorder}\n"
                f"Time: {self.clock}")


def _write_batch(batch):
    """Blocking - runs in a worker thread.

    Returns {index: (day_label, error, retryable)}. A layout problem is not
    retryable - the sheet has to be fixed by a human - but a 5xx or a timeout is.
    """
    results = {}
    with _sheet_lock:
        try:
            ws = get_worksheet(SALES_TAB)
            blocks = get_blocks()
        except LayoutError as e:
            logging.error(f"Sales Tracker layout check failed: {e}")
            reason = f"the '{SALES_TAB}' tab doesn't look right - {e}"
            return {i: (None, reason, False) for i in range(len(batch))}
        except Exception as e:
            logging.warning(f"couldn't open the sheet: {e}")
            reset_sheet_cache()
            return {i: (None, str(e), True) for i in range(len(batch))}

        # Group the burst by the day block each sale lands in.
        by_block = {}
        for i, s in enumerate(batch):
            show_date, letter, data_start = block_for(s.day, blocks)
            by_block.setdefault(letter, (show_date, data_start, []))[2].append((i, s))

        for letter, (show_date, data_start, items) in by_block.items():
            try:
                start = get_cursor(ws, letter, data_start)
            except Exception as e:
                logging.warning(f"couldn't find the next free row in column {letter}: {e}")
                for i, _ in items:
                    results[i] = (None, str(e), True)
                continue

            rows, row = [], start
            for _, s in items:
                for _ in range(s.qty):
                    # S/N restarts at 1 for each day block
                    rows.append([row - data_start + 1, s.name, s.product,
                                 s.clock, s.delivery, s.preorder])
                    row += 1

            rng = block_range(letter, start, row - 1)
            err = None
            for attempt in range(WRITE_RETRIES):
                try:
                    ws.update(rows, rng)
                    err = None
                    break
                except Exception as e:
                    err = str(e)
                    logging.warning(f"write to {rng} attempt {attempt + 1} failed: {e}")
                    if attempt < WRITE_RETRIES - 1:
                        time.sleep(2 ** attempt)

            if err is None:
                # Only commit the rows once they're actually on the sheet, so a
                # failed write doesn't leave a permanent gap in the day block.
                _layout["cursors"][letter] = row
                label = day_label(show_date)
                for i, _ in items:
                    results[i] = (label, None, False)
            else:
                for i, _ in items:
                    results[i] = (None, err, True)
    return results


async def _requeue(sale):
    """Nobody is waiting, so a failed sale can afford to sit and try again."""
    await asyncio.sleep(REQUEUE_DELAY * sale.tries)
    await _write_queue.put(sale)


async def _report_failure(sale, err):
    """The promoter already walked away, so the loss has to be pushed to them."""
    logging.error(f"giving up on {sale.product} for {sale.name}: {err}")
    if _app is None or sale.chat_id is None:
        return
    try:
        await _app.bot.send_message(
            sale.chat_id,
            "⚠️ This sale did NOT reach the sheet — please add it by hand:\n\n"
            f"{sale.describe()}\n\n"
            f"Reason: {err}")
    except Exception:
        logging.exception("couldn't warn the chat about a lost sale")


async def _writer_loop():
    """One consumer, so writes are serialised and rows can never collide."""
    global _pending
    while True:
        try:
            first = await _write_queue.get()
            await asyncio.sleep(FLUSH_WINDOW)  # let the rest of the burst pile in
            batch = [first]
            while not _write_queue.empty() and len(batch) < MAX_BATCH:
                batch.append(_write_queue.get_nowait())

            try:
                results = await asyncio.to_thread(_write_batch, batch)
            except Exception as e:
                logging.exception("batch write failed outright")
                results = {i: (None, str(e), True) for i in range(len(batch))}

            for i, s in enumerate(batch):
                _, err, retryable = results.get(
                    i, (None, "the write was dropped", True))
                if err is None:
                    _pending -= 1
                    continue
                s.tries += 1
                if retryable and s.tries < MAX_ROUNDS:
                    logging.warning(f"requeueing {s.product} for {s.name} "
                                    f"(round {s.tries} of {MAX_ROUNDS})")
                    asyncio.create_task(_requeue(s))
                else:
                    _pending -= 1
                    asyncio.create_task(_report_failure(s, err))
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("writer loop error")
            await asyncio.sleep(1)


def label_for_now(now=None):
    """Today's day label straight from the cached layout - no Google call.

    Lets the confirmation name the right day block without waiting for the write.
    """
    blocks = _layout["blocks"]
    if not blocks:
        return None
    now = now or datetime.now(ZoneInfo("Asia/Singapore"))
    return day_label(block_for(now.date(), blocks)[0])


def queue_sale(name, product, delivery, preorder, qty=1, chat_id=None):
    """Accept the sale and return at once - the sheet catches up behind us.

    Returns the day label this sale is bound for, or None if the layout isn't
    known yet. Nothing here touches Google, so it can't make the promoter wait.
    If the write ultimately fails, _report_failure warns the chat.
    """
    global _pending
    now = datetime.now(ZoneInfo("Asia/Singapore"))
    _write_queue.put_nowait(
        _Sale(name, product, delivery, preorder, qty, now, chat_id))
    _pending += 1
    return label_for_now(now)


# ---------- Keyboard builder ----------
def build_keyboard(items, prefix, per_row=1, add_cancel=True, add_back=None):
    buttons = [InlineKeyboardButton(str(i), callback_data=f"{prefix}:{i}"[:64]) for i in items]
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
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


AGAIN_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton("➕ Log another sale", callback_data="again:x")]])


# ---------- Flow-message helpers ----------
# In a group several flows run side by side. Each promoter's flow owns exactly one
# message: buttons on anyone else's message are ignored, and every step edits that
# one message rather than sending a new one, which keeps a busy group well under
# Telegram's per-chat send limit.
def owns(context, query):
    return context.user_data.get("flow_msg") == query.message.message_id


async def edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tap /sale to log a sale.")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify the Sales Tracker layout without logging anything."""
    try:
        blocks = await asyncio.to_thread(get_blocks, True)
    except LayoutError as e:
        await update.message.reply_text(f"⚠️ '{SALES_TAB}' looks wrong — {e}")
        return
    except Exception as e:
        await update.message.reply_text(f"⚠️ Couldn't read the sheet: {e}")
        return

    today = datetime.now(ZoneInfo("Asia/Singapore")).date()
    target = block_for(today, blocks)
    lines = []
    for show_date, letter, data_start in blocks:
        end = index_to_col(col_to_index(letter) + BLOCK_WIDTH - 1)
        mark = "  ← today" if (show_date, letter, data_start) == target else ""
        next_row = _layout["cursors"].get(letter, data_start)
        lines.append(f"{day_label(show_date)}: {letter}–{end}, "
                     f"next row {next_row}{mark}")
    queued = (f"\n\n⏳ {_pending} sale(s) still being written."
              if _pending else "\n\nAll sales are on the sheet.")
    await update.message.reply_text(
        f"✅ {len(blocks)} day block(s) found, headers OK:\n\n"
        + "\n".join(lines) + queued)


async def sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sale"] = {}
    context.user_data["all_products"] = await get_products()  # cached, no wait
    context.user_data["awaiting_name"] = True
    text, markup = name_prompt(context)
    msg = await update.message.reply_text(text, reply_markup=markup)
    context.user_data["flow_msg"] = msg.message_id


async def name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manager types the promoter's name."""
    if not context.user_data.get("awaiting_name"):
        # In a group the bot sees every message; nagging people who never started
        # a sale is just noise, so stay quiet unless we're actually asking.
        if update.effective_chat.type == "private":
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
        context.user_data["all_products"] = await get_products()
    text, markup = brand_screen(context)
    msg = await update.message.reply_text(text, reply_markup=markup)
    context.user_data["flow_msg"] = msg.message_id


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    step, value = query.data.split(":", 1)
    sale_data = context.user_data.setdefault("sale", {})

    # In a group, only the promoter who opened this flow may drive it.
    if not owns(context, query):
        await query.answer("That's someone else's sale — tap /sale to start your own.",
                           show_alert=True)
        return
    await query.answer()

    # ----- Cancel -----
    if step == "cancel":
        context.user_data["sale"] = {}
        context.user_data["awaiting_name"] = False
        await edit(query, "Cancelled.", AGAIN_MARKUP)
        return

    # ----- Log another: reuses the same message, so no extra send -----
    if step == "again":
        context.user_data["sale"] = {}
        context.user_data["all_products"] = await get_products()
        context.user_data["awaiting_name"] = True
        text, markup = name_prompt(context)
        await edit(query, text, markup)
        return

    # ----- Back -----
    if step == "back":
        if value == "name":
            context.user_data["awaiting_name"] = True
            text, markup = name_prompt(context)
        elif value == "brand":
            text, markup = brand_screen(context)
        elif value == "cat":
            text, markup = category_screen(context, sale_data["brand"])
        elif value == "model":
            text, markup = model_screen(context, sale_data["brand"], sale_data["cat"])
        elif value == "qty":
            text, markup = qty_screen(context, sale_data["product"])
        elif value == "delivery":
            text, markup = delivery_screen(context, sale_data["product"], sale_data["qty"])
        else:
            return
        await edit(query, text, markup)
        return

    # ----- Forward flow -----
    if step == "name":
        sale_data["name"] = value
        context.user_data["awaiting_name"] = False
        remember_name(context, sale_data["name"])
        text, markup = brand_screen(context)
        await edit(query, text, markup)

    elif step == "brand":
        sale_data["brand"] = value
        text, markup = category_screen(context, value)
        await edit(query, text, markup)

    elif step == "cat":
        sale_data["cat"] = value
        text, markup = model_screen(context, sale_data["brand"], value)
        await edit(query, text, markup)

    elif step == "model":
        models = context.user_data.get("models", [])
        sale_data["product"] = models[int(value)]
        text, markup = qty_screen(context, sale_data["product"])
        await edit(query, text, markup)

    elif step == "qty":
        sale_data["qty"] = value
        text, markup = delivery_screen(context, sale_data["product"], value)
        await edit(query, text, markup)

    elif step == "delivery":
        sale_data["delivery"] = value
        text, markup = preorder_screen(context, sale_data["product"],
                                       sale_data["qty"], value)
        await edit(query, text, markup)

    elif step == "preorder":
        name = sale_data.get("name") or query.from_user.first_name
        product = sale_data["product"]
        qty = int(sale_data["qty"])
        delivery, preorder = sale_data["delivery"], value
        context.user_data["sale"] = {}

        # Hand the sale to the background writer and confirm straight away. The
        # promoter can start the next one immediately; the sheet catches up on its
        # own time, and only shouts if a sale can't be written at all.
        label = queue_sale(name, product, delivery, preorder, qty=qty,
                           chat_id=query.message.chat_id)
        await edit(query,
                   "Recorded ✅\n\n"
                   f"Promoter: {name}\n"
                   f"Product: {product}\n"
                   f"Qty: {qty}\n"
                   f"Delivery: {delivery}\n"
                   f"Pre-order: {preorder}\n"
                   + (f"Logged to: {label}" if label else "Saving to the sheet…"),
                   AGAIN_MARKUP)


async def on_error(update, context):
    logging.error("handler error", exc_info=context.error)


_writer_task = None


async def post_init(app):
    """Warm every cache and start the writer before the first promoter taps /sale."""
    global _write_queue, _writer_task, _app
    _app = app
    _write_queue = asyncio.Queue()
    # Deliberately NOT app.create_task: Application.stop() awaits those, and this
    # loop never returns, so registering it there would hang every shutdown.
    _writer_task = asyncio.create_task(_writer_loop())
    try:
        await asyncio.to_thread(refresh_products)
        blocks = await asyncio.to_thread(get_blocks, True)
        logging.info(f"warm: {len(_products_cache)} products, {len(blocks)} day blocks, "
                     f"cursors {_layout['cursors']}")
    except Exception as e:
        logging.warning(f"warm-up failed (will retry on first use): {e}")


async def post_stop(app):
    """Finish writing accepted sales before the process exits.

    Sales are confirmed before they reach the sheet, so quitting with a full queue
    would lose them silently. Give the writer a chance to land them.
    """
    if _pending:
        logging.info(f"draining {_pending} pending sale(s) before shutdown")
    deadline = time.time() + DRAIN_TIMEOUT
    while _pending and time.time() < deadline:
        await asyncio.sleep(0.2)
    if _pending:
        logging.error(f"{_pending} sale(s) STILL unwritten at shutdown - "
                      f"check the sheet against Telegram")
    if _writer_task:
        _writer_task.cancel()


def main():
    app = (Application.builder()
           .token(BOT_TOKEN)
           .concurrent_updates(True)      # promoters no longer queue behind each other
           .connection_pool_size(64)      # enough sockets for 8 parallel flows
           .pool_timeout(30)
           .post_init(post_init)
           .post_stop(post_stop)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sale", sale))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, name_input))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
