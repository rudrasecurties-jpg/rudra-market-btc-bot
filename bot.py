"""
RUDRA SECURITIES — Indian Market Signal Bot v4.0

Key Changes from v3:
  • Score threshold 7 → 3 (zyada signals milenge)
  • CE + PE DONO simultaneously scan — bull + bear dono
  • Realistic ATM option premium (index-based fixed ranges)
  • 15 rupees profit target — ATM options ke liye perfect
  • Duplicate block 30min → 15min (zyada frequent)
  • Har 3 min scan (pehle 5 min tha)
  • Debug logging — Railway logs mein score dikhega
  • Market hours extended: 9:16 AM – 3:25 PM

Platform: Railway.app
"""

import logging, os, threading, asyncio
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

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

# Duplicate block — same index + same direction 15 min tak nahi
last_signal: dict = {}

# ── Indices config ─────────────────────────────────────────────────────────────
# premium_base = typical ATM option premium range for that index
# step         = strike gap (NIFTY=50, BANKNIFTY=100, SENSEX=100)
INDICES = {
    "NIFTY": {
        "yf":           "^NSEI",
        "step":         50,
        "lot":          75,
        "prem_min":     80,    # typical ATM premium minimum
        "prem_max":     200,   # typical ATM premium maximum
        "target_pts":   15,    # 15 rupees target
        "sl_pts":       10,    # 10 rupees SL
    },
    "BANKNIFTY": {
        "yf":           "^NSEBANK",
        "step":         100,
        "lot":          15,
        "prem_min":     150,
        "prem_max":     400,
        "target_pts":   15,
        "sl_pts":       10,
    },
    "SENSEX": {
        "yf":           "^BSESN",
        "step":         100,
        "lot":          10,
        "prem_min":     100,
        "prem_max":     300,
        "target_pts":   15,
        "sl_pts":       10,
    },
}


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
    e12  = s.ewm(span=12, adjust=False).mean()
    e26  = s.ewm(span=26, adjust=False).mean()
    m    = e12 - e26
    sig  = m.ewm(span=9, adjust=False).mean()
    v    = (m - sig).iloc[-1]
    return r2(v) if not np.isnan(v) else 0.0


def atr14(high: pd.Series, low: pd.Series, close: pd.Series) -> float:
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


def vwap(df: pd.DataFrame) -> float:
    """Intraday VWAP — last 50 candles"""
    d    = df.tail(50).copy()
    tp   = (d["High"] + d["Low"] + d["Close"]) / 3
    vwap = (tp * d["Volume"]).cumsum() / d["Volume"].cumsum()
    v    = vwap.iloc[-1]
    return r2(v) if not np.isnan(v) else float(df["Close"].iloc[-1])


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE — CE + PE dono check karta hai
# ══════════════════════════════════════════════════════════════════════════════

