"""Multiple: one promoter, one customer, several different products in one go."""
import os, sys, asyncio, types
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot
from test_bundles import reset
from test_layout import ok, FAIL
from test_live_shape import FormulaWS, BLOCKS

CAT = {"brands": ["Marshall", "Sonos"], "tree": {
    "Marshall": {"cats": ["Headphones"], "models": {
        "Headphones": ["Marshall Major V Black", "Marshall Major V Cream"]}},
    "Sonos": {"cats": ["Home Theatre"], "models": {
        "Home Theatre": ["Sonos Ace Black", "Sonos Ace White"]}}}}
MAJOR_B, MAJOR_C = CAT["tree"]["Marshall"]["models"]["Headphones"]
ACE_B = CAT["tree"]["Sonos"]["models"]["Home Theatre"][0]


class Q:
    def __init__(self, data):
        self.data = data
        self.message = types.SimpleNamespace(message_id=9, chat_id=-1001)
        self.from_user = types.SimpleNamespace(first_name="Fallback")
        self.text = self.markup = None
    async def answer(self, *a, **k):
        await asyncio.sleep(0)
    async def edit_message_text(self, text, reply_markup=None):
        await asyncio.sleep(0)
        self.text, self.markup = text, reply_markup


async def tap(ctx, data):
    q = Q(data)
    await bot.button_handler(types.SimpleNamespace(callback_query=q), ctx)
    return q


def ctx_for(name="Amy"):
    c = types.SimpleNamespace(user_data={
        "catalog": CAT, "sale": {"name": name}, "flow_msg": 9})
    bot.first_screen(c)
    return c


def label_index(markup, text):
    for row in markup.inline_keyboard:
        for b in row:
            if b.text == text:
                return b.callback_data.split(":", 1)[1]
    raise AssertionError(f"{text!r} not on screen")


def backs(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r
            if b.callback_data.startswith("back:")]


async def add(ctx, brand_i, cat_i, model_i, qty_i):
    await tap(ctx, f"brand:{brand_i}")
    await tap(ctx, f"cat:{cat_i}")
    await tap(ctx, f"model:{model_i}")
    return await tap(ctx, f"qty:{qty_i}")


