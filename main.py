#!/usr/bin/env python3
"""
Daily watchlist digest.

Reads a list of stock and crypto tickers from watchlist.json, pulls a quote and
recent news for each, builds an HTML digest, and emails it via Gmail SMTP.

  - Stocks/ETFs: quotes + per-company news from Finnhub (https://finnhub.io).
  - Crypto:      price + 24h change from CoinGecko (no API key), with recent
                 headlines matched from Finnhub's general crypto news feed.

This sends factual price/news summaries only. It is NOT investment advice.

Required environment variables (see .env.example):
  FINNHUB_API_KEY     Finnhub API key
  GMAIL_ADDRESS       The Gmail address to send from AND to
  GMAIL_APP_PASSWORD  A Gmail App Password (not your normal password)

Optional:
  DIGEST_TO           Recipient override (defaults to GMAIL_ADDRESS)
  DRY_RUN=1           Write preview.html instead of sending an email
"""

import base64
import json
import os
import re
import smtplib
import struct
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

FINNHUB_BASE = "https://finnhub.io/api/v1"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HERE = Path(__file__).resolve().parent

# Built-in fast path: crypto symbol -> (CoinGecko id, news-matching keywords).
# Symbols NOT listed here are resolved automatically via CoinGecko's search API
# (top market-cap match wins), so new tickers in watchlist.json just work.
# Ambiguous symbols can be pinned explicitly via "crypto_ids" in watchlist.json.
COIN_MAP = {
    "BTC":  ("bitcoin",  ["bitcoin", "btc"]),
    "ETH":  ("ethereum", ["ethereum", "ether", "eth"]),
    "SOL":  ("solana",   ["solana", "sol"]),
    "XRP":  ("ripple",   ["ripple", "xrp"]),
    "DOGE": ("dogecoin", ["dogecoin", "doge"]),
}

# Headlines matching these get priority in the macro section.
MACRO_KEYWORDS = [
    "fed", "federal reserve", "powell", "inflation", "cpi", "ppi", "gdp",
    "jobs report", "payroll", "unemployment", "interest rate", "rate cut",
    "rate hike", "treasury", "yield", "tariff", "trade deal", "ecb", "boj",
    "china", "oil", "opec", "recession", "stimulus", "deficit", "dollar",
    "retail sales", "housing market", "consumer confidence", "earnings season",
]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    with open(HERE / "watchlist.json") as f:
        cfg = json.load(f)
    cfg.setdefault("news_per_ticker", 3)
    cfg.setdefault("news_lookback_days", 2)
    cfg.setdefault("labels", {})
    cfg["stocks"] = [t.strip().upper() for t in cfg.get("stocks", []) if t.strip()]
    cfg["crypto"] = [t.strip().upper() for t in cfg.get("crypto", []) if t.strip()]
    if not cfg["stocks"] and not cfg["crypto"]:
        sys.exit("watchlist.json has no stocks or crypto tickers.")
    return cfg


def require_env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


# Which edition this run is, from the actual Eastern time it executes.
# GitHub Actions fires scheduled jobs LATE (often by hours), so the workflow now
# uses exactly one cron per edition and always sends — never a time-window gate,
# which silently skipped every delayed run.
MORNING_CUTOFF_HOUR = 14  # ET hour before which a run is the "morning" edition


