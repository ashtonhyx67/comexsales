"""The Sales Tracker exactly as it stands for the 2026 show.

Four identical 7-column day blocks at A, I, Q, Y, and an S/N column in each that
the SHEET fills with =IF(<Name>="","",ROW()-2). The bot must never write into
those cells - doing so would replace the formula with a literal and break the
numbering from that row down.
"""
import os, sys
from datetime import date, datetime

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot
from test_bundles import reset
from test_layout import ok, FAIL

# --- the live sheet, verified against Google on 3 Sept 2026 ---
HEADERS = ["S/N", "Name", "Product (Colour)", "Time",
           "Cash & Carry", "Delivery?", "Pre-order?"]
BLOCKS = [("DAY 1 (3 Sept)", 1, "A", "B"), ("DAY 2 (4 Sept)", 9, "I", "J"),
          ("DAY 3 (5 Sept)", 17, "Q", "R"), ("DAY 4 (6 Sept)", 25, "Y", "Z")]
DATA_START = 2 + 1          # labels row 1, headers row 2, data from row 3
FORMULA_LAST = 1000


class FormulaWS:
    """A sheet whose S/N cells hold formulas, and that shouts if they're clobbered."""
    col_count = 61

    def __init__(self):
        self.grid = {}
        self.formulas = {}      # (row, col) -> formula text, as the sheet has them
        self.clobbered = []
        self.calls = []
        for label, c0, _, name_col in BLOCKS:
            self.grid[(1, c0)] = label
            for j, h in enumerate(HEADERS):
                self.grid[(2, c0 + j)] = h
            for r in range(DATA_START, FORMULA_LAST + 1):
                self.formulas[(r, c0)] = f'=IF({name_col}{r}="","",ROW()-2)'

    # a formula that evaluates to "" reads back as an empty cell
    def _cell(self, r, c):
        return str(self.grid.get((r, c), ""))

    def _rows(self, r1, c1, r2, c2):
        out = []
        for r in range(r1, r2 + 1):
            row = [self._cell(r, c) for c in range(c1, c2 + 1)]
            while row and row[-1] == "":
                row.pop()
            out.append(row)
        while out and not out[-1]:
            out.pop()
        return out

    def get(self, rng):
        import re
        m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d*)$", rng)
        r2 = int(m.group(4)) if m.group(4) else 1001
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
                    key = (r0 + i, c0 + j)
                    if key in self.formulas:        # writing over a formula cell
                        self.clobbered.append((key, self.formulas[key], v))
                    self.grid[key] = v

    def sn(self, row, c0, name_col_idx):
        """What the sheet's own formula would show for this row."""
        return "" if not self._cell(row, name_col_idx) else str(row - 2)


ws = FormulaWS()
reset(ws)
blocks = bot.get_blocks(True)

# ---- layout matches the live sheet exactly ----
ok([b.letter for b in blocks] == ["A", "I", "Q", "Y"],
   f"blocks at the live columns: {[b.letter for b in blocks]}")
ok([b.label for b in blocks] == [b[0] for b in BLOCKS],
   f"labels: {[b.label for b in blocks]}")
ok(all(b.width == 7 for b in blocks), f"all four are 7 columns: {[b.width for b in blocks]}")
ok(all(b.flags == ("cash", "delivery", "preorder") for b in blocks),
   "all four carry Cash & Carry, Delivery?, Pre-order? in that order")
ok(all(b.data_start == 3 for b in blocks), "data starts on row 3 under the frozen header")
ok([b.name_col for b in blocks] == ["B", "J", "R", "Z"],
   f"writes begin at the Name column: {[b.name_col for b in blocks]}")
ok(all(b.write_width == 6 for b in blocks), "six columns written per row, S/N excluded")

# ---- a full day of sales never touches an S/N formula ----
now = datetime(2026, 9, 3, 15, 40)
for i in range(30):
    bot._write_batch([bot._Sale(f"P{i%8}", [("Widget", 1)], "Cash & Carry", now, 1)])
ok(not ws.clobbered, f"30 sales left every S/N formula intact: {ws.clobbered[:2]}")
ok(all((r, 1) not in ws.grid for r in range(3, 33)),
   "nothing at all was written into column A")

# ---- the sheet's own numbering then reads 1..30 ----
serials = [ws.sn(r, 1, 2) for r in range(3, 33)]
ok(serials == [str(n) for n in range(1, 31)],
   f"the formula numbers the rows 1-30: {serials[:5]}...{serials[-3:]}")
ok(ws.sn(33, 1, 2) == "", "the first unused row stays blank, as the formula intends")

# ---- and on every other day block too ----
ws2 = FormulaWS(); reset(ws2); bot.get_blocks(True)
for i, (label, c0, letter, name_col) in enumerate(BLOCKS):
    bot._write_batch([bot._Sale("Amy", [("Widget", 2)], "Delivery",
                                datetime(2026, 9, 3 + i, 12, 0), 1)])
ok(not ws2.clobbered, "no formula clobbered on any of the four days")
for i, (label, c0, letter, _) in enumerate(BLOCKS):
    got = [ws2.sn(r, c0, c0 + 1) for r in (3, 4, 5)]
    ok(got == ["1", "2", ""], f"{label}: S/N reads 1,2 then blank -> {got}")

# ---- the write ranges are exactly the six non-S/N columns ----
ws3 = FormulaWS(); reset(ws3); bot.get_blocks(True)
bot._write_batch([bot._Sale("Amy", [("W", 1)], "Delivery",
                            datetime(2026, 9, 3 + i, 12, 0), 1) for i in range(4)])
rngs = sorted(c[1] for c in ws3.calls if c[0] == "batch_update")[-1]
ok(sorted(rngs) == ["B3:G3", "J3:O3", "R3:W3", "Z3:AE3"],
   f"one range per day, none starting in an S/N column: {sorted(rngs)}")

# ---- the cursor is read from Name, so the formula column can't confuse it ----
ws4 = FormulaWS(); reset(ws4)
bot.get_blocks(True)
reads = [c[1] for c in ws4.calls if c[0] == "batch_get"][0]
ok(list(reads) == ["B3:B", "J3:J", "R3:R", "Z3:Z"],
   f"the free-row scan reads the Name columns, not S/N: {list(reads)}")
ok(all(bot._layout["cursors"][b.letter] == 3 for b in blocks),
   "an empty sheet starts every day at row 3, not below the formulas")

if __name__ == "__main__":
    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    sys.exit(1 if FAIL else 0)