def analyze_index(name: str, cfg: dict) -> list[dict]:
    """
    Returns list of signals — could be CE, PE, or both, or empty.
    Threshold: score >= 3  (lenient — 15 rs target ke liye enough)
    """
    # ── Data fetch ─────────────────────────────────────────────────────────────
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

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close  = df["Close"].squeeze()
    high   = df["High"].squeeze()
    low    = df["Low"].squeeze()
    volume = df["Volume"].squeeze()

    price = r2(close.iloc[-1])

    # ── Compute indicators ─────────────────────────────────────────────────────
    rsi_v    = rsi(close)
    ema9_v   = ema(close, 9)
    ema21_v  = ema(close, 21)
    ema50_v  = ema(close, 50)
    macd_v   = macd_hist(close)
    atr_v    = atr14(high, low, close)
    vol_r    = vol_ratio(volume)
    vwap_v   = vwap(df)

    # Price momentum — last 3 candles
    c3 = close.iloc[-3]
    c2 = close.iloc[-2]
    c1 = close.iloc[-1]
    mom_up   = c1 > c2 > c3      # rising
    mom_down = c1 < c2 < c3      # falling

    log.info(
        f"{name} | Price={price} RSI={rsi_v} EMA9={ema9_v} EMA21={ema21_v} "
        f"MACD={macd_v} VolR={vol_r} VWAP={vwap_v}"
    )

    results = []

    # ── Check BOTH CE and PE ───────────────────────────────────────────────────
    for direction in ["CE", "PE"]:
        bull = direction == "CE"

        score   = 0
        reasons = []

        # 1. RSI  (max 2 pts)
        if bull:
            if rsi_v < 35:   score += 2; reasons.append(f"RSI Oversold ({rsi_v})")
            elif rsi_v < 45: score += 1; reasons.append(f"RSI Neutral-Low ({rsi_v})")
        else:
            if rsi_v > 65:   score += 2; reasons.append(f"RSI Overbought ({rsi_v})")
            elif rsi_v > 55: score += 1; reasons.append(f"RSI Neutral-High ({rsi_v})")

        # 2. EMA alignment  (max 2 pts)
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

        # 3. MACD histogram  (max 1 pt)
        if bull and macd_v > 0:
            score += 1; reasons.append(f"MACD Positive ({macd_v})")
        elif not bull and macd_v < 0:
            score += 1; reasons.append(f"MACD Negative ({macd_v})")

        # 4. VWAP  (max 1 pt)
        if bull and price > vwap_v:
            score += 1; reasons.append(f"Price above VWAP ({vwap_v})")
        elif not bull and price < vwap_v:
            score += 1; reasons.append(f"Price below VWAP ({vwap_v})")

        # 5. Volume surge  (max 1 pt)
        if vol_r >= 1.3:
            score += 1; reasons.append(f"Volume Surge {vol_r}x")

        # 6. Momentum  (max 1 pt)
        if bull and mom_up:
            score += 1; reasons.append("3 Bullish Candles")
        elif not bull and mom_down:
            score += 1; reasons.append("3 Bearish Candles")

        log.info(f"{name} {direction}: score={score}/8")

        # ── Threshold: 3 out of 8 ──────────────────────────────────────────────
        if score < 3:
            continue

        # ── Strike price ───────────────────────────────────────────────────────
        step   = cfg["step"]
        strike = nearest_strike(price, step)

        # ATM premium — use middle of expected range
        prem_mid = (cfg["prem_min"] + cfg["prem_max"]) // 2

        # Entry = mid premium, round to nearest 5
        entry  = int(round(prem_mid / 5) * 5)
        target = entry + cfg["target_pts"]    # +15 rs
        sl     = entry - cfg["sl_pts"]        # -10 rs

        # Confidence
        confidence = min(92, 50 + score * 6)

        # ── Duplicate check — 15 min block ────────────────────────────────────
        key  = f"{name}_{direction}"
        last = last_signal.get(key)
        if last:
            diff = (datetime.now(IST) - last).total_seconds()
            if diff < 900:   # 15 min
                log.info(f"Duplicate skip: {key} ({int(diff)}s ago)")
                continue
        last_signal[key] = datetime.now(IST)

        results.append({
            "name":       name,
            "direction":  direction,
            "strike":     strike,
            "entry":      entry,
            "target":     target,
            "sl":         sl,
            "price":      price,
            "confidence": confidence,
            "score":      score,
            "rsi":        rsi_v,
            "ema9":       ema9_v,
            "ema21":      ema21_v,
            "macd":       macd_v,
            "vol_r":      vol_r,
            "vwap":       vwap_v,
            "atr":        atr_v,
            "reasons":    reasons,
        })

    return results


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE FORMAT
# ══════════════════════════════════════════════════════════════════════════════

