#!/usr/bin/env python3
"""Biggest losers of the day (S&P 500 + Nasdaq-100) with fundamentals, for the Stock Watch app.

Runs on GitHub Actions (hourly during US hours). Pulls daily bars for the whole
universe in one batch from Yahoo Finance, ranks today's % moves, then fetches
fundamentals and the latest headlines for the worst N and scores each one with
simple, transparent quality rules. Output: site/data/losers.json (public, no
personal data).
"""
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone

import pandas as pd
import requests

OUT = os.environ.get("LOSERS_OUT", "site/data/losers.json")
TOP_N = int(os.environ.get("LOSERS_TOP_N", "25"))
PAGES_BASE = os.environ.get("PAGES_BASE", "https://maheshsapgeek.github.io/stock-watch")
UA = {"User-Agent": "Mozilla/5.0 (stock-watch losers job; github actions)"}

# Last-resort universe if both Wikipedia and the cached copy are unreachable.
FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK.B", "AVGO", "TSLA", "LLY", "JPM", "WMT", "V", "MA",
    "XOM", "UNH", "ORCL", "COST", "JNJ", "HD", "PG", "ABBV", "NFLX", "BAC", "KO", "CRM", "CVX", "MRK", "AMD", "PEP",
    "CSCO", "TMO", "ACN", "LIN", "MCD", "ADBE", "WFC", "ABT", "IBM", "GE", "PM", "TXN", "CAT", "QCOM", "INTU", "ISRG",
    "DHR", "AMGN", "NOW", "GS", "NEE", "VZ", "DIS", "RTX", "SPGI", "PFE", "CMCSA", "LOW", "UBER", "AMAT", "BKNG", "T",
    "HON", "UNP", "AXP", "ETN", "PGR", "BLK", "SYK", "COP", "MU", "VRTX", "BSX", "LRCX", "TJX", "ADP", "PANW", "ADI",
    "MDT", "GILD", "SCHW", "C", "KLAC", "BMY", "ANET", "FI", "SBUX", "MMC", "DE", "PLD", "CB", "VST", "MP", "FCX",
    "CRWD", "PLTR", "INTC", "SOFI", "RKLB", "IONQ", "NBIS", "SPCX",
]

RULES = [
    "Operating margin positive (+15), above 15% (+5); net loss (-10)",
    "Revenue growing (+10), above 10% (+5); shrinking (-5)",
    "Net debt below 2x EBITDA (+15), 2-4x (+5), above 4x or no EBITDA with net debt (-10)",
    "Free cash flow positive (+10)",
    "Forward P/E between 8 and 30 (+10); above 50 (-5); price/sales above 15 (-5)",
    "Analysts: buy/strong buy (+5); average target more than 15% above price (+5)",
    "Market cap above $10B (+5); dividend yield above 2% (+5)",
    "More than 40% below its 52-week high (-5): a long downtrend, not a one-day dip",
    "65+ = quality on a bad day; 45-64 = worth a look, check the reason; under 45 = falling for a reason",
]


LOG = []


def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.append(line)


def yahoo_symbol(sym):
    return sym.replace(".", "-")


SYM_RX = re.compile(r"[A-Z]{1,5}(\.[A-Z])?")

# Nasdaq-100 names that are not in the S&P 500, plus the stocks Raj follows, so his names show up when they fall hard.
EXTRAS = [
    "PDD", "MELI", "ASML", "AZN", "TEAM", "DDOG", "ZS", "ARM", "MSTR", "MRVL", "CSGP", "LULU", "ON", "BIIB", "GFS",
    "SPCX", "RKLB", "IONQ", "NBIS", "SOFI", "MP", "CEVA", "VST", "MU", "AVGO", "CRWD", "PLTR", "INTC",
]
SECTORS = {}


