import os, sys, json, asyncio, types
from datetime import date, datetime

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot

FAIL = []
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)

# ---- fake worksheet ----
class FakeWS:
    col_count = 20
    def __init__(self, grid):
        self.grid = grid          # dict[(row,col)] = value
        self.calls = []
    def _rows(self, r1, c1, r2, c2):
        out = []
        for r in range(r1, r2 + 1):
            row = [str(self.grid.get((r, c), "")) for c in range(c1, c2 + 1)]
            while row and row[-1] == "":
                row.pop()
            out.append(row)
        while out and not out[-1]:
            out.pop()
        return out
    def get(self, rng):
        self.calls.append(("get", rng))
        import re
        m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d*)$", rng)
        c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        r2 = int(r2) if r2 else 200
        return self._rows(r1, bot.col_to_index(c1), r2, bot.col_to_index(c2))
    def batch_get(self, ranges):
        self.calls.append(("batch_get", tuple(ranges)))
        return [self.get(r) for r in ranges]
    def batch_update(self, writes):
        self.calls.append(("batch_update", tuple(w["range"] for w in writes)))
        import re
        for w in writes:
            m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)$", w["range"])
            c0, r0 = bot.col_to_index(m.group(1)), int(m.group(2))
            for i, row in enumerate(w["values"]):
                for j, v in enumerate(row):
                    self.grid[(r0 + i, c0 + j)] = v

HEADERS = ["S/N", "Name", "Product (Colour)", "Time", "Delivery?", "Pre-order?"]

def make_sheet():
    g = {}
    for n, (label, c0) in enumerate([("DAY 1 (3 Sept)", 1), ("DAY 2 (4 Sept)", 8)]):
        g[(1, c0)] = label
        for j, h in enumerate(HEADERS):
            g[(3, c0 + j)] = h
    return FakeWS(g)

# ---- column helpers ----
ok(bot.col_to_index("A") == 1 and bot.col_to_index("H") == 8 and bot.col_to_index("AA") == 27,
   "col_to_index")
ok(all(bot.col_to_index(bot.index_to_col(i)) == i for i in range(1, 800)),
   "index_to_col round-trips")
ok(bot.block_range("H", 4, 6, 6) == "H4:M6", "block_range (6 cols)")
ok(bot.block_range("A", 3, 5, 7) == "A3:G5", "block_range (7 cols)")

# ---- clock ----
ok(bot.clock(datetime(2026, 9, 3, 0, 5)).startswith("12:05"), "clock midnight -> 12:05")
ok(bot.clock(datetime(2026, 9, 3, 15, 40)).startswith("3:40"), "clock 15:40 -> 3:40")
ok(bot.clock(datetime(2026, 9, 3, 12, 0)).startswith("12:00"), "clock noon -> 12:00")

# ---- day label parsing ----
ok(bot.parse_day_label("DAY 1 (3 Sept)") == date(2026, 9, 3), "parse day label")
ok(bot.parse_day_label("DAY 10 (12 Oct)") == date(2026, 10, 12), "parse two-digit day")
ok(bot.parse_day_label("Totals") is None, "non-label ignored")
ok(bot.parse_day_label("DAY 1 (31 Feb)") is None, "impossible date rejected")

# ---- layout discovery ----
ws = make_sheet()
blocks = bot.discover_blocks(ws)
ok([(b.show_date, b.letter, b.data_start) for b in blocks] ==
   [(date(2026, 9, 3), "A", 4), (date(2026, 9, 4), "H", 4)], "discover_blocks")
ok(blocks[0].label == "DAY 1 (3 Sept)", "block keeps the sheet's own label")

# missing header row
bad = make_sheet(); [bad.grid.pop((3, c)) for c in range(8, 14)]
try:
    bot.discover_blocks(bad); ok(False, "missing header raises")
except bot.LayoutError as e:
    ok("no 'S/N" in str(e) or "header row" in str(e), "missing header raises LayoutError")

# overlapping blocks
over = FakeWS({(1, 1): "DAY 1 (3 Sept)", (1, 4): "DAY 2 (4 Sept)"})
for c in (1, 4):
    for j, h in enumerate(HEADERS):
        over.grid[(3, c + j)] = h
try:
    bot.discover_blocks(over); ok(False, "overlap raises")
