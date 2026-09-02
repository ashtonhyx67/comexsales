"""Deleting rows from the sheet must pull the next write back up."""
import os, sys, time
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot
from test_layout import sheet, ok, FAIL          # live-shaped sheet: A(7) I(6) P(6) W(6)
from test_bundles import reset

D1 = datetime(2026, 9, 3, 15, 40)


def sale(who):
    return bot._Sale(who, [("Widget", 1)], "Delivery", D1, 1)


def names(ws, first=3, last=8):
    """The Name column of day 1, as the sheet has it."""
    return [str(ws.grid.get((r, 2), "")) for r in range(first, last + 1)]


def clear_rows(ws, rows, cols=range(2, 8)):
    """Delete a sale the way a person does: select the cells and hit delete."""
    for r in rows:
        for c in cols:
            ws.grid.pop((r, c), None)


# ---- three sales, then delete them all ----
ws = sheet(); reset(ws); bot.get_blocks(True)
for who in ("Amy", "Ben", "Cal"):
    bot._write_batch([sale(who)])
ok(names(ws) == ["Amy", "Ben", "Cal", "", "", ""], f"three sales written: {names(ws)}")
ok(bot._layout["cursors"]["A"] == 6, "cursor sits after the third")

clear_rows(ws, [3, 4, 5])
bot.get_blocks(True)                       # what /check does
ok(bot._layout["cursors"]["A"] == 3,
   f"after deleting every row the cursor returns to the top (got "
   f"{bot._layout['cursors']['A']}, was 6)")
bot._write_batch([sale("Dee")])
ok(names(ws) == ["Dee", "", "", "", "", ""],
   f"the next sale reuses row 3, not row 6: {names(ws)}")

# ---- delete only the last one ----
ws = sheet(); reset(ws); bot.get_blocks(True)
for who in ("Amy", "Ben", "Cal"):
    bot._write_batch([sale(who)])
clear_rows(ws, [5])
bot.get_blocks(True)
ok(bot._layout["cursors"]["A"] == 5, "deleting the last sale frees that row")
bot._write_batch([sale("Dee")])
ok(names(ws) == ["Amy", "Ben", "Dee", "", "", ""], f"row 5 reused: {names(ws)}")

# ---- a gap in the MIDDLE is still skipped, never overwritten ----
ws = sheet(); reset(ws); bot.get_blocks(True)
for who in ("Amy", "Ben", "Cal"):
    bot._write_batch([sale(who)])
clear_rows(ws, [4])                        # hole between Amy and Cal
bot.get_blocks(True)
ok(bot._layout["cursors"]["A"] == 6,
   f"a hole in the middle does NOT pull the cursor back over Cal (got "
   f"{bot._layout['cursors']['A']})")
bot._write_batch([sale("Dee")])
ok(names(ws) == ["Amy", "", "Cal", "Dee", "", ""],
   f"Cal is untouched and Dee appends after him: {names(ws)}")

# ---- the ratchet is gone but concurrency is still safe ----
# Within one burst the cursor advances per sale without re-reading the sheet.
ws = sheet(); reset(ws); bot.get_blocks(True)
bot._write_batch([sale("Amy"), sale("Ben"), sale("Cal")])
ok(names(ws) == ["Amy", "Ben", "Cal", "", "", ""],
   f"a burst still lays rows out contiguously: {names(ws)}")

# A failed write must not free rows that ARE on the sheet.
class Boom(type(ws)):
    def batch_update(self, w):
        raise RuntimeError("503")
b = Boom(ws.grid); reset(b); bot.time.sleep = lambda *_: None
bot.get_blocks(True)
ok(bot._layout["cursors"]["A"] == 6, "re-read after a burst keeps the written rows")
bot._write_batch([sale("Dee")])
ok(bot._layout["cursors"]["A"] == 6 and names(b) == ["Amy", "Ben", "Cal", "", "", ""],
   "a failed write leaves both the sheet and the cursor alone")

# ---- deletions on one day don't disturb another ----
ws = sheet(); reset(ws); bot.get_blocks(True)
d2 = datetime(2026, 9, 4, 9, 5)
bot._write_batch([sale("Amy"), bot._Sale("Ben", [("Widget", 1)], "Delivery", d2, 1)])
clear_rows(ws, [3])                        # wipe day 1 only
bot.get_blocks(True)
ok((bot._layout["cursors"]["A"], bot._layout["cursors"]["I"]) == (3, 4),
   f"day 1 rewinds, day 2 holds: {bot._layout['cursors']['A']}, {bot._layout['cursors']['I']}")

# ---- within the TTL the cursor is NOT re-read (that is the perf design) ----
ws = sheet(); reset(ws); bot.get_blocks(True)
bot._write_batch([sale("Amy")])
clear_rows(ws, [3])
before = len(ws.calls)
bot.get_blocks()                           # not forced, and not yet stale
ok(not [c for c in ws.calls[before:] if c[0] == "batch_get"],
   "inside the TTL no extra Google call is made")
ok(bot._layout["cursors"]["A"] == 4,
   "so a fresh delete is not seen until the layout refreshes or /check runs")
bot._layout["read_at"] = time.time() - bot.LAYOUT_TTL - 1
bot.get_blocks()                           # now stale -> re-reads on its own
ok(bot._layout["cursors"]["A"] == 3,
   f"once the TTL lapses it corrects itself with no /check (got "
   f"{bot._layout['cursors']['A']})")

if __name__ == "__main__":
    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)