def load_sp500():
    """S&P 500 constituents from the 'datasets' CSV on GitHub (reliable, no HTML parsing)."""
    r = requests.get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
                     headers=UA, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    col = next(c for c in df.columns if str(c).strip().lower() == "symbol")
    sec = next((c for c in df.columns if "sector" in str(c).lower()), None)
    syms = []
    for _, row in df.iterrows():
        s = str(row[col]).strip().upper()
        if SYM_RX.fullmatch(s):
            syms.append(s)
            if sec:
                SECTORS[s] = str(row[sec])
    if len(syms) < 400:
        raise RuntimeError(f"only {len(syms)} S&P symbols parsed")
    return syms


def load_nasdaq100():
    r = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=UA, timeout=30)
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        cols = [" ".join(str(x) for x in c).lower() if isinstance(c, tuple) else str(c).lower() for c in t.columns]
        for i, c in enumerate(cols):
            if "ticker" in c or "symbol" in c:
                syms = [str(s).strip().upper() for s in t.iloc[:, i].tolist()]
                syms = [s for s in syms if SYM_RX.fullmatch(s)]
                if len(syms) > 80:
                    return syms
    raise RuntimeError("no Nasdaq-100 table found")


def load_universe():
    """S&P 500 (+ Nasdaq-100 when reachable) + extras; then the copy published with the site; then a built-in list."""
    try:
        sp = load_sp500()
        nq = []
        try:
            nq = load_nasdaq100()
        except Exception as e:  # noqa: BLE001
            log(f"Nasdaq-100 list failed (continuing with extras): {type(e).__name__}: {e}")
        syms = sorted(set(sp) | set(nq) | set(EXTRAS))
        log(f"universe: {len(sp)} S&P 500 + {len(nq)} Nasdaq-100 + extras = {len(syms)}")
        return syms, "github+wikipedia" if nq else "github"
    except Exception as e:  # noqa: BLE001
        log(f"S&P 500 list failed: {type(e).__name__}: {e}")
    try:
        r = requests.get(f"{PAGES_BASE}/data/universe.json", headers=UA, timeout=20)
        r.raise_for_status()
        syms = r.json().get("symbols") or []
        if len(syms) > 300:
            log(f"universe from cached copy: {len(syms)}")
            return sorted(set(syms) | set(EXTRAS)), "cached"
    except Exception as e:  # noqa: BLE001
        log(f"cached universe failed: {type(e).__name__}: {e}")
    log(f"universe from built-in fallback: {len(FALLBACK)}")
    return sorted(set(FALLBACK) | set(EXTRAS)), "fallback"


def download_bars(symbols):
    import yfinance as yf

    ysyms = [yahoo_symbol(s) for s in symbols]
    last_err = None
    for attempt in range(3):
        try:
            df = yf.download(ysyms, period="7d", interval="1d", group_by="ticker", threads=True,
                             auto_adjust=False, progress=False)
            if df is not None and len(df):
                return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"download attempt {attempt + 1} failed: {type(e).__name__}: {e}")
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"price download failed: {last_err}")


def day_moves(symbols, df):
    """Return list of {sym, price, prev, pct, bar} using the last two daily bars per symbol."""
    moves = []
    multi = isinstance(df.columns, pd.MultiIndex)
    for sym in symbols:
        ys = yahoo_symbol(sym)
        try:
            sub = df[ys] if multi else df
            closes = sub["Close"].dropna()
            if len(closes) < 2:
                continue
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            if prev <= 0 or last <= 0:
                continue
            pct = (last / prev - 1) * 100
            if pct < -70 or pct > 200:  # almost always a data glitch, not a trade
                continue
            moves.append({"sym": sym, "price": round(last, 4), "prev": round(prev, 4), "pct": round(pct, 2),
                          "bar": str(closes.index[-1].date())})
        except Exception:  # noqa: BLE001
            continue
    return moves