except bot.LayoutError as e:
    ok(True, "overlapping day blocks are rejected: " + str(e)[:60])

# duplicate dates
dup = make_sheet(); dup.grid[(1, 8)] = "DAY 2 (3 Sept)"
try:
    bot.discover_blocks(dup); ok(False, "duplicate date raises")
except bot.LayoutError as e:
    ok("both for" in str(e), "duplicate day dates raise LayoutError")

# no labels
try:
    bot.discover_blocks(FakeWS({})); ok(False, "empty raises")
except bot.LayoutError:
    ok(True, "no day labels raises LayoutError")

# ---- block_for ----
ok(bot.block_for(date(2026, 9, 1), blocks).letter == "A", "before show -> first block")
ok(bot.block_for(date(2026, 9, 3), blocks).letter == "A", "day 1 -> A")
ok(bot.block_for(date(2026, 9, 4), blocks).letter == "H", "day 2 -> H")
ok(bot.block_for(date(2026, 12, 1), blocks).letter == "H", "after show -> last block")

# ---- cursor sync + writes ----
def reset(ws):
    bot._layout.update({"blocks": None, "read_at": 0.0, "cursors": {}})
    bot._worksheets["Sales Tracker"] = ws
    bot._spreadsheet = object()

ws = make_sheet(); reset(ws)
got = bot.get_blocks(True)
ok(bot._layout["cursors"] == {"A": 4, "H": 4}, f"cursors start at data_start: {bot._layout['cursors']}")
ok(sum(1 for c in ws.calls if c[0] == "batch_get") == 1,
   "cursor sync is ONE batch_get for all blocks")

# a title parked ABOVE the block must not shift the cursor (col_values used to count it)
ws2 = make_sheet(); ws2.grid[(2, 2)] = "Promoter"; reset(ws2)
bot.get_blocks(True)
ok(bot._layout["cursors"]["A"] == 4, "content above the block doesn't move the cursor")

# re-reading the layout re-derives the cursor from the sheet, in BOTH directions,
# so deleting sales frees those rows again (see test_delete.py)
ws3 = make_sheet(); reset(ws3)
bot.get_blocks(True); bot._layout["cursors"]["A"] = 9
bot.get_blocks(True)
ok(bot._layout["cursors"]["A"] == 4,
   f"re-reading the layout re-derives the cursor from the sheet: {bot._layout['cursors']['A']}")

# write a burst spanning both days -> one API call, correct rows
ws = make_sheet(); reset(ws)
bot.get_blocks(True)
now1 = datetime(2026, 9, 3, 15, 40)
now2 = datetime(2026, 9, 4, 9, 5)
batch = [bot._Sale("Amy", [("Emberton II", 2)], "Delivery", now1, 1),
         bot._Sale("Ben", [("Era 100", 1)], "Pre-Order", now1, 1),
         bot._Sale("Cal", [("Willen", 1)], "Cash & Carry", now2, 1)]
before = len(ws.calls)
res = bot._write_batch(batch)
writes = [c for c in ws.calls[before:] if c[0] == "batch_update"]
ok(len(writes) == 1, f"two day blocks written in ONE batch_update (got {len(writes)})")
ok(all(v[1] is None for v in res.values()), "all sales reported written")
ok(res[0][0] == "DAY 1 (3 Sept)", f"confirmation uses the sheet's label: {res[0][0]}")
rows_a = ws._rows(4, 2, 6, 6)
ok(rows_a == [["Amy", "Emberton II", "3:40PM", "Yes", "No"],
              ["Amy", "Emberton II", "3:40PM", "Yes", "No"],
              ["Ben", "Era 100", "3:40PM", "No", "Yes"]],
   f"qty expands to one row each: {rows_a}")
ok(all((r, 1) not in ws.grid for r in (4, 5, 6)),
   "the S/N column is never written")
ok(ws._rows(4, 9, 4, 13) == [["Cal", "Willen", "9:05AM", "No", "No"]],
   "day 2 sale lands in the H block, S/N left blank")
ok(bot._layout["cursors"] == {"A": 7, "H": 5}, f"cursors advanced: {bot._layout['cursors']}")

