import os, sys, types
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot

FAIL = []
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)

# ---- fake worksheet mirroring the REAL Sales Tracker ----
class FakeWS:
    col_count = 58
    def __init__(self, grid):
        self.grid, self.calls = grid, []
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
        import re
        self.calls.append(("get", rng))
        m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d*)$", rng)
        r2 = int(m.group(4)) if m.group(4) else 300
        return self._rows(int(m.group(2)), bot.col_to_index(m.group(1)),
                          r2, bot.col_to_index(m.group(3)))
    def batch_get(self, ranges):
        self.calls.append(("batch_get", tuple(ranges)))
        return [self.get(r) for r in ranges]
    def batch_update(self, writes):
        import re
        self.calls.append(("batch_update", tuple(w["range"] for w in writes)))
        for w in writes:
            m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)$", w["range"])
            c0, r0 = bot.col_to_index(m.group(1)), int(m.group(2))
            for i, row in enumerate(w["values"]):
                for j, v in enumerate(row):
                    self.grid[(r0 + i, c0 + j)] = v

HEADERS = ["S/N", "Name", "Product (Colour)", "Time", "Delivery?", "Pre-order?"]

def make_sheet():
    g = {}
    for label, c0 in [("DAY 1 (3 Sept)", 1), ("DAY 2 (4 Sept)", 8),
                      ("DAY 3 (5 Sept)", 15), ("DAY 4 (6 Sept)", 22)]:
        g[(1, c0)] = label
        for j, h in enumerate(HEADERS):
            g[(2, c0 + j)] = h
    for r in range(3, 80):
        g[(r, 1)] = str(r - 2)      # the sheet's pre-printed S/N column
    return FakeWS(g)

def reset(ws):
    bot._layout.update({"blocks": None, "read_at": 0.0, "cursors": {}})
    bot._worksheets["Sales Tracker"] = ws
    bot._spreadsheet = object()

# the real catalogue entries the bundles reference
REAL = ["Sonos Arc Ultra Smart Soundbar Black", "Sonos Arc Ultra Smart Soundbar White",
        "Sonos Sub Gen4 Wireless Subwoofer Black", "Sonos Sub Gen4 Wireless Subwoofer White",
        "Sonos Era 300 Stereo Speaker w Dolby Atmos Black",
        "Sonos Era 300 Stereo Speaker w Dolby Atmos White",
        "Sonos Beam Gen2 Smart Soundbar Black", "Sonos Beam Gen2 Smart Soundbar White",
        "Sonos Sub Mini Compact Subwoofer Black", "Sonos Sub Mini Compact Subwoofer White",
        "Sonos Era 100 SL Home Bookshelf Speaker Black",
        "Sonos Era 100 SL Home Bookshelf Speaker White"]
cat = {"brands": ["Sonos"], "tree": {"Sonos": {"cats": ["A"], "models": {"A": REAL}}}}

B1 = "Arc Ultra + Sub 4"
B2 = "Arc Ultra + Sub 4 + 2× Era 300"
B3 = "Beam (Gen 2) + Sub Mini"
B4 = "Beam (Gen 2) + Sub Mini + 2× Era 100 SL"

# ---- the four bundles are defined as asked ----
ok(list(bot.BUNDLES) == [B1, B2, B3, B4], f"four bundles: {list(bot.BUNDLES)}")
ok(bot.BUNDLE_COLOURS == ("Black", "White"), "colours are Black/White")

# ---- EVERY product in EVERY colour exists in the catalogue ----
probs = bot.bundle_problems(cat)
ok(probs == [], f"all bundle products valid in both colours: {probs}")
ok(bot.bundle_problems({"brands": [], "tree": {}}) == [], "no catalogue -> no false alarms")

half = {"brands": ["Sonos"], "tree": {"Sonos": {"cats": ["A"], "models": {"A":
        [n for n in REAL if n.endswith("Black")]}}}}
