"""Show-day stress: 8 promoters at once, flaky network, double taps, midnight.

Drives the real handlers and the real writer loop against a fake Sheets that
counts API calls and enforces the same quota Google does.
"""
import os, sys, asyncio, random, types, time
from datetime import date, datetime

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot
from test_bundles import reset
from test_layout import ok, FAIL

CORE = ["S/N", "Name", "Product (Colour)", "Time"]
FULL = CORE + ["Cash & Carry", "Delivery?", "Pre-order?"]
STARTS = [("DAY 1 (3 Sept)", 1, "A"), ("DAY 2 (4 Sept)", 9, "I"),
          ("DAY 3 (5 Sept)", 17, "Q"), ("DAY 4 (6 Sept)", 25, "Y")]
CAT = {"brands": ["Marshall", "Sonos"], "tree": {
    "Marshall": {"cats": ["Headphones", "Portable Speaker"], "models": {
        "Headphones": ["Marshall Major V Black", "Marshall Major V Cream"],
        "Portable Speaker": ["Marshall Emberton III Black & Brass"]}},
    "Sonos": {"cats": ["Home Theatre"], "models": {
        "Home Theatre": ["Sonos Arc Ultra Smart Soundbar Black",
                         "Sonos Beam Gen2 Smart Soundbar Black"]}}}}


class QuotaWS:
    """Fake worksheet that behaves like the API, including its limits."""
    col_count = 40

    def __init__(self, grid, fail_rate=0.0, latency=0.0):
        self.grid, self.calls = grid, []
        self.fail_rate, self.latency = fail_rate, latency
        self.writes = []            # (monotonic, n_ranges)
        self.rows_written = 0
        self.quota_breaches = []
        self.overwrites = []        # a cell written twice with different values

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
        m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d*)$", rng)
        r2 = int(m.group(4)) if m.group(4) else 400
        return self._rows(int(m.group(2)), bot.col_to_index(m.group(1)),
                          r2, bot.col_to_index(m.group(3)))

    def batch_get(self, ranges):
        self.calls.append("read")
        return [self.get(r) for r in ranges]

    def batch_update(self, writes):
        import re
        if self.latency:
            time.sleep(self.latency)
        now = time.monotonic()
        self.writes.append(now)
        # Sheets: 60 write requests per minute per user
        recent = [w for w in self.writes if now - w < 60]
        if len(recent) > 60:
            self.quota_breaches.append(len(recent))
        if self.fail_rate and random.random() < self.fail_rate:
            raise RuntimeError("503 backend error")
        for w in writes:
            m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)$", w["range"])
            c0, r0 = bot.col_to_index(m.group(1)), int(m.group(2))
            for i, row in enumerate(w["values"]):
                for j, v in enumerate(row):
                    key = (r0 + i, c0 + j)
                    if key in self.grid and self.grid[key] != v and j == 0:
                        self.overwrites.append((key, self.grid[key], v))
                    self.grid[key] = v
                self.rows_written += 1


def sheet(**kw):
    g = {}
    for label, c0, _ in STARTS:
        g[(1, c0)] = label
        for j, h in enumerate(FULL):
            g[(2, c0 + j)] = h
    return QuotaWS(g, **kw)


class Query:
    def __init__(self, data, mid=99):
        self.data = data
        self.message = types.SimpleNamespace(message_id=mid, chat_id=-1001)
        self.from_user = types.SimpleNamespace(first_name="Fallback")
        self.text = None
        self.markup = None
    async def answer(self, text=None, show_alert=False):
        await asyncio.sleep(0)          # a real network hop yields here
    async def edit_message_text(self, text, reply_markup=None):
        await asyncio.sleep(0)
        self.text, self.markup = text, reply_markup


async def tap(ctx, data, mid=99):
    q = Query(data, mid)
    await bot.button_handler(types.SimpleNamespace(callback_query=q), ctx)
    return q


def idx(markup, label):
    for row in markup.inline_keyboard:
        for b in row:
            if b.text == label:
                return b.callback_data.split(":", 1)[1]
    raise AssertionError(f"{label!r} not on screen")


def new_ctx(name):
    ctx = types.SimpleNamespace(user_data={
        "catalog": CAT, "sale": {"name": name}, "flow_msg": 99})
    bot.first_screen(ctx)
    return ctx