# a second burst continues, no gap, S/N keeps counting
bot._write_batch([bot._Sale("Dee", [("Acton III", 1)], "Delivery", now1, 1)])
ok(ws._rows(7, 2, 7, 6) == [["Dee", "Acton III", "3:40PM", "Yes", "No"]],
   "second burst appends contiguously on the next free row")

# ---- failed write must not advance the cursor (no permanent gap) ----
class Boom(FakeWS):
    def batch_update(self, writes):
        raise RuntimeError("503 backend error")
bad = Boom(make_sheet().grid); reset(bad)
bot.get_blocks(True)
bot.time.sleep = lambda *_: None
res = bot._write_batch([bot._Sale("Eve", [("Stanmore", 1)], "Cash & Carry", now1, 1)])
ok(res[0][1] is not None and res[0][2] is True, "a 5xx is reported as retryable")
ok(bot._layout["cursors"]["A"] == 4, "failed write leaves the cursor where it was")

# ---- layout error is NOT retryable ----
broken = make_sheet(); broken.grid.pop((3, 1)); reset(broken)
res = bot._write_batch([bot._Sale("Eve", [("Stanmore", 1)], "Cash & Carry", now1, 1)])
ok(res[0][2] is False, "a layout problem is not retried forever")

# ---- product catalogue ----
class ProdWS(FakeWS):
    def __init__(self, rows):
        self.rows = rows; self.calls = []
    def get(self, rng):
        self.calls.append(("get", rng)); return self.rows
prod = ProdWS([
    ["Emberton II", "", "", "Speakers", "Marshall"],
    ["Era 100", "", "", "Speakers", "Sonos"],
    ["Major V", "", "", "Headphones", "Marshall"],
    ["", "", "", "Speakers", "Sonos"],          # blank name -> skipped
    ["Mystery", "", "", "", ""],                # blanks -> Other/Other
])
bot._worksheets["COMEX Show 2026"] = prod
cat = bot.refresh_products()
ok(prod.calls[-1][1] == "A3:E", f"product read is narrowed to A3:E, not the whole tab: {prod.calls[-1][1]}")
ok(cat["brands"] == ["Marshall", "Other", "Sonos"], f"brands sorted: {cat['brands']}")
ok(cat["tree"]["Marshall"]["cats"] == ["Headphones", "Speakers"], "categories sorted per brand")
ok(cat["tree"]["Marshall"]["models"]["Speakers"] == ["Emberton II"], "models under brand+category")
ok(cat["tree"]["Other"]["cats"] == ["Other"], "blank brand/category fall back to Other")

# ---- callback data stays inside Telegram's 64-BYTE cap ----
long_name = "Björk Guðmundsdóttir-Sigurðardóttir Promotions Team Lead Singapore"
ctx = types.SimpleNamespace(user_data={"recent_names": [long_name]})
_, markup = bot.name_prompt(ctx)
data = markup.inline_keyboard[0][0].callback_data
ok(len(data.encode()) <= 64, f"long/non-ASCII name -> {len(data.encode())} bytes")
ok(bot.pick(ctx, "recent_names", data.split(":", 1)[1]) == long_name,
   "index resolves back to the FULL name (old code truncated it)")

ctx = types.SimpleNamespace(user_data={"catalog": cat, "sale": {"name": "Amy"}})
_, markup = bot.brand_screen(ctx)
ok(all(len(b.callback_data.encode()) <= 64 for r in markup.inline_keyboard for b in r),
   "brand buttons within the byte cap")
ok(bot.pick(ctx, "brands", "0") == "Marshall",
   "brand list holds only brands now - bundles moved to their own screen")
ok(bot.pick(ctx, "brands", "99") is None and bot.pick(ctx, "brands", "x") is None,
   "out-of-range / junk index returns None instead of raising")
ok(bot.fixed(bot.QTY_CHOICES, "2") == 3 and bot.fixed(bot.YES_NO, "0") == "Yes",
   "fixed lists resolve")
ok(bot.fixed(bot.YES_NO, "7") is None, "fixed list rejects a bad index")
ok(bot.fixed(bot.FULFILMENTS, "2")[1] == "Cash & Carry", "fulfilment index resolves")
ok(bot.fixed(bot.FULFILMENTS, "9") is None, "bad fulfilment index rejected")

print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print("  -", f)
sys.exit(1 if FAIL else 0)