def num(v):
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def news_for(tk):
    items = []
    try:
        raw = tk.news or []
    except Exception:  # noqa: BLE001
        raw = []
    for n in raw[:5]:
        c = n.get("content") if isinstance(n, dict) and isinstance(n.get("content"), dict) else n
        title = c.get("title")
        if not title:
            continue
        src = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
        url = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")
        when = c.get("pubDate") or c.get("displayTime")
        if not when and c.get("providerPublishTime"):
            when = datetime.fromtimestamp(int(c["providerPublishTime"]), tz=timezone.utc).isoformat(timespec="seconds")
        items.append({"title": title, "src": src or "", "url": url or "", "at": when or ""})
    return items[:3]


def fundamentals(sym):
    import yfinance as yf

    tk = yf.Ticker(yahoo_symbol(sym))
    info = {}
    for attempt in range(2):
        try:
            info = tk.info or {}
            if info:
                break
        except Exception as e:  # noqa: BLE001
            log(f"{sym} info attempt {attempt + 1}: {type(e).__name__}: {e}")
            time.sleep(2)
    g = info.get
    f = {
        "name": g("shortName") or g("longName") or sym,
        "sector": g("sector") or SECTORS.get(sym), "industry": g("industry"),
        "mcap": num(g("marketCap")),
        "peTTM": num(g("trailingPE")), "peFwd": num(g("forwardPE")), "ps": num(g("priceToSalesTrailing12Months")),
        "revGrowth": num(g("revenueGrowth")), "epsGrowth": num(g("earningsGrowth")),
        "opMargin": num(g("operatingMargins")), "netMargin": num(g("profitMargins")), "roe": num(g("returnOnEquity")),
        "cash": num(g("totalCash")), "debt": num(g("totalDebt")), "ebitda": num(g("ebitda")), "fcf": num(g("freeCashflow")),
        # trailingAnnualDividendYield is a fraction in every yfinance version; dividendYield switched to percent in 2025.
        "divYield": (num(g("trailingAnnualDividendYield")) or 0) * 100 if g("trailingAnnualDividendYield") is not None else num(g("dividendYield")),
        "hi52": num(g("fiftyTwoWeekHigh")), "lo52": num(g("fiftyTwoWeekLow")), "beta": num(g("beta")),
        "target": num(g("targetMeanPrice")), "rec": g("recommendationKey"), "analysts": num(g("numberOfAnalystOpinions")),
        "earningsAt": None,
    }
    ts = g("earningsTimestamp") or g("earningsTimestampStart")
    if ts:
        try:
            f["earningsAt"] = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        except Exception:  # noqa: BLE001
            pass
    if f["divYield"] is not None and f["divYield"] > 25:  # no index name yields that; treat as a unit mix-up
        f["divYield"] = None
    return f, news_for(tk)