async def promoter(name, n_sales, log, double_tap=False):
    """One promoter logging n_sales, yielding between every tap like a real one."""
    for _ in range(n_sales):
        ctx = new_ctx(name)
        if random.random() < 0.4:                       # a bundle
            q = await tap(ctx, "kind:0")
            b = random.choice(list(bot.BUNDLES))
            q = await tap(ctx, f"bundle:{list(bot.BUNDLES).index(b)}")
            if random.random() < 0.3 and len(bot.BUNDLES[b]) > 1:
                q = await tap(ctx, f"colour:{idx(q.markup, bot.MIX_LABEL)}")
                for _ in range(len(bot.BUNDLES[b])):
                    q = await tap(ctx, f"mix:{random.randint(0, 1)}")
            else:
                q = await tap(ctx, f"colour:{random.randint(0, 1)}")
            # rows = how many of the bundle x how many units it contains
            units = sum(c for _, c in bot.BUNDLES[b])
            expect = None
        elif random.random() < 0.5:                      # MULTIPLE: a basket
            q = await tap(ctx, "kind:2")
            n_items = random.randint(2, 4)
            basket = 0
            for k in range(n_items):
                bi = random.randrange(len(CAT["brands"]))
                await tap(ctx, f"brand:{bi}")
                brand = CAT["brands"][bi]
                ci = random.randrange(len(CAT["tree"][brand]["cats"]))
                await tap(ctx, f"cat:{ci}")
                cat_name = CAT["tree"][brand]["cats"][ci]
                mi = random.randrange(len(CAT["tree"][brand]["models"][cat_name]))
                await tap(ctx, f"model:{mi}")
                iq = random.randint(0, 4)
                q = await tap(ctx, f"qty:{iq}")
                basket += iq + 1
                if k < n_items - 1:
                    q = await tap(ctx, "cart:add")
            q = await tap(ctx, "cart:done")
            fi = random.randrange(len(bot.FULFILMENTS))
            if double_tap:
                await asyncio.gather(tap(ctx, f"fulfil:{fi}"), tap(ctx, f"fulfil:{fi}"))
            else:
                await tap(ctx, f"fulfil:{fi}")
            log.append((name, None, basket))
            await asyncio.sleep(0)
            continue
        else:                                            # SINGLE: one product
            q = await tap(ctx, "kind:1")
            bi = random.randrange(len(CAT["brands"]))
            q = await tap(ctx, f"brand:{bi}")
            brand = CAT["brands"][bi]
            ci = random.randrange(len(CAT["tree"][brand]["cats"]))
            q = await tap(ctx, f"cat:{ci}")
            cat_name = CAT["tree"][brand]["cats"][ci]
            mi = random.randrange(len(CAT["tree"][brand]["models"][cat_name]))
            q = await tap(ctx, f"model:{mi}")
            expect = CAT["tree"][brand]["models"][cat_name][mi]
            units = 1
        qty = random.randint(0, 4)
        q = await tap(ctx, f"qty:{qty}")
        fi = random.randrange(len(bot.FULFILMENTS))
        if double_tap:
            # two taps landing in the same instant, as an impatient promoter does
            await asyncio.gather(tap(ctx, f"fulfil:{fi}"), tap(ctx, f"fulfil:{fi}"))
        else:
            await tap(ctx, f"fulfil:{fi}")
        log.append((name, expect, (qty + 1) * units))
        await asyncio.sleep(0)


async def drain(timeout=30):
    deadline = time.monotonic() + timeout
    while bot._pending and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return bot._pending == 0


async def run(ws, promoters=8, each=6, fail_rate=0.0, double_tap=False):
    bot._write_queue = asyncio.Queue()
    bot._pending = 0
    bot._last_write = 0.0
    bot._app = None
    # warm catalogue: get_catalog() then serves the cache instead of calling Google
    bot._catalog = CAT
    bot._cache_time = time.time()
    reset(ws)
    bot.get_blocks(True)
    task = asyncio.create_task(bot._writer_loop())
    log = []
    await asyncio.gather(*(promoter(f"P{i}", each, log, double_tap)
                           for i in range(promoters)))
    finished = await drain()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return log, finished


def rows_on_sheet(ws, c0):
    out, r = [], 3
    while (r, c0 + 1) in ws.grid:
        out.append(tuple(str(ws.grid.get((r, c0 + j), "")) for j in range(1, 7)))
        r += 1
    return out