def format_alert(sig: dict) -> str:
    now   = datetime.now(IST).strftime("%d %b %Y | %I:%M %p")
    arrow = "📈" if sig["direction"] == "CE" else "📉"
    reasons_s = "\n".join(f"  ✅ {r}" for r in sig["reasons"])

    return (
        f"╔══════════════════════════╗\n"
        f"   🔔 RUDRA SECURITIES\n"
        f"       TRADING ALERT\n"
        f"╚══════════════════════════╝\n\n"
        f"{arrow} <b>Index:</b> {sig['name']}\n"
        f"📌 <b>Type:</b> {sig['direction']}\n"
        f"💰 <b>Entry Price:</b> ₹{sig['entry']}\n"
        f"🎯 <b>Strike:</b> {sig['strike']}\n"
        f"🛑 <b>Stop Loss:</b> ₹{sig['sl']}\n"
        f"✅ <b>Target:</b> ₹{sig['target']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Analysis:</b>\n"
        f"  Spot Price: ₹{sig['price']}\n"
        f"  RSI: {sig['rsi']} | EMA9: {sig['ema9']}\n"
        f"  VWAP: {sig['vwap']} | Vol: {sig['vol_r']}x\n"
        f"  Score: {sig['score']}/8\n\n"
        f"📝 <b>Reasons:</b>\n{reasons_s}\n\n"
        f"🎯 Confidence: {sig['confidence']}%\n"
        f"🕐 {now} IST\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Educational purpose only.</i>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL POSTER
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
        log.info(f"✅ Posted: {sig['name']} {sig['direction']} @ {sig['entry']}")
        return True
    except Exception as e:
        log.error(f"❌ Channel post fail: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED SCANNER — Har 3 minute, market hours mein
# ══════════════════════════════════════════════════════════════════════════════

async def smart_scan(ctx: ContextTypes.DEFAULT_TYPE):
    if not is_market_open():
        return

    log.info("🔍 Auto scan shuru...")
    total_signals = 0

    for name, cfg in INDICES.items():
        signals = analyze_index(name, cfg)
        for sig in signals:
            await post_to_channel(ctx.bot, sig)
            total_signals += 1
            await asyncio.sleep(1)

    log.info(f"🔍 Scan done. Signals found: {total_signals}")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def safe_reply(update: Update, text: str, **kwargs):
    msg = update.effective_message
    if msg:
        try:
            await msg.reply_html(text, **kwargs)
        except Exception as e:
            log.error(f"Reply error: {e}")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Manual Scan",  callback_data="scan")],
        [InlineKeyboardButton("📈 Market Status", callback_data="status")],
        [InlineKeyboardButton("❓ Help",          callback_data="help")],
    ])
    await safe_reply(update,
        "🔔 <b>RUDRA SECURITIES BOT v4.0</b>\n\n"
        "✅ <b>Kya naya hai:</b>\n"
        "• CE + PE dono side simultaneously scan\n"
        "• 15 rupees target focus\n"
        "• Har 3 minute auto scan\n"
        "• NIFTY | BANKNIFTY | SENSEX\n"
        "• Channel mein auto post\n\n"
        "📢 Bot khud channel mein signal bhejta hai!\n"
        "👇 Manual scan bhi ho sakta hai:",
        reply_markup=kb,
    )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and ADMIN_ID and user.id != ADMIN_ID:
        await safe_reply(update, "❌ Sirf admin manual scan kar sakta hai.")
        return

    msg = update.effective_message
    if not msg:
        return

    wait = await msg.reply_text("⏳ NIFTY, BANKNIFTY, SENSEX — CE + PE dono scan kar raha hoon...")
    total = 0

    for name, cfg in INDICES.items():
        signals = analyze_index(name, cfg)
        for sig in signals:
            posted = await post_to_channel(ctx.bot, sig)
            ch_txt = "✅ Channel mein post hua!" if posted else "⚠️ Channel post fail"
            await msg.reply_html(format_alert(sig) + f"\n\n{ch_txt}")
            total += 1
            await asyncio.sleep(1)

    if total == 0:
        now = datetime.now(IST).strftime("%I:%M %p IST")
        mkt = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
        await wait.edit_text(
            f"⚪ <b>Koi signal nahi mila.</b>\n\n"
            f"Market: {mkt} | {now}\n\n"
            f"<b>Possible reasons:</b>\n"
            f"• Market sideways hai — koi clear trend nahi\n"
            f"• Indicators agree nahi kar rahe\n"
            f"• 15 min baad dobara try karo\n\n"
            f"Bot automatically har 3 min mein scan karta hai.",
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
    ch  = CHANNEL_ID if CHANNEL_ID else "⚠️ Set nahi hai"

    await safe_reply(update,
        f"📡 <b>Bot Status v4.0</b>\n\n"
        f"Market: {mkt}\n"
        f"🕐 {now}\n\n"
        f"📢 Channel: <code>{ch}</code>\n"
        f"⏱ Auto Scan: Har 3 minute\n"
        f"📊 Indices: NIFTY | BANKNIFTY | SENSEX\n"
        f"🎯 Target: 15 pts | SL: 10 pts\n"
        f"📌 CE + PE dono side scan"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update,
        "📖 <b>RUDRA SECURITIES — Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start  — Bot info\n"
        "/scan   — Manual scan (admin only)\n"
        "/status — Status check\n"
        "/help   — Yeh message\n\n"
        "<b>Signal Logic:</b>\n"
        "RSI + EMA + MACD + VWAP + Volume\n"
        "Score 3/8+ milne par signal post\n\n"
        "<b>Target:</b> ₹15 profit per trade\n"
        "<b>SL:</b> ₹10 per trade\n\n"
        "⚠️ <i>Educational purpose only.</i>"
    )


async def button_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    class FakeUpdate:
        effective_message = q.message
        effective_user    = q.from_user

    if q.data == "scan":
        await cmd_scan(FakeUpdate(), ctx)
    elif q.data == "status":
        await cmd_status(FakeUpdate(), ctx)
    elif q.data == "help":
        await cmd_help(FakeUpdate(), ctx)


# ══════════════════════════════════════════════════════════════════════════════
# FLASK WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "version": "4.0", "service": "Rudra Securities"}, 200


@flask_app.route("/webhook", methods=["POST"])
def tv_webhook():
    """
    TradingView se manual alert.
    JSON: { "secret":"rudra123", "index":"NIFTY", "type":"CE",
            "strike":23850, "entry":110, "sl":95, "target":130 }
    """
    try:
        data = request.get_json(force=True)
        if not data or data.get("secret") != TV_SECRET:
            abort(403)

        sig = {
            "name":       data.get("index", "NIFTY"),
            "direction":  data.get("type", "CE").upper(),
            "strike":     int(data.get("strike", 0)),
            "entry":      int(data.get("entry", 0)),
            "sl":         int(data.get("sl", 0)),
            "target":     int(data.get("target", 0)),
            "price":      float(data.get("spot", 0)),
            "confidence": int(data.get("confidence", 80)),
            "score":      5,
            "rsi":        "N/A", "ema9": "N/A", "ema21": "N/A",
            "macd":       "N/A", "vol_r": "N/A", "vwap": "N/A",
            "atr":        0,
            "reasons":    [data.get("reason", "TradingView Alert")],
        }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def _send():
            if tg_app:
                await post_to_channel(tg_app.bot, sig)
        loop.run_until_complete(_send())
        loop.close()

        return {"status": "posted"}, 200
    except Exception as e:
        log.error(f"Webhook error: {e}")
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
        log.warning("⚠️  CHANNEL_ID set nahi hai!")

    tg_app = Application.builder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start",  cmd_start))
    tg_app.add_handler(CommandHandler("scan",   cmd_scan))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("help",   cmd_help))
    tg_app.add_handler(CallbackQueryHandler(button_cb))

    # Har 3 minute auto scan
    tg_app.job_queue.run_repeating(smart_scan, interval=180, first=15)

    threading.Thread(target=run_flask, daemon=True).start()
    log.info(f"✅ Webhook ready port {PORT}")
    log.info("✅ Rudra Securities Bot v4.0 — Live!")
    tg_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
