import asyncio, io, logging, os, random, json
from datetime import datetime, date, timedelta
import aiosqlite, aiohttp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types.error_event import ErrorEvent
from aiogram.filters import Command, CommandObject
from aiogram.types import (Message, CallbackQuery, LabeledPrice,
    PreCheckoutQuery, SuccessfulPayment, BotCommand, BufferedInputFile)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [883137958]
BOT_USERNAME = "cryptishkabot"

# === OpenRouter ===
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]

FREE_LIMIT = 15
TARIFFS = {
    "week":  {"name": "Неделя",   "stars": 50,   "days": 7,   "emoji": "🗓"},
    "month": {"name": "Месяц",    "stars": 150,  "days": 30,  "emoji": "📅", "hit": True},
    "3m":    {"name": "3 месяца", "stars": 400,  "days": 90,  "emoji": "📆", "save": "выгода 11%"},
    "year":  {"name": "Год",      "stars": 1200, "days": 365, "emoji": "🏆", "save": "выгода 33%"},
}
MAX_ALERTS = 50
REF_NEEDED = 3
REF_REWARD = 7
TRIAL_DAYS = 3
DB = os.getenv("DB_PATH", "bot.db")

WRITING_TO_SUPPORT = {}

SYSTEM_PROMPT = """Ты — дружелюбный ассистент Telegram-бота "Крипто-Помощник".

Что умеет бот:
- Цены 40+ монет (btc, eth, ton, sol и др.)
- Графики 1д/7д/30д/90д/365д (Premium)
- Топы монет, тренды, индекс страха и жадности
- Конвертер валют (Premium)
- Алерты на цену (Premium)
- Портфель с расчётом P&L (Premium)

Тарифы Premium:
- Неделя — 50 ⭐
- Месяц — 150 ⭐
- 3 месяца — 400 ⭐
- Год — 1200 ⭐

Как купить: нажми ⭐ Premium в меню → выбери тариф → оплати.

ПРАВИЛА:
- Отвечай ВСЕГДА на русском
- Отвечай КОРОТКО — 2-3 предложения
- Не пиши "Я ИИ" — ты ассистент бота
- Про оплату: скажи нажать ⭐ Premium
- Не знаешь — "уточните у администратора: /support"
- Эмодзи максимум 1-2"""

# ===================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
API = "https://api.coingecko.com/api/v3"

POPULAR = {
    "btc":"bitcoin","eth":"ethereum","ton":"the-open-network","sol":"solana",
    "bnb":"binancecoin","xrp":"ripple","doge":"dogecoin","ada":"cardano",
    "usdt":"tether","trx":"tron","avax":"avalanche-2","dot":"polkadot",
    "matic":"matic-network","link":"chainlink","shib":"shiba-inu","ltc":"litecoin",
    "atom":"cosmos","near":"near","apt":"aptos","arb":"arbitrum","op":"optimism",
    "pepe":"pepe","sui":"sui","inj":"injective-protocol","tia":"celestia",
    "sei":"sei-network","fil":"filecoin","etc":"ethereum-classic","uni":"uniswap",
    "aave":"aave","sand":"the-sandbox","mana":"decentraland","gala":"gala",
    "ftm":"fantom","algo":"algorand","vet":"vechain","hbar":"hedera-hashgraph",
    "icp":"internet-computer"}