gaps = bot.bundle_problems(half)
total_lines = sum(len(v) for v in bot.BUNDLES.values())
ok(len(gaps) == total_lines and all("White'" in g for g in gaps),
   f"White-only gaps all caught ({len(gaps)}/{total_lines}), not masked by Black working")

# ---- helpers ----
ok(bot.item_label("Sonos Arc Ultra Smart Soundbar {colour}") == "Sonos Arc Ultra Smart Soundbar",
   "item_label strips the colour placeholder")
ok(bot.bundle_lines(B1, "Black") == [
    ("Sonos Arc Ultra Smart Soundbar Black", 1),
    ("Sonos Sub Gen4 Wireless Subwoofer Black", 1)], "one colour fills the whole bundle")
ok(bot.bundle_lines(B2, ["Black", "Black", "White"]) == [
    ("Sonos Arc Ultra Smart Soundbar Black", 1),
    ("Sonos Sub Gen4 Wireless Subwoofer Black", 1),
    ("Sonos Era 300 Stereo Speaker w Dolby Atmos White", 2)], "mixed colours apply per item")
ok(bot.bundle_lines(B2, "White", qty=3) == [
    ("Sonos Arc Ultra Smart Soundbar White", 3),
    ("Sonos Sub Gen4 Wireless Subwoofer White", 3),
    ("Sonos Era 300 Stereo Speaker w Dolby Atmos White", 6)], "qty multiplies every line")

# ---- colour screen ----
ctx = types.SimpleNamespace(user_data={"catalog": cat, "sale": {"bundle": B2}})
text, markup = bot.colour_screen(ctx, B2)
labels = [b.text for r in markup.inline_keyboard for b in r]
ok(labels[:3] == ["⚫ All Black", "⚪ All White", bot.MIX_LABEL], f"colour options: {labels[:3]}")
ok(bot.pick(ctx, "colour_options", "0").split()[-1] == "Black", "All Black resolves to 'Black'")
ok(bot.pick(ctx, "colour_options", "1").split()[-1] == "White", "All White resolves to 'White'")
backs = [b.callback_data for r in markup.inline_keyboard for b in r if b.callback_data.startswith("back:")]
ok(backs == ["back:bundle"], f"colour screen backs to the bundle list: {backs}")
ok("Sonos Arc Ultra Smart Soundbar" in text and "{colour}" not in text,
   "colour screen lists contents without the placeholder leaking")

# a single-line bundle offers no Mix
saved = dict(bot.BUNDLES)
bot.BUNDLES["Solo"] = [("Sonos Ace {colour}", 1)]
_, m = bot.colour_screen(ctx, "Solo")
ok(bot.MIX_LABEL not in [b.text for r in m.inline_keyboard for b in r],
   "Mix is hidden when a bundle has only one product")
bot.BUNDLES.clear(); bot.BUNDLES.update(saved)

# ---- mix screen walks the items ----
t0, m0 = bot.mix_screen(ctx, B2, [])
ok("item 1 of 3" in t0 and "Sonos Arc Ultra Smart Soundbar" in t0, f"mix step 1: {t0[:60]!r}")
t1, _ = bot.mix_screen(ctx, B2, ["Black"])
ok("item 2 of 3" in t1 and "✓ Sonos Arc Ultra Smart Soundbar — Black" in t1,
   "mix step 2 shows what's already chosen")
t2, _ = bot.mix_screen(ctx, B2, ["Black", "White"])
ok("item 3 of 3" in t2 and "2 × Sonos Era 300" in t2, "mix step 3 names the 2× item")
backs = [b.callback_data for r in m0.inline_keyboard for b in r if b.callback_data.startswith("back:")]
ok(backs == ["back:mix"], f"mix screen backs one item at a time: {backs}")

