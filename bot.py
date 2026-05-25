"""
RUDRA SECURITIES — Indian Market Signal Bot v5.0

Changes:
  • /test command — demo trade private chat + channel dono mein
  • Channel fix — bot channel mein SEND karta hai, commands private chat mein
  • Sabhi commands private chat mein perfectly kaam karti hain
  • /test sabse pehle kaam karega bina market hours ke bhi
"""

import logging, os, threading, asyncio
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import random

import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, request, abort
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))
TV_SECRET  = os.getenv("TV_SECRET", "rudra123")
PORT       = int(os.getenv("PORT", "8080"))

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

tg_app: Application = None
last_signal: dict   = {}

INDICES = {
    "NIFTY": {
        "yf": "^NSEI", "step": 50, "lot": 75,
        "prem_min": 80, "prem_max": 200,
        "target_pts": 15, "sl_pts": 10,
    },
    "BANKNIFTY": {
        "yf": "^NSEBANK", "step": 100, "lot": 15,
        "prem_min": 150, "prem_max": 400,
        "target_pts": 15, "sl_pts": 10,
    },
    "SENSEX": {
        "yf": "^BSESN", "step": 100, "lot": 10,
        "prem_min": 100, "prem_max": 300,
        "target_pts": 15, "sl_pts": 10,
    },
}

# Demo trades — /test command ke liye realistic data
DEMO_TRADES = [
    {
        "name": "NIFTY", "direction": "CE",
        "strike": 23850, "entry": 110, "sl": 95, "target": 130,
        "price": 23847.5, "confidence": 82, "score": 6,
        "rsi": 38.4, "ema9": 23812.0, "ema21": 23798.0,
        "macd": 14.2, "vol_r": 1.6, "vwap": 23830.0, "atr": 45.0,
        "reasons": [
            "RSI Oversold (38.4)",
            "EMA Bullish + Price above EMA9",
            "MACD Positive (14.2)",
            "Price above VWAP (23830.0)",
            "Volume Surge 1.6x",
            "3 Bullish Candles",
        ],
        "demo": True,
    },
    {
        "name": "BANKNIFTY", "direction": "PE",
        "strike": 51500, "entry": 175, "sl": 165, "target": 195,
        "price": 51523.0, "confidence": 78, "score": 5,
        "rsi": 64.7, "ema9": 51540.0, "ema21": 51560.0,
        "macd": -22.5, "vol_r": 1.8, "vwap": 51550.0, "atr": 88.0,
        "reasons": [
            "RSI Overbought (64.7)",
            "EMA Bearish + Price below EMA9",
            "MACD Negative (-22.5)",
            "Price below VWAP (51550.0)",
            "Volume Surge 1.8x",
        ],
        "demo": True,
    },
    {
        "name": "SENSEX", "direction": "CE",
        "strike": 78400, "entry": 145, "sl": 130, "target": 165,
        "price": 78412.0, "confidence": 75, "score": 4,
        "rsi": 42.1, "ema9": 78390.0, "ema21": 78370.0,
        "macd": 8.9, "vol_r": 1.4, "vwap": 78395.0, "atr": 120.0,
        "reasons": [
            "RSI Neutral-Low (42.1)",
            "EMA Bullish + Price above EMA9",
            "MACD Positive (8.9)",
            "Volume Surge 1.4x",
        ],
        "demo": True,
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 16) <= t <= dtime(15, 25)


def nearest_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)


def r2(x) -> float:
    return round(float(x), 2)


# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def rsi(s: pd.Series, n=14) -> float:
    d  = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    v  = (100 - 100 / (1 + rs)).iloc[-1]
    return r2(v) if not np.isnan(v) else 50.0


def ema(s: pd.Series, n: int) -> float:
    return r2(s.ewm(span=n, adjust=False).mean().iloc[-1])


def macd_hist(s: pd.Series) -> float:
    e12 = s.ewm(span=12, adjust=False).mean()
    e26 = s.ewm(span=26, adjust=False).mean()
    m   = e12 - e26
    sig = m.ewm(span=9, adjust=False).mean()
    v   = (m - sig).iloc[-1]
    return r2(v) if not np.isnan(v) else 0.0


