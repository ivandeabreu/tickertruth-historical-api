"""
TickerTruth Historical API — free-tier friendly (Render / Railway / Fly).
Primary: Yahoo chart API with browser-like HTTP.
Fallback: Stooq daily CSV (server-side).
Optional: FINNHUB_API_KEY env var.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tt-hist")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

_CACHE: dict = {}
CACHE_TTL_SEC = 45 * 60  # 45 min — reduce Yahoo hits
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _cache_get(key: str):
    item = _CACHE.get(key)
    if not item:
        return None
    exp, payload = item
    if time.time() > exp:
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: str, payload: dict):
    _CACHE[key] = (time.time() + CACHE_TTL_SEC, payload)


def _normalize_ticker(raw: str) -> str:
    t = (raw or "").strip().upper()
    t = "".join(ch for ch in t if ch.isalnum() or ch in ".-")
    if not t or len(t) > 12:
        raise ValueError("Invalid ticker")
    return t


def _period_settings(period: str) -> tuple[str, int]:
    p = (period or "1y").lower().strip()
    # Yahoo range param
    yrange = {
        "3mo": "1y",
        "6mo": "2y",
        "1y": "2y",
        "3y": "5y",
        "5y": "5y",
    }.get(p, "2y")
    display = {
        "3mo": 70,
        "6mo": 140,
        "1y": 270,
        "3y": 800,
        "5y": 1300,
    }.get(p, 270)
    return yrange, display


def _http_get(url: str, timeout: int = 45) -> bytes:
    """GET with browser UA; prefer curl_cffi if available."""
    try:
        from curl_cffi import requests as creq

        r = creq.get(url, impersonate="chrome", timeout=timeout)
        if r.status_code == 429:
            raise RuntimeError("Rate limited (429)")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.content
    except ImportError:
        pass
    except Exception as e:
        log.warning("curl_cffi get failed: %s", e)

    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _from_yahoo_chart(ticker: str, yrange: str) -> dict:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
        f"?interval=1d&range={quote(yrange)}"
    )
    raw = _http_get(url)
    import json

    j = json.loads(raw.decode("utf-8", errors="replace"))
    res = (j.get("chart") or {}).get("result")
    if not res:
        err = (j.get("chart") or {}).get("error") or {}
        raise LookupError(err.get("description") or "Yahoo returned no result")
    res = res[0]
    ts = res.get("timestamp") or []
    quote_block = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_block.get("close") or []
    highs = quote_block.get("high") or []
    lows = quote_block.get("low") or []
    opens = quote_block.get("open") or []

    dates, c, h, l, o = [], [], [], [], []
    for i, t in enumerate(ts):
        if i >= len(closes) or closes[i] is None:
            continue
        try:
            cv = float(closes[i])
        except (TypeError, ValueError):
            continue
        dates.append(datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"))
        c.append(round(cv, 4))
        try:
            h.append(round(float(highs[i]), 4) if highs[i] is not None else round(cv, 4))
        except (TypeError, ValueError, IndexError):
            h.append(round(cv, 4))
        try:
            l.append(round(float(lows[i]), 4) if lows[i] is not None else round(cv, 4))
        except (TypeError, ValueError, IndexError):
            l.append(round(cv, 4))
        try:
            o.append(round(float(opens[i]), 4) if opens[i] is not None else round(cv, 4))
        except (TypeError, ValueError, IndexError):
            o.append(round(cv, 4))

    if len(c) < 15:
        raise LookupError(f"Yahoo: not enough bars for {ticker}")
    return {"dates": dates, "closes": c, "highs": h, "lows": l, "opens": o, "source": "yahoo-chart"}


def _from_stooq(ticker: str) -> dict:
    # US symbols on Stooq: ticker.us
    base = ticker.replace(".", "-").lower()
    candidates = [f"{base}.us", base]
    last_err = None
    for sym in candidates:
        url = f"https://stooq.com/q/d/l/?s={quote(sym)}&i=d"
        try:
            raw = _http_get(url, timeout=30)
            text = raw.decode("utf-8", errors="replace")
            if "Date" not in text or "<html" in text.lower():
                last_err = RuntimeError("Stooq blocked or empty")
                continue
            reader = csv.DictReader(io.StringIO(text))
            dates, c, h, l, o = [], [], [], [], []
            for row in reader:
                try:
                    d = (row.get("Date") or "").strip()
                    close = float(row["Close"])
                    high = float(row.get("High") or close)
                    low = float(row.get("Low") or close)
                    opn = float(row.get("Open") or close)
                except (KeyError, TypeError, ValueError):
                    continue
                if not d:
                    continue
                dates.append(d)
                c.append(round(close, 4))
                h.append(round(high, 4))
                l.append(round(low, 4))
                o.append(round(opn, 4))
            if len(c) >= 15:
                return {
                    "dates": dates,
                    "closes": c,
                    "highs": h,
                    "lows": l,
                    "opens": o,
                    "source": f"stooq:{sym}",
                }
            last_err = LookupError("Stooq short history")
        except Exception as e:
            last_err = e
            log.warning("stooq %s failed: %s", sym, e)
    raise LookupError(f"Stooq failed ({last_err})")


def _from_finnhub(ticker: str, period: str) -> dict:
    token = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not token:
        raise RuntimeError("FINNHUB_API_KEY not set")
    now = int(time.time())
    span = {
        "3mo": 86400 * 500,
        "6mo": 86400 * 600,
        "1y": 86400 * 800,
        "3y": 86400 * 1200,
        "5y": 86400 * 2000,
    }.get(period, 86400 * 800)
    url = (
        f"https://finnhub.io/api/v1/stock/candle?symbol={quote(ticker)}"
        f"&resolution=D&from={now - span}&to={now}&token={quote(token)}"
    )
    import json

    raw = _http_get(url, timeout=40)
    j = json.loads(raw.decode("utf-8", errors="replace"))
    if j.get("s") == "no_data":
        raise LookupError(f"Finnhub: no data for {ticker}")
    if j.get("s") and j.get("s") != "ok":
        raise LookupError(f"Finnhub status: {j.get('s')}")
    ts = j.get("t") or []
    closes = j.get("c") or []
    highs = j.get("h") or []
    lows = j.get("l") or []
    opens = j.get("o") or []
    dates, c, h, l, o = [], [], [], [], []
    for i, t in enumerate(ts):
        if i >= len(closes) or closes[i] is None:
            continue
        cv = float(closes[i])
        dates.append(datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"))
        c.append(round(cv, 4))
        h.append(round(float(highs[i]), 4) if i < len(highs) and highs[i] is not None else round(cv, 4))
        l.append(round(float(lows[i]), 4) if i < len(lows) and lows[i] is not None else round(cv, 4))
        o.append(round(float(opens[i]), 4) if i < len(opens) and opens[i] is not None else round(cv, 4))
    if len(c) < 15:
        raise LookupError("Finnhub: not enough bars")
    return {"dates": dates, "closes": c, "highs": h, "lows": l, "opens": o, "source": "finnhub"}


def fetch_ohlc(ticker: str, period: str) -> dict:
    yrange, max_bars = _period_settings(period)
    cache_key = f"{ticker}|{period}|{max_bars}"
    cached = _cache_get(cache_key)
    if cached:
        log.info("cache hit %s", cache_key)
        return cached

    t0 = time.time()
    series = None
    errors = []

    # 1) Yahoo chart API (no yfinance library — fewer rate-limit patterns)
    try:
        series = _from_yahoo_chart(ticker, yrange)
        log.info("yahoo ok %s", ticker)
    except Exception as e:
        errors.append(f"yahoo:{e}")
        log.warning("yahoo failed: %s", e)
        time.sleep(0.8)

    # 2) Stooq
    if series is None:
        try:
            series = _from_stooq(ticker)
            log.info("stooq ok %s", ticker)
        except Exception as e:
            errors.append(f"stooq:{e}")
            log.warning("stooq failed: %s", e)

    # 3) Finnhub if env key set
    if series is None:
        try:
            series = _from_finnhub(ticker, period)
            log.info("finnhub ok %s", ticker)
        except Exception as e:
            errors.append(f"finnhub:{e}")
            log.warning("finnhub failed: %s", e)

    if series is None:
        raise LookupError(
            "All price sources failed. Wait 1–2 minutes and retry. Details: "
            + " | ".join(errors[:3])
        )

    # Trim to display window (keep end of series)
    n = len(series["closes"])
    if max_bars and n > max_bars:
        cut = n - max_bars
        for k in ("dates", "closes", "highs", "lows", "opens"):
            if k in series:
                series[k] = series[k][cut:]

    payload = {
        "ticker": ticker,
        "period": period,
        "dates": series["dates"],
        "opens": series.get("opens") or series["closes"],
        "highs": series["highs"],
        "lows": series["lows"],
        "closes": series["closes"],
        "count": len(series["closes"]),
        "source": series.get("source", "unknown"),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    _cache_set(cache_key, payload)
    return payload


@app.get("/")
def root():
    return jsonify(
        {
            "service": "tickertruth-historical-api",
            "status": "ok",
            "endpoints": {
                "health": "GET /health",
                "history": "GET /history?ticker=MU&period=1y",
            },
            "periods": ["3mo", "6mo", "1y", "3y", "5y"],
        }
    )


@app.get("/health")
def health():
    return jsonify({"status": "healthy", "ts": datetime.now(timezone.utc).isoformat()})


@app.get("/history")
def history():
    try:
        ticker = _normalize_ticker(request.args.get("ticker", ""))
        period = (request.args.get("period") or "1y").lower().strip()
        if period not in {"3mo", "6mo", "1y", "3y", "5y"}:
            return jsonify({"error": "Invalid period. Use 3mo,6mo,1y,3y,5y"}), 400
        data = fetch_ohlc(ticker, period)
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        log.exception("history failed")
        return jsonify({"error": f"Server error: {type(e).__name__}: {e}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