# ==================== ИИ (OpenRouter auto-free) ====================
async def ask_ai(user_message: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me",
        "X-Title": "Crypto Bot",
    }
    try:
        payload = {
            "model": "openrouter/free",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 400,
            "temperature": 0.7,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    model_used = data.get("model", "?")
                    logging.info(f"AI answered via {model_used}")
                    if text and len(text) > 2:
                        return text
                else:
                    err_text = await r.text()
                    logging.error(f"OpenRouter error {r.status}: {err_text[:300]}")
    except Exception as e:
        logging.error(f"OpenRouter exception: {e}")

    return "ИИ временно недоступен. Напишите администратору: /support"


# ==================== БАЗА ====================
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP, premium_until TEXT,
            requests_today INTEGER DEFAULT 0, last_req_date TEXT,
            total_reqs INTEGER DEFAULT 0, ref_id INTEGER,
            ref_count INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
            favs TEXT DEFAULT '', trial_used INTEGER DEFAULT 0, last_daily TEXT);
        CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            coin_id TEXT, symbol TEXT, target REAL, direction TEXT,
            triggered INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS portfolio(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            coin_id TEXT, symbol TEXT, amount REAL, buy_price REAL);
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            amount INTEGER, charge_id TEXT, days INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            username TEXT, first_name TEXT, message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'open');
        """)
        await db.commit()


async def q1(sql, p=()):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, p) as c: return await c.fetchone()


async def qa(sql, p=()):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, p) as c: return await c.fetchall()


async def ex(sql, p=()):
    async with aiosqlite.connect(DB) as db:
        await db.execute(sql, p); await db.commit()


async def get_user(uid):
    return await q1("SELECT * FROM users WHERE user_id=?", (uid,))


async def is_premium(u):
    if not u: return False
    if u["user_id"] in ADMIN_IDS: return True
    if not u["premium_until"]: return False
    try: return datetime.fromisoformat(u["premium_until"]) > datetime.now()
    except: return False


async def grant_premium(uid, days):
    u = await get_user(uid)
    now = datetime.now()
    if u and u["premium_until"]:
        try:
            cur = datetime.fromisoformat(u["premium_until"])
            if cur > now: now = cur
        except: pass
    await ex("UPDATE users SET premium_until=? WHERE user_id=?",
             ((now + timedelta(days=days)).isoformat(), uid))


# ==================== API КРИПТЫ ====================
async def _api(path, params):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API}{path}", params=params, timeout=15) as r:
                return await r.json() if r.status == 200 else None
    except: return None


async def get_price(cid):
    d = await _api("/simple/price", {
        "ids": cid, "vs_currencies": "usd,rub,eur",
        "include_24hr_change": "true", "include_1hr_change": "true",
        "include_7d_change": "true", "include_market_cap": "true",
        "include_24hr_vol": "true"})
    return d.get(cid) if d else None


async def get_multi(ids):
    d = await _api("/simple/price", {"ids": ",".join(ids), "vs_currencies": "usd",
        "include_24hr_change": "true", "include_market_cap": "true"})
    return d or {}


async def get_top(n=10):
    return await _api("/coins/markets", {"vs_currency":"usd","order":"market_cap_desc",
        "per_page":n,"page":1,"price_change_percentage":"1h,24h,7d"})


async def get_trending():
    d = await _api("/search/trending", {})
    return d.get("coins", []) if d else []


async def get_global(): return await _api("/global", {})


async def get_market_chart(cid, days=7):
    return await _api(f"/coins/{cid}/market_chart", {"vs_currency":"usd","days":days})


async def get_fear_greed():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.alternative.me/fng/", timeout=10) as r:
                if r.status != 200: return None
                d = await r.json()
                item = d["data"][0]
                return {"value": int(item["value"]), "class": item["value_classification"]}
    except: return None


def resolve(t): return POPULAR.get(t.lower(), t.lower())


def fmt_num(n):
    if not n: return "0"
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.2f}K"
    return f"{n:,.2f}"


def fmt_price(sym, d):
    if not d: return "❌ Монета не найдена"
    usd, rub, eur = d.get("usd",0), d.get("rub",0), d.get("eur",0)
    ch1 = d.get("usd_1h_change") or 0
    ch24 = d.get("usd_24h_change") or 0
    ch7 = d.get("usd_7d_change") or 0
    mcap = d.get("usd_market_cap",0) or 0
    vol = d.get("usd_24h_vol",0) or 0
    a = lambda v: "🟢" if v >= 0 else "🔴"
    usd_str = f"${usd:,.6f}" if usd < 1 else f"${usd:,.4f}"
    return (f"💎 <b>{sym.upper()}</b>\n━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>{usd_str}</b>\n🇷🇺 {rub:,.2f} ₽  |  🇪🇺 {eur:,.2f} €\n"
            f"━━━━━━━━━━━━━━━━━\n⏱ 1ч:  {a(ch1)} <b>{ch1:+.2f}%</b>\n"
            f"📅 24ч: {a(ch24)} <b>{ch24:+.2f}%</b>\n"
            f"📆 7д:  {a(ch7)} <b>{ch7:+.2f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━\n📊 MCap: <b>${fmt_num(mcap)}</b>\n"
            f"📈 Vol 24ч: <b>${fmt_num(vol)}</b>")


async def make_chart(cid, sym, days=7):
    data = await get_market_chart(cid, days)
    if not data or "prices" not in data: return None
    prices = data["prices"]
    xs = [datetime.fromtimestamp(p[0]/1000) for p in prices]
    ys = [p[1] for p in prices]
    fig, ax = plt.subplots(figsize=(8,4), facecolor="#1c1c24")
    ax.set_facecolor("#1c1c24")
    c = "#22c55e" if ys[-1] >= ys[0] else "#ef4444"
    ax.plot(xs, ys, color=c, linewidth=2)
    ax.fill_between(xs, ys, min(ys), color=c, alpha=0.15)
    ax.tick_params(colors="white", labelsize=8)
    for s in ax.spines.values(): s.set_color("#333")
    ax.grid(alpha=0.15, color="#666")
    plt.title(f"{sym.upper()} — {days} дн.", color="white", fontsize=12, pad=10)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor="#1c1c24")
    buf.seek(0); plt.close(fig)
    return BufferedInputFile(buf.read(), filename=f"{sym}.png")


# ==================== КЛАВИАТУРЫ ====================
def main_kb():
    k = InlineKeyboardBuilder()
    k.button(text="💎 Цены", callback_data="prices")
    k.button(text="🏆 Топ", callback_data="top_menu")
    k.button(text="📈 Тренды", callback_data="trending")
    k.button(text="😱 Индекс страха", callback_data="fng")
    k.button(text="🌍 Обзор рынка", callback_data="global")
    k.button(text="🎁 Ежедневный бонус", callback_data="daily")
    k.button(text="⭐ Premium", callback_data="premium")
    k.button(text="👤 Профиль", callback_data="profile")
    k.button(text="📞 Поддержка", callback_data="support")
    k.button(text="❓ Помощь", callback_data="help")
    k.adjust(2, 2, 2, 2, 2)
    return k.as_markup()


def prices_kb():
    k = InlineKeyboardBuilder()
    for s in POPULAR:
        k.button(text=s.upper(), callback_data=f"p:{POPULAR[s]}:{s}")
    k.button(text="⬅️ Назад", callback_data="main")
    k.adjust(4, 4, 4, 4, 4, 4, 4, 4, 4, 1)
    return k.as_markup()


def premium_kb():
    k = InlineKeyboardBuilder()
    for key, t in TARIFFS.items():
        mark = "🔥 " if t.get("hit") else f"{t['emoji']} "
        k.button(text=f"{mark}{t['name']} — {t['stars']} ⭐", callback_data=f"buy:{key}")
    k.button(text=f"🎁 Пробный Premium на {TRIAL_DAYS} дня", callback_data="trial")
    k.button(text="🎁 Пригласить друга", callback_data="ref")
    k.button(text="⬅️ Назад", callback_data="main")
    k.adjust(2, 2, 1, 1, 1)
    return k.as_markup()


def profile_kb():
    k = InlineKeyboardBuilder()
    k.button(text="📊 Портфель", callback_data="pf")
    k.button(text="🔔 Алерты", callback_data="alerts")
    k.button(text="⭐ Избранное", callback_data="favs")
    k.button(text="🎁 Рефералы", callback_data="ref")
    k.button(text="⬅️ Назад", callback_data="main")
    k.adjust(2, 2, 1)
    return k.as_markup()


def back_kb(to="main"):
    k = InlineKeyboardBuilder()
    k.button(text="⬅️ Назад", callback_data=to)
    return k.as_markup()


def top_kb():
    k = InlineKeyboardBuilder()
    k.button(text="🔟 Топ-10", callback_data="top:10")
    k.button(text="5️⃣0️⃣ Топ-50", callback_data="top:50")
    k.button(text="💯 Топ-100", callback_data="top:100")
    k.button(text="⬅️ Назад", callback_data="main")
    k.adjust(3, 1)
    return k.as_markup()


def chart_kb(cid, sym):
    k = InlineKeyboardBuilder()
    k.button(text="1д", callback_data=f"chart:{cid}:{sym}:1")
    k.button(text="7д", callback_data=f"chart:{cid}:{sym}:7")
    k.button(text="30д", callback_data=f"chart:{cid}:{sym}:30")
    k.button(text="90д", callback_data=f"chart:{cid}:{sym}:90")
    k.button(text="365д", callback_data=f"chart:{cid}:{sym}:365")
    k.adjust(5)
    return k.as_markup()


# ==================== ПОЛЬЗОВАТЕЛИ ====================
async def check_user(ev, need_limit=True):
    u = ev.from_user
    if u.id in ADMIN_IDS:
        row = await get_user(u.id)
        if not row:
            await ex("INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)",
                     (u.id, u.username, u.first_name))
            row = await get_user(u.id)
        return row, True

    row = await get_user(u.id)
    if not row:
        ref = None
        if isinstance(ev, Message) and ev.text and "ref_" in ev.text:
            try:
                ref = int(ev.text.split("ref_")[1].split()[0])
                if ref == u.id: ref = None
            except: ref = None
        await ex("INSERT OR IGNORE INTO users(user_id,username,first_name,ref_id) VALUES(?,?,?,?)",
                 (u.id, u.username, u.first_name, ref))
        if ref:
            await ex("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (ref,))
            r = await get_user(ref)
            if r and (r["ref_count"] or 0) % REF_NEEDED == 0:
                await grant_premium(ref, REF_REWARD)
                try: await bot.send_message(ref, f"🎉 {REF_NEEDED} друзей! +{REF_REWARD} дней Premium")
                except: pass
        row = await get_user(u.id)

    if row["banned"]: return row, False
    if not need_limit: return row, True
    if await is_premium(row): return row, True

    today = str(date.today())
    if row["last_req_date"] != today:
        await ex("UPDATE users SET requests_today=0,last_req_date=? WHERE user_id=?", (today, u.id))
        row = await get_user(u.id)

    if (row["requests_today"] or 0) >= FREE_LIMIT: return row, False
    await ex("UPDATE users SET requests_today=requests_today+1,total_reqs=total_reqs+1,last_req_date=? WHERE user_id=?",
             (today, u.id))
    return row, True


# ==================== START / HELP ====================
@dp.message(Command("start"))
async def start(m: Message):
    row, _ = await check_user(m, need_limit=False)
    prem = await is_premium(row)
    status = "⭐ Premium" if prem else "🆓 Free"
    await m.answer(
        f"👋 Привет, <b>{m.from_user.first_name}</b>!\n\n"
        f"Я твой личный <b>крипто-помощник</b> 💎\n\n"
        f"📊 Статус: <b>{status}</b>\n"
        f"💡 Бесплатно: <b>{FREE_LIMIT}</b> запросов/день\n\n"
        f"<b>Что умею:</b>\n💎 Цены 40+ монет\n📊 Графики 1д/7д/30д/90д/365д\n"
        f"🏆 Топы и тренды\n😱 Индекс страха\n🌍 Обзор рынка\n"
        f"🔔 Алерты\n📊 Портфель с P&L\n🎁 Ежедневный бонус\n📞 Поддержка\n\nВыбирай 👇",
        reply_markup=main_kb())


@dp.message(Command("help"))
async def help_cmd(m: Message): await show_help(m)


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await show_help(c.message, edit=True)
    try: await c.answer()
    except: pass


async def show_help(target, edit=False):
    txt = ("📖 <b>Помощь</b>\n━━━━━━━━━━━━━━━━━\n"
           "<b>Цены:</b>\n• <code>btc</code> — тикер\n• <code>/price btc</code>\n"
           "• <code>/p btc eth sol</code>\n\n"
           "<b>Графики (Premium):</b>\n• <code>/chart btc 7</code>\n\n"
           "<b>Рынок:</b>\n• <code>/top</code>\n• <code>/trending</code>\n"
           "• <code>/fng</code>\n• <code>/global</code>\n\n"
           "<b>Конвертер (P):</b>\n• <code>/convert 100 btc usd</code>\n\n"
           "<b>Алерты (P):</b>\n• <code>/alert btc 100000 up</code>\n\n"
           "<b>Портфель (P):</b>\n• <code>/add btc 0.5 60000</code>\n"
           "• <code>/portfolio</code>\n\n"
           "<b>Прочее:</b>\n• <code>/premium</code>\n• <code>/ref</code>\n• <code>/profile</code>")
    if edit:
        try: await target.edit_text(txt, reply_markup=back_kb())
        except TelegramBadRequest: pass
    else:
        await target.answer(txt)


@dp.callback_query(F.data == "main")
async def cb_main(c: CallbackQuery):
    row = await get_user(c.from_user.id)
    prem = await is_premium(row)
    status = "⭐ Premium" if prem else "🆓 Free"
    try:
        await c.message.edit_text(
            f"👋 <b>{c.from_user.first_name}</b>\n📊 Статус: <b>{status}</b>\n\nВыбирай 👇",
            reply_markup=main_kb())
    except TelegramBadRequest: pass
    try: await c.answer()
    except: pass


# ==================== ЕЖЕДНЕВНЫЙ БОНУС ====================
@dp.callback_query(F.data == "daily")
async def cb_daily(c: CallbackQuery):
    row = await get_user(c.from_user.id)
    today = str(date.today())
    if row["last_daily"] == today:
        try: await c.answer("🎁 Уже получил сегодня. Заходи завтра!", show_alert=True)
        except: pass
        return
    bonus = random.randint(3, 10)
    await ex("UPDATE users SET requests_today = requests_today + ?, last_daily=? WHERE user_id=?",
             (bonus, today, c.from_user.id))
    await c.message.answer(f"🎁 <b>Ежедневный бонус!</b>\n\n+<b>{bonus}</b> запросов на сегодня.\n\nВозвращайся завтра 💎")
    try: await c.answer()
    except: pass


# ==================== ЦЕНЫ ====================
@dp.callback_query(F.data == "prices")
async def cb_prices(c: CallbackQuery):
    try:
        await c.message.edit_text("💎 <b>Выбери монету:</b>", reply_markup=prices_kb())
    except TelegramBadRequest: pass
    try: await c.answer()
    except: pass


@dp.callback_query(F.data.startswith("p:"))
async def cb_price(c: CallbackQuery):
    row, ok = await check_user(c)
    if not ok:
        try: await c.answer(f"⚠️ Лимит {FREE_LIMIT}/день. Купи Premium.", show_alert=True)
        except: pass
        return
    _, cid, sym = c.data.split(":")
    d = await get_price(cid)
    await c.message.answer(fmt_price(sym, d), reply_markup=chart_kb(cid, sym))
    try: await c.answer()
    except: pass


@dp.message(Command("price", "p"))
async def cmd_price(m: Message, command: CommandObject):
    row, ok = await check_user(m)
    if not ok: await m.answer(f"⚠️ Лимит {FREE_LIMIT}/день. /premium"); return
    if not command.args:
        await m.answer("Формат: <code>/price btc</code>\nИли: <code>/p btc eth sol</code>"); return
    tickers = command.args.strip().lower().split()
    if len(tickers) == 1:
        t = tickers[0]
        d = await get_price(resolve(t))
        if not d: await m.answer(f"❌ Не нашёл <code>{t}</code>"); return
        await m.answer(fmt_price(t, d), reply_markup=chart_kb(resolve(t), t))
    else:
        ids = [resolve(t) for t in tickers]
        data = await get_multi(ids)
        txt = "📊 <b>Быстрый обзор:</b>\n\n"
        for t, cid in zip(tickers, ids):
            d = data.get(cid)
            if not d: txt += f"❌ {t.upper()}\n"; continue
            ch = d.get("usd_24h_change") or 0
            a = "🟢" if ch >= 0 else "🔴"
            txt += f"💎 <b>{t.upper()}</b> — ${d['usd']:,.4f} {a} {ch:+.2f}%\n"
        await m.answer(txt)


# ==================== ГРАФИКИ ====================
@dp.callback_query(F.data.startswith("chart:"))
async def cb_chart(c: CallbackQuery):
    _, cid, sym, days = c.data.split(":")
    row = await get_user(c.from_user.id)
    if not await is_premium(row):
        try: await c.answer("🔒 Графики только в Premium. /premium", show_alert=True)
        except: pass
        return
    try: await c.answer("⏳ Строю график...")
    except: pass
    days = int(days)
    img = await make_chart(cid, sym, days)
    if not img: await c.message.answer("❌ Не удалось построить график"); return
    await c.message.answer_photo(img, caption=f"📊 <b>{sym.upper()}</b> — {days} дн.")


@dp.message(Command("chart"))
async def cmd_chart(m: Message, command: CommandObject):
    row = await get_user(m.from_user.id)
    if not await is_premium(row):
        await m.answer("🔒 Графики только в Premium. /premium"); return
    parts = (command.args or "").split()
    if not parts: await m.answer("Формат: <code>/chart btc 7</code>"); return
    t = parts[0].lower()
    days = 7
    if len(parts) > 1:
        try: days = int(parts[1])
        except: pass
    cid = resolve(t)
    await m.answer("⏳ Строю график...")
    img = await make_chart(cid, t, days)
    if not img: await m.answer("❌ Не удалось построить график"); return
    await m.answer_photo(img, caption=f"📊 <b>{t.upper()}</b> — {days} дн.")


# ==================== ТОП ====================
@dp.callback_query(F.data == "top_menu")
async def cb_top_menu(c: CallbackQuery):
    try:
        await c.message.edit_text("🏆 <b>Размер топа:</b>", reply_markup=top_kb())
    except TelegramBadRequest: pass
    try: await c.answer()
    except: pass


@dp.callback_query(F.data.startswith("top:"))
async def cb_top(c: CallbackQuery):
    n = int(c.data.split(":")[1])
    row, ok = await check_user(c)
    if not ok:
        try: await c.answer("⚠️ Лимит", show_alert=True)
        except: pass
        return
    coins = await get_top(n)
    if not coins:
        try: await c.answer("Ошибка API", show_alert=True)
        except: pass
        return
    txt = f"🏆 <b>Топ-{n}:</b>\n\n"
    for i, x in enumerate(coins, 1):
        ch = x.get("price_change_percentage_24h") or 0
        a = "🟢" if ch >= 0 else "🔴"
        p = x["current_price"]
        ps = f"${p:,.6f}" if p < 1 else f"${p:,.2f}"
        txt += f"{i}. <b>{x['symbol'].upper()}</b> — {ps} {a} {ch:+.2f}%\n"
    for part in [txt[i:i+4000] for i in range(0, len(txt), 4000)]:
        await c.message.answer(part)
    try: await c.answer()
    except: pass


@dp.message(Command("top"))
async def cmd_top(m: Message, command: CommandObject):
    row, ok = await check_user(m)
    if not ok: await m.answer("⚠️ Лимит. /premium"); return
    n = 10
    if command.args:
        try: n = min(int(command.args.strip()), 100)
        except: pass
    coins = await get_top(n)
    if not coins: await m.answer("Ошибка API"); return
    txt = f"🏆 <b>Топ-{n}:</b>\n\n"
    for i, x in enumerate(coins, 1):
        ch = x.get("price_change_percentage_24h") or 0
        a = "🟢" if ch >= 0 else "🔴"
        p = x["current_price"]
        ps = f"${p:,.6f}" if p < 1 else f"${p:,.2f}"
        txt += f"{i}. <b>{x['symbol'].upper()}</b> — {ps} {a} {ch:+.2f}%\n"
    for part in [txt[i:i+4000] for i in range(0, len(txt), 4000)]:
        await m.answer(part)


# ==================== ТРЕНДЫ ====================
@dp.callback_query(F.data == "trending")
async def cb_trending(c: CallbackQuery):
    row, ok = await check_user(c)
    if not ok:
        try: await c.answer("⚠️ Лимит", show_alert=True)
        except: pass
        return
    coins = await get_trending()
    if not coins:
        try: await c.answer("Ошибка API", show_alert=True)
        except: pass
        return
    txt = "📈 <b>В тренде:</b>\n\n"
    for i, item in enumerate(coins[:10], 1):
        it = item["item"]
        txt += f"{i}. <b>{it['symbol'].upper()}</b> — {it['name']}\n"
    await c.message.answer(txt)
    try: await c.answer()
    except: pass


@dp.message(Command("trending"))
async def cmd_trending(m: Message):
    row, ok = await check_user(m)
    if not ok: await m.answer("⚠️ Лимит. /premium"); return
    coins = await get_trending()
    if not coins: await m.answer("Ошибка API"); return
    txt = "📈 <b>В тренде:</b>\n\n"
    for i, item in enumerate(coins[:10], 1):
        it = item["item"]
        txt += f"{i}. <b>{it['symbol'].upper()}</b> — {it['name']}\n"
    await m.answer(txt)


# ==================== FEAR & GREED ====================
@dp.callback_query(F.data == "fng")
async def cb_fng(c: CallbackQuery):
    row, ok = await check_user(c)
    if not ok:
        try: await c.answer("⚠️ Лимит", show_alert=True)
        except: pass
        return
    f = await get_fear_greed()
    if not f:
        try: await c.answer("Ошибка API", show_alert=True)
        except: pass
        return
    v = f["value"]
    emoji = "😱" if v < 25 else "😨" if v < 45 else "😐" if v < 55 else "😊" if v < 75 else "🤑"
    await c.message.answer(
        f"{emoji} <b>Индекс страха и жадности</b>\n\n📊 Значение: <b>{v}</b> / 100\n"
        f"📝 Оценка: <b>{f['class']}</b>\n\n"
        f"<i>Меньше 25 — паника (хорошее время покупать)\n"
        f"Больше 75 — эйфория (риск коррекции)</i>")
    try: await c.answer()
    except: pass


@dp.message(Command("fng"))
async def cmd_fng(m: Message):
    row, ok = await check_user(m)
    if not ok: await m.answer("⚠️ Лимит. /premium"); return
    f = await get_fear_greed()
    if not f: await m.answer("Ошибка API"); return
    v = f["value"]
    emoji = "😱" if v < 25 else "😨" if v < 45 else "😐" if v < 55 else "😊" if v < 75 else "🤑"
    await m.answer(f"{emoji} <b>Индекс страха и жадности</b>\n\n📊 Значение: <b>{v}</b> / 100\n📝 Оценка: <b>{f['class']}</b>")


# ==================== ГЛОБАЛЬНЫЙ ОБЗОР ====================
@dp.callback_query(F.data == "global")
async def cb_global(c: CallbackQuery):
    row, ok = await check_user(c)
    if not ok:
        try: await c.answer("⚠️ Лимит", show_alert=True)
        except: pass
        return
    g = await get_global()
    if not g:
        try: await c.answer("Ошибка API", show_alert=True)
        except: pass
        return
    d = g["data"]
    mcap = d["total_market_cap"]["usd"]; vol = d["total_volume"]["usd"]
    btc_d = d["market_cap_percentage"]["btc"]; eth_d = d["market_cap_percentage"]["eth"]
    ch = d["market_cap_change_percentage_24h_usd"]
    a = "🟢" if ch >= 0 else "🔴"
    await c.message.answer(
        f"🌍 <b>Обзор рынка</b>\n━━━━━━━━━━━━━━━━━\n"
        f"💰 MCap: <b>${fmt_num(mcap)}</b>\n{a} 24ч: <b>{ch:+.2f}%</b>\n"
        f"📊 Vol 24ч: <b>${fmt_num(vol)}</b>\n━━━━━━━━━━━━━━━━━\n"
        f"👑 Доминация BTC: <b>{btc_d:.1f}%</b>\n💎 ETH: <b>{eth_d:.1f}%</b>")
    try: await c.answer()
    except: pass


@dp.message(Command("global"))
async def cmd_global(m: Message):
    row, ok = await check_user(m)
    if not ok: await m.answer("⚠️ Лимит. /premium"); return
    g = await get_global()
    if not g: await m.answer("Ошибка API"); return
    d = g["data"]
    mcap = d["total_market_cap"]["usd"]; vol = d["total_volume"]["usd"]
    btc_d = d["market_cap_percentage"]["btc"]
    ch = d["market_cap_change_percentage_24h_usd"]
    a = "🟢" if ch >= 0 else "🔴"
    await m.answer(f"🌍 <b>Обзор рынка</b>\n💰 MCap: <b>${fmt_num(mcap)}</b>\n{a} 24ч: <b>{ch:+.2f}%</b>\n📊 Vol: <b>${fmt_num(vol)}</b>\n👑 BTC: <b>{btc_d:.1f}%</b>")


# ==================== КОНВЕРТЕР ====================
@dp.message(Command("convert", "c"))
async def cmd_conv(m: Message, command: CommandObject):
    row = await get_user(m.from_user.id)
    if not await is_premium(row):
        await m.answer("🔒 Только Premium. /premium"); return
    parts = (command.args or "").split()
    if len(parts) != 3:
        await m.answer("Формат: <code>/convert 100 btc usd</code>"); return
    try: amount = float(parts[0])
    except: await m.answer("Первое — число"); return
    src, dst = parts[1].lower(), parts[2].lower()
    fiats = {"usd", "rub", "eur"}
    if src in fiats and dst in fiats:
        await m.answer("Фиат→фиат не умею"); return
    if src in fiats:
        cid = resolve(dst); d = await get_price(cid)
        if not d: await m.answer("❌ Монета не найдена"); return
        price = d.get(src, 0)
        if not price: await m.answer("❌ Нет курса"); return
        await m.answer(f"🔄 <b>{amount:,.2f} {src.upper()}</b> = <b>{amount/price:,.8f} {dst.upper()}</b>")
    else:
        cid = resolve(src); d = await get_price(cid)
        if not d: await m.answer("❌ Монета не найдена"); return
        if dst in fiats:
            price = d.get(dst, 0)
            if not price: await m.answer("❌ Нет курса"); return
            await m.answer(f"🔄 <b>{amount} {src.upper()}</b> = <b>{amount*price:,.2f} {dst.upper()}</b>")
        else:
            d2 = await get_price(resolve(dst))
            if not d2: await m.answer("❌ Вторая монета не найдена"); return
            if not d.get("usd") or not d2.get("usd"):
                await m.answer("❌ Нет курса"); return
            await m.answer(f"🔄 <b>{amount} {src.upper()}</b> = <b>{amount*d['usd']/d2['usd']:,.8f} {dst.upper()}</b>")


# ==================== ПРОФИЛЬ ====================
async def render_profile(target, uid, edit=False):
    row = await get_user(uid)
    prem = await is_premium(row)
    until = "∞" if uid in ADMIN_IDS else (datetime.fromisoformat(row["premium_until"]).strftime("%d.%m.%Y") if prem and row["premium_until"] else "—")
    today = row["requests_today"] if row["last_req_date"] == str(date.today()) else 0
    limit = "∞" if prem else FREE_LIMIT
    txt = (f"👤 <b>Профиль</b>\n━━━━━━━━━━━━━━━━━\n🆔 <code>{uid}</code>\n"
           f"⭐ Статус: <b>{'Premium' if prem else 'Free'}</b>\n⏳ До: <b>{until}</b>\n"
           f"━━━━━━━━━━━━━━━━━\n📊 Запросов сегодня: <b>{today}/{limit}</b>\n"
           f"📈 Всего: <b>{row['total_reqs'] or 0}</b>\n👥 Приглашено: <b>{row['ref_count'] or 0}</b>")
    if edit:
        try: await target.edit_text(txt, reply_markup=profile_kb())
        except TelegramBadRequest: pass
    else:
        await target.answer(txt, reply_markup=profile_kb())


@dp.message(Command("profile"))
async def cmd_profile(m: Message): await render_profile(m, m.from_user.id)


@dp.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    await render_profile(c.message, c.from_user.id, edit=True)
    try: await c.answer()
    except: pass


# ==================== PREMIUM ====================
async def render_premium(target, edit=False):
    txt = (f"⭐ <b>Premium-подписка</b>\n━━━━━━━━━━━━━━━━━\n"
           f"✅ Безлимитные запросы цен\n✅ Графики 1д/7д/30д/90д/365д\n"
           f"✅ До {MAX_ALERTS} алертов на цену\n✅ Портфель с расчётом P&L\n"
           f"✅ Конвертер валют\n✅ Топ-100 монет\n✅ Приоритетная поддержка\n"
           f"━━━━━━━━━━━━━━━━━\n💎 <b>Выбери тариф:</b>\n")
    for key, t in TARIFFS.items():
        mark = " 🔥" if t.get("hit") else ""
        save = f"  <i>({t['save']})</i>" if t.get("save") else ""
        txt += f"{t['emoji']} <b>{t['name']}</b> — {t['stars']} ⭐{mark}{save}\n"
    txt += f"\n🎁 Или пригласи {REF_NEEDED} друзей — {REF_REWARD} дней Premium бесплатно!"
    if edit:
        try: await target.edit_text(txt, reply_markup=premium_kb())
        except TelegramBadRequest: pass
    else:
        await target.answer(txt, reply_markup=premium_kb())


@dp.message(Command("premium"))
async def cmd_premium(m: Message): await render_premium(m)


@dp.callback_query(F.data == "premium")
async def cb_premium(c: CallbackQuery):
    await render_premium(c.message, edit=True)
    try: await c.answer()
    except: pass


@dp.callback_query(F.data == "trial")
async def cb_trial(c: CallbackQuery):
    row = await get_user(c.from_user.id)
    if row["trial_used"]:
        try: await c.answer("❌ Пробный период уже использован", show_alert=True)
        except: pass
        return
    if await is_premium(row):
        try: await c.answer("У тебя уже есть Premium", show_alert=True)
        except: pass
        return
    await grant_premium(c.from_user.id, TRIAL_DAYS)
    await ex("UPDATE users SET trial_used=1 WHERE user_id=?", (c.from_user.id,))
    await c.message.answer(f"🎉 <b>Пробный Premium активирован!</b>\n\n✅ Доступ на <b>{TRIAL_DAYS} дня</b>\nНаслаждайся!")
    try: await c.answer()
    except: pass


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    key = c.data.split(":", 1)[1]
    t = TARIFFS.get(key)
    if not t:
        try: await c.answer("Тариф не найден", show_alert=True)
        except: pass
        return
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title=f"⭐ Premium — {t['name']}",
        description=f"Безлимитный доступ на {t['days']} дней",
        payload=f"prem_{t['days']}",
        provider_token="", currency="XTR",
        prices=[LabeledPrice(label=f"Premium {t['name']}", amount=t["stars"])])
    try: await c.answer()
    except: pass


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await q.answer(ok=True)


@dp.message(F.successful_payment)
async def on_pay(m: Message, successful_payment: SuccessfulPayment):
    sp = successful_payment
    days = int(sp.invoice_payload.split("_")[1])
    await grant_premium(m.from_user.id, days)
    await ex("INSERT INTO payments(user_id,amount,charge_id,days) VALUES(?,?,?,?)",
             (m.from_user.id, sp.total_amount, sp.provider_payment_charge_id, days))
    await m.answer(f"🎉 <b>Premium активирован!</b>\n\n✅ Доступ на <b>{days}</b> дней\nСпасибо ⭐")
    for a in ADMIN_IDS:
        try: await bot.send_message(a, f"💰 Оплата: {m.from_user.id} — {sp.total_amount}⭐ ({days} дн.)")
        except: pass


# ==================== РЕФЕРАЛЫ ====================
@dp.message(Command("ref"))
async def cmd_ref(m: Message): await render_ref(m, m.from_user.id)


@dp.callback_query(F.data == "ref")
async def cb_ref(c: CallbackQuery):
    await render_ref(c.message, c.from_user.id, edit=True)
    try: await c.answer()
    except: pass


async def render_ref(target, uid, edit=False):
    row = await get_user(uid)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    cnt = row["ref_count"] or 0
    left = REF_NEEDED - (cnt % REF_NEEDED)
    txt = (f"🎁 <b>Реферальная программа</b>\n━━━━━━━━━━━━━━━━━\n"
           f"За каждые <b>{REF_NEEDED} друзей</b> — <b>{REF_REWARD} дней Premium</b> бесплатно!\n\n"
           f"👥 Приглашено: <b>{cnt}</b>\n🎯 До награды: <b>{left}</b>\n"
           f"━━━━━━━━━━━━━━━━━\n🔗 Твоя ссылка:\n<code>{link}</code>")
    if edit:
        try: await target.edit_text(txt)
        except TelegramBadRequest: pass
    else:
        await target.answer(txt)


# ==================== АЛЕРТЫ ====================
@dp.message(Command("alert"))
async def cmd_alert(m: Message, command: CommandObject):
    row = await get_user(m.from_user.id)
    if not await is_premium(row):
        await m.answer("🔒 Алерты только в <b>Premium</b>. /premium"); return
    parts = (command.args or "").split()
    if len(parts) != 3:
        await m.answer("Формат: <code>/alert btc 100000 up</code>"); return
    t, price_s, dir_ = parts[0].lower(), parts[1], parts[2].lower()
    if dir_ not in ("up", "down"):
        await m.answer("Направление: <code>up</code> или <code>down</code>"); return
    try: price = float(price_s)
    except: await m.answer("Цена — число."); return
    cnt = await q1("SELECT COUNT(*) c FROM alerts WHERE user_id=? AND triggered=0", (m.from_user.id,))
    if cnt["c"] >= MAX_ALERTS:
        await m.answer(f"Макс {MAX_ALERTS} алертов."); return
    cid = resolve(t)
    d = await get_price(cid)
    if not d: await m.answer("❌ Монета не найдена."); return
    await ex("INSERT INTO alerts(user_id,coin_id,symbol,target,direction) VALUES(?,?,?,?,?)",
             (m.from_user.id, cid, t, price, dir_))
    a = "📈" if dir_ == "up" else "📉"
    cur = d.get("usd", 0)
    diff = ((price - cur) / cur * 100) if cur else 0
    await m.answer(f"✅ <b>Алерт создан</b>\n\n{a} <b>{t.upper()}</b>\n🎯 Цель: <b>${price:,.4f}</b>\n💵 Сейчас: ${cur:,.4f}\n📊 До цели: <b>{diff:+.2f}%</b>")


@dp.callback_query(F.data == "alerts")
async def cb_alerts(c: CallbackQuery):
    rows = await qa("SELECT * FROM alerts WHERE user_id=? AND triggered=0", (c.from_user.id,))
    if not rows:
        try:
            await c.message.edit_text("🔔 Нет алертов.\n\nСоздай: <code>/alert btc 100000 up</code>", reply_markup=back_kb("profile"))
        except TelegramBadRequest: pass
        try: await c.answer()
        except: pass
        return
    k = InlineKeyboardBuilder()
    for a in rows:
        ar = "📈" if a["direction"] == "up" else "📉"
        k.button(text=f"❌ {ar} {a['symbol'].upper()} ${a['target']:,.4f}", callback_data=f"al_del:{a['id']}")
    k.button(text="⬅️ Назад", callback_data="profile"); k.adjust(1)
    try:
        await c.message.edit_text("🔔 <b>Твои алерты:</b>", reply_markup=k.as_markup())
    except TelegramBadRequest: pass
    try: await c.answer()
    except: pass


@dp.callback_query(F.data.startswith("al_del:"))
async def cb_al_del(c: CallbackQuery):
    aid = int(c.data.split(":")[1])
    await ex("DELETE FROM alerts WHERE id=? AND user_id=?", (aid, c.from_user.id))
    try: await c.answer("Удалено")
    except: pass
    rows = await qa("SELECT * FROM alerts WHERE user_id=? AND triggered=0", (c.from_user.id,))
    if not rows:
        try:
            await c.message.edit_text("🔔 Нет алертов.", reply_markup=back_kb("profile"))
        except TelegramBadRequest: pass
    else:
        k = InlineKeyboardBuilder()
        for a in rows:
            ar = "📈" if a["direction"] == "up" else "📉"
            k.button(text=f"❌ {ar} {a['symbol'].upper()} ${a['target']:,.4f}", callback_data=f"al_del:{a['id']}")
        k.button(text="⬅️ Назад", callback_data="profile"); k.adjust(1)
        try:
            await c.message.edit_reply_markup(reply_markup=k.as_markup())
        except TelegramBadRequest: pass


async def alerts_worker():
    while True:
        try:
            rows = await qa("SELECT * FROM alerts WHERE triggered=0")
            cache = {}
            for a in rows:
                cid = a["coin_id"]
                if cid not in cache: cache[cid] = await get_price(cid)
                d = cache.get(cid)
                if not d: continue
                p = d.get("usd", 0)
                hit = (a["direction"] == "up" and p >= a["target"]) or \
                      (a["direction"] == "down" and p <= a["target"])
                if hit:
                    ar = "📈" if a["direction"] == "up" else "📉"
                    try:
                        await bot.send_message(a["user_id"],
                            f"🔔 <b>Алерт сработал!</b>\n\n{ar} <b>{a['symbol'].upper()}</b>\n"
                            f"🎯 Цель: <b>${a['target']:,.4f}</b>\n💵 Сейчас: <b>${p:,.4f}</b>")
                    except: pass
                    await ex("UPDATE alerts SET triggered=1 WHERE id=?", (a["id"],))
        except Exception as e: print("[alerts]", e)
        await asyncio.sleep(60)


# ==================== ПОРТФЕЛЬ ====================
@dp.message(Command("add"))
async def cmd_add(m: Message, command: CommandObject):
    row = await get_user(m.from_user.id)
    if not await is_premium(row):
        await m.answer("🔒 Портфель только в Premium. /premium"); return
    parts = (command.args or "").split()
    if len(parts) != 3:
        await m.answer("Формат: <code>/add btc 0.5 60000</code>"); return
    t = parts[0].lower()
    try: amt, price = float(parts[1]), float(parts[2])
    except: await m.answer("Числа!"); return
    cid = resolve(t)
    if not await get_price(cid): await m.answer("❌ Монета не найдена."); return
    await ex("INSERT INTO portfolio(user_id,coin_id,symbol,amount,buy_price) VALUES(?,?,?,?,?)",
             (m.from_user.id, cid, t, amt, price))
    await m.answer(f"✅ Добавлено: <b>{t.upper()}</b> × {amt} по ${price:,.4f}")


@dp.message(Command("portfolio"))
async def cmd_pf(m: Message): await render_pf(m, m.from_user.id)


@dp.callback_query(F.data == "pf")
async def cb_pf(c: CallbackQuery):
    await render_pf(c.message, c.from_user.id, edit=True)
    try: await c.answer()
    except: pass


async def render_pf(target, uid, edit=False):
    row = await get_user(uid)
    if not await is_premium(row):
        txt = "🔒 Портфель только для Premium. /premium"
        if edit:
            try: await target.edit_text(txt, reply_markup=back_kb("profile"))
            except TelegramBadRequest: pass
        else:
            await target.answer(txt)
        return
    items = await qa("SELECT * FROM portfolio WHERE user_id=?", (uid,))
    if not items:
        txt = "📊 <b>Портфель пуст</b>\n\nДобавь: <code>/add btc 0.5 60000</code>"
        if edit:
            try: await target.edit_text(txt, reply_markup=back_kb("profile"))
            except TelegramBadRequest: pass
        else:
            await target.answer(txt)
        return
    grp = {}
    for p in items: grp.setdefault((p["coin_id"], p["symbol"]), []).append(p)
    txt = "📊 <b>Твой портфель</b>\n━━━━━━━━━━━━━━━━━\n"
    ti = tv = 0
    for (cid, sym), arr in grp.items():
        d = await get_price(cid); cur = d.get("usd", 0) if d else 0
        amt = sum(x["amount"] for x in arr)
        inv = sum(x["amount"] * x["buy_price"] for x in arr)
        val = amt * cur; pnl = val - inv
        pct = (pnl / inv * 100) if inv else 0
        ar = "🟢" if pnl >= 0 else "🔴"
        ti += inv; tv += val
        txt += (f"💎 <b>{sym.upper()}</b>\n  📦 {amt:.6f}\n  💰 Куплено: ${inv:,.2f}\n"
                f"  💵 Сейчас: ${val:,.2f}\n  {ar} <b>${pnl:+,.2f} ({pct:+.2f}%)</b>\n\n")
    tp = tv - ti; tpct = (tp / ti * 100) if ti else 0
    ar = "🟢" if tp >= 0 else "🔴"
    txt += f"━━━━━━━━━━━━━━━━━\n💰 Вложено: <b>${ti:,.2f}</b>\n💵 Стоимость: <b>${tv:,.2f}</b>\n{ar} Итого: <b>${tp:+,.2f} ({tpct:+.2f}%)</b>"
    if edit:
        try: await target.edit_text(txt, reply_markup=back_kb("profile"))
        except TelegramBadRequest: pass
    else:
        await target.answer(txt)


@dp.message(Command("clearportfolio"))
async def cmd_clear_pf(m: Message):
    await ex("DELETE FROM portfolio WHERE user_id=?", (m.from_user.id,))
    await m.answer("🗑 Портфель очищен.")


# ==================== ИЗБРАННОЕ ====================
@dp.callback_query(F.data == "favs")
async def cb_favs(c: CallbackQuery):
    row = await get_user(c.from_user.id)
    favs = [f for f in (row["favs"] or "").split(",") if f]
    if not favs:
        try:
            await c.message.edit_text("⭐ <b>Избранные</b>\n\nПока пусто.\n\n<code>/fav btc</code>", reply_markup=back_kb("profile"))
        except TelegramBadRequest: pass
        try: await c.answer()
        except: pass
        return
    data = await get_multi([resolve(f) for f in favs])
    txt = "⭐ <b>Избранные:</b>\n\n"
    for f in favs:
        d = data.get(resolve(f))
        if not d: continue
        ch = d.get("usd_24h_change") or 0
        a = "🟢" if ch >= 0 else "🔴"
        txt += f"💎 <b>{f.upper()}</b> — ${d['usd']:,.4f} {a} {ch:+.2f}%\n"
    try:
        await c.message.edit_text(txt, reply_markup=back_kb("profile"))
    except TelegramBadRequest: pass
    try: await c.answer()
    except: pass


@dp.message(Command("fav"))
async def cmd_fav(m: Message, command: CommandObject):
    if not command.args: await m.answer("Формат: <code>/fav btc</code>"); return
    t = command.args.strip().lower()
    if not await get_price(resolve(t)): await m.answer("❌ Не нашёл монету."); return
    row = await get_user(m.from_user.id)
    favs = [f for f in (row["favs"] or "").split(",") if f]
    if t in favs: await m.answer(f"⭐ {t.upper()} уже в избранном"); return
    favs.append(t)
    await ex("UPDATE users SET favs=? WHERE user_id=?", (",".join(favs), m.from_user.id))
    await m.answer(f"⭐ {t.upper()} добавлен в избранное")


@dp.message(Command("unfav"))
async def cmd_unfav(m: Message, command: CommandObject):
    if not command.args: await m.answer("Формат: <code>/unfav btc</code>"); return
    t = command.args.strip().lower()
    row = await get_user(m.from_user.id)
    favs = [f for f in (row["favs"] or "").split(",") if f and f != t]
    await ex("UPDATE users SET favs=? WHERE user_id=?", (",".join(favs), m.from_user.id))
    await m.answer(f"✅ {t.upper()} убран")


# ==================== ПОДДЕРЖКА ====================
@dp.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    WRITING_TO_SUPPORT[c.from_user.id] = True
    await c.message.answer(
        "📞 <b>Поддержка с ИИ</b>\n\n"
        "Напиши свой вопрос, и я постараюсь помочь. "
        "Если не смогу — передам администратору.\n\n"
        "<i>Отмена — /cancel</i>")
    try: await c.answer()
    except: pass


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    if m.from_user.id in WRITING_TO_SUPPORT:
        del WRITING_TO_SUPPORT[m.from_user.id]
    await m.answer("❌ Отменено")


@dp.message(Command("reply"))
async def cmd_reply(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    if not command.args:
        await m.answer("Формат: <code>/reply ID ТЕКСТ</code>"); return
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Формат: <code>/reply ID ТЕКСТ</code>"); return
    try: uid = int(parts[0])
    except: await m.answer("ID — число"); return
    text = parts[1]
    try:
        await bot.send_message(uid,
            f"📩 <b>Ответ поддержки:</b>\n\n{text}\n\n"
            f"<i>Ответить ещё — кнопка 📞 Поддержка</i>")
        await m.answer(f"✅ Отправлено юзеру <code>{uid}</code>")
    except Exception as e:
        await m.answer(f"❌ Не отправлено: {e}")


@dp.message(Command("tickets"))
async def cmd_tickets(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    rows = await qa("SELECT * FROM tickets WHERE status='open' ORDER BY id DESC LIMIT 30")
    if not rows:
        await m.answer("📭 Открытых обращений нет"); return
    txt = f"📬 <b>Обращения ({len(rows)})</b>\n━━━━━━━━━━━━━━━━━\n"
    for t in rows:
        txt += (f"🆔 <code>{t['user_id']}</code> | {t['first_name'] or '—'} "
                f"@{t['username'] or '—'}\n"
                f"📝 {t['message'][:200]}\n"
                f"🕐 {t['created_at']}\n"
                f"↩️ <code>/reply {t['user_id']} текст</code>\n\n")
    await m.answer(txt)


@dp.message(Command("close"))
async def cmd_close_ticket(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    if not command.args:
        await m.answer("Формат: <code>/close ID_юзера</code>"); return
    try: uid = int(command.args.strip())
    except: return
    await ex("UPDATE tickets SET status='closed' WHERE user_id=? AND status='open'", (uid,))
    await m.answer(f"✅ Обращения от {uid} закрыты")


# ==================== АДМИН ====================
@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    u = await q1("SELECT COUNT(*) c FROM users")
    p = await q1("SELECT COUNT(*) c FROM users WHERE premium_until > ?", (datetime.now().isoformat(),))
    r = await q1("SELECT COALESCE(SUM(amount),0) s FROM payments")
    t = await q1("SELECT COUNT(*) c FROM tickets WHERE status='open'")
    await m.answer(
        f"🛠 <b>Админ-панель</b>\n━━━━━━━━━━━━━━━━━\n"
        f"👥 Юзеров: <b>{u['c']}</b>\n"
        f"⭐ Premium: <b>{p['c']}</b>\n"
        f"💰 Заработано: <b>{r['s']} ⭐</b>\n"
        f"📬 Обращений: <b>{t['c']}</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"<b>👥 Юзеры:</b>\n/users\n/users 2\n/find ник\n/find 123\n\n"
        f"<b>💰 Управление:</b>\n/give ID ДНИ\n/take ID\n/ban ID\n/unban ID\n/broadcast ТЕКСТ\n\n"
        f"<b>📞 Поддержка:</b>\n/tickets\n/reply ID ТЕКСТ\n/close ID")


@dp.message(Command("users"))
async def cmd_users(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    page = 1
    if command.args:
        try: page = max(1, int(command.args.strip()))
        except: pass
    per_page = 20
    total = await q1("SELECT COUNT(*) c FROM users")
    total = total["c"]
    offset = (page - 1) * per_page
    rows = await qa("SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                    (per_page, offset))
    if not rows:
        await m.answer("Нет юзеров на этой странице"); return
    txt = f"👥 <b>Юзеры</b> (стр. {page}/{(total + per_page - 1)//per_page})\n"
    txt += f"Всего: <b>{total}</b>\n━━━━━━━━━━━━━━━━━\n"
    for u in rows:
        prem = await is_premium(u)
        mark = "⭐" if prem else "·"
        uname = f"@{u['username']}" if u["username"] else "<i>без @</i>"
        name = u["first_name"] or "—"
        txt += (f"{mark} <b>{name}</b> — {uname}\n"
                f"   🆔 <code>{u['user_id']}</code>\n"
                f"   📊 {u['total_reqs'] or 0} | 👥 {u['ref_count'] or 0}\n\n")
    if page * per_page < total:
        txt += f"➡️ След: <code>/users {page + 1}</code>"
    await m.answer(txt)


@dp.message(Command("find"))
async def cmd_find(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    if not command.args:
        await m.answer("Формат:\n<code>/find ник</code> — по нику\n<code>/find 123456</code> — по ID")
        return
    q = command.args.strip().lstrip("@")
    if q.isdigit():
        rows = await qa("SELECT * FROM users WHERE user_id=?", (int(q),))
    else:
        like = f"%{q}%"
        rows = await qa("SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? LIMIT 20",
                        (like, like))
    if not rows:
        await m.answer("❌ Не найдено"); return
    txt = f"🔍 <b>Найдено: {len(rows)}</b>\n━━━━━━━━━━━━━━━━━\n"
    for u in rows:
        prem = await is_premium(u)
        mark = "⭐" if prem else "·"
        uname = f"@{u['username']}" if u["username"] else "—"
        txt += f"{mark} <b>{u['first_name'] or '—'}</b> — {uname}\n🆔 <code>{u['user_id']}</code>\n\n"
    await m.answer(txt)


@dp.message(Command("give"))
async def cmd_give(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    try: uid, d = map(int, (command.args or "").split())
    except: await m.answer("/give ID ДНИ"); return
    await grant_premium(uid, d)
    await m.answer(f"✅ +{d} дней юзеру {uid}")
    try: await bot.send_message(uid, f"🎉 Premium на {d} дней!")
    except: pass


@dp.message(Command("take"))
async def cmd_take(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    try: uid = int((command.args or "").strip())
    except: return
    await ex("UPDATE users SET premium_until=NULL WHERE user_id=?", (uid,))
    await m.answer(f"✅ Premium снят с {uid}")


@dp.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    try: uid = int((command.args or "").strip())
    except: return
    await ex("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await m.answer(f"🚫 {uid} забанен")


@dp.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    try: uid = int((command.args or "").strip())
    except: return
    await ex("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await m.answer(f"✅ {uid} разбанен")


@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    if not command.args: await m.answer("/broadcast ТЕКСТ"); return
    users = await qa("SELECT user_id FROM users WHERE banned=0")
    s = f = 0
    await m.answer(f"📤 Рассылка для {len(users)} юзеров...")
    for u in users:
        try:
            await bot.send_message(u["user_id"], command.args)
            s += 1; await asyncio.sleep(0.05)
        except: f += 1
    await m.answer(f"✅ Отправлено: {s}, ошибок: {f}")


# ==================== FALLBACK ====================
@dp.message(F.text & ~F.text.startswith("/"))
async def fallback(m: Message):
    if m.from_user.id in WRITING_TO_SUPPORT:
        del WRITING_TO_SUPPORT[m.from_user.id]
        await m.answer("🤔 Думаю...")
        ai_response = await ask_ai(m.text)
        ai_failed = "ИИ временно недоступен" in ai_response or len(ai_response) < 10
        await ex("INSERT INTO tickets(user_id,username,first_name,message) VALUES(?,?,?,?)",
                 (m.from_user.id, m.from_user.username, m.from_user.first_name, m.text))
        if ai_failed:
            await m.answer(
                "📩 <b>Ваш вопрос передан администратору</b>\n\n"
                "Ответ придёт сюда же в ближайшее время. Спасибо за понимание!")
        else:
            await m.answer(
                f"🤖 <b>ИИ-Ассистент:</b>\n\n{ai_response}\n\n"
                f"<i>Если ответ не помог — напишите /support ещё раз.</i>")
        for a in ADMIN_IDS:
            try:
                status = "⚠️ ИИ НЕ СМОГ" if ai_failed else "🤖 ИИ ответил"
                await bot.send_message(a,
                    f"📩 <b>Обращение</b>\n\n"
                    f"👤 {m.from_user.first_name} (@{m.from_user.username or '—'})\n"
                    f"🆔 <code>{m.from_user.id}</code>\n\n"
                    f"📝 {m.text}\n\n"
                    f"{status}\n"
                    f"↩️ <code>/reply {m.from_user.id} текст</code>")
            except: pass
        return

    row, ok = await check_user(m)
    if not ok:
        await m.answer(f"⚠️ Лимит {FREE_LIMIT}/день. /premium"); return
    t = m.text.strip().lower()
    if len(t) > 20 or " " in t:
        await m.answer("Напиши тикер: <code>btc</code> или /help"); return
    d = await get_price(resolve(t))
    if not d:
        await m.answer(f"❌ Не нашёл <code>{t}</code>. /help"); return
    await m.answer(fmt_price(t, d), reply_markup=chart_kb(resolve(t), t))


# ==================== ОБРАБОТЧИК ОШИБОК ====================
@dp.error()
async def error_handler(event: ErrorEvent):
    exc = event.exception
    if isinstance(exc, TelegramBadRequest):
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return True
        if "query is too old" in msg:
            return True
        if "query id is invalid" in msg:
            return True
    return False


# ==================== ЗАПУСК ====================
async def set_commands():
    cmds = [
        BotCommand(command="start", description="🚀 Запустить"),
        BotCommand(command="price", description="💎 Цена монеты"),
        BotCommand(command="chart", description="📊 График (P)"),
        BotCommand(command="top", description="🏆 Топ монет"),
        BotCommand(command="trending", description="📈 Тренды"),
        BotCommand(command="fng", description="😱 Индекс страха"),
        BotCommand(command="global", description="🌍 Обзор рынка"),
        BotCommand(command="convert", description="🔄 Конвертер (P)"),
        BotCommand(command="alert", description="🔔 Алерт (P)"),
        BotCommand(command="add", description="➕ В портфель (P)"),
        BotCommand(command="portfolio", description="📊 Портфель (P)"),
        BotCommand(command="fav", description="⭐ В избранное"),
        BotCommand(command="premium", description="💎 Premium"),
        BotCommand(command="ref", description="🎁 Рефералы"),
        BotCommand(command="profile", description="👤 Профиль"),
        BotCommand(command="help", description="❓ Помощь"),
    ]
    try: await bot.set_my_commands(cmds)
    except Exception as e: print("set_commands:", e)


async def main():
    await init_db()
    await set_commands()
    asyncio.create_task(alerts_worker())
    print("=" * 40)
    print("✅ БОТ ЗАПУЩЕН (ИИ через OpenRouter)")
    print("=" * 40)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())