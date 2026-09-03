"""REGRESSION: day blocks of DIFFERENT widths must both work.

This models the sheet mid-change, when day 1 had a Cash & Carry column and days
2-4 did not. All four days now match (see test_live_shape.py), but the ragged
case is kept so adding or removing a column on one day only can never silently
misalign that day's writes.
"""
import os, sys, types
from datetime import date, datetime

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot
from test_bundles import FakeWS, reset, cat, B2

FAIL = []
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)

CORE = ["S/N", "Name", "Product (Colour)", "Time"]
OLD = CORE + ["Delivery?", "Pre-order?"]
NEW = CORE + ["Cash & Carry", "Delivery?", "Pre-order?"]


def sheet(day1=NEW, rest=OLD):
    """A deliberately ragged layout: A(7), I(6), P(6), W(6)."""
    g = {}
    for label, c0, hdrs in [("DAY 1 (3 Sept)", 1, day1), ("DAY 2 (4 Sept)", 9, rest),
                            ("DAY 3 (5 Sept)", 16, rest), ("DAY 4 (6 Sept)", 23, rest)]:
        g[(1, c0)] = label
        for j, h in enumerate(hdrs):
            g[(2, c0 + j)] = h
    for r in range(3, 60):
        g[(r, 1)] = str(r - 2)
    return FakeWS(g)


# ---- discovery ----
ws = sheet(); reset(ws)
blocks = bot.get_blocks(True)
ok([b.letter for b in blocks] == ["A", "I", "P", "W"],
   f"all four blocks found at the live columns: {[b.letter for b in blocks]}")
ok(blocks[0].flags == ("cash", "delivery", "preorder"),
   f"day 1 flags: {blocks[0].flags}")
ok(blocks[1].flags == ("delivery", "preorder"), f"day 2 flags: {blocks[1].flags}")
ok((blocks[0].width, blocks[1].width) == (7, 6),
   f"widths differ per block: {[b.width for b in blocks]}")
ok(bot.block_range("A", 3, 5, 7) == "A3:G5" and bot.block_range("I", 3, 5, 6) == "I3:N5",
   "range follows the block's own width")

# a block whose Cash & Carry sits in a different position
odd = sheet(day1=CORE + ["Delivery?", "Cash & Carry", "Pre-order?"])
reset(odd)
ok(bot.get_blocks(True)[0].flags == ("delivery", "cash", "preorder"),
   "flag order is taken from the sheet, not assumed")

# a block missing Delivery? entirely is a layout error, not a guess
broken = sheet(day1=CORE + ["Cash & Carry"]); reset(broken)
try:
    bot.get_blocks(True); ok(False, "missing Delivery? raises")
except bot.LayoutError as e:
    ok("Delivery?" in str(e) and "Pre-order?" in str(e),
       f"a block missing its flag columns is rejected: {str(e)[:70]}")

# ---- writing: the same choice lands correctly in both shapes ----
for choice, want7, want6 in [
        ("Cash & Carry", ["Yes", "No", "No"], ["No", "No"]),
        ("Delivery", ["No", "Yes", "No"], ["Yes", "No"]),
        ("Pre-Order", ["No", "No", "Yes"], ["No", "Yes"])]:
    ws = sheet(); reset(ws); bot.get_blocks(True)
    d1 = datetime(2026, 9, 3, 15, 40)
    d2 = datetime(2026, 9, 4, 9, 5)
    bot._write_batch([
        bot._Sale("Amy", [("Widget", 1)], choice, d1, 1),
        bot._Sale("Ben", [("Widget", 1)], choice, d2, 1),
    ])
    got7 = ws._rows(3, 2, 3, 7)[0]
    got6 = ws._rows(3, 10, 3, 14)[0]
    ok(got7 == ["Amy", "Widget", "3:40PM"] + want7,
       f"[{choice}] day 1 (7 cols) -> {got7[3:]}")
    ok(got6 == ["Ben", "Widget", "9:05AM"] + want6,
       f"[{choice}] day 2 (6 cols) -> {got6[3:]}")
    ok(str(ws.grid.get((3, 1), "")) == "1" and (3, 9) not in ws.grid,
       f"[{choice}] S/N columns untouched (pre-printed kept, blank stays blank)")
    ok(str(ws.grid.get((3, 8), "")) == "",
       f"[{choice}] the spacer column H is left alone")

# ---- Cash & Carry on a 6-column day is still the old No/No ----
ws = sheet(); reset(ws); bot.get_blocks(True)
bot._write_batch([bot._Sale("Cal", [("Widget", 1)], "Cash & Carry",
                            datetime(2026, 9, 5, 12, 0), 1)])
ok(ws._rows(3, 17, 3, 21)[0][3:] == ["No", "No"],
   "Cash & Carry on a day with no such column reads as No/No, as it always did")

# ---- once the column is added to every day, it just works ----
ws = sheet(day1=NEW, rest=NEW); reset(ws); bot.get_blocks(True)
ok(all(b.flags == ("cash", "delivery", "preorder") for b in bot._layout["blocks"]),
   "adding the column to every day needs no code change")
bot._write_batch([bot._Sale("Dee", [("Widget", 1)], "Cash & Carry",
                            datetime(2026, 9, 6, 12, 0), 1)])
ok(ws._rows(3, 24, 3, 29)[0][3:] == ["Yes", "No", "No"],
   "day 4 with the new column records Cash & Carry properly")

# ---- describe() for a hand-entered row ----
d = bot._Sale("Amy", [("Widget", 2)], "Cash & Carry", datetime(2026, 9, 3, 15, 40), 1).describe()
ok("Cash & Carry" in d and "Delivery?" not in d,
   f"failure notice names the choice plainly: {d.splitlines()[-2:]}")

# ---- a bundle across the two shapes ----
ws = sheet(); reset(ws); bot.get_blocks(True)
bot._write_batch([bot._Sale("Amy", bot.bundle_lines(B2, "Black"), "Cash & Carry",
                            datetime(2026, 9, 3, 15, 40), 1, bundle=B2)])
rows = ws._rows(3, 2, 6, 7)
ok(all(r[3:] == ["Yes", "No", "No"] for r in rows) and len(rows) == 4,
   f"every row of a bundle carries the choice: {[r[3:] for r in rows]}")

# ---- the write range starts at Name, so S/N can hold a formula safely ----
ws = sheet(); reset(ws); bot.get_blocks(True)
before = len(ws.calls)
bot._write_batch([bot._Sale("Amy", [("Widget", 1)], "Delivery",
                            datetime(2026, 9, 3, 15, 40), 1)])
ranges = [c[1] for c in ws.calls[before:] if c[0] == "batch_update"][0]
ok(ranges == ("B3:G3",), f"day 1 write range skips column A: {ranges}")
b2 = bot._layout["blocks"][1]
ok((b2.name_col, b2.write_width) == ("J", 5), f"day 2 writes J.. ({b2.write_width} cols)")

if __name__ == "__main__":
    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)
