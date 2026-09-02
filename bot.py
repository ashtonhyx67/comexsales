import asyncio
import logging
import json
import os
import re
import threading
import time
from typing import NamedTuple
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

# Built once: ZoneInfo() re-reads the tz database on a cache miss, and this is on
# the path of every single sale.
SGT = ZoneInfo("Asia/Singapore")

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
# The catalogue is fixed for the whole show, so it's read once and then shaped
# into the exact structure the menus need: {brand: {category: [model, ...]}}.
# Building it here means a button press is a dict lookup instead of a sort over
# every product, on every screen, for every promoter.
_catalog = {"brands": [], "tree": {}}
_cache_time = 0.0
_product_refreshing = False


def _build_catalog(products):
    tree = {}
    for prod in products:
        tree.setdefault(prod["brand"], {}).setdefault(prod["category"], []).append(prod["name"])
    return {
        "brands": sorted(tree),
        "tree": {b: {"cats": sorted(c), "models": c} for b, c in tree.items()},
    }


def refresh_products():
    """Blocking - call from a thread only."""
    global _catalog, _cache_time
    with _sheet_lock:
        # A:E only - the product tab is far wider than the three columns we use,
        # and get_all_values() drags every one of them over the wire.
        rows = get_worksheet(PRODUCT_TAB).get("A3:E") or []
    products = []
    for row in rows:  # A3 already skips the two header rows
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
        _catalog = _build_catalog(products)
        _cache_time = time.time()
    return _catalog


async def get_catalog():
    """Never blocks a promoter: serve the cache, refresh in the background when stale.

    Only the very first call (cold cache) actually waits on Google.
    """
    global _product_refreshing
    if not _catalog["brands"]:
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
    return _catalog


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
    """Fallback label, e.g. '3 SEPT (WED)'. Blocks normally carry the sheet's own
    label text ('DAY 1 (3 Sept)') so a confirmation names the block a promoter can
    actually find on the sheet."""
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


class Block(NamedTuple):
    """One show day's 6-column strip on the Sales Tracker tab."""
    show_date: date     # the day the block is for
    letter: str         # its first column ("A", "H", ...)
    data_start: int     # first sheet row of sales data
    label: str          # the sheet's own label text, e.g. "DAY 1 (3 Sept)"


def discover_blocks(ws):
    """Read the day blocks off the sheet as a list of Block, in date order.

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
                # sheet row r+1 holds the headers, so data starts at r+2
                blocks.append(Block(show_date, letter, r + 2, " ".join(text.split())))
                break
        else:
            problems.append(f"{_norm(text)!r} at column {letter} has no "
                            f"'S/N | Name | Product...' header row under it")

    if problems:
        raise LayoutError("; ".join(problems))
    if not blocks:
        raise LayoutError(f"no 'DAY n (d Mon)' labels found in row 1 of '{SALES_TAB}'")

    by_col = sorted(blocks, key=lambda b: col_to_index(b.letter))
    for b1, b2 in zip(by_col, by_col[1:]):
        if col_to_index(b2.letter) - col_to_index(b1.letter) < BLOCK_WIDTH:
            raise LayoutError(f"the day blocks at columns {b1.letter} and {b2.letter} "
                              f"overlap (each needs {BLOCK_WIDTH} columns)")

    blocks.sort(key=lambda b: b.show_date)
    # Two blocks for the same date would make the target ambiguous and silently
    # send every sale to whichever one sorted first.
    for b1, b2 in zip(blocks, blocks[1:]):
        if b1.show_date == b2.show_date:
            raise LayoutError(f"{b1.label!r} (column {b1.letter}) and {b2.label!r} "
                              f"(column {b2.letter}) are both for {b1.show_date}")
    return blocks


def block_for(d, blocks):
    """The block a given date writes into.

    Dates before the show fall into the first day's block; dates after it, the last.
    """
    for b in blocks:
        if d <= b.show_date:
            return b
    return blocks[-1]


# ---------- Layout + row cursors ----------
# The old code re-read the layout AND scanned a whole Name column on every single
# sale - four extra Google round-trips per save, and it still raced: two promoters
# saving at once both read the same "next free row", so one overwrote the other.
# Now the layout is cached and the next free row is tracked in memory, handed out
# by the single writer, so concurrent sales can't collide.
_layout = {"blocks": None, "read_at": 0.0, "cursors": {}}


def _name_col_range(block):
    """The block's Name column, from its first data row down."""
    letter = index_to_col(col_to_index(block.letter) + 1)  # Name is the 2nd column
    return f"{letter}{block.data_start}:{letter}"


