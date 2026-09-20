"""
TickerTruth Historical API — free-tier friendly (Render / Railway / Fly).
Fetches OHLCV via yfinance (server-side, no browser CORS issues).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tt-hist")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

_CACHE: dict = {}
CACHE_TTL_SEC = 30 * 60


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
    fetch = {
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
    return fetch, display


def fetch_ohlc(ticker: str, period: str) -> dict:
    yf_period, max_bars = _period_settings(period)
    cache_key = f"{ticker}|{yf_period}|{max_bars}"
    cached = _cache_get(cache_key)
    if cached:
        log.info("cache hit %s", cache_key)
        return cached

    log.info("yfinance history %s period=%s", ticker, yf_period)
    t0 = time.time()

    stock = yf.Ticker(ticker)
    df = stock.history(period=yf_period, interval="1d", auto_adjust=True, actions=False)

    if df is None or df.empty:
        log.warning("Ticker.history empty, retry download()")
        df = yf.download(
            ticker,
            period=yf_period,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )

    if df is None or df.empty:
        raise LookupError(f"No data for ticker '{ticker}'")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    cols = {c: str(c).strip().title() for c in df.columns}
    df = df.rename(columns=cols)

    for col in ("Open", "High", "Low", "Close"):
        if col not in df.columns:
            raise LookupError(f"Missing column {col} for {ticker}")

    df = df[["Open", "High", "Low", "Close"]].dropna()
    df.index = pd.to_datetime(df.index)
    try:
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)
    except Exception:
        pass
    df = df.sort_index()

    if len(df) < 15:
        raise LookupError(f"Not enough history for {ticker} ({len(df)} bars)")

    if max_bars and len(df) > max_bars:
        df = df.iloc[-max_bars:]

    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    payload = {
        "ticker": ticker,
        "period": period,
        "dates": dates,
        "opens": [round(float(x), 4) for x in df["Open"].tolist()],
        "highs": [round(float(x), 4) for x in df["High"].tolist()],
        "lows": [round(float(x), 4) for x in df["Low"].tolist()],
        "closes": [round(float(x), 4) for x in df["Close"].tolist()],
        "count": len(dates),
        "source": "yfinance",
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    _cache_set(cache_key, payload)
    log.info("ok %s bars=%s in %sms", ticker, payload["count"], payload["elapsed_ms"])
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