def current_edition():
    return ("morning"
            if datetime.now(ZoneInfo("America/New_York")).hour < MORNING_CUTOFF_HOUR
            else "evening")


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #
def http_get_json(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (watchlist-digest)"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429 and attempt < 2:  # rate limited, back off
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except URLError:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return None


def finnhub_get(path, params, api_key):
    params = dict(params, token=api_key)
    return http_get_json(f"{FINNHUB_BASE}/{path}?{urlencode(params)}")


def http_get_bytes(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (watchlist-digest)"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as resp:
                return resp.read()
        except (HTTPError, URLError):
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            return None
    return None


def image_subtype(data):
    """Sniff an image's format from its magic bytes."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# --------------------------------------------------------------------------- #
# Stock data (Finnhub)
# --------------------------------------------------------------------------- #
def get_stock_quote(ticker, api_key):
    try:
        q = finnhub_get("quote", {"symbol": ticker}, api_key)
    except Exception:
        q = None
    if not q or q.get("c") in (0, None):
        return None
    return {
        "current": q.get("c"),
        "high": q.get("h"),
        "low": q.get("l"),
        "pct": q.get("dp"),          # % vs previous close
        "change_basis": "vs prev close",
        "has_range": True,
    }


def get_stock_news(ticker, api_key, lookback_days, limit):
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    try:
        items = finnhub_get(
            "company-news",
            {"symbol": ticker, "from": start.isoformat(), "to": today.isoformat()},
            api_key,
        )
    except Exception:
        items = []
    return _clean_news(items, limit)


# --------------------------------------------------------------------------- #
# Crypto data (CoinGecko price + Finnhub general crypto news)
# --------------------------------------------------------------------------- #
def resolve_coins(symbols, overrides):
    """Map each crypto symbol to a CoinGecko id + news keywords.
    Priority: explicit watchlist.json override > built-in COIN_MAP >
    CoinGecko search (best market-cap match), so new symbols just work."""
    out = {}
    for sym in symbols:
        if sym in overrides:
            cg_id = overrides[sym]
            out[sym] = {"id": cg_id, "keywords": [cg_id.replace("-", " "), sym]}
        elif sym in COIN_MAP:
            cg_id, keywords = COIN_MAP[sym]
            out[sym] = {"id": cg_id, "keywords": keywords}
        else:
            try:
                res = http_get_json(
                    f"{COINGECKO_BASE}/search?{urlencode({'query': sym})}"
                ) or {}
                match = next(
                    (c for c in res.get("coins", [])
                     if c.get("symbol", "").upper() == sym), None)
            except Exception:
                match = None
            out[sym] = (
                {"id": match["id"], "keywords": [match.get("name", sym), sym]}
                if match else None
            )
    return out


def get_crypto_market(coins):
    """ONE batched CoinGecko call for all coins: USD price, 24h % change,
    logo URL, and a 7-day hourly sparkline (we chart the last ~24h of it).
    Batching avoids the per-coin rate limits that used to drop charts."""
    ids = [info["id"] for info in coins.values() if info]
    if not ids:
        return {}
    url = f"{COINGECKO_BASE}/coins/markets?" + urlencode(
        {"vs_currency": "usd", "ids": ",".join(ids), "sparkline": "true"}
    )
    try:
        data = http_get_json(url) or []
    except Exception:
        data = []
    by_id = {c.get("id"): c for c in data if isinstance(c, dict)}
    out = {}
    for sym, info in coins.items():
        c = by_id.get(info["id"]) if info else None
        if not c or c.get("current_price") is None:
            out[sym] = None
            continue
        spark = [p for p in ((c.get("sparkline_in_7d") or {}).get("price") or [])
                 if p is not None]
        series = spark[-24:]
        # The same 7-day sparkline gives the weekly change for free.
        week_pct = ((spark[-1] - spark[0]) / spark[0] * 100
                    if len(spark) >= 2 and spark[0] else None)
        out[sym] = {
            "quote": {
                "current": c["current_price"],
                "pct": c.get("price_change_percentage_24h"),
                "change_basis": "24h",
                "has_range": False,
            },
            "series": series if len(series) >= 8 else None,
            "week_pct": week_pct,
            "from_ath": c.get("ath_change_percentage"),
            "logo_url": c.get("image"),
        }
    return out


def get_crypto_news_feed(api_key):
    """Finnhub's general crypto news feed (fetched once, filtered per coin)."""
    try:
        items = finnhub_get("news", {"category": "crypto"}, api_key)
    except Exception:
        return []
    return items if isinstance(items, list) else []


def crypto_news_for(keywords, feed, limit):
    if not keywords:
        return []
    patterns = [re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in keywords]
    matched = []
    for it in feed:
        text = f"{it.get('headline', '')} {it.get('summary', '')}"
        if any(p.search(text) for p in patterns):
            matched.append(it)
    return _clean_news(matched, limit)


def get_macro_news(api_key, limit):
    """Broad market/business headlines from Finnhub's general feed, with
    macro-keyword matches (Fed, inflation, tariffs, ...) prioritized."""
    if limit <= 0:
        return []
    try:
        items = finnhub_get("news", {"category": "general"}, api_key)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    patterns = [re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE)
                for k in MACRO_KEYWORDS]
    macro, other = [], []
    for it in items:
        text = f"{it.get('headline', '')} {it.get('summary', '')}"
        (macro if any(p.search(text) for p in patterns) else other).append(it)
    picked = _clean_news(macro, limit)
    if len(picked) < limit:  # top up with general market headlines
        seen = {n["headline"] for n in picked}
        picked += [n for n in _clean_news(other, limit - len(picked))
                   if n["headline"] not in seen]
    return picked


# --------------------------------------------------------------------------- #
# Recaps (computed from price data — no API key, no cost)
# --------------------------------------------------------------------------- #
def get_stock_extras(ticker):
    """One Yahoo call for weekly change plus 52-week range context.
    Returns {"week_pct", "from_high", "range_pos"} — from_high is the percent
    below the 52-week high, range_pos where the price sits in that range (0-100)."""
    out = {"week_pct": None, "from_high": None, "range_pos": None}
    try:
        data = http_get_json(
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}?range=5d&interval=1d"
        )
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) >= 2 and closes[0]:
            out["week_pct"] = (closes[-1] - closes[0]) / closes[0] * 100
        meta = result["meta"]
        price = meta.get("regularMarketPrice")
        hi, lo = meta.get("fiftyTwoWeekHigh"), meta.get("fiftyTwoWeekLow")
        if price and hi:
            out["from_high"] = (price - hi) / hi * 100      # negative = below high
        if price and hi and lo and hi > lo:
            out["range_pos"] = (price - lo) / (hi - lo) * 100
    except Exception:
        pass
    return out