def _sync_cursors(ws, blocks):
    """Point each block's cursor at its first free row, never moving it backwards.

    One batch_get covering every day block, instead of one col_values call per
    block, and it starts at the block's own first data row so nothing above the
    block counts. Anything a human parks BELOW the block still pushes the cursor
    past it - deliberately, since skipping a row is safer than overwriting one.
    """
    if not blocks:
        return
    columns = ws.batch_get([_name_col_range(b) for b in blocks])
    for block, values in zip(blocks, columns):
        from_sheet = block.data_start + len(values or [])
        _layout["cursors"][block.letter] = max(
            _layout["cursors"].get(block.letter, 0), from_sheet)


def get_blocks(force=False):
    """Blocking - call from a thread only."""
    with _sheet_lock:
        stale = (time.time() - _layout["read_at"]) > LAYOUT_TTL
        if force or _layout["blocks"] is None or stale:
            ws = get_worksheet(SALES_TAB)
            blocks = discover_blocks(ws)
            # Re-sync cursors in case rows were added to the sheet by hand.
            _sync_cursors(ws, blocks)
            _layout["blocks"] = blocks
            _layout["read_at"] = time.time()
        return _layout["blocks"]


def get_cursor(ws, block):
    if block.letter not in _layout["cursors"]:
        _sync_cursors(ws, [block])
    return _layout["cursors"][block.letter]


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
            block = block_for(s.day, blocks)
            by_block.setdefault(block.letter, (block, []))[1].append((i, s))

        # Lay out every block's rows first, then send them all in ONE batch_update.
        # A burst that spans midnight used to cost one API call per day block.
        writes, planned = [], []
        for block, items in by_block.values():
            try:
                start = get_cursor(ws, block)
            except Exception as e:
                logging.warning(f"couldn't find the next free row in column "
                                f"{block.letter}: {e}")
                for i, _ in items:
                    results[i] = (None, str(e), True)
                continue

            rows, row = [], start
            for _, s in items:
                for _ in range(s.qty):
                    # S/N restarts at 1 for each day block
                    rows.append([row - block.data_start + 1, s.name, s.product,
                                 s.clock, s.delivery, s.preorder])
                    row += 1

            rng = block_range(block.letter, start, row - 1)
            writes.append({"range": rng, "values": rows})
            planned.append((block, row, items, rng))

        if not planned:
            return results

        err = None
        for attempt in range(WRITE_RETRIES):
            try:
                ws.batch_update(writes)
                err = None
                break
            except Exception as e:
                err = str(e)
                ranges = ", ".join(p[3] for p in planned)
                logging.warning(f"write to {ranges} attempt {attempt + 1} failed: {e}")
                if attempt < WRITE_RETRIES - 1:
                    time.sleep(2 ** attempt)

        for block, row, items, _ in planned:
            if err is None:
                # Only commit the rows once they're actually on the sheet, so a
                # failed write doesn't leave a permanent gap in the day block.
                _layout["cursors"][block.letter] = row
            for i, _ in items:
                results[i] = (block.label, None, False) if err is None else (None, err, True)
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
    now = now or datetime.now(SGT)
    return block_for(now.date(), blocks).label


def queue_sale(name, product, delivery, preorder, qty=1, chat_id=None):
    """Accept the sale and return at once - the sheet catches up behind us.

    Returns the day label this sale is bound for, or None if the layout isn't
    known yet. Nothing here touches Google, so it can't make the promoter wait.
    If the write ultimately fails, _report_failure warns the chat.
    """
    global _pending
    now = datetime.now(SGT)
    _write_queue.put_nowait(
        _Sale(name, product, delivery, preorder, qty, now, chat_id))
    _pending += 1
    return label_for_now(now)


# ---------- Keyboard builder ----------
# Callback data is capped at 64 BYTES by Telegram. The old code pasted the label
# straight in and sliced to 64 characters, so a long or non-ASCII brand, category
# or promoter name was silently truncated - the menu then matched nothing and the
# flow dead-ended, or worse, a clipped name went onto the sheet. Everything is
# addressed by index now, and the labels live in user_data.
QTY_CHOICES = [1, 2, 3, 4, 5]
YES_NO = ["Yes", "No"]