def atr14(high, low, close) -> float:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    v = tr.rolling(14).mean().iloc[-1]
    return r2(v) if not np.isnan(v) else 0.0


def vol_ratio(volume: pd.Series, n=20) -> float:
    avg = volume.rolling(n).mean().iloc[-1]
    cur = volume.iloc[-1]
    if avg > 0:
        v = cur / avg
        return r2(v) if not np.isnan(v) else 1.0
    return 1.0


def vwap_val(df: pd.DataFrame) -> float:
    d  = df.tail(50).copy()
    tp = (d["High"] + d["Low"] + d["Close"]) / 3
    v  = (tp * d["Volume"]).cumsum() / d["Volume"].cumsum()
    val = v.iloc[-1]
    return r2(val) if not np.isnan(val) else float(df["Close"].iloc[-1])


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def analyze_index(name: str, cfg: dict) -> list[dict]:
    try:
        df = yf.download(cfg["yf"], period="5d", interval="5m",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 30:
            df = yf.download(cfg["yf"], period="10d", interval="15m",
                             progress=False, auto_adjust=True)
        if df is None or len(df) < 20:
            log.warning(f"{name}: Data nahi mila")
            return []
    except Exception as e:
        log.error(f"{name} fetch error: {e}")
        return []

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close  = df["Close"].squeeze()
    high   = df["High"].squeeze()
    low    = df["Low"].squeeze()
    volume = df["Volume"].squeeze()

    price  = r2(close.iloc[-1])
    rsi_v  = rsi(close)
    ema9_v = ema(close, 9)
    ema21_v= ema(close, 21)
    macd_v = macd_hist(close)
    vol_r  = vol_ratio(volume)
    vwap_v = vwap_val(df)
    c1, c2, c3 = close.iloc[-1], close.iloc[-2], close.iloc[-3]
    mom_up   = c1 > c2 > c3
    mom_down = c1 < c2 < c3

    log.info(f"{name} | Price={price} RSI={rsi_v} EMA9={ema9_v} EMA21={ema21_v} "
             f"MACD={macd_v} VolR={vol_r} VWAP={vwap_v}")

    results = []
    for direction in ["CE", "PE"]:
        bull  = (direction == "CE")
        score = 0
        reasons = []

        if bull:
            if rsi_v < 35:    score += 2; reasons.append(f"RSI Oversold ({rsi_v})")
            elif rsi_v < 45:  score += 1; reasons.append(f"RSI Neutral-Low ({rsi_v})")
        else:
            if rsi_v > 65:    score += 2; reasons.append(f"RSI Overbought ({rsi_v})")
            elif rsi_v > 55:  score += 1; reasons.append(f"RSI Neutral-High ({rsi_v})")

        if bull:
            if ema9_v > ema21_v and price > ema9_v:
                score += 2; reasons.append("EMA Bullish + Price above EMA9")
            elif price > ema21_v:
                score += 1; reasons.append("Price above EMA21")
        else:
            if ema9_v < ema21_v and price < ema9_v:
                score += 2; reasons.append("EMA Bearish + Price below EMA9")
            elif price < ema21_v:
                score += 1; reasons.append("Price below EMA21")

        if bull and macd_v > 0:
            score += 1; reasons.append(f"MACD Positive ({macd_v})")
        elif not bull and macd_v < 0:
            score += 1; reasons.append(f"MACD Negative ({macd_v})")

        if bull and price > vwap_v:
            score += 1; reasons.append(f"Price above VWAP ({vwap_v})")
        elif not bull and price < vwap_v:
            score += 1; reasons.append(f"Price below VWAP ({vwap_v})")

        if vol_r >= 1.3:
            score += 1; reasons.append(f"Volume Surge {vol_r}x")

        if bull and mom_up:
            score += 1; reasons.append("3 Bullish Candles")
        elif not bull and mom_down:
            score += 1; reasons.append("3 Bearish Candles")

        log.info(f"  {name} {direction}: score={score}/8 | reasons={len(reasons)}")

        if score < 3:
            continue

        strike = nearest_strike(price, cfg["step"])
        prem   = (cfg["prem_min"] + cfg["prem_max"]) // 2
        entry  = int(round(prem / 5) * 5)
        target = entry + cfg["target_pts"]
        sl     = entry - cfg["sl_pts"]
        conf   = min(92, 50 + score * 6)

        key  = f"{name}_{direction}"
        last = last_signal.get(key)
        if last:
            diff = (datetime.now(IST) - last).total_seconds()
            if diff < 900:
                log.info(f"Duplicate skip: {key} ({int(diff)}s)")
                continue
        last_signal[key] = datetime.now(IST)

        results.append({
            "name": name, "direction": direction,
            "strike": strike, "entry": entry, "target": target, "sl": sl,
            "price": price, "confidence": conf, "score": score,
            "rsi": rsi_v, "ema9": ema9_v, "ema21": ema21_v,
            "macd": macd_v, "vol_r": vol_r, "vwap": vwap_v,
            "atr": atr14(high, low, close),
            "reasons": reasons, "demo": False,
        })

    return results


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE FORMAT
# ══════════════════════════════════════════════════════════════════════════════

def format_alert(sig: dict) -> str:
    now   = datetime.now(IST).strftime("%d %b %Y | %I:%M %p")
    arrow = "📈" if sig["direction"] == "CE" else "📉"
    demo_tag = "\n🔸 <b>[DEMO TRADE — Real nahi hai]</b>" if sig.get("demo") else ""
    reasons_s = "\n".join(f"  ✅ {r}" for r in sig["reasons"])

    return (
        f"╔══════════════════════════╗\n"
        f"   🔔 RUDRA SECURITIES\n"
        f"       TRADING ALERT\n"
        f"╚══════════════════════════╝\n"
        f"{demo_tag}\n"
        f"{arrow} <b>Index:</b> {sig['name']}\n"
        f"📌 <b>Type:</b> {sig['direction']}\n"
        f"💰 <b>Entry Price:</b> ₹{sig['entry']}\n"
        f"🎯 <b>Strike:</b> {sig['strike']}\n"
        f"🛑 <b>Stop Loss:</b> ₹{sig['sl']}\n"
        f"✅ <b>Target:</b> ₹{sig['target']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Analysis:</b>\n"
        f"  Spot: ₹{sig['price']} | RSI: {sig['rsi']}\n"
        f"  EMA9: {sig['ema9']} | VWAP: {sig['vwap']}\n"
        f"  Vol: {sig['vol_r']}x | Score: {sig['score']}/8\n\n"
        f"📝 <b>Reasons:</b>\n{reasons_s}\n\n"
        f"🎯 Confidence: {sig['confidence']}%\n"
        f"🕐 {now} IST\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Educational purpose only.</i>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL POST
# ══════════════════════════════════════════════════════════════════════════════

async def post_to_channel(bot, sig: dict) -> bool:
    if not CHANNEL_ID:
        log.warning("CHANNEL_ID set nahi hai!")
        return False
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=format_alert(sig),
            parse_mode="HTML",
        )
        log.info(f"✅ Channel post: {sig['name']} {sig['direction']} @ {sig['entry']}")
        return True
    except Exception as e:
        log.error(f"❌ Channel post FAIL: {e}")
        log.error(f"   CHANNEL_ID='{CHANNEL_ID}' — Format check karo (@channel ya -100xxx)")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# AUTO SCAN
# ══════════════════════════════════════════════════════════════════════════════

async def smart_scan(ctx: ContextTypes.DEFAULT_TYPE):
    if not is_market_open():
        return
    log.info("🔍 Auto scan shuru...")
    total = 0
    for name, cfg in INDICES.items():
        for sig in analyze_index(name, cfg):
            await post_to_channel(ctx.bot, sig)
            total += 1
            await asyncio.sleep(1)
    log.info(f"🔍 Scan complete — {total} signals")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# Note: Yeh sirf PRIVATE CHAT mein kaam karte hain.
# Channel mein bot sirf SEND karta hai (receive nahi kar sakta).
# ══════════════════════════════════════════════════════════════════════════════

async def safe_reply(update: Update, text: str, **kwargs):
    """Private chat mein reply karo"""
    msg = update.effective_message
    if msg:
        try:
            await msg.reply_html(text, **kwargs)
        except Exception as e:
            log.error(f"Reply error: {e}")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Test Trade",     callback_data="test")],
        [InlineKeyboardButton("📊 Manual Scan",    callback_data="scan")],
        [InlineKeyboardButton("📈 Market Status",  callback_data="status")],
        [InlineKeyboardButton("❓ Help",            callback_data="help")],
    ])
    await safe_reply(update,
        "🔔 <b>RUDRA SECURITIES BOT v5.0</b>\n\n"
        "✅ CE + PE dono side scan\n"
        "✅ 15 rupees target\n"
        "✅ Har 3 min auto scan\n"
        "✅ NIFTY | BANKNIFTY | SENSEX\n"
        "✅ Channel mein auto post\n\n"
        "🧪 Pehle /test karo — demo trade dekhne ke liye!\n\n"
        "⚠️ <b>Note:</b> Commands sirf is private chat mein kaam\n"
        "karti hain. Channel mein bot khud signal bhejta hai.",
        reply_markup=kb,
    )


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /test — Demo trade dikhao + channel mein bhi post karo.
    Market hours ki zarurat nahi — kab bhi kaam karta hai.
    """
    msg = update.effective_message
    if not msg:
        return

    # Random demo trade choose karo
    sig = random.choice(DEMO_TRADES).copy()

    # Time update karo
    now = datetime.now(IST).strftime("%d %b %Y | %I:%M %p")

    # Step 1: Private chat mein dikhao
    await msg.reply_html(
        "🧪 <b>TEST MODE — Demo Trade</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Yeh ek demo trade hai — real signal nahi.\n"
        "Channel mein bhi post ho raha hai...\n"
    )

    # Step 2: Channel mein post karo
    posted = await post_to_channel(ctx.bot, sig)

    # Step 3: Result dikhao
    if posted:
        await msg.reply_html(
            format_alert(sig) + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ <b>Channel mein post ho gaya!</b>\n"
            "Ab apna channel check karo — wahan bhi yeh trade dikhega.\n\n"
            "🟢 <b>Bot sahi kaam kar raha hai!</b>\n"
            "Real signals automatically market hours mein aayenge."
        )
    else:
        await msg.reply_html(
            format_alert(sig) + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Private chat mein toh aaya!</b>\n"
            "Lekin channel post FAIL hua.\n\n"
            "<b>Channel fix karne ke liye:</b>\n"
            "1. Bot ko channel ka admin banao\n"
            "2. Railway mein CHANNEL_ID check karo\n"
            "   Format: <code>@channelname</code> ya <code>-100xxxxxxxxxx</code>\n"
            "3. Bot ko channel mein add karke Admin rights do\n\n"
            f"Current CHANNEL_ID: <code>{CHANNEL_ID or 'SET NAHI HAI!'}</code>"
        )

    log.info(f"Test command — demo trade: {sig['name']} {sig['direction']}, "
             f"channel_posted={posted}")


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and ADMIN_ID and user.id != ADMIN_ID:
        await safe_reply(update, "❌ Sirf admin manual scan kar sakta hai.")
        return

    msg = update.effective_message
    if not msg:
        return

    wait = await msg.reply_text("⏳ Scanning NIFTY, BANKNIFTY, SENSEX — CE + PE dono...")
    total = 0

    for name, cfg in INDICES.items():
        for sig in analyze_index(name, cfg):
            posted = await post_to_channel(ctx.bot, sig)
            ch_txt = "✅ Channel post hua!" if posted else "⚠️ Channel post fail"
            await msg.reply_html(format_alert(sig) + f"\n\n{ch_txt}")
            total += 1
            await asyncio.sleep(1)

    if total == 0:
        mkt = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
        now = datetime.now(IST).strftime("%I:%M %p IST")
        await wait.edit_text(
            f"⚪ <b>Koi signal nahi mila</b>\n\n"
            f"Market: {mkt} | {now}\n\n"
            f"Reasons:\n"
            f"• Market sideways — clear trend nahi\n"
            f"• Indicators align nahi kar rahe\n\n"
            f"Bot har 3 min mein auto scan karta hai.\n"
            f"Agar test karna ho: /test",
            parse_mode="HTML"
        )
    else:
        try:
            await wait.delete()
        except:
            pass


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mkt = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
    now = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    ch  = CHANNEL_ID if CHANNEL_ID else "⚠️ SET NAHI HAI"

    await safe_reply(update,
        f"📡 <b>Bot Status v5.0</b>\n\n"
        f"Market: {mkt}\n"
        f"🕐 {now}\n\n"
        f"📢 Channel: <code>{ch}</code>\n"
        f"⏱ Auto Scan: Har 3 minute (market hours)\n"
        f"📊 Indices: NIFTY | BANKNIFTY | SENSEX\n"
        f"🎯 Target: ₹15 | SL: ₹10\n\n"
        f"🧪 Test ke liye: /test"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update,
        "📖 <b>RUDRA SECURITIES — Help</b>\n\n"
        "<b>Commands (private chat mein):</b>\n"
        "/start       — Bot info + buttons\n"
        "/test        — Demo trade (channel mein bhi post)\n"
        "/scan        — Manual scan (admin)\n"
        "/status      — Bot + market status\n"
        "/help        — Yeh message\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>Channel ke baare mein:</b>\n"
        "Channel mein commands type karne se kuch nahi hoga.\n"
        "Bot channel mein khud se signal post karta hai.\n"
        "Commands sirf bot ke private chat mein kaam karti hain.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Educational purpose only.</i>"
    )


async def button_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    class FU:
        effective_message = q.message
        effective_user    = q.from_user

    if q.data == "test":
        await cmd_test(FU(), ctx)
    elif q.data == "scan":
        await cmd_scan(FU(), ctx)
    elif q.data == "status":
        await cmd_status(FU(), ctx)
    elif q.data == "help":
        await cmd_help(FU(), ctx)


# ══════════════════════════════════════════════════════════════════════════════
# FLASK
# ══════════════════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "version": "5.0"}, 200

@flask_app.route("/webhook", methods=["POST"])
def tv_webhook():
    try:
        data = request.get_json(force=True)
        if not data or data.get("secret") != TV_SECRET:
            abort(403)
        sig = {
            "name": data.get("index", "NIFTY"),
            "direction": data.get("type", "CE").upper(),
            "strike": int(data.get("strike", 0)),
            "entry": int(data.get("entry", 0)),
            "sl": int(data.get("sl", 0)),
            "target": int(data.get("target", 0)),
            "price": float(data.get("spot", 0)),
            "confidence": int(data.get("confidence", 80)),
            "score": 5, "rsi": "N/A", "ema9": "N/A", "ema21": "N/A",
            "macd": "N/A", "vol_r": "N/A", "vwap": "N/A", "atr": 0,
            "reasons": [data.get("reason", "TradingView Alert")], "demo": False,
        }
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def _s():
            if tg_app: await post_to_channel(tg_app.bot, sig)
        loop.run_until_complete(_s())
        loop.close()
        return {"status": "posted"}, 200
    except Exception as e:
        return {"error": str(e)}, 500

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    global tg_app
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing!")
    if not CHANNEL_ID:
        log.warning("⚠️  CHANNEL_ID set nahi hai — channel post nahi hoga!")

    tg_app = Application.builder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start",  cmd_start))
    tg_app.add_handler(CommandHandler("test",   cmd_test))   # NEW
    tg_app.add_handler(CommandHandler("scan",   cmd_scan))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("help",   cmd_help))
    tg_app.add_handler(CallbackQueryHandler(button_cb))

    tg_app.job_queue.run_repeating(smart_scan, interval=180, first=15)

    threading.Thread(target=run_flask, daemon=True).start()
    log.info(f"✅ Webhook port {PORT}")
    log.info("✅ Rudra Securities Bot v5.0 — Live!")
    tg_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