def _named(rows, key):
    """Rows that have a usable value for the given percent key."""
    return [r for r in rows if r.get(key) is not None]


def _best_worst(rows, key):
    ranked = sorted(_named(rows, key), key=lambda r: r[key])
    return (ranked[-1], ranked[0]) if ranked else (None, None)


def macro_themes(macro_news, limit=2):
    """Most-mentioned macro topics across the headlines, for one summary line."""
    counts = {}
    for n in macro_news:
        text = n["headline"].lower()
        for kw in MACRO_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", text):
                counts[kw] = counts.get(kw, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [kw for kw, _ in top]


def build_recaps(rows, macro_news, edition):
    """Two plain-language recaps built entirely from the fetched numbers."""
    # Flatten day/week percentages onto a simple shape for ranking.
    flat = [
        {"ticker": r["ticker"], "kind": r["kind"],
         "day": (r["quote"] or {}).get("pct"), "week": r.get("week_pct"),
         "from_high": r.get("from_high"), "range_pos": r.get("range_pos")}
        for r in rows
    ]
    stocks = [r for r in flat if r["kind"] == "stock"]
    crypto = [r for r in flat if r["kind"] == "crypto"]

    # ---------------- Day recap ----------------
    day_rows = _named(flat, "day")
    if not day_rows:
        day = "Price data was unavailable for this edition."
    else:
        ups = sum(1 for r in day_rows if r["day"] >= 0)
        avg = sum(r["day"] for r in day_rows) / len(day_rows)
        window = ("since the previous close, going into today's open"
                  if edition == "morning" else "on the day")
        parts = [
            f"{ups} of {len(day_rows)} tracked assets are higher {window}, "
            f"{len(day_rows) - ups} lower, averaging {fmt_pct(avg)}."
        ]
        s_best, s_worst = _best_worst(stocks, "day")
        if s_best:
            if s_best is s_worst:
                parts.append(f"Among stocks, {s_best['ticker']} is "
                             f"{fmt_pct(s_best['day'])}.")
            else:
                parts.append(
                    f"{s_best['ticker']} leads the stocks at {fmt_pct(s_best['day'])}, "
                    f"while {s_worst['ticker']} lags at {fmt_pct(s_worst['day'])}."
                )
        c_best, c_worst = _best_worst(crypto, "day")
        if c_best:
            c_ups = sum(1 for r in _named(crypto, "day") if r["day"] >= 0)
            parts.append(
                f"In crypto, {c_best['ticker']} is the standout at "
                f"{fmt_pct(c_best['day'])} with {c_ups} of "
                f"{len(_named(crypto, 'day'))} coins higher"
                + (f", and {c_worst['ticker']} the weakest at {fmt_pct(c_worst['day'])}."
                   if c_worst is not c_best else ".")
            )
        # Where the stocks sit against their own 52-week ranges.
        highs = _named(stocks, "from_high")
        if highs:
            nearest = max(highs, key=lambda r: r["from_high"])
            furthest = min(highs, key=lambda r: r["from_high"])
            if nearest is not furthest:
                parts.append(
                    f"{nearest['ticker']} is closest to a 52-week high, "
                    f"{abs(nearest['from_high']):.1f}% away, while "
                    f"{furthest['ticker']} sits {abs(furthest['from_high']):.1f}% "
                    f"below its own peak."
                )
        themes = macro_themes(macro_news)
        if themes:
            parts.append("Macro headlines are centred on "
                         + " and ".join(themes) + ".")
        day = " ".join(parts)

    # ---------------- Week recap ----------------
    week_rows = _named(flat, "week")
    if not week_rows:
        week = "Weekly change data was unavailable for this edition."
    else:
        ups = sum(1 for r in week_rows if r["week"] >= 0)
        w_avg = sum(r["week"] for r in week_rows) / len(week_rows)
        best, worst = _best_worst(flat, "week")
        parts = [
            f"Over the past week, {ups} of {len(week_rows)} are higher, "
            f"{len(week_rows) - ups} lower, averaging {fmt_pct(w_avg)}."
        ]
        if best and best is not worst:
            parts.append(
                f"{best['ticker']} is the week's biggest gainer at "
                f"{fmt_pct(best['week'])}, and {worst['ticker']} the biggest "
                f"decliner at {fmt_pct(worst['week'])}."
            )
        s_week, c_week = _named(stocks, "week"), _named(crypto, "week")
        if s_week and c_week:
            s_avg = sum(r["week"] for r in s_week) / len(s_week)
            c_avg = sum(r["week"] for r in c_week) / len(c_week)
            leader = "crypto" if c_avg > s_avg else "stocks"
            parts.append(
                f"Stocks averaged {fmt_pct(s_avg)} for the week against "
                f"{fmt_pct(c_avg)} for crypto, so {leader} carried the week."
            )
        # Longer-range positioning: 52-week range for stocks, ATH gap for crypto.
        pos = _named(stocks, "range_pos")
        if pos:
            upper = sum(1 for r in pos if r["range_pos"] >= 50)
            parts.append(
                f"{upper} of {len(pos)} stocks are trading in the upper half of "
                f"their 52-week range."
            )
        ath = _named(crypto, "from_high")
        if ath:
            worst_ath = min(ath, key=lambda r: r["from_high"])
            best_ath = max(ath, key=lambda r: r["from_high"])
            parts.append(
                f"Crypto remains off record highs: {best_ath['ticker']} is the "
                f"closest at {abs(best_ath['from_high']):.0f}% below its all-time "
                f"high, {worst_ath['ticker']} the furthest at "
                f"{abs(worst_ath['from_high']):.0f}%."
            )
        week = " ".join(parts)

    return {"day": day, "week": week}



# --------------------------------------------------------------------------- #
# Sparkline charts (intraday series -> small PNG, pure stdlib)
# --------------------------------------------------------------------------- #
def get_stock_series(ticker):
    """Intraday closes from Yahoo's chart endpoint (no key needed).
    Falls back to a 5-day/30-min series early in the trading day."""
    for rng, interval in (("1d", "5m"), ("5d", "30m")):
        try:
            data = http_get_json(
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                f"{ticker}?range={rng}&interval={interval}"
            )
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            series = [c for c in closes if c is not None]
            if len(series) >= 8:
                return series
        except Exception:
            continue
    return None


def get_stock_logo(ticker, api_key):
    """Company/ETF logo bytes via Finnhub's profile endpoint (may be empty
    for some ETFs on the free tier — the card falls back to a monogram)."""
    try:
        prof = finnhub_get("stock/profile2", {"symbol": ticker}, api_key)
        url = (prof or {}).get("logo")
        return http_get_bytes(url) if url else None
    except Exception:
        return None


def _png_encode(width, height, rgba):
    """Encode a raw RGBA bytearray as a PNG using only stdlib zlib/struct."""
    def chunk(tag, payload):
        out = struct.pack(">I", len(payload)) + tag + payload
        return out + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    stride = width * 4
    raw = b"".join(
        b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render_sparkline_png(series, up, width=520, height=112):
    """Skinny price chart: colored line with a soft gradient fill underneath.
    Rendered at 2x and displayed at half size for sharpness. Transparent bg."""
    if not series or len(series) < 2:
        return None
    lo, hi = min(series), max(series)
    if hi - lo < 1e-12:
        hi = lo + 1e-12
    pad, n = 8, len(series)

    # Interpolate one y per x column.
    ys = []
    for x in range(width):
        t = x * (n - 1) / (width - 1)
        i = min(int(t), n - 2)
        frac = t - i
        v = series[i] * (1 - frac) + series[i + 1] * frac
        ys.append(pad + (height - 2 * pad) * (1 - (v - lo) / (hi - lo)))

    buf = bytearray(width * height * 4)
    r, g, b = (26, 127, 55) if up else (193, 18, 31)

    def set_px(x, y, alpha):
        if 0 <= x < width and 0 <= y < height:
            idx = (y * width + x) * 4
            if buf[idx + 3] < alpha:
                buf[idx], buf[idx + 1], buf[idx + 2], buf[idx + 3] = r, g, b, alpha

    # Gradient fill under the line.
    for x in range(width):
        y0 = int(ys[x])
        span = max(1, height - y0)
        for y in range(y0, height):
            a = int(52 * (1 - (y - y0) / span))
            if a > 0:
                set_px(x, y, a)

    # The line itself, connecting vertical gaps between adjacent columns.
    prev = int(ys[0])
    for x in range(width):
        cur = int(ys[x])
        for y in range(min(prev, cur), max(prev, cur) + 1):
            for dy in (-1, 0, 1):
                set_px(x, y + dy, 255)
        prev = cur

    return _png_encode(width, height, buf)


# --------------------------------------------------------------------------- #
# Shared news cleanup
# --------------------------------------------------------------------------- #
def _clean_news(items, limit):
    if not isinstance(items, list):
        return []
    items = sorted(items, key=lambda x: x.get("datetime", 0), reverse=True)
    seen, out = set(), []
    for it in items:
        head = (it.get("headline") or "").strip()
        if not head or head in seen:
            continue
        seen.add(head)
        out.append({
            "headline": head,
            "source": it.get("source", ""),
            "url": it.get("url", ""),
            "when": datetime.fromtimestamp(it.get("datetime", 0), tz=timezone.utc),
        })
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Assemble rows
# --------------------------------------------------------------------------- #
def build_rows(cfg, api_key):
    rows = []

    def is_up(quote):
        pct = (quote or {}).get("pct")
        return pct is not None and pct >= 0

    for ticker in cfg["stocks"]:
        quote = get_stock_quote(ticker, api_key)
        rows.append({
            "ticker": ticker,
            "label": cfg["labels"].get(ticker, ""),
            "kind": "stock",
            "quote": quote,
            "news": get_stock_news(ticker, api_key,
                                   cfg["news_lookback_days"], cfg["news_per_ticker"]),
            "spark": render_sparkline_png(get_stock_series(ticker), up=is_up(quote)),
            "logo": get_stock_logo(ticker, api_key),
            **get_stock_extras(ticker),
        })
        time.sleep(0.3)  # gentle pacing for the free tier

    if cfg["crypto"]:
        coins = resolve_coins(cfg["crypto"], cfg.get("crypto_ids", {}))
        market = get_crypto_market(coins)  # one call for everything
        feed = get_crypto_news_feed(api_key)
        for ticker in cfg["crypto"]:
            info = coins.get(ticker)
            m = market.get(ticker)
            quote = m["quote"] if m else None
            rows.append({
                "ticker": ticker,
                "label": cfg["labels"].get(ticker, ""),
                "kind": "crypto",
                "quote": quote,
                "news": crypto_news_for(info["keywords"] if info else [],
                                        feed, cfg["news_per_ticker"]),
                "spark": render_sparkline_png(m["series"] if m else None, up=is_up(quote)),
                "logo": http_get_bytes(m["logo_url"]) if m and m.get("logo_url") else None,
                "week_pct": m["week_pct"] if m else None,
                "from_high": m["from_ath"] if m else None,
            })

    return rows


def top_mover(rows):
    movers = [r for r in rows if r["quote"] and r["quote"].get("pct") is not None]
    if not movers:
        return None
    return max(movers, key=lambda r: abs(r["quote"]["pct"]))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def fmt_money(v):
    if v is None:
        return "-"
    return f"${v:,.2f}" if v >= 1 else f"${v:,.4f}"


def fmt_pct(v):
    if v is None:
        return "-"
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


FONT = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


def render_row(r):
    q = r["quote"]
    label_html = (
        f'<div style="font-size:12px;color:#8a94a6;margin-top:1px;">{escape(r["label"])}</div>'
        if r["label"] else ""
    )

    if q:
        pct = q.get("pct")
        up = pct is not None and pct >= 0
        pill_bg, pill_fg = ("#e6f4ea", "#1a7f37") if up else ("#fdecea", "#c1121f")
        pill = (
            f'<span style="display:inline-block;background:{pill_bg};color:{pill_fg};'
            f'font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;">'
            f'{fmt_pct(pct)} <span style="font-weight:400;">{q["change_basis"]}</span></span>'
        )
        price_html = f'<div style="font-size:17px;font-weight:700;color:#0f172a;">{fmt_money(q["current"])}</div>'
        range_html = ""
        if q.get("has_range") and q.get("low") is not None:
            range_html = (
                f'<div style="font-size:12px;color:#8a94a6;margin-top:6px;">'
                f'Day range {fmt_money(q["low"])} – {fmt_money(q["high"])}</div>'
            )
    else:
        pill = ('<span style="display:inline-block;background:#fdecea;color:#c1121f;'
                'font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;">no quote</span>')
        price_html, range_html = "", ""

    spark_html = ""
    if r.get("spark_src"):
        spark_html = (
            f'<div style="margin-top:12px;"><img src="{r["spark_src"]}" '
            f'width="260" height="56" alt="{r["ticker"]} price chart" '
            f'style="display:block;border:0;max-width:100%;"></div>'
        )

    if r["news"]:
        items = "".join(
            f'<div style="padding:7px 0 0;font-size:13px;line-height:1.4;">'
            f'<a href="{n["url"]}" style="color:#2563eb;text-decoration:none;font-weight:500;">{escape(n["headline"])}</a>'
            f'<span style="color:#8a94a6;font-size:12px;"> — {escape(n["source"])}, {n["when"].strftime("%b %-d")}</span>'
            f"</div>"
            for n in r["news"]
        )
        news_html = (
            f'<div style="margin-top:12px;border-top:1px solid #eef1f5;padding-top:5px;">{items}</div>'
        )
    else:
        news_html = ('<div style="margin-top:12px;border-top:1px solid #eef1f5;padding-top:10px;'
                     'color:#8a94a6;font-size:12px;">No recent headlines.</div>')

    if r.get("logo_src"):
        logo_cell = (
            f'<td width="42" style="vertical-align:middle;">'
            f'<img src="{r["logo_src"]}" width="32" height="32" alt="" '
            f'style="display:block;border-radius:9px;"></td>'
        )
    else:
        # Monogram fallback when no logo is available.
        logo_cell = (
            f'<td width="42" style="vertical-align:middle;">'
            f'<div style="width:32px;height:32px;border-radius:9px;background:#e2e8f0;'
            f'color:#475569;font-family:{FONT};font-weight:700;font-size:15px;'
            f'text-align:center;line-height:32px;">{r["ticker"][0]}</div></td>'
        )

    return f"""<div style="background:#ffffff;border:1px solid #e5e9f0;border-radius:16px;padding:16px 18px;margin:0 0 12px;">
      <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
        {logo_cell}
        <td style="vertical-align:middle;font-family:{FONT};">
          <div style="font-size:16px;font-weight:700;color:#0f172a;">{r['ticker']}</div>
          {label_html}
        </td>
        <td style="vertical-align:middle;text-align:right;font-family:{FONT};">
          {price_html}
          <div style="margin-top:4px;">{pill}</div>
        </td>
      </tr></table>
      {range_html}
      {spark_html}
      {news_html}
    </div>"""


def section(title, rows):
    if not rows:
        return ""
    body = "".join(render_row(r) for r in rows)
    return (
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
        f'color:#8a94a6;margin:22px 6px 10px;">{title}</div>{body}'
    )


def _tinted_box(title, body):
    return (
        f'<div style="background:#f8fafc;border:1px solid #e5e9f0;border-radius:16px;'
        f'padding:14px 16px;">'
        f'<div style="font-size:10px;font-weight:700;letter-spacing:.09em;'
        f'text-transform:uppercase;color:#8a94a6;margin-bottom:6px;">{title}</div>'
        f'<div style="font-size:13px;line-height:1.55;color:#334155;font-family:{FONT};">'
        f'{escape(body)}</div></div>'
    )


def render_macro(news, recaps, edition):
    if not news and not recaps:
        return ""
    day_title = ("Today's setup · into the open" if edition == "morning"
                 else "Day recap")
    brief_box = _tinted_box(day_title, recaps["day"])
    outlook_box = _tinted_box("Week recap · last 7 days", recaps["week"])

    items = "".join(
        f'<div style="padding:10px 0;{"" if i == 0 else "border-top:1px solid #eef1f5;"}'
        f'font-size:13px;line-height:1.45;">'
        f'<a href="{n["url"]}" style="color:#2563eb;text-decoration:none;font-weight:500;">{escape(n["headline"])}</a>'
        f'<span style="color:#8a94a6;font-size:12px;"> — {escape(n["source"])}, {n["when"].strftime("%b %-d")}</span>'
        f"</div>"
        for i, n in enumerate(news)
    )
    headlines_card = (
        f'<div style="background:#ffffff;border:1px solid #e5e9f0;border-radius:16px;'
        f'padding:6px 18px;">{items}</div>'
    ) if items else ""

    return f"""<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#8a94a6;margin:22px 6px 10px;">Macro &amp; global markets</div>
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
      <td width="46%" style="vertical-align:top;padding-right:10px;">
        {brief_box}
        <div style="margin-top:12px;">{outlook_box}</div>
      </td>
      <td style="vertical-align:top;">{headlines_card}</td>
    </tr></table>"""


def render_html(rows, macro_news, recaps):
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today = now_et.strftime("%A, %B %-d")
    edition_word = current_edition()
    edition = f"{edition_word.capitalize()} edition"

    mover = top_mover(rows)
    mover_html = ""
    if mover:
        q = mover["quote"]
        up = q["pct"] is not None and q["pct"] >= 0
        mv_color = "#4ade80" if up else "#f87171"
        mover_html = f"""<div style="margin-top:16px;background:#1e293b;border-radius:12px;padding:12px 16px;">
          <div style="font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#94a3b8;">Biggest mover</div>
          <div style="font-size:18px;font-weight:700;color:#ffffff;margin-top:3px;">{mover['ticker']}
            <span style="color:{mv_color};">{fmt_pct(q['pct'])}</span>
            <span style="font-size:13px;font-weight:400;color:#94a3b8;">at {fmt_money(q['current'])}</span>
          </div>
        </div>"""

    stocks = [r for r in rows if r["kind"] == "stock"]
    crypto = [r for r in rows if r["kind"] == "crypto"]

    return f"""<div style="background:#eef1f5;padding:28px 12px;">
      <div style="max-width:600px;margin:0 auto;font-family:{FONT};">
        <div style="background:#0f172a;border-radius:20px;padding:24px;margin-bottom:4px;">
          <div style="font-size:21px;font-weight:800;color:#ffffff;">Holdings &amp; Market Digest</div>
          <div style="font-size:13px;color:#94a3b8;margin-top:3px;">{today} &nbsp;&middot;&nbsp; {edition}</div>
          {mover_html}
        </div>
        {section("Stocks &amp; ETFs", stocks)}
        {section("Crypto", crypto)}
        {render_macro(macro_news, recaps, edition_word)}
        <div style="text-align:center;color:#9aa3b2;font-size:11px;line-height:1.5;margin:20px 10px 0;">
          Stock prices &amp; % change vs. previous close (Finnhub). Crypto: USD price &amp; 24h change (CoinGecko).<br>
          Headlines from business/crypto news sources. Informational only — not investment advice.
        </div>
      </div>
    </div>"""


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(html, sender, app_password, recipient, images):
    now_et = datetime.now(ZoneInfo("America/New_York"))
    edition = current_edition().capitalize()
    subject = f"Holdings and Market Digest — {edition} Edition · {now_et:%B %-d, %Y}"

    # multipart/related lets the sparkline PNGs travel inside the email and be
    # referenced from the HTML via cid: URLs (reliable in Gmail).
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Your daily watchlist digest (view in an HTML client).", "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    for cid, (data, subtype) in images.items():
        img = MIMEImage(data, _subtype=subtype)
        img.add_header("Content-ID", f"<{cid}>")
        # Inline with NO filename: a filename makes Gmail show the image as an
        # attachment chip at the bottom of the email.
        img.add_header("Content-Disposition", "inline")
        msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, [recipient], msg.as_string())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    api_key = require_env("FINNHUB_API_KEY")
    rows = build_rows(cfg, api_key)
    macro_news = get_macro_news(api_key, cfg.get("macro_news", 6))
    edition = current_edition()
    recaps = build_recaps(rows, macro_news, edition)
    dry_run = os.environ.get("DRY_RUN") == "1"

    # Point each row's images (chart + logo) at either embedded data URIs
    # (browser preview) or cid: references resolved by inline attachments (email).
    images = {}

    def attach_image(row, key, prefix):
        data = row.get(key)
        subtype = image_subtype(data)
        if not data or not subtype:
            return
        if dry_run:
            b64 = base64.b64encode(data).decode()
            row[f"{key}_src"] = f"data:image/{subtype};base64,{b64}"
        else:
            cid = f"{prefix}_{re.sub(r'[^A-Za-z0-9]', '', row['ticker'])}"
            row[f"{key}_src"] = f"cid:{cid}"
            images[cid] = (data, subtype)

    for r in rows:
        attach_image(r, "spark", "spark")
        attach_image(r, "logo", "logo")

    html = render_html(rows, macro_news, recaps)

    if dry_run:
        out = HERE / "preview.html"
        out.write_text(html)
        print(f"DRY_RUN: wrote preview to {out} (no email sent).")
        return

    sender = require_env("GMAIL_ADDRESS")
    app_password = require_env("GMAIL_APP_PASSWORD")
    # `or`, not a get() default: GitHub Actions passes unset optional secrets as
    # an EMPTY STRING rather than omitting them, so a default would be skipped
    # and the recipient would end up blank.
    recipient = os.environ.get("DIGEST_TO") or sender
    send_email(html, sender, app_password, recipient, images)
    print(f"Sent digest to {recipient} ({len(rows)} tickers, {len(images)} charts).")


if __name__ == "__main__":
    main()
