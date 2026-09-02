"""Each show day's sales must land in that day's table, and no other."""
import os, sys
from datetime import date, datetime, timedelta

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot
from test_bundles import FakeWS, reset
from test_layout import ok, FAIL

CORE = ["S/N", "Name", "Product (Colour)", "Time"]
FULL = CORE + ["Cash & Carry", "Delivery?", "Pre-order?"]
# the live sheet as it stands now: four 7-column blocks at A, I, Q, Y
STARTS = [("DAY 1 (3 Sept)", 1, "A"), ("DAY 2 (4 Sept)", 9, "I"),
          ("DAY 3 (5 Sept)", 17, "Q"), ("DAY 4 (6 Sept)", 25, "Y")]


def sheet():
    g = {}
    for label, c0, _ in STARTS:
        g[(1, c0)] = label
        for j, h in enumerate(FULL):
            g[(2, c0 + j)] = h
    return FakeWS(g)


ws = sheet(); reset(ws)
blocks = bot.get_blocks(True)

# ---- the labels parse to the right calendar dates ----
ok([b.show_date for b in blocks] == [date(2026, 9, 3), date(2026, 9, 4),
                                     date(2026, 9, 5), date(2026, 9, 6)],
   f"day labels -> dates: {[str(b.show_date) for b in blocks]}")
ok([b.letter for b in blocks] == ["A", "I", "Q", "Y"],
   f"four separate tables across the columns: {[b.letter for b in blocks]}")
ok(len({b.letter for b in blocks}) == 4, "no two days share a table")

# ---- every hour of every show day routes to its own table ----
for label, _, letter in STARTS:
    d = bot.parse_day_label(label)
    for hour in range(24):
        got = bot.block_for(datetime(2026, 9, d.day, hour, 30).date(), blocks)
        if got.letter != letter:
            ok(False, f"{label} {hour:02d}:30 went to {got.letter}, not {letter}")
            break
    else:
        ok(True, f"{label}: all 24 hours route to column {letter}")

# ---- the midnight boundary is exact ----
last = datetime(2026, 9, 3, 23, 59, 59)
first = datetime(2026, 9, 4, 0, 0, 0)
ok(bot.block_for(last.date(), blocks).letter == "A", "23:59:59 on the 3rd -> DAY 1")
ok(bot.block_for(first.date(), blocks).letter == "I", "00:00:00 on the 4th -> DAY 2")

# ---- before and after the show clamp to the ends ----
ok(bot.block_for(date(2026, 9, 2), blocks).letter == "A", "the day before -> DAY 1")
ok(bot.block_for(date(2026, 8, 1), blocks).letter == "A", "long before -> DAY 1")
ok(bot.block_for(date(2026, 9, 7), blocks).letter == "Y", "the day after -> DAY 4")
ok(bot.block_for(date(2026, 12, 25), blocks).letter == "Y", "long after -> DAY 4")

# ---- write one sale per day: each lands in its own table, none bleeds across ----
ws = sheet(); reset(ws); bot.get_blocks(True)
batch = [bot._Sale(f"P{i+1}", [(f"Item {i+1}", 1)], "Delivery",
                   datetime(2026, 9, 3 + i, 14, 0), 1) for i in range(4)]
bot._write_batch(batch)
for i, (label, c0, letter) in enumerate(STARTS):
    row = ws._rows(3, c0, 3, c0 + 6)[0]
    ok(row == ["", f"P{i+1}", f"Item {i+1}", "2:00PM", "No", "Yes", "No"],
       f"{label}: row 3 of {letter} holds only its own day's sale -> {row[1:3]}")

# nothing landed anywhere else
used = {(r, c) for (r, c) in ws.grid if r >= 3}
expected = {(3, c0 + j) for _, c0, _ in STARTS for j in range(1, 7)}
ok(used == expected, f"no stray cells written outside the four day tables: {used - expected}")

# ---- a burst spanning all four days is still ONE api call ----
ws = sheet(); reset(ws); bot.get_blocks(True)
before = len(ws.calls)
bot._write_batch([bot._Sale("Amy", [("W", 1)], "Delivery",
                            datetime(2026, 9, 3 + i, 14, 0), 1) for i in range(4)])
writes = [c for c in ws.calls[before:] if c[0] == "batch_update"]
ok(len(writes) == 1, f"four days written in one call: {len(writes)}")
ok(sorted(writes[0][1]) == ["B3:G3", "J3:O3", "R3:W3", "Z3:AE3"],
   f"one range per day table: {sorted(writes[0][1])}")

# ---- the day is stamped when the sale is ACCEPTED, not when it is written ----
# A sale taken at 23:59 that only reaches Google after midnight must still be
# filed under the day it was made.
ws = sheet(); reset(ws); bot.get_blocks(True)
late = bot._Sale("Amy", [("W", 1)], "Delivery", datetime(2026, 9, 3, 23, 59), 1)
ok(late.day == date(2026, 9, 3), "the sale carries the date it was taken")
late.tries = 2                      # pretend it failed and was retried past midnight
bot._write_batch([late])
ok(ws._rows(3, 2, 3, 3)[0] == ["Amy", "W"], "a retried sale still lands in DAY 1")
ok(not [c for c in ws.grid if c[1] >= 9 and c[0] >= 3], "nothing leaked into DAY 2")

# ---- the confirmation names the day the promoter will see on the sheet ----
bot._layout["blocks"] = blocks
ok(bot.label_for_now(datetime(2026, 9, 5, 15, 0)) == "DAY 3 (5 Sept)",
   f"confirmation says: {bot.label_for_now(datetime(2026, 9, 5, 15, 0))}")

# ---- an extra day added to the sheet later is picked up with no code change ----
g = sheet().grid
g[(1, 33)] = "DAY 5 (7 Sept)"
for j, h in enumerate(FULL):
    g[(2, 33 + j)] = h
reset(FakeWS(g))
five = bot.get_blocks(True)
ok(len(five) == 5 and five[4].letter == "AG" and five[4].show_date == date(2026, 9, 7),
   f"a 5th day table is discovered automatically: {[b.letter for b in five]}")
ok(bot.block_for(date(2026, 9, 7), five).letter == "AG", "and the 7th routes to it")

if __name__ == "__main__":
    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)