def build_keyboard(items, prefix, per_row=1, add_cancel=True, add_back=None):
    buttons = [InlineKeyboardButton(str(item), callback_data=f"{prefix}:{i}")
               for i, item in enumerate(items)]
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    nav = []
    if add_back:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"back:{add_back}"))
    if add_cancel:
        nav.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel:x"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def pick(context, key, value):
    """Resolve an index-based callback back to its label.

    Returns None if the list is gone (the bot restarted under an old message) or
    the index is out of range, so a stale button restarts the flow than raising.
    """
    options = context.user_data.get(key)
    if not options:
        return None
    try:
        idx = int(value)
    except ValueError:
        return None
    return options[idx] if 0 <= idx < len(options) else None


# ---------- Screen renderers (so Back can redraw any step) ----------
def name_prompt(context):
    recent = context.user_data.get("recent_names", [])
    text = "Who made this sale?\n\nType the promoter's name:"
    rows = [[InlineKeyboardButton(n, callback_data=f"name:{i}")]
            for i, n in enumerate(recent)]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:x")])
    if recent:
        text += "\n\n(or tap a recent name below)"
    return text, InlineKeyboardMarkup(rows)


def remember_name(context, name):
    recent = [n for n in context.user_data.get("recent_names", []) if n.lower() != name.lower()]
    recent.insert(0, name)
    context.user_data["recent_names"] = recent[:5]


def brand_screen(context):
    brands = context.user_data["catalog"]["brands"]
    context.user_data["brands"] = brands
    promoter = context.user_data.get("sale", {}).get("name", "")
    return (f"Promoter: {promoter}\n\nSelect a brand:",
            build_keyboard(brands, "brand", per_row=2, add_cancel=True, add_back="name"))


def category_screen(context, brand):
    cats = context.user_data["catalog"]["tree"][brand]["cats"]
    context.user_data["cats"] = cats
    return (f"Brand: {brand}\n\nSelect a category:",
            build_keyboard(cats, "cat", per_row=2, add_cancel=True, add_back="brand"))


def model_screen(context, brand, cat):
    models = context.user_data["catalog"]["tree"][brand]["models"][cat]
    context.user_data["models"] = models
    return (f"{brand} › {cat}\n\nSelect a model:",
            build_keyboard(models, "model", per_row=1, add_cancel=True, add_back="cat"))


def qty_screen(context, product):
    return (f"Product: {product}\n\nSelect quantity:",
            build_keyboard(QTY_CHOICES, "qty", per_row=5,
                           add_cancel=True, add_back="model"))


def delivery_screen(context, product, qty):
    return (f"Product: {product}\nQty: {qty}\n\nDelivery?",
            build_keyboard(YES_NO, "delivery", per_row=2,
                           add_cancel=True, add_back="qty"))


def preorder_screen(context, product, qty, delivery):
    return (f"Product: {product}\nQty: {qty}\nDelivery: {delivery}\n\nPre-order?",
            build_keyboard(YES_NO, "preorder", per_row=2,
                           add_cancel=True, add_back="delivery"))


AGAIN_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton("➕ Log another sale", callback_data="again:x")]])


# ---------- Flow-message helpers ----------
# In a group several flows run side by side. Each promoter's flow owns exactly one
# message: buttons on anyone else's message are ignored, and every step edits that
# one message rather than sending a new one, which keeps a busy group well under
# Telegram's per-chat send limit.
def owns(context, query):
    # query.message is None once Telegram considers the message inaccessible
    # (too old, or deleted), so it can't be dereferenced blindly.
    msg = query.message
    return msg is not None and context.user_data.get("flow_msg") == msg.message_id


async def edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def fixed(options, value):
    """Resolve an index against a constant list (quantities, Yes/No)."""
    return options[int(value)] if value.isdigit() and int(value) < len(options) else None


async def restart_flow(query, context, note):
    """A stale or unresolvable button: say so and put the promoter back at step 1."""
    context.user_data["sale"] = {}
    context.user_data["awaiting_name"] = True
    context.user_data["catalog"] = await get_catalog()
    text, markup = name_prompt(context)
    await edit(query, f"{note}\n\n{text}", markup)


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

    today = datetime.now(SGT).date()
    target = block_for(today, blocks)
    lines = []
    for b in blocks:
        end = index_to_col(col_to_index(b.letter) + BLOCK_WIDTH - 1)
        mark = "  ← today" if b == target else ""
        next_row = _layout["cursors"].get(b.letter, b.data_start)
        lines.append(f"{b.label} ({day_label(b.show_date)}): {b.letter}–{end}, "
                     f"next row {next_row}{mark}")
    queued = (f"\n\n⏳ {_pending} sale(s) still being written."
              if _pending else "\n\nAll sales are on the sheet.")
    await update.message.reply_text(
        f"✅ {len(blocks)} day block(s) found, headers OK:\n\n"
        + "\n".join(lines) + queued)