def score(f, price):
    s, why = 0, []
    om, nm = f.get("opMargin"), f.get("netMargin")
    if om is not None:
        if om > 0:
            s += 15; why.append(f"operating margin {om*100:.0f}%")
            if om > 0.15:
                s += 5
        if nm is not None and nm < 0:
            s -= 10; why.append("net loss")
    rg = f.get("revGrowth")
    if rg is not None:
        if rg > 0:
            s += 10; why.append(f"revenue +{rg*100:.0f}%")
            if rg > 0.10:
                s += 5
        else:
            s -= 5; why.append(f"revenue {rg*100:.0f}%")
    cash, debt, ebitda = f.get("cash") or 0, f.get("debt") or 0, f.get("ebitda")
    net_debt = debt - cash
    if ebitda and ebitda > 0:
        lev = net_debt / ebitda
        if lev < 2:
            s += 15; why.append("little or no net debt" if lev <= 0 else f"net debt {lev:.1f}x EBITDA")
        elif lev < 4:
            s += 5; why.append(f"net debt {lev:.1f}x EBITDA")
        else:
            s -= 10; why.append(f"heavy debt {lev:.1f}x EBITDA")
    elif net_debt > 0:
        s -= 10; why.append("net debt with no EBITDA")
    fcf = f.get("fcf")
    if fcf is not None:
        if fcf > 0:
            s += 10; why.append("positive free cash flow")
        else:
            why.append("burning cash")
    pe = f.get("peFwd")
    if pe is not None:
        if 8 <= pe <= 30:
            s += 10; why.append(f"forward P/E {pe:.0f}")
        elif pe > 50:
            s -= 5; why.append(f"forward P/E {pe:.0f}")
        elif pe > 0:
            why.append(f"forward P/E {pe:.0f}")
    ps = f.get("ps")
    if ps is not None and ps > 15:
        s -= 5; why.append(f"{ps:.0f}x sales")
    rec = (f.get("rec") or "").lower()
    if rec in ("strong_buy", "buy"):
        s += 5; why.append("analysts: " + rec.replace("_", " "))
    tgt = f.get("target")
    if tgt and price and tgt / price - 1 > 0.15:
        s += 5; why.append(f"target {tgt/price*100-100:.0f}% above")
    if (f.get("mcap") or 0) > 10e9:
        s += 5
    dy = f.get("divYield")
    if dy and dy > 2:
        s += 5; why.append(f"dividend {dy:.1f}%")
    hi = f.get("hi52")
    if hi and price and price / hi - 1 < -0.40:
        s -= 5; why.append(f"{(1-price/hi)*100:.0f}% below 52-week high")
    s = max(0, min(100, s))
    verdict = "quality" if s >= 65 else "look" if s >= 45 else "reason"
    return s, verdict, why


def write(obj):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))


def main():
    t0 = time.time()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    symbols, source = load_universe()
    try:
        with open(os.path.join(os.path.dirname(OUT), "universe.json"), "w") as fh:
            json.dump({"at": now, "source": source, "symbols": symbols}, fh)
    except Exception:  # noqa: BLE001
        pass
    df = download_bars(symbols + ["SPY", "QQQ"])
    moves = day_moves(symbols, df)
    ctx = {m["sym"]: m for m in day_moves(["SPY", "QQQ"], df)}
    if len(moves) < 20:
        raise RuntimeError(f"only {len(moves)} symbols priced")
    bar = max(m["bar"] for m in moves)
    today = [m for m in moves if m["bar"] == bar]
    down = sum(1 for m in today if m["pct"] < 0)
    losers = sorted(today, key=lambda m: m["pct"])[:TOP_N]
    log(f"{len(today)} priced for {bar}; {down} down; worst: {losers[0]['sym']} {losers[0]['pct']}%")
    out = []
    for m in losers:
        if time.time() - t0 > 600:
            log("time budget reached; remaining names get prices only")
            out.append({**m, "f": {}, "news": [], "score": None, "verdict": None, "why": []})
            continue
        try:
            f, news = fundamentals(m["sym"])
        except Exception as e:  # noqa: BLE001
            log(f"{m['sym']} fundamentals failed: {type(e).__name__}: {e}")
            f, news = {}, []
        sc, verdict, why = score(f, m["price"]) if f else (None, None, [])
        out.append({**m, "name": f.get("name") or m["sym"], "sector": f.get("sector"), "f": f, "news": news,
                    "score": sc, "verdict": verdict, "why": why})
        time.sleep(0.4)
    write({
        "ok": True, "at": now, "bar": bar, "universe": {"size": len(symbols), "priced": len(today), "source": source},
        "market": {"spy": ctx.get("SPY", {}).get("pct"), "qqq": ctx.get("QQQ", {}).get("pct"),
                   "pctDown": round(down / len(today) * 100) if today else None},
        "rules": RULES, "losers": out, "log": LOG[-60:],
    })
    log(f"wrote {OUT}: {len(out)} losers in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        write({"ok": False, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "reason": f"{type(e).__name__}: {e}"[:200], "log": LOG[-60:]})
        sys.exit(1)