async def main():
    random.seed(7)
    bot.time.sleep = lambda *_: None          # no real backoff in tests

    # ---------- 1. clean run: 8 promoters x 6 sales ----------
    ws = sheet()
    log, finished = await run(ws)
    ok(finished, f"every sale drained ({bot._pending} left pending)")
    expect_rows = sum(q for _, _, q in log)
    got = rows_on_sheet(ws, 1)
    ok(len(got) == expect_rows,
       f"48 sales -> {expect_rows} rows expected, {len(got)} on the sheet")
    ok(not ws.overwrites, f"no row was ever overwritten: {ws.overwrites[:3]}")
    ok(all(r[0] for r in got), "every row has a promoter name (no blank/partial rows)")
    ok(len(set(range(3, 3 + len(got)))) == len(got), "rows are contiguous, no gaps")
    per_promoter = {}
    for r in got:
        per_promoter[r[0]] = per_promoter.get(r[0], 0) + 1
    expect_per = {}
    for n, _, q in log:
        expect_per[n] = expect_per.get(n, 0) + q
    ok(per_promoter == expect_per, f"each promoter's unit count is exact: {per_promoter}")
    reads = ws.calls.count("read")
    ok(len(ws.writes) <= 20,
       f"48 sales coalesced into {len(ws.writes)} write call(s), {reads} read(s)")
    ok(not ws.quota_breaches, f"never exceeded 60 writes/min: {ws.quota_breaches}")

    # ---------- 2. double taps must not duplicate ----------
    ws = sheet()
    log, finished = await run(ws, promoters=8, each=4, double_tap=True)
    ok(finished, "double-tap run drained")
    expect_rows = sum(q for _, _, q in log)
    got = rows_on_sheet(ws, 1)
    ok(len(got) == expect_rows,
       f"double taps did NOT duplicate: expected {expect_rows} rows, got {len(got)}")
    ok(not ws.overwrites, "no overwrites under double tapping")

    # ---------- 3. a flaky Google (30% of writes fail) ----------
    ws = sheet(fail_rate=0.3)
    log, finished = await run(ws, promoters=8, each=4, fail_rate=0.3)
    ok(finished, f"all sales landed despite 30% write failures ({bot._pending} lost)")
    got = rows_on_sheet(ws, 1)
    expect_rows = sum(q for _, _, q in log)
    ok(len(got) == expect_rows,
       f"retries recovered every sale: expected {expect_rows}, got {len(got)}")

    # ---------- 4. sales spread across all four days at once ----------
    ws = sheet()
    reset(ws); bot.get_blocks(True)
    bot._write_queue = asyncio.Queue(); bot._pending = 0; bot._last_write = 0.0
    task = asyncio.create_task(bot._writer_loop())
    for i in range(40):
        d = datetime(2026, 9, 3 + (i % 4), 14, 0)
        bot._write_queue.put_nowait(
            bot._Sale(f"P{i%8}", [("Widget", 1)], "Delivery", d, 1))
        bot._pending += 1
    ok(await drain(), "cross-day burst drained")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    counts = {letter: len(rows_on_sheet(ws, c0)) for _, c0, letter in STARTS}
    ok(counts == {"A": 10, "I": 10, "Q": 10, "Y": 10},
       f"40 sales split evenly across the four day tables: {counts}")

    # ---------- 5. queue depth / burst ceiling ----------
    ws = sheet()
    reset(ws); bot.get_blocks(True)
    bot._write_queue = asyncio.Queue(); bot._pending = 0; bot._last_write = 0.0
    task = asyncio.create_task(bot._writer_loop())
    for i in range(300):                       # far beyond anything realistic
        bot._write_queue.put_nowait(
            bot._Sale(f"P{i%8}", [("Widget", 1)], "Cash & Carry",
                      datetime(2026, 9, 3, 14, 0), 1))
        bot._pending += 1
    ok(await drain(60), f"300-sale flood drained ({bot._pending} left)")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    ok(len(rows_on_sheet(ws, 1)) == 300, f"all 300 rows present: {len(rows_on_sheet(ws, 1))}")
    ok(not ws.overwrites, "no collisions in a 300-sale flood")
    ok(len(ws.writes) < 40, f"300 sales took only {len(ws.writes)} API writes")

    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