async def main():
    bot._write_queue = asyncio.Queue()
    bot._pending = 0
    bot._catalog = CAT
    ws = FormulaWS(); reset(ws); bot.get_blocks(True)

    # ---- the three kinds are offered ----
    c = ctx_for()
    t, m = bot.kind_screen(c)
    ok([b.text for r in m.inline_keyboard for b in r][:3]
       == [bot.KIND_BUNDLE, bot.KIND_SINGLE, bot.KIND_MULTIPLE],
       "Bundle / Single / Multiple all offered")

    # ---- two products, one customer ----
    c = ctx_for()
    q = await tap(c, "kind:2")                       # Multiple
    ok("Select a brand" in q.text, "Multiple goes straight to the brands")
    q = await add(c, 0, 0, 0, 1)                     # 2 x Major V Black
    ok("Items so far" in q.text and MAJOR_B in q.text, "cart appears after the first qty")
    ok(c.user_data["sale"]["cart"] == [(MAJOR_B, 2)], f"cart: {c.user_data['sale']['cart']}")
    q = await tap(c, "cart:add")
    ok("Select a brand" in q.text, "Add another returns to the brands")
    q = await add(c, 1, 0, 0, 0)                     # 1 x Ace Black
    ok(c.user_data["sale"]["cart"] == [(MAJOR_B, 2), (ACE_B, 1)],
       f"second product added: {c.user_data['sale']['cart']}")
    ok("2 product(s), 3 unit(s)" in q.text, f"cart totals shown: {q.text.splitlines()[0]}")
    q = await tap(c, "cart:done")
    ok("How is it going out?" in q.text, "Done leads to the one fulfilment question")
    ok(MAJOR_B in q.text and ACE_B in q.text, "fulfilment screen lists the whole basket")
    q = await tap(c, f"fulfil:{label_index(q.markup, '🛍 Cash & Carry')}")
    ok("Recorded" in q.text and "2 products" in q.text and "3 rows" in q.text,
       "confirmation names both products and the row count")

    s = bot._write_queue.get_nowait()
    ok(tuple(s.lines) == ((MAJOR_B, 2), (ACE_B, 1)), f"queued as one sale: {s.lines}")
    ok(s.units == 3 and s.bundle is None, "3 rows, not a bundle")
    ok(s.fulfilment == "Cash & Carry", "one fulfilment for the whole basket")

    # ---- it writes as 3 contiguous rows on one day ----
    bot._write_batch([s])
    got = [(ws._cell(r, 2), ws._cell(r, 3), ws._cell(r, 5), ws._cell(r, 6), ws._cell(r, 7))
           for r in (3, 4, 5)]
    ok(got == [("Amy", MAJOR_B, "Yes", "No", "No"),
               ("Amy", MAJOR_B, "Yes", "No", "No"),
               ("Amy", ACE_B, "Yes", "No", "No")],
       f"one row per unit, same promoter and fulfilment: {got}")
    ok(not ws.clobbered, "S/N formulas untouched")

    # ---- Remove last ----
    c = ctx_for()
    await tap(c, "kind:2")
    await add(c, 0, 0, 0, 0)
    q = await add(c, 1, 0, 0, 2)
    ok(len(c.user_data["sale"]["cart"]) == 2, "two in the cart")
    q = await tap(c, "cart:undo")
    ok(c.user_data["sale"]["cart"] == [(MAJOR_B, 1)],
       f"Remove last drops only the last line: {c.user_data['sale']['cart']}")
    ok("Items so far" in q.text, "still on the cart screen")
    q = await tap(c, "cart:undo")
    ok(c.user_data["sale"]["cart"] == [] and "Select a brand" in q.text,
       "emptying the cart returns to the brands rather than an empty basket")

    # ---- Done on an empty cart cannot submit nothing ----
    q = await tap(c, "cart:done")
    ok("Select a brand" in q.text, "Done with nothing in the basket just asks for a product")
    ok(bot._write_queue.empty(), "and queues no sale")

    # ---- Back navigation ----
    c = ctx_for()
    q = await tap(c, "kind:2")          # this screen IS the brand list
    ok(backs(q.markup) == ["back:kind"],
       f"first brand screen backs to the kind question: {backs(q.markup)}")
    await add(c, 0, 0, 0, 0)
    q = await tap(c, "cart:add")
    ok(backs(q.markup) == ["back:cart"], f"mid-basket the brand screen backs to the cart: {backs(q.markup)}")
    q = await tap(c, "back:cart")
    ok("Items so far" in q.text, "and that returns to the basket")
    q = await tap(c, "cart:done")
    ok(backs(q.markup) == ["back:cart"], "fulfilment backs to the cart, not to a qty screen")
    q = await tap(c, "back:cart")
    ok("Items so far" in q.text, "back from fulfilment lands on the basket")

    # ---- Single is unchanged: straight from qty to fulfilment ----
    c = ctx_for()
    await tap(c, "kind:1")
    q = await add(c, 0, 0, 1, 2)
    ok("How is it going out?" in q.text, "Single skips the cart entirely")
    ok("cart" not in c.user_data["sale"], "Single keeps no basket")
    q = await tap(c, f"fulfil:{label_index(q.markup, '🚚 Delivery')}")
    s = bot._write_queue.get_nowait()
    ok(tuple(s.lines) == ((MAJOR_C, 3),) and s.fulfilment == "Delivery",
       f"single sale intact: {s.lines}")

    # ---- Bundle is unchanged ----
    c = ctx_for()
    q = await tap(c, "kind:0")
    ok("Select a bundle" in q.text, "Bundle still reaches the bundle list")

    # ---- switching kind mid-flow clears the basket ----
    c = ctx_for()
    await tap(c, "kind:2")
    await add(c, 0, 0, 0, 0)
    ok(c.user_data["sale"]["cart"], "basket started")
    await tap(c, "back:kind")
    q = await tap(c, "kind:1")                       # switch to Single
    ok(not c.user_data["sale"].get("cart"), "switching to Single clears the basket")
    q = await add(c, 1, 0, 0, 0)
    ok("How is it going out?" in q.text, "and Single then behaves normally")

    # ---- a double tap on Done must not duplicate ----
    c = ctx_for()
    await tap(c, "kind:2")
    q = await add(c, 0, 0, 0, 0)
    await tap(c, "cart:done")
    fi = label_index((await tap(c, "back:cart")) and (await tap(c, "cart:done")).markup,
                     "📦 Pre-Order")
    await asyncio.gather(tap(c, f"fulfil:{fi}"), tap(c, f"fulfil:{fi}"))
    n = 0
    while not bot._write_queue.empty():
        bot._write_queue.get_nowait(); n += 1
    ok(n == 1, f"double-tapped Done queued exactly one sale, not {n}")

    # ---- a big basket ----
    c = ctx_for()
    await tap(c, "kind:2")
    for i in range(10):
        await add(c, i % 2, 0, i % 2, 4)             # 5 units each
        if i < 9:
            await tap(c, "cart:add")
    q = await tap(c, "cart:done")
    q = await tap(c, f"fulfil:{label_index(q.markup, '🚚 Delivery')}")
    s = bot._write_queue.get_nowait()
    ok(len(s.lines) == 10 and s.units == 50, f"10 products / 50 units: {s.units}")
    ws2 = FormulaWS(); reset(ws2); bot.get_blocks(True)
    bot._write_batch([s])
    ok(sum(1 for r in range(3, 60) if ws2._cell(r, 2)) == 50,
       "all 50 rows written contiguously")
    ok(not ws2.clobbered, "no S/N formula touched by a 50-row basket")

    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