async def sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sale"] = {}
    context.user_data["catalog"] = await get_catalog()  # cached, no wait
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
    if not context.user_data.get("catalog"):
        context.user_data["catalog"] = await get_catalog()
    text, markup = brand_screen(context)
    msg = await update.message.reply_text(text, reply_markup=markup)
    context.user_data["flow_msg"] = msg.message_id


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    step, _, value = (query.data or "").partition(":")
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
        await restart_flow(query, context, "Next sale.")
        return

    # Every step below needs the catalogue and the choices made so far. After a
    # restart user_data is empty, so an old message's buttons would have raised a
    # KeyError and left the promoter staring at a dead screen; now they get the
    # name prompt back instead.
    if not context.user_data.get("catalog"):
        await restart_flow(query, context, "That sale expired — starting over.")
        return

    # ----- Back -----
    if step == "back":
        try:
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
                text, markup = delivery_screen(context, sale_data["product"],
                                               sale_data["qty"])
            else:
                return
        except KeyError:
            await restart_flow(query, context, "Lost track of that sale — starting over.")
            return
        await edit(query, text, markup)
        return

    # ----- Forward flow -----
    # Buttons carry indices, so each label is resolved against the list that was
    # on screen; a stale index restarts the flow rather than logging the wrong
    # product or a truncated promoter name.
    if step == "name":
        name = pick(context, "recent_names", value)
        if name is None:
            await restart_flow(query, context, "That name is no longer on the list.")
            return
        sale_data["name"] = name
        context.user_data["awaiting_name"] = False
        remember_name(context, name)
        text, markup = brand_screen(context)
        await edit(query, text, markup)

    elif step == "brand":
        brand = pick(context, "brands", value)
        if brand is None or brand not in context.user_data["catalog"]["tree"]:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        sale_data["brand"] = brand
        text, markup = category_screen(context, brand)
        await edit(query, text, markup)

    elif step == "cat":
        cat = pick(context, "cats", value)
        if cat is None or "brand" not in sale_data:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        sale_data["cat"] = cat
        text, markup = model_screen(context, sale_data["brand"], cat)
        await edit(query, text, markup)

    elif step == "model":
        product = pick(context, "models", value)
        if product is None:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        sale_data["product"] = product
        text, markup = qty_screen(context, product)
        await edit(query, text, markup)

    elif step == "qty":
        qty = fixed(QTY_CHOICES, value)
        if qty is None or "product" not in sale_data:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        sale_data["qty"] = qty
        text, markup = delivery_screen(context, sale_data["product"], qty)
        await edit(query, text, markup)

    elif step == "delivery":
        answer = fixed(YES_NO, value)
        if answer is None or "qty" not in sale_data:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        sale_data["delivery"] = answer
        text, markup = preorder_screen(context, sale_data["product"],
                                       sale_data["qty"], answer)
        await edit(query, text, markup)

    elif step == "preorder":
        answer = fixed(YES_NO, value)
        if answer is None or "delivery" not in sale_data:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        name = sale_data.get("name") or query.from_user.first_name
        product = sale_data["product"]
        qty = int(sale_data["qty"])
        delivery, preorder = sale_data["delivery"], answer
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
    """Never leave a promoter looking at a frozen screen.

    Anything the per-step guards miss still has to say something: the flow's
    message is the promoter's only feedback, and silence looks exactly like a
    sale that went through.
    """
    logging.error("handler error", exc_info=context.error)
    if not isinstance(update, Update):
        return
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(
                "Something went wrong — tap /sale to start again.", show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(
                "Something went wrong — tap /sale to start again.")
    except Exception:
        logging.debug("couldn't deliver the error notice", exc_info=True)


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
        models = sum(len(m) for b in _catalog["tree"].values()
                     for m in b["models"].values())
        logging.info(f"warm: {models} products across {len(_catalog['brands'])} brand(s), "
                     f"{len(blocks)} day blocks, cursors {_layout['cursors']}")
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