# ---- qty / delivery screens reflect the chosen colours ----
ctx.user_data["sale"] = {"bundle": B2, "colours": ["Black", "Black", "White"]}
t, m = bot.qty_screen(ctx, None, B2)
ok("Sonos Era 300 Stereo Speaker w Dolby Atmos White" in t, "qty screen shows resolved colours")
backs = [b.callback_data for r in m.inline_keyboard for b in r if b.callback_data.startswith("back:")]
ok(backs == ["back:colour"], f"qty backs to the colour question: {backs}")
t, m = bot.fulfilment_screen(ctx, None, 2, B2)
ok("Bundle: " + B2 + " × 2" in t and "Sonos Arc Ultra Smart Soundbar Black" in t,
   "fulfilment screen spells out the bundle in its colours")
opts = [b.text for r in m.inline_keyboard for b in r]
ok(opts[:3] == ["🚚 Delivery", "📦 Pre-Order", "🛍 Cash & Carry"],
   f"three fulfilment options: {opts[:3]}")
backs = [b.callback_data for r in m.inline_keyboard for b in r if b.callback_data.startswith("back:")]
ok(backs == ["back:qty"], f"fulfilment backs to qty: {backs}")

# each choice maps to a distinct pair of sheet cells
names = [f[1] for f in bot.FULFILMENTS]
ok(names == ["Delivery", "Pre-Order", "Cash & Carry"], f"three choices: {names}")
# on a block with all three flag columns, and on one with only the old two
three = ("cash", "delivery", "preorder")
two = ("delivery", "preorder")
mapped3 = {n: bot.flag_cells(n, three) for n in names}
ok(mapped3 == {"Delivery": ["No", "Yes", "No"],
               "Pre-Order": ["No", "No", "Yes"],
               "Cash & Carry": ["Yes", "No", "No"]}, f"3-column mapping: {mapped3}")
mapped2 = {n: bot.flag_cells(n, two) for n in names}
ok(mapped2 == {"Delivery": ["Yes", "No"],
               "Pre-Order": ["No", "Yes"],
               "Cash & Carry": ["No", "No"]}, f"2-column mapping: {mapped2}")
ok(len({tuple(v) for v in mapped3.values()}) == 3
   and len({tuple(v) for v in mapped2.values()}) == 3,
   "the three choices never collide, in either block shape")

# ---- writing ----
ws = make_sheet(); reset(ws); bot.get_blocks(True)
now = datetime(2026, 9, 3, 15, 40)
s = bot._Sale("Amy", bot.bundle_lines(B2, "Black"), "Delivery", now, 1, bundle=B2)
ok(s.units == 4, f"Arc Ultra + Sub 4 + 2x Era 300 = 4 rows (got {s.units})")
bot._write_batch([s])
ok(ws._rows(3, 2, 6, 6) == [
    ["Amy", "Sonos Arc Ultra Smart Soundbar Black", "3:40PM", "Yes", "No"],
    ["Amy", "Sonos Sub Gen4 Wireless Subwoofer Black", "3:40PM", "Yes", "No"],
    ["Amy", "Sonos Era 300 Stereo Speaker w Dolby Atmos Black", "3:40PM", "Yes", "No"],
    ["Amy", "Sonos Era 300 Stereo Speaker w Dolby Atmos Black", "3:40PM", "Yes", "No"],
], f"all-black bundle written untagged, one row per unit: {ws._rows(3, 2, 6, 3)}")

# a mixed bundle
s = bot._Sale("Ben", bot.bundle_lines(B4, ["White", "White", "Black"]), "Pre-Order", now, 1, bundle=B4)
bot._write_batch([s])
got = [r[1] for r in ws._rows(7, 2, 10, 6)]
ok(got == ["Sonos Beam Gen2 Smart Soundbar White",
           "Sonos Sub Mini Compact Subwoofer White",
           "Sonos Era 100 SL Home Bookshelf Speaker Black",
           "Sonos Era 100 SL Home Bookshelf Speaker Black"],
   f"mixed bundle writes each item in its own colour: {got}")
