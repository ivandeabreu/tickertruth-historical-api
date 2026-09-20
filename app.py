"""
TickerTruth Historical API — free-tier friendly (Render / Railway / Fly).
Fetches OHLCV via yfinance (server-side, no browser CORS issues).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tt-hist")

app = Flask(__name__)
# Allow TickerTruth hosting origins (and localhost for dev)
CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    supports_credentials=False,
)

# Simple in-memory cache: key -> (expires_ts, payload)
_CACHE: dict = {}
CACHE_TTL_SEC = 30 * 60  # 30 minutes


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


def _period_to_yfinance(period: str) -> tuple[str, int | None]:
    """
    Returns (yfinance_period, max_bars_to_return).
    Fetch extra history so client EMAs (up to 250) can warm up; trim for display.
    """
    p = (period or "1y").lower().strip()
    # display bars approx trading days
    display = {
        "3mo": 70,
        "6mo": 140,
        "1y": 270,
        "3y": 800,
        "5y": 1400,
    }.get(p, 270)
    # yfinance allowed periods — use longer fetch for EMA warmup
    fetch = {
        "3mo": "2y",
        "6mo": "2y",
        "1y": "5y",
        "3y": "5y",
        "5y": "max",
    }.get(p, "5y")
    return fetch, display


def fetch_ohlc(ticker: str, period: str) -> dict:
    yf_period, max_bars = _period_to_yfinance(period)
    cache_key = f"{ticker}|{yf_period}|{max_bars}"
    cached = _cache_get(cache_key)
    if cached:
        log.info("cache hit %s", cache_key)
        return cached

    log.info("yfinance download %s period=%s", ticker, yf_period)
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

    need = ["Open", "High", "Low", "Close"]
    for col in need:
        if col not in df.columns:
            raise LookupError(f"Missing column {col} for {ticker}")

    df = df[need].dropna()
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    if len(df) < 15:
        raise LookupError(f"Not enough history for {ticker}")

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
            return jsonify({"error": "Invalid period"}), 400
        data = fetch_ohlc(ticker, period)
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        log.exception("history failed")
        return jsonify({"error": f"Server error: {e}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
