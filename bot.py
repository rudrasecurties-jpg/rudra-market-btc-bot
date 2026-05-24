"""
RUDRA SECURITIES — BTC/USDT Binance Signal Bot v1.0

Strategy (same as Indian market bot):
  • RSI + EMA + MACD + Volume confluence
  • Binance public API se live data (no API key needed for signals)
  • Minimum score 7/12 for signal
  • Duplicate signal 30 min tak block
  • Auto post to Telegram channel
  • LONG = BUY | SHORT = SELL signal
  • 24/7 scan — crypto kabhi band nahi hota

Platform: Railway.app
"""

import logging, os, threading, asyncio, time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
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
CHANNEL_ID = os.getenv("CHANNEL_ID", "")       # @yourchannel ya -100xxxxx
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

# Duplicate signal block — same direction 30 min ke andar nahi
last_signal: dict = {}   # {"BTCUSDT_LONG": datetime, ...}

# ── Trading pairs — sirf BTC/USDT focus ──────────────────────────────────────
PAIRS = {
    "BTC/USDT": {
        "symbol":    "BTCUSDT",       # Binance symbol
        "timeframe": "15m",           # 15-minute candles
        "sl_pct":    1.5,             # Stop loss 1.5%
        "tp_pct":    3.0,             # Target 3% (2:1 R:R)
    }
}

# Binance public API — no key needed
BINANCE_API = "https://api.binance.com/api/v3"


# ══════════════════════════════════════════════════════════════════════════════
# BINANCE DATA FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_binance_ohlcv(symbol: str, interval: str = "15m", limit: int = 200) -> pd.DataFrame | None:
    """
    Binance public API se OHLCV data fetch karo.
    No API key needed — completely free.
    """
    try:
        url    = f"{BINANCE_API}/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp   = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        raw = resp.json()

        if not raw:
            return None

        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore"
        ])

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)

        return df

    except requests.exceptions.RequestException as e:
        log.error(f"Binance API error ({symbol}): {e}")
        return None