ok(str(ws.grid[(10, 1)]) == "8",
   "the sheet's own S/N in column A is untouched by the write")

# every written product exists in the catalogue
written = {str(ws.grid[(r, 3)]) for r in range(3, 11)}  # Product column
ok(written <= set(REAL), f"every written product is a real catalogue entry: {written - set(REAL)}")

# ---- a bundle is still atomic ----
class Boom(FakeWS):
    def batch_update(self, w): raise RuntimeError("503")
b = Boom(make_sheet().grid); reset(b); bot.get_blocks(True)
bot.time.sleep = lambda *_: None
res = bot._write_batch([bot._Sale("Amy", bot.bundle_lines(B1, "Black"), "Cash & Carry",
                                  now, 1, bundle=B1)])
ok(res[0][2] is True and bot._layout["cursors"]["A"] == 3,
   "failed bundle retried whole, cursor untouched")

# ---- failure notice ----
d = bot._Sale("Amy", bot.bundle_lines(B4, "White"), "Cash & Carry", now, 1, bundle=B4).describe()
ok(B4 in d and "Sonos Era 100 SL Home Bookshelf Speaker White" in d,
   "failure notice lists the bundle and every product with its colour")

# ---- the first screen is Bundle / Individual ----
ctx2 = types.SimpleNamespace(user_data={"catalog": cat, "sale": {"name": "Amy"}})
t, markup = bot.kind_screen(ctx2)
labels = [b.text for r in markup.inline_keyboard for b in r]
ok(labels[:3] == [bot.KIND_BUNDLE, bot.KIND_SINGLE, bot.KIND_MULTIPLE],
   f"kind options: {labels[:3]}")
ok("What kind of sale?" in t and "Amy" in t, "kind screen names the promoter")
backs = [b.callback_data for r in markup.inline_keyboard for b in r
         if b.callback_data.startswith("back:")]
ok(backs == ["back:name"], f"kind screen backs to the name prompt: {backs}")

_, markup = bot.brand_screen(ctx2)
labels = [b.text for r in markup.inline_keyboard for b in r]
ok(bot.KIND_BUNDLE not in labels and labels[:len(cat["brands"])] == cat["brands"],
   f"the brand list is brands only: {labels}")
backs = [b.callback_data for r in markup.inline_keyboard for b in r
         if b.callback_data.startswith("back:")]
ok(backs == ["back:kind"], f"brand screen backs to the kind question: {backs}")

_, markup = bot.bundle_screen(ctx2)
backs = [b.callback_data for r in markup.inline_keyboard for b in r
         if b.callback_data.startswith("back:")]
ok(backs == ["back:kind"], f"bundle screen backs to the kind question: {backs}")

# with no bundles configured the extra tap disappears entirely
saved = dict(bot.BUNDLES); bot.BUNDLES.clear()
ok(bot.kind_options() == [bot.KIND_SINGLE, bot.KIND_MULTIPLE],
   "no bundles configured -> the Bundle choice disappears")
t, m = bot.first_screen(ctx2)
ok("What kind of sale?" in t, "Single/Multiple are still offered")
bot.BUNDLES.update(saved)
t, _ = bot.first_screen(ctx2)
ok(bot.KIND_BUNDLE in [b.text for r in _.inline_keyboard for b in r],
   "with bundles configured the Bundle choice is back")
_, markup = bot.bundle_screen(ctx2)
ok(all(len(b.callback_data.encode()) <= 64 for r in markup.inline_keyboard for b in r),
   "bundle buttons within Telegram's 64-byte cap")
ok(bot.pick(ctx2, "kind_options", "0") == bot.KIND_BUNDLE
   and bot.pick(ctx2, "kind_options", "1") == bot.KIND_SINGLE
   and bot.pick(ctx2, "kind_options", "2") == bot.KIND_MULTIPLE,
   "kind indices resolve")

if __name__ == "__main__":   # importable by test_flow without exiting
    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)
