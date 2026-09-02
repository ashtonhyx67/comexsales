"""End-to-end: drive the real button_handler tap by tap and check the sheet."""
import os, sys, asyncio, types

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS", "{}")
sys.path.insert(0, r"C:\Users\ashto\Desktop\Comex Sales Bot")
import bot
from test_bundles import FakeWS, make_sheet, reset, cat, B2, B4  # reuse the fakes

FAIL = []
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


class Query:
    """Just enough of telegram.CallbackQuery for button_handler."""
    def __init__(self, data, ctx):
        self.data = data
        self.ctx = ctx
        self.message = types.SimpleNamespace(message_id=99, chat_id=-1001)
        self.from_user = types.SimpleNamespace(first_name="Fallback")
        self.text = None
        self.alerts = []
    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)
    async def edit_message_text(self, text, reply_markup=None):
        self.text, self.markup = text, reply_markup


async def tap(ctx, data):
    """One button press. Returns the query so the screen text can be inspected."""
    q = Query(data, ctx)
    upd = types.SimpleNamespace(callback_query=q)
    await bot.button_handler(upd, ctx)
    return q


def new_ctx():
    """A flow sitting on the first screen, as /sale + a typed name would leave it."""
    ctx = types.SimpleNamespace(user_data={
        "catalog": cat, "sale": {"name": "Amy"}, "flow_msg": 99})
    bot.first_screen(ctx)
    bot.brand_screen(ctx)      # populates user_data["brands"] like the real screen
    return ctx


def index_of(markup, label):
    for row in markup.inline_keyboard:
        for b in row:
            if b.text == label:
                return b.callback_data.split(":", 1)[1]
    raise AssertionError(f"button {label!r} not found")


async def main():
    bot._write_queue = asyncio.Queue()
    bot._pending = 0
    ws = make_sheet(); reset(ws); bot.get_blocks(True)

    # ---- walk a bundle right through, once per fulfilment option ----
    expected = {"🚚 Delivery": "Delivery",
                "📦 Pre-Order": "Pre-Order",
                "🛍 Cash & Carry": "Cash & Carry"}
    for label, want in expected.items():
        ctx = new_ctx()
        q = await tap(ctx, "kind:0")                      # 🎁 Bundle
        ok("Select a bundle" in q.text, f"[{label}] bundles screen")
        q = await tap(ctx, f"bundle:{list(bot.BUNDLES).index(B2)}")
        ok("What colour?" in q.text, f"[{label}] colour question comes after the bundle")
        q = await tap(ctx, f"colour:{index_of(q.markup, '⚫ All Black')}")
        ok("How many of this bundle?" in q.text, f"[{label}] qty after an all-one-colour pick")
        q = await tap(ctx, "qty:0")                        # 1
        ok("How is it going out?" in q.text, f"[{label}] single fulfilment question")
        opts = [b.text for r in q.markup.inline_keyboard for b in r]
        ok(opts[:3] == list(expected), f"[{label}] three options offered")
        q = await tap(ctx, f"fulfil:{index_of(q.markup, label)}")
        ok("Recorded" in q.text and label.split(maxsplit=1)[1] in q.text,
           f"[{label}] confirmation names the choice")
        ok("Pre-order:" not in q.text and "Delivery: " not in q.text,
           f"[{label}] confirmation no longer shows two Yes/No lines")

        sale = bot._write_queue.get_nowait()
        ok(sale.fulfilment == want, f"[{label}] -> recorded as {sale.fulfilment!r}")
        ok(sale.units == 4, f"[{label}] bundle still 4 rows")

    # ---- the sheet cells, for real, via the writer ----
    ctx = new_ctx()
    await tap(ctx, "kind:0")
    q = await tap(ctx, f"bundle:{list(bot.BUNDLES).index(B4)}")
    q = await tap(ctx, f"colour:{index_of(q.markup, '🎨 Mix colours…')}")
    ok("item 1 of 3" in q.text, "Mix walks item by item")
    q = await tap(ctx, "mix:1")                            # White
    q = await tap(ctx, "mix:1")                            # White
    ok("item 3 of 3" in q.text, "mix reaches the last item")
    q = await tap(ctx, "mix:0")                            # Black
    ok("How many of this bundle?" in q.text, "mix finishes into qty")
    q = await tap(ctx, "qty:0")
    q = await tap(ctx, f"fulfil:{index_of(q.markup, '🛍 Cash & Carry')}")
    sale = bot._write_queue.get_nowait()
    bot._write_batch([sale])
    rows = ws._rows(3, 2, 6, 6)
    ok([r[1] for r in rows] == [
        "Sonos Beam Gen2 Smart Soundbar White",
        "Sonos Sub Mini Compact Subwoofer White",
        "Sonos Era 100 SL Home Bookshelf Speaker Black",
        "Sonos Era 100 SL Home Bookshelf Speaker Black"], f"mixed colours written: {[r[2] for r in rows]}")
    ok(all(r[3] == "No" and r[4] == "No" for r in rows),
       f"Cash & Carry writes No/No in both columns: {[(r[3], r[4]) for r in rows]}")

    # ---- Back from fulfilment returns to qty, not to a Delivery? question ----
    ctx = new_ctx()
    await tap(ctx, "kind:0")
    q = await tap(ctx, f"bundle:{list(bot.BUNDLES).index(B2)}")
    q = await tap(ctx, f"colour:{index_of(q.markup, '⚪ All White')}")
    q = await tap(ctx, "qty:2")                            # 3
    q = await tap(ctx, "back:qty")
    ok("How many of this bundle?" in q.text, "Back from fulfilment returns to qty")
    q = await tap(ctx, "back:colour")
    ok("What colour?" in q.text, "Back from qty returns to the colour question")

    # ---- a plain single-product sale still works ----
    ctx = new_ctx()
    q = await tap(ctx, "kind:1")                           # 🎧 Individual
    ok("Select a brand" in q.text, "Individual leads to the brand list")
    ok(bot.KIND_BUNDLE not in [b.text for r in q.markup.inline_keyboard for b in r],
       "no bundle entry mixed in with the brands")
    q = await tap(ctx, "brand:0")                          # Sonos
    q = await tap(ctx, "cat:0")
    q = await tap(ctx, "model:0")
    ok("Select quantity" in q.text, "single product reaches qty")
    q = await tap(ctx, "qty:1")                            # 2
    ok("How is it going out?" in q.text, "single product gets the same one question")
    q = await tap(ctx, f"fulfil:{index_of(q.markup, '🚚 Delivery')}")
    sale = bot._write_queue.get_nowait()
    ok(tuple(sale.lines) == ((cat["tree"]["Sonos"]["models"]["A"][0], 2),)
       and sale.fulfilment == "Delivery",
       f"single product sale intact: {sale.lines}, {sale.fulfilment}")

    print()
    print("FAILURES:", len(FAIL))
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
