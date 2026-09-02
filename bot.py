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
# Each show day gets its own block of columns on the Sales Tracker tab:
#   a day label row ("DAY 1 (3 Sept)"), then the header row (S/N, Name,
#   Product (Colour), Time, then the flag columns), then the sales rows beneath.
# Blocks need not be the same width - day 1 has a Cash & Carry column the others
# don't - so the layout is read off the sheet rather than hardcoded, and an
# inserted column or an extra day block can't silently send sales astray.
# The bot writes Name onwards only; the S/N column belongs to the sheet.
SHOW_YEAR = 2026  # the day labels carry no year
# Every block opens with these four, in this order, then carries one or more
# "flag" columns. Which flags, and in what order, is read off each block's own
# header row: day 1 has Cash & Carry | Delivery? | Pre-order? while the other
# days still have only Delivery? | Pre-order?, and both have to work.
CORE_HEADERS = ["s/n", "name", "product", "time"]
REQUIRED_FLAGS = ("delivery", "preorder")   # a block without these is malformed
FLAG_NAMES = {"cash": "Cash & Carry", "delivery": "Delivery?", "preorder": "Pre-order?"}
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


def block_range(start_letter, row_start, row_end, width):
    c0 = col_to_index(start_letter)
    return f"{start_letter}{row_start}:{index_to_col(c0 + width - 1)}{row_end}"


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


def flag_kind(text):
    """Which flag column a header cell is, or None if it isn't one."""
    t = _norm(text)
    if t.startswith("cash"):
        return "cash"
    if t.startswith("delivery"):
        return "delivery"
    if t.startswith("pre-order") or t.startswith("pre order") or t.startswith("preorder"):
        return "preorder"
    return None


def header_at(row, c0):
    """The block's flag columns if a header row starts at 0-based column c0.

    Returns e.g. ("cash", "delivery", "preorder") for day 1 and
    ("delivery", "preorder") for the others; None if this isn't a header row.
    The flags are taken in the order they appear, so inserting Cash & Carry in
    the middle of a block moves the value, not the meaning.
    """
    def cell(i):
        return row[i] if i < len(row) else ""

    if not all(_norm(cell(c0 + i)).startswith(h) for i, h in enumerate(CORE_HEADERS)):
        return None
    flags = []
    for i in range(c0 + len(CORE_HEADERS), len(row)):
        kind = flag_kind(cell(i))
        if kind is None or kind in flags:
            break
        flags.append(kind)
    return tuple(flags) or None