def fetch_btc_price() -> float | None:
    """Current BTC price fetch karo"""
    try:
        resp = requests.get(f"{BINANCE_API}/ticker/price", params={"symbol": "BTCUSDT"}, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        log.error(f"Price fetch error: {e}")
        return None


def fetch_24h_stats() -> dict | None:
    """24h stats — volume, high, low, change%"""
    try:
        resp = requests.get(f"{BINANCE_API}/ticker/24hr", params={"symbol": "BTCUSDT"}, timeout=5)
        resp.raise_for_status()
        d = resp.json()
        return {
            "change_pct": round(float(d["priceChangePercent"]), 2),
            "high":       round(float(d["highPrice"]), 2),
            "low":        round(float(d["lowPrice"]), 2),
            "volume":     round(float(d["volume"]), 2),
            "trades":     int(d["count"]),
        }
    except Exception as e:
        log.error(f"24h stats error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def compute_rsi(close: pd.Series, n: int = 14) -> float:
    d  = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return round(float((100 - 100 / (1 + rs)).iloc[-1]), 2)


def compute_ema(close: pd.Series, n: int) -> float:
    return round(float(close.ewm(span=n, adjust=False).mean().iloc[-1]), 2)


def compute_macd(close: pd.Series) -> tuple[float, float, float]:
    e12  = close.ewm(span=12, adjust=False).mean()
    e26  = close.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    sig  = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sig
    return (
        round(float(macd.iloc[-1]), 2),
        round(float(sig.iloc[-1]),  2),
        round(float(hist.iloc[-1]), 2),
    )


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> float:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return round(float(tr.rolling(n).mean().iloc[-1]), 2)


def compute_bollinger(close: pd.Series, n: int = 20) -> tuple[float, float, float]:
    """Upper, Middle, Lower bands"""
    mid   = close.rolling(n).mean()
    std   = close.rolling(n).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    return (
        round(float(upper.iloc[-1]), 2),
        round(float(mid.iloc[-1]),   2),
        round(float(lower.iloc[-1]), 2),
    )


def volume_ratio(volume: pd.Series, n: int = 20) -> float:
    avg = volume.rolling(n).mean().iloc[-1]
    cur = volume.iloc[-1]
    return round(float(cur / avg), 2) if avg > 0 else 1.0


def compute_stoch_rsi(close: pd.Series, rsi_n: int = 14, stoch_n: int = 14) -> float:
    """Stochastic RSI — RSI ka RSI, extra sensitive"""
    d  = close.diff()
    up = d.clip(lower=0).rolling(rsi_n).mean()
    dn = (-d.clip(upper=0)).rolling(rsi_n).mean()
    rs = up / dn.replace(0, np.nan)
    rsi_s = 100 - 100 / (1 + rs)
    lo  = rsi_s.rolling(stoch_n).min()
    hi  = rsi_s.rolling(stoch_n).max()
    stoch_rsi = (rsi_s - lo) / (hi - lo).replace(0, np.nan) * 100
    return round(float(stoch_rsi.iloc[-1]), 2)


def detect_candle_pattern(df: pd.DataFrame) -> str:
    """
    Last 3 candles se pattern detect karo.
    Returns: 'BULLISH' / 'BEARISH' / 'NONE'
    """
    o = df["open"]
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # Hammer (bullish reversal)
    c1, o1, h1, l1 = c.iloc[-1], o.iloc[-1], h.iloc[-1], l.iloc[-1]
    body  = abs(c1 - o1)
    lower_wick = min(c1, o1) - l1
    upper_wick = h1 - max(c1, o1)

    if lower_wick >= 2 * body and upper_wick < body * 0.5 and c1 > o1:
        return "BULLISH"   # Hammer

    # Shooting star (bearish reversal)
    if upper_wick >= 2 * body and lower_wick < body * 0.5 and c1 < o1:
        return "BEARISH"   # Shooting star

    # Bullish engulfing
    o2, c2 = o.iloc[-2], c.iloc[-2]
    if c2 < o2 and c1 > o1 and c1 > o2 and o1 < c2:
        return "BULLISH"

    # Bearish engulfing
    if c2 > o2 and c1 < o1 and c1 < o2 and o1 > c2:
        return "BEARISH"

    # 3 consecutive bullish candles
    if c.iloc[-1] > o.iloc[-1] and c.iloc[-2] > o.iloc[-2] and c.iloc[-3] > o.iloc[-3]:
        return "BULLISH"

    # 3 consecutive bearish candles
    if c.iloc[-1] < o.iloc[-1] and c.iloc[-2] < o.iloc[-2] and c.iloc[-3] < o.iloc[-3]:
        return "BEARISH"

    return "NONE"


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE — Same strategy as Indian bot, crypto ke liye tuned
# ══════════════════════════════════════════════════════════════════════════════

def analyze_btc(pair_name: str, cfg: dict) -> dict | None:
    """
    Multi-indicator confluence — 6 indicators:
    RSI + StochRSI + EMA (9/21/50) + MACD + Volume + Bollinger + Candle Pattern

    Signal tabhi milega jab score >= 7
    """
    df = fetch_binance_ohlcv(cfg["symbol"], cfg["timeframe"], limit=200)
    if df is None or len(df) < 60:
        log.warning(f"{pair_name}: Insufficient data")
        return None

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    price = round(float(close.iloc[-1]), 2)

    # ── All indicators ─────────────────────────────────────────────────────────
    rsi_v      = compute_rsi(close)
    stoch_v    = compute_stoch_rsi(close)
    ema9       = compute_ema(close, 9)
    ema21      = compute_ema(close, 21)
    ema50      = compute_ema(close, 50)
    ema200     = compute_ema(close, 200)
    macd_v, macd_sig, macd_hist = compute_macd(close)
    atr_v      = compute_atr(high, low, close)
    vol_r      = volume_ratio(volume)
    bb_upper, bb_mid, bb_lower = compute_bollinger(close)
    candle     = detect_candle_pattern(df)

    # ── Confluence Scoring ─────────────────────────────────────────────────────
    bull = 0
    bear = 0
    bull_reasons = []
    bear_reasons = []

    # 1. RSI (max 3 points)
    if rsi_v <= 25:
        bull += 3; bull_reasons.append(f"RSI Extremely Oversold ({rsi_v})")
    elif rsi_v <= 35:
        bull += 2; bull_reasons.append(f"RSI Oversold ({rsi_v})")
    elif rsi_v <= 45:
        bull += 1; bull_reasons.append(f"RSI Weak ({rsi_v})")
    elif rsi_v >= 75:
        bear += 3; bear_reasons.append(f"RSI Extremely Overbought ({rsi_v})")
    elif rsi_v >= 65:
        bear += 2; bear_reasons.append(f"RSI Overbought ({rsi_v})")
    elif rsi_v >= 55:
        bear += 1; bear_reasons.append(f"RSI Strong ({rsi_v})")

    # 2. Stochastic RSI (max 2 points)
    if stoch_v <= 20:
        bull += 2; bull_reasons.append(f"StochRSI Oversold ({stoch_v})")
    elif stoch_v <= 35:
        bull += 1; bull_reasons.append(f"StochRSI Low ({stoch_v})")
    elif stoch_v >= 80:
        bear += 2; bear_reasons.append(f"StochRSI Overbought ({stoch_v})")
    elif stoch_v >= 65:
        bear += 1; bear_reasons.append(f"StochRSI High ({stoch_v})")

    # 3. EMA Alignment (max 3 points)
    if ema9 > ema21 > ema50 and price > ema200:
        bull += 3; bull_reasons.append("EMA Full Bullish (9>21>50, above 200)")
    elif ema9 > ema21 > ema50:
        bull += 2; bull_reasons.append("EMA Bullish Alignment (9>21>50)")
    elif price > ema21:
        bull += 1; bull_reasons.append("Price above EMA21")
    elif ema9 < ema21 < ema50 and price < ema200:
        bear += 3; bear_reasons.append("EMA Full Bearish (9<21<50, below 200)")
    elif ema9 < ema21 < ema50:
        bear += 2; bear_reasons.append("EMA Bearish Alignment (9<21<50)")
    elif price < ema21:
        bear += 1; bear_reasons.append("Price below EMA21")

    # 4. MACD (max 2 points)
    if macd_v > macd_sig and macd_hist > 0:
        bull += 2; bull_reasons.append(f"MACD Bullish Cross (hist: {macd_hist})")
    elif macd_hist > 0:
        bull += 1; bull_reasons.append("MACD Histogram Positive")
    elif macd_v < macd_sig and macd_hist < 0:
        bear += 2; bear_reasons.append(f"MACD Bearish Cross (hist: {macd_hist})")
    elif macd_hist < 0:
        bear += 1; bear_reasons.append("MACD Histogram Negative")

    # 5. Volume surge (max 2 points)
    if vol_r >= 2.0:
        if bull > bear: bull += 2; bull_reasons.append(f"Strong Volume Surge {vol_r}x")
        else:           bear += 2; bear_reasons.append(f"Strong Volume Surge {vol_r}x")
    elif vol_r >= 1.5:
        if bull > bear: bull += 1; bull_reasons.append(f"Volume Surge {vol_r}x")
        else:           bear += 1; bear_reasons.append(f"Volume Surge {vol_r}x")

    # 6. Bollinger Band (max 2 points)
    if price <= bb_lower:
        bull += 2; bull_reasons.append(f"Price at Lower BB (oversold zone)")
    elif price <= bb_mid:
        bull += 1; bull_reasons.append("Price below BB Mid")
    elif price >= bb_upper:
        bear += 2; bear_reasons.append(f"Price at Upper BB (overbought zone)")
    elif price >= bb_mid:
        bear += 1; bear_reasons.append("Price above BB Mid")

    # 7. Candle Pattern (max 2 points)
    if candle == "BULLISH":
        bull += 2; bull_reasons.append("Bullish Candle Pattern")
    elif candle == "BEARISH":
        bear += 2; bear_reasons.append("Bearish Candle Pattern")

    # ── Decision — minimum 7 points chahiye ───────────────────────────────────
    log.info(f"{pair_name}: Bull={bull} Bear={bear}")

    if bull >= 7 and bull > bear + 2:
        direction = "LONG"
        score     = bull
        reasons   = bull_reasons
    elif bear >= 7 and bear > bull + 2:
        direction = "SHORT"
        score     = bear
        reasons   = bear_reasons
    else:
        return None   # No clear signal

    # ── SL / Target based on ATR ───────────────────────────────────────────────
    sl_pct = cfg["sl_pct"] / 100
    tp_pct = cfg["tp_pct"] / 100

    if direction == "LONG":
        entry  = price
        sl     = round(price * (1 - sl_pct), 2)
        target = round(price * (1 + tp_pct), 2)
    else:
        entry  = price
        sl     = round(price * (1 + sl_pct), 2)
        target = round(price * (1 - tp_pct), 2)

    rr         = round(abs(target - entry) / abs(sl - entry), 2) if sl != entry else 0
    confidence = min(95, 50 + score * 3)

    # ── Duplicate block — same direction 30 min tak nahi ──────────────────────
    key  = f"{cfg['symbol']}_{direction}"
    last = last_signal.get(key)
    if last:
        diff = (datetime.now(timezone.utc) - last).total_seconds()
        if diff < 1800:
            log.info(f"Duplicate skip: {key} ({int(diff)}s ago)")
            return None
    last_signal[key] = datetime.now(timezone.utc)

    return {
        "pair":       pair_name,
        "symbol":     cfg["symbol"],
        "direction":  direction,
        "entry":      entry,
        "sl":         sl,
        "target":     target,
        "rr":         rr,
        "confidence": confidence,
        "score":      score,
        "rsi":        rsi_v,
        "stoch":      stoch_v,
        "ema9":       ema9,
        "ema21":      ema21,
        "ema50":      ema50,
        "macd_hist":  macd_hist,
        "vol_r":      vol_r,
        "bb_upper":   bb_upper,
        "bb_lower":   bb_lower,
        "atr":        atr_v,
        "reasons":    reasons,
        "timeframe":  cfg["timeframe"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE FORMAT
# ══════════════════════════════════════════════════════════════════════════════

def format_signal(sig: dict) -> str:
    now       = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    arrow     = "📈" if sig["direction"] == "LONG" else "📉"
    dir_emoji = "🟢 LONG" if sig["direction"] == "LONG" else "🔴 SHORT"
    reasons_s = "\n".join(f"    ✅ {r}" for r in sig["reasons"][:5])  # max 5 dikhao

    conf_bar = "█" * (sig["confidence"] // 10) + "░" * (10 - sig["confidence"] // 10)

    return (
        f"╔══════════════════════════╗\n"
        f"  🔔 RUDRA SECURITIES\n"
        f"     CRYPTO ALERT\n"
        f"╚══════════════════════════╝\n\n"
        f"{arrow} <b>Pair:</b> {sig['pair']}\n"
        f"📌 <b>Signal:</b> {dir_emoji}\n"
        f"⏱ <b>Timeframe:</b> {sig['timeframe']}\n\n"
        f"💰 <b>Entry:</b>    <code>${sig['entry']:,.2f}</code>\n"
        f"🎯 <b>Target:</b>   <code>${sig['target']:,.2f}</code>\n"
        f"🛑 <b>Stop Loss:</b><code>${sig['sl']:,.2f}</code>\n"
        f"⚖️ <b>R:R Ratio:</b> <code>{sig['rr']}:1</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Indicators:</b>\n"
        f"    RSI: <code>{sig['rsi']}</code>  "
        f"StochRSI: <code>{sig['stoch']}</code>\n"
        f"    EMA9: <code>{sig['ema9']:,.0f}</code>  "
        f"EMA21: <code>{sig['ema21']:,.0f}</code>  "
        f"EMA50: <code>{sig['ema50']:,.0f}</code>\n"
        f"    MACD Hist: <code>{sig['macd_hist']}</code>  "
        f"Vol: <code>{sig['vol_r']}x</code>\n\n"
        f"🏆 <b>Confidence:</b> <code>{sig['confidence']}%</code>\n"
        f"    {conf_bar}\n\n"
        f"✅ <b>Reasons:</b>\n{reasons_s}\n\n"
        f"🕐 {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Educational purpose only.\n"
        f"Crypto trading involves high risk.</i>\n"
        f"📲 <i>@RudraSecurities</i>"
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
            text=format_signal(sig),
            parse_mode="HTML",
        )
        log.info(f"✅ Posted: {sig['pair']} {sig['direction']} @ ${sig['entry']}")
        return True
    except Exception as e:
        log.error(f"❌ Channel post fail: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED SCANNER — Har 3 minute (crypto fast move karta hai)
# ══════════════════════════════════════════════════════════════════════════════

async def smart_scan(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Har 3 minute scan.
    Crypto 24/7 — koi market hours restriction nahi.
    Signal milne par channel mein post.
    """
    log.info("🔍 BTC scan shuru...")

    for pair_name, cfg in PAIRS.items():
        sig = analyze_btc(pair_name, cfg)
        if sig:
            await post_to_channel(ctx.bot, sig)
            await asyncio.sleep(1)


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
        [InlineKeyboardButton("📊 Manual Scan",   callback_data="scan")],
        [InlineKeyboardButton("💰 BTC Price",     callback_data="price")],
        [InlineKeyboardButton("📈 24h Stats",     callback_data="stats")],
        [InlineKeyboardButton("❓ Help",           callback_data="help")],
    ])
    await safe_reply(update,
        "🔔 <b>RUDRA SECURITIES — Crypto Bot</b>\n\n"
        "₿ <b>BTC/USDT Signal Bot</b>\n\n"
        "✅ <b>Strategy:</b>\n"
        "• RSI + StochRSI + EMA (9/21/50/200)\n"
        "• MACD + Volume + Bollinger Bands\n"
        "• Candle Pattern Detection\n"
        "• Minimum 7/16 score for signal\n"
        "• 24/7 automatic scanning\n\n"
        "📢 Channel mein auto post hota hai!\n"
        "👇 Manual scan bhi kar sakte ho:",
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

    wait = await msg.reply_text("⏳ BTC/USDT analysis kar raha hoon...")

    found = 0
    for pair_name, cfg in PAIRS.items():
        sig = analyze_btc(pair_name, cfg)
        if sig:
            posted = await post_to_channel(ctx.bot, sig)
            status = "✅ Channel mein post hua!" if posted else "⚠️ Channel post fail"
            await msg.reply_html(format_signal(sig) + f"\n\n{status}")
            found += 1

    if found == 0:
        price = fetch_btc_price()
        price_txt = f"Current BTC: ${price:,.2f}" if price else ""
        await wait.edit_text(
            f"⚪ <b>Abhi koi strong signal nahi.</b>\n\n"
            f"{price_txt}\n\n"
            f"Indicators mein clarity nahi — wait karo.\n"
            f"Bot automatically scan karta rehta hai.",
            parse_mode="HTML"
        )
    else:
        await wait.delete()


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price = fetch_btc_price()
    stats = fetch_24h_stats()
    now   = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")

    if not price:
        await safe_reply(update, "❌ Price fetch nahi hua. Thodi der baad try karo.")
        return

    change_emoji = "📈" if (stats and stats["change_pct"] >= 0) else "📉"
    stats_txt = ""
    if stats:
        stats_txt = (
            f"\n{change_emoji} <b>24h Change:</b> <code>{stats['change_pct']}%</code>\n"
            f"🔺 <b>24h High:</b>   <code>${stats['high']:,.2f}</code>\n"
            f"🔻 <b>24h Low:</b>    <code>${stats['low']:,.2f}</code>\n"
            f"📦 <b>24h Volume:</b> <code>{stats['volume']:,.0f} BTC</code>"
        )

    await safe_reply(update,
        f"₿ <b>BTC/USDT Live Price</b>\n\n"
        f"💰 <b>Price: <code>${price:,.2f}</code></b>"
        f"{stats_txt}\n\n"
        f"🕐 {now}"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price = fetch_btc_price()
    now   = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    ch    = CHANNEL_ID if CHANNEL_ID else "Set nahi hai ⚠️"
    p_txt = f"${price:,.2f}" if price else "N/A"

    await safe_reply(update,
        f"📡 <b>Bot Status</b>\n\n"
        f"₿ BTC Price: <code>{p_txt}</code>\n"
        f"🟢 Status: Running (24/7)\n"
        f"🕐 {now}\n\n"
        f"📢 Channel: <code>{ch}</code>\n"
        f"⏱ Scan: Har 3 minute\n"
        f"📊 Pairs: BTC/USDT"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update,
        "📖 <b>Crypto Bot — Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start  — Bot info\n"
        "/scan   — Manual scan (sirf admin)\n"
        "/price  — BTC live price + 24h stats\n"
        "/status — Bot status\n"
        "/help   — Yeh message\n\n"
        "<b>Signal kab aata hai:</b>\n"
        "7+ indicators agree karein tabhi\n"
        "Har 3 minute mein auto scan\n\n"
        "⚠️ <i>Educational purpose only.\n"
        "Crypto trading mein bahut risk hai.</i>"
    )


# ── Button callbacks ──────────────────────────────────────────────────────────
async def button_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    class FakeUpdate:
        effective_message = q.message
        effective_user    = q.from_user

    if q.data == "scan":
        await cmd_scan(FakeUpdate(), ctx)
    elif q.data == "price":
        await cmd_price(FakeUpdate(), ctx)
    elif q.data == "stats":
        await cmd_price(FakeUpdate(), ctx)
    elif q.data == "help":
        await cmd_help(FakeUpdate(), ctx)


# ══════════════════════════════════════════════════════════════════════════════
# FLASK — TradingView webhook
# ══════════════════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "service": "Rudra Crypto Bot", "pair": "BTC/USDT"}, 200


@flask_app.route("/webhook", methods=["POST"])
def tv_webhook():
    """
    TradingView se manual BTC alert receive karo.
    JSON format:
    {
      "secret": "rudra123",
      "direction": "LONG",
      "entry": 67500,
      "sl": 66500,
      "target": 69000,
      "confidence": 85,
      "reason": "EMA Cross + RSI Oversold"
    }
    """
    try:
        data = request.get_json(force=True)
        if not data or data.get("secret") != TV_SECRET:
            abort(403)

        direction = data.get("direction", "LONG").upper()
        entry     = float(data.get("entry", 0))
        sl        = float(data.get("sl", 0))
        target    = float(data.get("target", 0))
        conf      = int(data.get("confidence", 80))
        reason    = data.get("reason", "TradingView Alert")
        rr        = round(abs(target - entry) / abs(sl - entry), 2) if sl != entry else 0

        sig = {
            "pair":       "BTC/USDT",
            "symbol":     "BTCUSDT",
            "direction":  direction,
            "entry":      entry,
            "sl":         sl,
            "target":     target,
            "rr":         rr,
            "confidence": conf,
            "score":      conf // 10,
            "rsi":        "N/A",
            "stoch":      "N/A",
            "ema9":       entry,
            "ema21":      entry,
            "ema50":      entry,
            "macd_hist":  "N/A",
            "vol_r":      "N/A",
            "bb_upper":   target,
            "bb_lower":   sl,
            "atr":        abs(entry - sl),
            "reasons":    [reason],
            "timeframe":  data.get("timeframe", "15m"),
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
        raise SystemExit("❌ BOT_TOKEN set nahi hai!")

    if not CHANNEL_ID:
        log.warning("⚠️  CHANNEL_ID set nahi — channel post nahi hoga!")

    tg_app = Application.builder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start",  cmd_start))
    tg_app.add_handler(CommandHandler("scan",   cmd_scan))
    tg_app.add_handler(CommandHandler("price",  cmd_price))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("help",   cmd_help))
    tg_app.add_handler(CallbackQueryHandler(button_cb))

    # Har 3 minute scan — crypto fast move karta hai
    tg_app.job_queue.run_repeating(smart_scan, interval=180, first=20)

    # Flask webhook thread
    threading.Thread(target=run_flask, daemon=True).start()
    log.info(f"✅ Webhook ready on port {PORT}")

    log.info("✅ BTC Bot live — 24/7 scanning!")
    tg_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