class Block(NamedTuple):
    """One show day's strip of columns on the Sales Tracker tab."""
    show_date: date     # the day the block is for
    letter: str         # its first column ("A", "I", ...)
    data_start: int     # first sheet row of sales data
    label: str          # the sheet's own label text, e.g. "DAY 1 (3 Sept)"
    flags: tuple        # flag columns in sheet order, e.g. ("cash", "delivery", "preorder")

    @property
    def width(self):
        return len(CORE_HEADERS) + len(self.flags)

    @property
    def name_col(self):
        """The Name column - the first one the bot writes.

        S/N is deliberately skipped: the sheet owns that column, whether it's
        pre-numbered by hand or filled by a formula. The bot only ever writes
        Name onwards, so nothing it does can disturb the numbering.
        """
        return index_to_col(col_to_index(self.letter) + 1)

    @property
    def write_width(self):
        """How many columns the bot writes - everything but S/N."""
        return self.width - 1


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
            flags = header_at(top[r], c0)
            if flags:
                missing = [f for f in REQUIRED_FLAGS if f not in flags]
                if missing:
                    problems.append(
                        f"{_norm(text)!r} at column {letter} has no "
                        + " or ".join(FLAG_NAMES[f] for f in missing) + " column")
                    break
                # sheet row r+1 holds the headers, so data starts at r+2
                blocks.append(Block(show_date, letter, r + 2,
                                    " ".join(text.split()), flags))
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
        if col_to_index(b2.letter) - col_to_index(b1.letter) < b1.width:
            raise LayoutError(f"the day blocks at columns {b1.letter} and {b2.letter} "
                              f"overlap ({b1.label} needs {b1.width} columns)")

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
    """The block's Name column, from its first data row down.

    The cursor is found from Name, never from S/N, so a row that carries only a
    pre-printed serial number still counts as empty and gets filled.
    """
    return f"{block.name_col}{block.data_start}:{block.name_col}"


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
    """One transaction: a single product, or every product in a bundle.

    A bundle is held as ONE _Sale rather than split into several, so the whole
    thing succeeds or fails together - a promoter can't end up with two of the
    three bundle items on the sheet and no idea which one is missing.
    """
    __slots__ = ("name", "lines", "bundle", "fulfilment",
                 "day", "clock", "chat_id", "tries")

    def __init__(self, name, lines, fulfilment, now, chat_id, bundle=None):
        self.name = name
        self.lines = tuple(lines)   # ((product, count), ...) - one row per count
        # The choice is kept as-is and turned into cells at write time, because
        # which columns exist differs from one day block to the next.
        self.fulfilment = fulfilment         # "Delivery" / "Pre-Order" / "Cash & Carry"
        self.bundle = bundle        # bundle name, or None for a single product
        self.day = now.date()
        self.clock = clock(now)
        self.chat_id = chat_id
        self.tries = 0

    @property
    def units(self):
        """How many sheet rows this sale becomes."""
        return sum(count for _, count in self.lines)

    def summary(self):
        """The product line(s) for a confirmation or a failure notice."""
        items = "\n".join(f"  • {count} × {product}" for product, count in self.lines)
        if self.bundle:
            return f"Bundle: {self.bundle}\n{items}"
        product, count = self.lines[0]
        return f"Product: {product}\nQty: {count}"

    def describe(self):
        """Everything needed to write these rows by hand."""
        return (f"Promoter: {self.name}\n"
                f"{self.summary()}\n"
                f"{self.fulfilment}\n"
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
                # A bundle contributes one row per unit of each of its products,
                # written exactly like individually-logged sales, so a bundle of
                # four counts as four units.
                cells = flag_cells(s.fulfilment, block.flags)
                for product, count in s.lines:
                    for _ in range(count):
                        rows.append([s.name, product, s.clock] + cells)
                        row += 1

            rng = block_range(block.name_col, start, row - 1, block.write_width)
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
    logging.error(f"giving up on {sale.bundle or sale.lines[0][0]} "
                  f"for {sale.name}: {err}")
    if _app is None or sale.chat_id is None:
        return
    try:
        await _app.bot.send_message(
            sale.chat_id,
            f"⚠️ This sale did NOT reach the sheet — please add "
            f"{'these rows' if sale.units > 1 else 'it'} by hand:\n\n"
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
                    logging.warning(f"requeueing {s.bundle or s.lines[0][0]} "
                                    f"for {s.name} "
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


def queue_sale(name, lines, fulfilment, chat_id=None, bundle=None):
    """Accept the sale and return at once - the sheet catches up behind us.

    `lines` is [(product, count), ...] - a single product for a normal sale, or
    every product in a bundle. Returns the day label this sale is bound for, or
    None if the layout isn't known yet. Nothing here touches Google, so it can't
    make the promoter wait. If the write ultimately fails, _report_failure warns
    the chat.
    """
    global _pending
    now = datetime.now(SGT)
    _write_queue.put_nowait(
        _Sale(name, lines, fulfilment, now, chat_id, bundle))
    _pending += 1
    return label_for_now(now)


# ---------- Bundle deals ----------
# Each bundle is a name and the products it contains, as (product, how many).
# {colour} is filled in from the promoter's colour choice - every product below
# comes in Black and White and always spells the colour last, so one placeholder
# covers both. A product with no {colour} is written as-is.
#
# The finished strings must match column A of 'COMEX Show 2026' EXACTLY. /check
# verifies every product in every colour and names anything that doesn't match,
# so run it once after editing this. Set BUNDLES = {} to hide the Bundles button.
BUNDLE_COLOURS = ("Black", "White")

BUNDLES = {
    "Arc Ultra + Sub 4": [
        ("Sonos Arc Ultra Smart Soundbar {colour}", 1),
        ("Sonos Sub Gen4 Wireless Subwoofer {colour}", 1),
    ],
    "Arc Ultra + Sub 4 + 2× Era 300": [
        ("Sonos Arc Ultra Smart Soundbar {colour}", 1),
        ("Sonos Sub Gen4 Wireless Subwoofer {colour}", 1),
        ("Sonos Era 300 Stereo Speaker w Dolby Atmos {colour}", 2),
    ],
    "Beam (Gen 2) + Sub Mini": [
        ("Sonos Beam Gen2 Smart Soundbar {colour}", 1),
        ("Sonos Sub Mini Compact Subwoofer {colour}", 1),
    ],
    "Beam (Gen 2) + Sub Mini + 2× Era 100 SL": [
        ("Sonos Beam Gen2 Smart Soundbar {colour}", 1),
        ("Sonos Sub Mini Compact Subwoofer {colour}", 1),
        ("Sonos Era 100 SL Home Bookshelf Speaker {colour}", 2),
    ],
}

# The Bundles entry sits alongside the brands on the first product screen, so a
# promoter reaches a bundle in the same number of taps as anything else.
BUNDLE_MENU_LABEL = "🎁 Bundles"
MIX_LABEL = "🎨 Mix colours…"


def item_label(template):
    """'Sonos Arc Ultra Smart Soundbar {colour}' -> 'Sonos Arc Ultra Smart Soundbar'."""
    return template.replace("{colour}", "").strip()


def bundle_lines(bundle, colours, qty=1):
    """The bundle's products as [(full name, count), ...].

    `colours` is one colour for the whole bundle, or one per line for a mix.
    """
    lines = BUNDLES[bundle]
    if isinstance(colours, str):
        colours = [colours] * len(lines)
    return [(template.format(colour=colour), count * qty)
            for (template, count), colour in zip(lines, colours)]


def bundle_summary(bundle, colours=None, indent="  "):
    """The bundle's contents as display lines, with colours once they're chosen."""
    if colours:
        pairs = bundle_lines(bundle, colours)
    else:
        pairs = [(item_label(t), n) for t, n in BUNDLES[bundle]]
    return "\n".join(f"{indent}• {count} × {name}" for name, count in pairs)


def bundle_problems(catalog):
    """Bundle products that don't exist in the product tab, as readable strings.

    Checked in EVERY colour: a bundle that works in Black and silently writes an
    unknown name in White is exactly the kind of thing nobody spots until the
    show is over. Run at start-up and by /check.
    """
    known = {name for brand in catalog["tree"].values()
             for models in brand["models"].values() for name in models}
    if not known:
        return []  # catalogue not loaded yet - nothing to check against
    return [f"{bundle!r}: {name!r} is not in '{PRODUCT_TAB}'"
            for bundle, lines in BUNDLES.items()
            for template, _ in lines
            for name in {template.format(colour=c) for c in BUNDLE_COLOURS}
            if name not in known]


# ---------- Keyboard builder ----------
# Callback data is capped at 64 BYTES by Telegram. The old code pasted the label
# straight in and sliced to 64 characters, so a long or non-ASCII brand, category
# or promoter name was silently truncated - the menu then matched nothing and the
# flow dead-ended, or worse, a clipped name went onto the sheet. Everything is
# addressed by index now, and the labels live in user_data.
QTY_CHOICES = [1, 2, 3, 4, 5]
YES_NO = ["Yes", "No"]

# How the sale is fulfilled - one question instead of the old Delivery? then
# Pre-order? pair, because the three cases are mutually exclusive in practice.
# The Sales Tracker still has its two Yes/No columns and they are untouched, so
# nothing downstream has to change: each choice writes a distinct pair, and
# Cash & Carry is the "neither" the sheet already used for a walk-out sale.
#   (button label, short name)
FULFILMENTS = [
    ("🚚 Delivery", "Delivery"),
    ("📦 Pre-Order", "Pre-Order"),
    ("🛍 Cash & Carry", "Cash & Carry"),
]

# What each choice writes into each flag column. A block that hasn't got a given
# column simply doesn't get that value - which is why Cash & Carry still reads
# correctly on the day blocks that only have Delivery? and Pre-order?: it is the
# "No to both" those two columns already meant.
FULFILMENT_CELLS = {
    "Delivery": {"cash": "No", "delivery": "Yes", "preorder": "No"},
    "Pre-Order": {"cash": "No", "delivery": "No", "preorder": "Yes"},
    "Cash & Carry": {"cash": "Yes", "delivery": "No", "preorder": "No"},
}


def flag_cells(fulfilment, flags):
    """The flag-column values for this sale, in one block's column order."""
    cells = FULFILMENT_CELLS.get(fulfilment, {})
    return [cells.get(flag, "") for flag in flags]


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
    # Bundles sit at the front of the same menu rather than behind a separate
    # command: it's one screen a promoter is already looking at, and a bundle is
    # then the same number of taps as a single product.
    entries = ([BUNDLE_MENU_LABEL] if BUNDLES else []) + context.user_data["catalog"]["brands"]
    context.user_data["brands"] = entries
    promoter = context.user_data.get("sale", {}).get("name", "")
    return (f"Promoter: {promoter}\n\nSelect a brand:",
            build_keyboard(entries, "brand", per_row=2, add_cancel=True, add_back="name"))


def bundle_screen(context):
    names = list(BUNDLES)
    context.user_data["bundles"] = names
    listing = "\n\n".join(f"• {name}\n{bundle_summary(name, indent='   ')}"
                          for name in names)
    return ("Select a bundle:\n\n" + listing,
            build_keyboard(names, "bundle", per_row=1, add_cancel=True, add_back="brand"))


def colour_screen(context, bundle):
    """One tap for the whole bundle - all-black or all-white is what nearly every
    customer takes. Mix is offered only when there is more than one product to
    differ, and walks them item by item."""
    options = [f"{'⚫' if c == 'Black' else '⚪'} All {c}" for c in BUNDLE_COLOURS]
    if len(BUNDLES[bundle]) > 1:
        options.append(MIX_LABEL)
    context.user_data["colour_options"] = options
    return (f"Bundle: {bundle}\n{bundle_summary(bundle)}\n\nWhat colour?",
            build_keyboard(options, "colour", per_row=2,
                           add_cancel=True, add_back="bundle"))


def mix_screen(context, bundle, chosen):
    """Colour for one item of a mixed bundle. `chosen` is what's picked so far."""
    lines = BUNDLES[bundle]
    index = len(chosen)
    template, count = lines[index]
    done = "".join(f"  ✓ {item_label(t)} — {c}\n"
                   for (t, _), c in zip(lines, chosen))
    return (f"Bundle: {bundle}\n{done}\n"
            f"Colour for {count} × {item_label(template)}?\n"
            f"(item {index + 1} of {len(lines)})",
            build_keyboard(list(BUNDLE_COLOURS), "mix", per_row=2,
                           add_cancel=True, add_back="mix"))


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


def qty_screen(context, product, bundle=None):
    """Quantity of a single product, or of a whole bundle."""
    if bundle:
        colours = context.user_data.get("sale", {}).get("colours")
        text = (f"Bundle: {bundle}\n{bundle_summary(bundle, colours)}\n\n"
                f"How many of this bundle?")
    else:
        text = f"Product: {product}\n\nSelect quantity:"
    return (text, build_keyboard(QTY_CHOICES, "qty", per_row=5, add_cancel=True,
                                 add_back="colour" if bundle else "model"))


def _what(context, product, qty, bundle=None):
    if not bundle:
        return f"Product: {product}\nQty: {qty}"
    colours = context.user_data.get("sale", {}).get("colours")
    return f"Bundle: {bundle} × {qty}\n{bundle_summary(bundle, colours)}"


def fulfilment_screen(context, product, qty, bundle=None):
    return (f"{_what(context, product, qty, bundle)}\n\nHow is it going out?",
            build_keyboard([f[0] for f in FULFILMENTS], "fulfil", per_row=1,
                           add_cancel=True, add_back="qty"))


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
        end = index_to_col(col_to_index(b.letter) + b.width - 1)
        mark = "  ← today" if b == target else ""
        next_row = _layout["cursors"].get(b.letter, b.data_start)
        cols = ", ".join(FLAG_NAMES[f] for f in b.flags)
        note = "" if "cash" in b.flags else "  ⚠️ no Cash & Carry column"
        lines.append(f"{b.label} ({day_label(b.show_date)}): {b.letter}–{end} "
                     f"[{cols}], writes {b.name_col}–{end} (S/N untouched), "
                     f"next row {next_row}{mark}{note}")
    queued = (f"\n\n⏳ {_pending} sale(s) still being written."
              if _pending else "\n\nAll sales are on the sheet.")

    problems = bundle_problems(await get_catalog())
    if problems:
        bundles = ("\n\n⚠️ BUNDLE PROBLEMS — these will write a product "
                   "name that matches nothing in the catalogue:\n"
                   + "\n".join(f"  • {p}" for p in problems))
    elif BUNDLES:
        bundles = f"\n\n🎁 {len(BUNDLES)} bundle(s) loaded, all products recognised."
    else:
        bundles = "\n\nNo bundles configured."

    await update.message.reply_text(
        f"✅ {len(blocks)} day block(s) found, headers OK:\n\n"
        + "\n".join(lines) + bundles + queued)


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
            elif value == "bundle":
                text, markup = bundle_screen(context)
            elif value == "colour":
                text, markup = colour_screen(context, sale_data["bundle"])
            elif value == "mix":
                # Rewind one item rather than dumping the promoter back to the
                # start of the bundle - they are usually fixing the last tap.
                chosen = sale_data.get("colours") or []
                chosen = chosen[:-1]
                sale_data["colours"] = chosen
                if chosen:
                    text, markup = mix_screen(context, sale_data["bundle"], chosen)
                else:
                    sale_data.pop("colours", None)
                    text, markup = colour_screen(context, sale_data["bundle"])
            elif value == "qty":
                text, markup = qty_screen(context, sale_data.get("product"),
                                          sale_data.get("bundle"))
            elif value == "fulfil":
                text, markup = fulfilment_screen(context, sale_data.get("product"),
                                                 sale_data["qty"],
                                                 sale_data.get("bundle"))
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
        if brand == BUNDLE_MENU_LABEL and BUNDLES:
            sale_data.pop("product", None)
            text, markup = bundle_screen(context)
            await edit(query, text, markup)
            return
        if brand is None or brand not in context.user_data["catalog"]["tree"]:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        sale_data["brand"] = brand
        sale_data.pop("bundle", None)  # leaving the bundle path
        text, markup = category_screen(context, brand)
        await edit(query, text, markup)

    elif step == "bundle":
        bundle = pick(context, "bundles", value)
        if bundle is None or bundle not in BUNDLES:
            await restart_flow(query, context, "That bundle is no longer available.")
            return
        sale_data["bundle"] = bundle
        sale_data.pop("product", None)
        sale_data.pop("colours", None)
        text, markup = colour_screen(context, bundle)
        await edit(query, text, markup)

    elif step == "colour":
        choice = pick(context, "colour_options", value)
        bundle = sale_data.get("bundle")
        if choice is None or bundle not in BUNDLES:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        if choice == MIX_LABEL:
            sale_data["colours"] = []
            text, markup = mix_screen(context, bundle, [])
        else:
            # "⚫ All Black" -> "Black": the colour is the last word of the label.
            colour = choice.split()[-1]
            sale_data["colours"] = [colour] * len(BUNDLES[bundle])
            text, markup = qty_screen(context, None, bundle)
        await edit(query, text, markup)

    elif step == "mix":
        colour = fixed(list(BUNDLE_COLOURS), value)
        bundle = sale_data.get("bundle")
        chosen = sale_data.get("colours")
        if colour is None or bundle not in BUNDLES or chosen is None:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        chosen = chosen + [colour]
        sale_data["colours"] = chosen
        if len(chosen) < len(BUNDLES[bundle]):
            text, markup = mix_screen(context, bundle, chosen)
        else:
            text, markup = qty_screen(context, None, bundle)
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
        sale_data.pop("bundle", None)
        text, markup = qty_screen(context, product)
        await edit(query, text, markup)

    elif step == "qty":
        qty = fixed(QTY_CHOICES, value)
        bundle = sale_data.get("bundle")
        if qty is None or not (sale_data.get("product") or bundle):
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        if bundle and len(sale_data.get("colours") or []) != len(BUNDLES.get(bundle, ())):
            await restart_flow(query, context, "Colours weren't finished — starting over.")
            return
        sale_data["qty"] = qty
        text, markup = fulfilment_screen(context, sale_data.get("product"), qty,
                                         sale_data.get("bundle"))
        await edit(query, text, markup)

    elif step == "fulfil":
        choice = fixed(FULFILMENTS, value)
        if choice is None or "qty" not in sale_data:
            await restart_flow(query, context, "That menu is out of date — starting over.")
            return
        _, fulfilment = choice

        name = sale_data.get("name") or query.from_user.first_name
        bundle = sale_data.get("bundle")
        product = sale_data.get("product")
        colours = sale_data.get("colours") or []
        qty = int(sale_data["qty"])
        if bundle and len(colours) != len(BUNDLES.get(bundle, ())):
            await restart_flow(query, context, "Colours weren't finished — starting over.")
            return
        context.user_data["sale"] = {}

        # A bundle expands into one line per product, multiplied by how many of
        # the bundle were sold; a normal sale is just the single product. Either
        # way it goes to the writer as ONE sale, so it lands (or fails) whole.
        if bundle:
            lines = bundle_lines(bundle, colours, qty)
        else:
            lines = [(product, qty)]

        # Hand the sale to the background writer and confirm straight away. The
        # promoter can start the next one immediately; the sheet catches up on its
        # own time, and only shouts if a sale can't be written at all.
        label = queue_sale(name, lines, fulfilment,
                           chat_id=query.message.chat_id, bundle=bundle)
        rows = sum(count for _, count in lines)
        if bundle:
            what = (f"Bundle: {bundle} × {qty}\n"
                    + "".join(f"  • {count} × {item}\n" for item, count in lines)
                    + f"({rows} row{'s' if rows != 1 else ''} on the sheet)\n")
        else:
            what = f"Product: {product}\nQty: {qty}\n"
        await edit(query,
                   "Recorded ✅\n\n"
                   f"Promoter: {name}\n"
                   f"{what}"
                   f"{fulfilment}\n"
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
                     f"{len(blocks)} day blocks, {len(BUNDLES)} bundle(s), "
                     f"cursors {_layout['cursors']}")
        # Loud on purpose: a mistyped bundle product writes a name that matches
        # nothing, and nobody would notice until the show was over.
        for problem in bundle_problems(_catalog):
            logging.error(f"BUNDLE PROBLEM - {problem}")
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
