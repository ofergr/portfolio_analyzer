"""Standalone portfolio monitor using Yahoo Finance and Gemini.

This script keeps its own watchlist. It does not depend on this repository's
portfolio CSV files, so you can copy it to another machine and use it there.

Core workflow:
- `--add TICKER` validates the symbol on Yahoo Finance and only saves it if it
  appears to be listed on NYSE or Nasdaq.
- `--remove TICKER` removes it from the local watchlist.
- Normal runs fetch Yahoo Finance price/fundamental/news data for saved tickers,
  compare against prior snapshots, and ask Gemini to explain what actually
  changed.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from gmail_mailer import send_email_via_gmail_api


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = SCRIPT_DIR / "portfolio_monitor_state"
DEFAULT_MODEL = "gemini-3.7-flash"
WATCHLIST_FILE = "watchlist.json"
REPORTS_DIR = "reports"
SNAPSHOTS_DIR = "snapshots"
RAW_DIR = "raw"

ALLOWED_EXCHANGE_CODES = {
    "NYQ",
    "NYE",
    "NYS",
    "NYQM",
    "NMS",
    "NCM",
    "NGM",
}
ALLOWED_EXCHANGE_NAMES = {"NYSE", "NASDAQ"}
SNAPSHOT_KEYS = {
    "marketCap",
    "trailingPE",
    "forwardPE",
    "revenueGrowth",
    "earningsGrowth",
    "operatingMargins",
    "profitMargins",
    "ebitda",
    "totalRevenue",
    "targetMeanPrice",
    "recommendationKey",
}
SECTOR_ETFS = {
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}
NEWS_RELEVANCE_KEYWORDS = {
    "earnings": {"earnings", "revenue", "eps", "guidance", "profit", "quarter"},
    "analyst": {"analyst", "upgrade", "downgrade", "price target", "rating"},
    "legal_regulatory": {"lawsuit", "regulator", "sec", "doj", "fda", "probe", "investigation"},
    "corporate_action": {"dividend", "split", "buyback", "spinoff", "acquisition", "merger"},
    "macro_sector": {"tariff", "rates", "inflation", "fed", "sector", "industry", "china"},
}
DETECTOR_LABELS = {
    "analyst_revisions": "Analyst Revisions",
    "insider_transactions": "Insider Transactions",
    "sec_filings": "SEC Filings",
    "valuation_changes": "Valuation Changes",
    "technical_indicators": "Technical Indicators",
    "sector_performance": "Sector Performance",
    "corporate_actions": "Corporate Actions",
    "news_relevance": "News Relevance",
}

load_dotenv(SCRIPT_DIR / ".env")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper()


def ensure_state_dirs(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / REPORTS_DIR).mkdir(parents=True, exist_ok=True)
    (state_dir / SNAPSHOTS_DIR).mkdir(parents=True, exist_ok=True)
    (state_dir / RAW_DIR).mkdir(parents=True, exist_ok=True)


def watchlist_path(state_dir: Path) -> Path:
    return state_dir / WATCHLIST_FILE


def load_watchlist(state_dir: Path) -> list[str]:
    path = watchlist_path(state_dir)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    tickers = data.get("tickers", [])
    return sorted({normalize_ticker(t) for t in tickers if isinstance(t, str) and t.strip()})


def save_watchlist(state_dir: Path, tickers: list[str]) -> None:
    ensure_state_dirs(state_dir)
    payload = {
        "updated_at": utc_now_iso(),
        "tickers": sorted({normalize_ticker(t) for t in tickers}),
    }
    watchlist_path(state_dir).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return ((current - previous) / abs(previous)) * 100.0


def load_previous_snapshot(snapshot_path: Path) -> dict[str, Any] | None:
    if not snapshot_path.exists():
        return None
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def save_snapshot(snapshot_path: Path, snapshot: dict[str, Any]) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def extract_snapshot_from_info(info: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in SNAPSHOT_KEYS:
        value = info.get(key)
        if is_finite_number(value):
            snapshot[key] = float(value)
        elif isinstance(value, str) and value:
            snapshot[key] = value
        else:
            snapshot[key] = None
    return snapshot


def compare_snapshots(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    min_change_pct: float = 5.0,
) -> dict[str, float]:
    if not previous:
        return {}

    changes: dict[str, float] = {}
    for key, value in current.items():
        change = percent_change(to_float(value), to_float(previous.get(key)))
        if change is not None and abs(change) >= min_change_pct:
            changes[key] = round(change, 2)
    return changes


def extract_news_items(raw_news: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for article in raw_news[:limit]:
        title = str(article.get("title") or "").strip()
        if not title:
            continue
        publisher = (
            article.get("publisher")
            or article.get("provider")
            or article.get("source")
            or "Unknown"
        )
        items.append(
            {
                "title": title,
                "publisher": str(publisher),
                "published": article.get("providerPublishTime") or article.get("pubDate"),
                "link": article.get("link") or article.get("canonicalUrl"),
            }
        )
    return items


def compact_records(data: Any, limit: int = 5) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, pd.Series):
        data = data.to_frame().T
    if isinstance(data, pd.DataFrame):
        if data.empty:
            return []
        frame = data.reset_index()
        date_columns = [column for column in frame.columns if "date" in str(column).lower()]
        for column in date_columns:
            parsed_dates = pd.to_datetime(frame[column], errors="coerce", utc=True)
            if parsed_dates.notna().any():
                frame = frame.assign(_sort_date=parsed_dates).sort_values("_sort_date", ascending=False)
                frame = frame.drop(columns=["_sort_date"])
                break
        else:
            frame = frame.tail(limit)
        frame = frame.head(limit)
        return to_json_safe(frame.to_dict(orient="records"))
    if isinstance(data, list):
        records = [item for item in data if isinstance(item, dict)]
        return to_json_safe(records[:limit])
    if isinstance(data, dict):
        if isinstance(data.get("filings"), list):
            return compact_records(data["filings"], limit=limit)
        return to_json_safe([data])
    return []


def get_yahoo_property(instrument: yf.Ticker, name: str) -> Any:
    try:
        return getattr(instrument, name)
    except Exception:
        return None


def calculate_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes.index) <= period:
        return None
    delta = closes.diff()
    gains = delta.clip(lower=0).rolling(period).mean()
    losses = (-delta.clip(upper=0)).rolling(period).mean()
    latest_loss = to_float(losses.iloc[-1])
    latest_gain = to_float(gains.iloc[-1])
    if latest_gain is None or latest_loss is None:
        return None
    if latest_loss == 0:
        return 100.0
    rs = latest_gain / latest_loss
    return round(100 - (100 / (1 + rs)), 2)


def detect_technical_indicators(history: pd.DataFrame) -> dict[str, Any]:
    if history.empty or "Close" not in history.columns:
        return {"available": False, "signals": ["No price history available."]}

    closes = history["Close"].dropna()
    if closes.empty:
        return {"available": False, "signals": ["No close prices available."]}

    latest_close = to_float(closes.iloc[-1])
    sma_20 = to_float(closes.tail(20).mean()) if len(closes.index) >= 20 else None
    sma_50 = to_float(closes.tail(50).mean()) if len(closes.index) >= 50 else None
    previous_close = to_float(closes.iloc[-2]) if len(closes.index) >= 2 else None
    previous_sma_50 = to_float(closes.iloc[-51:-1].mean()) if len(closes.index) >= 51 else None
    dma_50_break = None
    high_52w = to_float(closes.max())
    low_52w = to_float(closes.min())
    rsi_14 = calculate_rsi(closes)
    daily_returns = closes.pct_change().dropna()
    volatility_20d = None
    if len(daily_returns.index) >= 20:
        volatility = to_float(daily_returns.tail(20).std())
        volatility_20d = round(volatility * math.sqrt(252) * 100, 2) if volatility is not None else None

    signals: list[str] = []
    if latest_close is not None and sma_20:
        signals.append(f"Close is {percent_change(latest_close, sma_20):+.2f}% versus the 20-day average.")
    if latest_close is not None and sma_50:
        if previous_close is not None and previous_sma_50 is not None:
            if previous_close <= previous_sma_50 and latest_close > sma_50:
                dma_50_break = "up"
                signals.append(f"Price broke above the 50 DMA at {sma_50:.2f}.")
            elif previous_close >= previous_sma_50 and latest_close < sma_50:
                dma_50_break = "down"
                signals.append(f"Price broke below the 50 DMA at {sma_50:.2f}.")
        signals.append(f"Close is {percent_change(latest_close, sma_50):+.2f}% versus the 50-day average.")
    if latest_close is not None and high_52w:
        signals.append(f"Close is {percent_change(latest_close, high_52w):+.2f}% from the 1-year high.")
    if rsi_14 is not None and (rsi_14 >= 70 or rsi_14 <= 30):
        signals.append(f"RSI-14 is {rsi_14}, an extended technical reading.")

    return {
        "available": True,
        "signals": signals[:5] or ["No notable technical signal."],
        "latest_close": round(latest_close, 2) if latest_close is not None else None,
        "sma_20": round(sma_20, 2) if sma_20 is not None else None,
        "sma_50": round(sma_50, 2) if sma_50 is not None else None,
        "previous_close": round(previous_close, 2) if previous_close is not None else None,
        "previous_sma_50": round(previous_sma_50, 2) if previous_sma_50 is not None else None,
        "dma_50_break": dma_50_break,
        "rsi_14": rsi_14,
        "volatility_20d_annualized_pct": volatility_20d,
        "distance_from_1y_high_pct": round(percent_change(latest_close, high_52w), 2)
        if latest_close is not None and high_52w
        else None,
        "distance_from_1y_low_pct": round(percent_change(latest_close, low_52w), 2)
        if latest_close is not None and low_52w
        else None,
    }


def detect_sector_performance(sector: str | None, history: pd.DataFrame) -> dict[str, Any]:
    etf = SECTOR_ETFS.get(sector or "")
    if not etf:
        return {"available": False, "signals": ["No sector ETF mapping available."], "sector": sector}

    try:
        etf_history = yf.Ticker(etf).history(period="1mo", interval="1d", auto_adjust=False)
    except Exception as exc:
        return {"available": False, "signals": [f"Sector ETF lookup failed: {exc}"], "sector": sector, "etf": etf}

    stock_closes = history["Close"].dropna() if "Close" in history.columns else pd.Series(dtype=float)
    etf_closes = etf_history["Close"].dropna() if "Close" in etf_history.columns else pd.Series(dtype=float)
    stock_5d = percent_change(to_float(stock_closes.iloc[-1]), to_float(stock_closes.iloc[-6])) if len(stock_closes.index) >= 6 else None
    etf_5d = percent_change(to_float(etf_closes.iloc[-1]), to_float(etf_closes.iloc[-6])) if len(etf_closes.index) >= 6 else None
    relative_5d = stock_5d - etf_5d if stock_5d is not None and etf_5d is not None else None

    signals = []
    if relative_5d is not None:
        signals.append(f"5-day move is {relative_5d:+.2f} percentage points versus {etf}.")

    return {
        "available": True,
        "sector": sector,
        "etf": etf,
        "stock_change_pct_5d": round(stock_5d, 2) if stock_5d is not None else None,
        "sector_etf_change_pct_5d": round(etf_5d, 2) if etf_5d is not None else None,
        "relative_change_pct_5d": round(relative_5d, 2) if relative_5d is not None else None,
        "signals": signals or ["No sector-relative signal."],
    }


def detect_analyst_activity(instrument: yf.Ticker) -> dict[str, Any]:
    revisions = compact_records(get_yahoo_property(instrument, "eps_revisions"), limit=4)
    trend = compact_records(get_yahoo_property(instrument, "eps_trend"), limit=4)
    recommendations = compact_records(get_yahoo_property(instrument, "recommendations_summary"), limit=4)
    upgrades = compact_records(get_yahoo_property(instrument, "upgrades_downgrades"), limit=5)
    price_targets = get_yahoo_property(instrument, "analyst_price_targets")

    signals: list[str] = []
    if revisions:
        for row in revisions[:2]:
            period = row.get("period", "unknown period")
            up_30 = row.get("upLast30days")
            down_30 = row.get("downLast30days")
            signals.append(f"EPS revisions for {period}: {up_30} up and {down_30} down in the last 30 days.")
    for row in trend[:2]:
        period = row.get("period", "unknown period")
        current = to_float(row.get("current"))
        days_30 = to_float(row.get("30daysAgo"))
        change = percent_change(current, days_30)
        if change is not None:
            signals.append(f"EPS trend for {period} changed {change:+.2f}% versus 30 days ago.")
    if upgrades:
        latest = upgrades[0]
        firm = latest.get("Firm", "Unknown firm")
        action = latest.get("priceTargetAction") or latest.get("Action") or "updated"
        date = latest.get("GradeDate", "unknown date")
        target = latest.get("currentPriceTarget")
        signals.append(f"Latest analyst action: {firm} {action} target to {target} on {date}.")
    if price_targets:
        signals.append("Analyst price target data is available.")

    return {
        "available": bool(revisions or trend or recommendations or upgrades or price_targets),
        "signals": signals or ["No analyst data available from Yahoo."],
        "eps_revisions": revisions,
        "eps_trend": trend,
        "recommendations_summary": recommendations,
        "upgrades_downgrades": upgrades,
        "price_targets": to_json_safe(price_targets),
    }


def detect_insider_activity(instrument: yf.Ticker) -> dict[str, Any]:
    transactions = compact_records(get_yahoo_property(instrument, "insider_transactions"), limit=5)
    purchases = compact_records(get_yahoo_property(instrument, "insider_purchases"), limit=5)
    roster = compact_records(get_yahoo_property(instrument, "insider_roster_holders"), limit=5)
    signals = []
    if transactions:
        latest = transactions[0]
        insider = latest.get("Insider", "Unknown insider")
        text = latest.get("Text") or latest.get("Transaction") or "transaction"
        date = latest.get("Start Date", "unknown date")
        signals.append(f"Latest insider record: {insider}, {text} on {date}.")
    if purchases:
        signals.append("Insider purchase summary is available.")

    return {
        "available": bool(transactions or purchases or roster),
        "signals": signals or ["No insider data available from Yahoo."],
        "transactions": transactions,
        "purchases": purchases,
        "roster": roster,
    }


def detect_sec_filings(instrument: yf.Ticker) -> dict[str, Any]:
    filings = compact_records(get_yahoo_property(instrument, "sec_filings"), limit=5)
    signals = []
    if filings:
        latest = filings[0]
        filing_type = latest.get("type", "filing")
        date = latest.get("date", "unknown date")
        title = latest.get("title", "SEC filing")
        signals.append(f"Latest SEC filing: {filing_type} on {date} ({title}).")
    else:
        signals.append("No SEC filing data available from Yahoo.")
    return {"available": bool(filings), "signals": signals, "filings": filings}


def detect_valuation(info: dict[str, Any], fundamental_changes: dict[str, float], price_event: dict[str, Any]) -> dict[str, Any]:
    close = price_event.get("close")
    target_mean = to_float(info.get("targetMeanPrice"))
    target_gap = percent_change(target_mean, to_float(close)) if target_mean is not None and close is not None else None
    metrics = {
        "market_cap": info.get("marketCap"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "price_to_sales": info.get("priceToSalesTrailing12Months"),
        "price_to_book": info.get("priceToBook"),
        "enterprise_to_ebitda": info.get("enterpriseToEbitda"),
        "target_mean_price": target_mean,
        "target_gap_pct": round(target_gap, 2) if target_gap is not None else None,
    }
    valuation_change_keys = [
        key for key in ("marketCap", "trailingPE", "forwardPE", "targetMeanPrice") if key in fundamental_changes
    ]
    signals = []
    if valuation_change_keys:
        signals.append("Valuation-related snapshot changed in: " + ", ".join(valuation_change_keys) + ".")
    if target_gap is not None:
        signals.append(f"Mean analyst target is {target_gap:+.2f}% versus latest close.")

    return {
        "available": any(value is not None for value in metrics.values()),
        "signals": signals or ["No notable valuation signal."],
        "metrics": to_json_safe(metrics),
    }


def detect_corporate_actions(instrument: yf.Ticker) -> dict[str, Any]:
    actions = get_yahoo_property(instrument, "actions")
    records: list[dict[str, Any]] = []
    if isinstance(actions, pd.DataFrame) and not actions.empty:
        frame = actions.reset_index()
        if "Date" in frame.columns:
            parsed_dates = pd.to_datetime(frame["Date"], errors="coerce", utc=True)
            cutoff = datetime.now(timezone.utc) - timedelta(days=370)
            frame = frame[parsed_dates >= cutoff]
        frame = frame.tail(10)
        records = to_json_safe(frame.to_dict(orient="records"))
    signals = []
    if records:
        latest = records[0]
        date = latest.get("Date", "unknown date")
        dividend = latest.get("Dividends")
        split = latest.get("Stock Splits")
        signals.append(f"Latest corporate action: dividend {dividend}, split {split} on {date}.")
    return {"available": bool(records), "signals": signals or ["No recent corporate actions found."], "actions": records}


def classify_news_relevance(news_items: list[dict[str, Any]]) -> dict[str, Any]:
    classified = []
    category_counts: dict[str, int] = {}
    for item in news_items:
        title = str(item.get("title") or "")
        title_lower = title.lower()
        categories = [
            category
            for category, keywords in NEWS_RELEVANCE_KEYWORDS.items()
            if any(keyword in title_lower for keyword in keywords)
        ]
        if not categories:
            categories = ["general"]
        for category in categories:
            category_counts[category] = category_counts.get(category, 0) + 1
        classified.append({**item, "relevance_categories": categories})

    signals = [f"{count} headline(s) tagged as {category}." for category, count in sorted(category_counts.items())]
    return {
        "available": bool(classified),
        "signals": signals or ["No recent news items to classify."],
        "classified_news": classified,
    }


def collect_detectors(
    instrument: yf.Ticker,
    info: dict[str, Any],
    history: pd.DataFrame,
    price_event: dict[str, Any],
    fundamental_changes: dict[str, float],
    recent_news: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "analyst_revisions": detect_analyst_activity(instrument),
        "insider_transactions": detect_insider_activity(instrument),
        "sec_filings": detect_sec_filings(instrument),
        "valuation_changes": detect_valuation(info, fundamental_changes, price_event),
        "technical_indicators": detect_technical_indicators(history),
        "sector_performance": detect_sector_performance(info.get("sector"), history),
        "corporate_actions": detect_corporate_actions(instrument),
        "news_relevance": classify_news_relevance(recent_news),
    }


def detector_signal_lines(detectors: dict[str, Any], limit: int = 12) -> list[str]:
    lines: list[str] = []
    grouped_lines: list[list[str]] = []
    for detector_name, detector in detectors.items():
        label = DETECTOR_LABELS.get(detector_name, detector_name.replace("_", " ").title())
        detector_lines = [
            f"{label}: {signal}"
            for signal in detector.get("signals", [])
            if signal
            and not str(signal).startswith("No ")
            and "data is available" not in str(signal)
            and "summary is available" not in str(signal)
        ]
        if detector_lines:
            grouped_lines.append(detector_lines)

    for detector_lines in grouped_lines:
        lines.append(detector_lines[0])
    for detector_lines in grouped_lines:
        lines.extend(detector_lines[1:])
    return lines[:limit]


def to_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [to_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            return value.isoformat()
        except Exception:
            pass
    return value


def detect_price_event(history: pd.DataFrame, threshold_pct: float) -> dict[str, Any]:
    if history.empty or len(history.index) < 2:
        return {"triggered": False, "reason": "Not enough price history"}

    closes = history["Close"].dropna()
    if len(closes.index) < 2:
        return {"triggered": False, "reason": "Missing close prices"}

    latest_close = to_float(closes.iloc[-1])
    previous_close = to_float(closes.iloc[-2])
    if latest_close is None or previous_close is None:
        return {"triggered": False, "reason": "Invalid close prices"}

    one_day_change = percent_change(latest_close, previous_close)
    five_day_change = None
    if len(closes.index) >= 6:
        five_day_change = percent_change(latest_close, to_float(closes.iloc[-6]))

    volume_ratio = None
    if "Volume" in history.columns:
        volumes = history["Volume"].dropna()
        if len(volumes.index) >= 2:
            latest_volume = to_float(volumes.iloc[-1])
            avg_20 = to_float(volumes.tail(20).mean())
            if latest_volume is not None and avg_20 not in (None, 0):
                volume_ratio = round(latest_volume / avg_20, 2)

    return {
        "triggered": one_day_change is not None and abs(one_day_change) >= threshold_pct,
        "date": history.index[-1].date().isoformat(),
        "close": round(latest_close, 2),
        "previous_close": round(previous_close, 2),
        "change_pct_1d": round(one_day_change, 2) if one_day_change is not None else None,
        "change_pct_5d": round(five_day_change, 2) if five_day_change is not None else None,
        "volume_vs_20d_avg": volume_ratio,
    }


def validate_allowed_ticker(ticker: str) -> tuple[bool, str, dict[str, Any] | None]:
    symbol = normalize_ticker(ticker)
    instrument = yf.Ticker(symbol)

    try:
        info = instrument.info or {}
    except Exception as exc:
        return False, f"Yahoo Finance lookup failed: {exc}", None

    try:
        history = instrument.history(period="5d", interval="1d", auto_adjust=False)
    except Exception as exc:
        return False, f"Yahoo Finance history lookup failed: {exc}", info

    exchange = str(info.get("exchange") or "").upper()
    full_exchange_name = str(info.get("fullExchangeName") or "").upper()
    quote_type = str(info.get("quoteType") or "").upper()
    long_name = str(info.get("longName") or info.get("shortName") or symbol)

    if history.empty and not info:
        return False, f"{symbol} did not resolve to a usable Yahoo Finance symbol.", None

    is_allowed_exchange = exchange in ALLOWED_EXCHANGE_CODES or any(
        name in full_exchange_name for name in ALLOWED_EXCHANGE_NAMES
    )
    if not is_allowed_exchange:
        detail = full_exchange_name or exchange or "unknown exchange"
        return False, f"{symbol} resolved, but Yahoo reports it as {detail}, not NYSE or Nasdaq.", info

    if quote_type not in {"EQUITY", ""}:
        return False, f"{symbol} is listed on an allowed exchange but its Yahoo quote type is {quote_type}, not an equity.", info

    return True, f"{symbol} validated on an allowed exchange ({long_name}).", info


def fetch_yahoo_context(ticker: str, state_dir: Path, price_threshold_pct: float) -> dict[str, Any]:
    instrument = yf.Ticker(ticker)
    history = instrument.history(period="1y", interval="1d", auto_adjust=False)

    try:
        info = instrument.info or {}
    except Exception:
        info = {}

    try:
        raw_news = instrument.news or []
    except Exception:
        raw_news = []

    earnings_date = None
    try:
        calendar = instrument.calendar
        if hasattr(calendar, "to_dict"):
            calendar_dict = calendar.to_dict()
            earnings_date = calendar_dict.get("Earnings Date")
        elif isinstance(calendar, dict):
            earnings_date = calendar.get("Earnings Date")
    except Exception:
        earnings_date = None

    snapshot_path = state_dir / SNAPSHOTS_DIR / f"{ticker}.json"
    previous_snapshot = load_previous_snapshot(snapshot_path)
    fundamentals = extract_snapshot_from_info(info)
    fundamental_changes = compare_snapshots(fundamentals, previous_snapshot)
    price_event = detect_price_event(history, price_threshold_pct)
    recent_news = extract_news_items(raw_news)
    detectors = collect_detectors(
        instrument=instrument,
        info=info,
        history=history,
        price_event=price_event,
        fundamental_changes=fundamental_changes,
        recent_news=recent_news,
    )

    context = {
        "ticker": ticker,
        "as_of": utc_now_iso(),
        "company_name": info.get("longName") or info.get("shortName") or ticker,
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "price_event": price_event,
        "fundamentals": fundamentals,
        "fundamental_changes": fundamental_changes,
        "earnings_date": earnings_date,
        "recent_news": recent_news,
        "detectors": detectors,
        "detector_signals": detector_signal_lines(detectors),
        "source": {
            "provider": "Yahoo Finance via yfinance",
            "history_period": "1y",
        },
    }
    context = to_json_safe(context)

    (state_dir / RAW_DIR / f"{ticker}.json").write_text(
        json.dumps(context, indent=2) + "\n",
        encoding="utf-8",
    )
    save_snapshot(snapshot_path, fundamentals)
    return context


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Gemini response did not contain a JSON object.")
    return json.loads(cleaned[start : end + 1])


def default_analysis(context: dict[str, Any]) -> dict[str, Any]:
    price_event = context.get("price_event", {})
    fundamental_changes = context.get("fundamental_changes", {})
    news_count = len(context.get("recent_news", []))
    detector_signals = context.get("detector_signals", [])

    priority = "LOW"
    changes: list[str] = []
    review: list[str] = []

    one_day_move = price_event.get("change_pct_1d")
    if price_event.get("triggered") and one_day_move is not None:
        priority = "MEDIUM"
        changes.append(f"Price moved {one_day_move:+.2f}% in one day.")
        review.append("Check the latest session price action and volume.")

    if fundamental_changes:
        priority = "HIGH" if priority == "MEDIUM" else "MEDIUM"
        changes.append(
            "Fundamental snapshot changed materially in: "
            + ", ".join(sorted(fundamental_changes.keys())[:4])
            + "."
        )
        review.append("Compare today’s fundamentals with the prior saved snapshot.")

    if news_count:
        changes.append(f"Collected {news_count} recent Yahoo Finance news items.")
        review.append("Scan the top headlines for company-specific catalysts.")

    if detector_signals:
        changes.append("Additional detector signals: " + " ".join(detector_signals[:2]))
        review.append("Review the detector signals before deciding whether this is company-specific.")

    if not changes:
        changes.append("No material change crossed the current deterministic thresholds.")
        review.append("No immediate review required.")

    return {
        "priority": priority,
        "what_changed": changes[:3],
        "why_it_matters": (
            "This is a deterministic fallback summary because Gemini analysis was skipped "
            "or unavailable."
        ),
        "what_to_review": review[:2],
    }


def analyze_with_gemini(context: dict[str, Any], api_key: str, model: str) -> dict[str, Any]:
    from google import genai

    client = genai.Client(api_key=api_key)
    prompt = f"""
You are monitoring a long-term equity watchlist.
Do NOT recommend BUY, SELL, or HOLD.
Do NOT predict future prices.

Your only job is to decide whether something materially changed that deserves attention.

Analyze the structured JSON below and respond with JSON only in this exact schema:
{{
  "priority": "LOW|MEDIUM|HIGH",
  "what_changed": ["bullet 1", "bullet 2", "bullet 3"],
  "why_it_matters": "2-3 sentences max.",
  "what_to_review": ["bullet 1", "bullet 2"]
}}

Focus on:
- unusual price moves
- earnings timing if relevant
- material fundamental shifts versus the prior snapshot
- meaningful company-specific news
- analyst revisions, recommendations, and price target changes
- insider transactions
- SEC filings
- valuation changes
- technical indicators
- explicit breaks above or below the 50-day moving average / 50 DMA
- sector-relative performance
- corporate actions such as dividends or splits
- news relevance categories

Ignore normal market noise.

Context:
{json.dumps(context, indent=2)}
""".strip()

    chat = client.chats.create(model=model)
    response = chat.send_message(prompt)
    return extract_json_object(response.text or "")


def format_report_section(ticker: str, analysis: dict[str, Any]) -> str:
    lines = [f"## {ticker}", f"Priority: {analysis.get('priority', 'UNKNOWN')}"]

    changes = analysis.get("what_changed", [])
    if changes:
        lines.append("What Changed:")
        lines.extend(f"- {item}" for item in changes)

    why = analysis.get("why_it_matters")
    if why:
        lines.append(f"Why It Matters: {why}")

    review = analysis.get("what_to_review", [])
    if review:
        lines.append("What To Review:")
        lines.extend(f"- {item}" for item in review)

    return "\n".join(lines)


def format_pct(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:+.2f}%"
    return "n/a"


def format_ratio(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.2f}x"
    return "n/a"


def build_plain_text_report(report_payload: dict[str, Any]) -> str:
    lines = [
        "PORTFOLIO MONITOR",
        "Daily Attention Report",
        f"Generated: {report_payload.get('generated_at', '')}",
        "",
    ]

    for ticker in report_payload["tickers"]:
        item = report_payload["items"][ticker]
        analysis = item["analysis"]
        context = item["context"]
        price_event = context.get("price_event", {})

        news_titles = []
        for news in context.get("recent_news", [])[:5]:
            title = news.get("title")
            publisher = news.get("publisher") or "Unknown"
            if title:
                news_titles.append(f"- {title} ({publisher})")

        lines.extend(
            [
                "=" * 72,
                f"{ticker} | {context.get('company_name') or ticker}",
                f"Priority: {analysis.get('priority', 'UNKNOWN')}",
                "",
                f"Summary: {analysis.get('why_it_matters', '')}",
                "",
                "Snapshot:",
                f"- Exchange: {context.get('exchange') or 'Unknown exchange'}",
                f"- Sector: {context.get('sector') or 'Unknown sector'}",
                f"- Close: {price_event.get('close') if price_event.get('close') is not None else 'n/a'}",
                f"- 1D Move: {format_pct(price_event.get('change_pct_1d'))}",
                f"- 5D Move: {format_pct(price_event.get('change_pct_5d'))}",
                f"- 20D Volume Ratio: {format_ratio(price_event.get('volume_vs_20d_avg'))}",
                f"- Next Earnings: {context.get('earnings_date') or 'Not available'}",
                "",
                "Detector Signals:",
            ]
        )

        detector_signals = context.get("detector_signals", [])
        if detector_signals:
            lines.extend(f"- {entry}" for entry in detector_signals)
        else:
            lines.append("- None")

        lines.extend(
            [
                "",
                "What Changed:",
            ]
        )

        if analysis.get("what_changed"):
            lines.extend(f"- {entry}" for entry in analysis["what_changed"])
        else:
            lines.append("- None")

        lines.append("")
        lines.append("What To Review:")
        if analysis.get("what_to_review"):
            lines.extend(f"- {entry}" for entry in analysis["what_to_review"])
        else:
            lines.append("- None")

        lines.append("")
        lines.append("Recent News:")
        if news_titles:
            lines.extend(news_titles)
        else:
            lines.append("- None")
        lines.append("")

    return "\n".join(lines)


def render_colored_text_report(text_report: str) -> str:
    priority_colors = {
        "HIGH": "#b91c1c",
        "MEDIUM": "#b45309",
        "LOW": "#15803d",
    }
    section_headers = {
        "Snapshot:",
        "Detector Signals:",
        "What Changed:",
        "What To Review:",
        "Recent News:",
    }

    def highlight_numbers(escaped_line: str) -> str:
        def replace_match(match: re.Match[str]) -> str:
            value = match.group(0)
            color = "#15803d" if value.startswith("+") else "#b91c1c"
            return f"<span style=\"color:{color};font-weight:700;\">{value}</span>"

        return re.sub(r"(?<!\w)[+-]\d+(?:\.\d+)?%", replace_match, escaped_line)

    html_lines: list[str] = []
    lines = text_report.splitlines()
    for index, line in enumerate(lines):
        if set(line) == {"="}:
            html_lines.append(f"<span style=\"color:#94a3b8;\">{html.escape(line)}</span>")
        elif line == "PORTFOLIO MONITOR":
            html_lines.append(
                f"<span style=\"color:#1d4ed8;font-weight:900;\">{html.escape(line)}</span>"
            )
        elif line == "Daily Attention Report":
            html_lines.append(
                f"<span style=\"color:#475569;font-weight:700;\">{html.escape(line)}</span>"
            )
        elif line in section_headers:
            html_lines.append(
                f"<span style=\"color:#1e40af;font-weight:900;\">{html.escape(line)}</span>"
            )
        elif line.startswith("Priority: "):
            label, value = line.split(": ", 1)
            color = priority_colors.get(value.upper(), "#334155")
            html_lines.append(
                f"{html.escape(label)}: "
                f"<span style=\"color:{color};font-weight:800;\">{html.escape(value)}</span>"
            )
        elif " | " in line and not line.startswith("- "):
            ticker, company = line.split(" | ", 1)
            next_priority = lines[index + 1] if index + 1 < len(lines) else ""
            priority = next_priority.removeprefix("Priority: ") if next_priority.startswith("Priority: ") else ""
            color = priority_colors.get(priority.upper(), "#0f172a")
            html_lines.append(
                f"<span style=\"color:{color};font-weight:800;\">{html.escape(ticker)}</span>"
                f" | {html.escape(company)}"
            )
        elif line.startswith("Summary: "):
            label, value = line.split(": ", 1)
            html_lines.append(
                f"<span style=\"color:#334155;font-weight:800;\">{html.escape(label)}:</span> "
                f"{highlight_numbers(html.escape(value))}"
            )
        elif line.startswith("- ") and ": " in line:
            label, value = line[2:].split(": ", 1)
            escaped_value = highlight_numbers(html.escape(value))
            html_lines.append(
                f"- <span style=\"color:#475569;font-weight:800;\">{html.escape(label)}:</span> "
                f"{escaped_value}"
            )
        elif line.startswith("- "):
            html_lines.append(highlight_numbers(html.escape(line)))
        else:
            html_lines.append(highlight_numbers(html.escape(line)))
    return "\n".join(html_lines)


def build_html_report(report_payload: dict[str, Any]) -> str:
    high_count = 0
    med_count = 0
    low_count = 0

    for ticker in report_payload["tickers"]:
        item = report_payload["items"][ticker]
        analysis = item["analysis"]
        priority = str(analysis.get("priority", "UNKNOWN")).upper()

        if priority == "HIGH":
            high_count += 1
        elif priority == "MEDIUM":
            med_count += 1
        else:
            low_count += 1

    generated_at = report_payload.get("generated_at", "")
    total = len(report_payload["tickers"])
    colored_text_report = render_colored_text_report(build_plain_text_report(report_payload))
    return f"""
    <html>
      <body style="margin:0;padding:32px 18px;background:#eef4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#0f172a;">
        <div style="max-width:980px;margin:0 auto;">
          <div style="margin-bottom:24px;padding:28px 30px;border-radius:24px;background:linear-gradient(135deg,#0f172a 0%,#1d4ed8 100%);color:#ffffff;
                      box-shadow:0 20px 45px rgba(15,23,42,0.22);">
            <div style="font-size:12px;letter-spacing:0.16em;text-transform:uppercase;opacity:0.78;">Portfolio Monitor</div>
            <h1 style="margin:10px 0 10px 0;font-size:34px;line-height:1.05;">Daily Attention Report</h1>
            <p style="margin:0 0 18px 0;font-size:16px;line-height:1.6;opacity:0.9;max-width:760px;">
              A focused summary of which holdings deserve attention, why they matter, and what to review next.
            </p>
            <div style="display:flex;gap:12px;flex-wrap:wrap;">
              <div style="padding:10px 14px;border-radius:14px;background:rgba(255,255,255,0.12);">
                <div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.75;">Generated</div>
                <div style="margin-top:4px;font-size:15px;font-weight:700;">{generated_at}</div>
              </div>
              <div style="padding:10px 14px;border-radius:14px;background:rgba(255,255,255,0.12);">
                <div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.75;">Tracked</div>
                <div style="margin-top:4px;font-size:15px;font-weight:700;">{total} ticker{'s' if total != 1 else ''}</div>
              </div>
              <div style="padding:10px 14px;border-radius:14px;background:rgba(255,255,255,0.12);">
                <div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.75;">Priority Mix</div>
                <div style="margin-top:4px;font-size:15px;font-weight:700;">High {high_count}, Med {med_count}, Low {low_count}</div>
              </div>
            </div>
          </div>

          <pre style="margin:0;padding:22px 24px;background:#ffffff;border:1px solid #dbe3ea;border-radius:14px;
                      font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;
                      font-size:14px;line-height:1.62;white-space:pre-wrap;word-break:break-word;color:#0f172a;">{colored_text_report}</pre>

        </div>
      </body>
    </html>
    """.strip()


def update_watchlist(state_dir: Path, add: list[str], remove: list[str]) -> int:
    ensure_state_dirs(state_dir)
    tickers = set(load_watchlist(state_dir))

    for ticker in add:
        symbol = normalize_ticker(ticker)
        ok, message, _ = validate_allowed_ticker(symbol)
        print(message)
        if ok:
            tickers.add(symbol)

    for ticker in remove:
        tickers.discard(normalize_ticker(ticker))

    save_watchlist(state_dir, sorted(tickers))
    print(f"Current watchlist: {', '.join(sorted(tickers)) if tickers else '(empty)'}")
    return 0


def print_watchlist(state_dir: Path) -> int:
    tickers = load_watchlist(state_dir)
    if not tickers:
        print("Watchlist is empty.")
        return 0
    for ticker in tickers:
        print(ticker)
    return 0


def run_monitor(state_dir: Path, model: str, price_threshold_pct: float, skip_ai: bool, send_email: bool) -> int:
    ensure_state_dirs(state_dir)
    tickers = load_watchlist(state_dir)
    if not tickers:
        print("Watchlist is empty. Add tickers first with --add.")
        return 1

    api_key = os.getenv("GEMINI_API_KEY")
    report_payload: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "tickers": tickers,
        "items": {},
    }
    rendered_sections: list[str] = []

    for ticker in tickers:
        context = fetch_yahoo_context(ticker, state_dir, price_threshold_pct)
        if skip_ai or not api_key:
            analysis = default_analysis(context)
        else:
            try:
                analysis = analyze_with_gemini(context, api_key, model)
            except Exception as exc:
                analysis = default_analysis(context)
                analysis["why_it_matters"] += f" Gemini error: {exc}"

        report_payload["items"][ticker] = {
            "context": context,
            "analysis": analysis,
        }
        rendered_sections.append(format_report_section(ticker, analysis))

    report_path = state_dir / REPORTS_DIR / f"portfolio-monitor-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    report_path.write_text(json.dumps(report_payload, indent=2) + "\n", encoding="utf-8")

    print("\n\n".join(rendered_sections))
    print(f"\nSaved report: {report_path}")
    if not api_key and not skip_ai:
        print("Gemini was not used because GEMINI_API_KEY is not set.")

    if send_email:
        html_report = build_html_report(report_payload)
        plain_text_report = build_plain_text_report(report_payload)
        subject = f"Portfolio Monitor Report - {datetime.now().strftime('%Y-%m-%d')}"
        result = send_email_via_gmail_api(
            subject=subject,
            html_content=html_report,
            plain_text=plain_text_report,
        )
        print(f"Email sent to {result['count']} recipient(s): {', '.join(result['recipients'])}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone portfolio monitor using Yahoo Finance and Gemini")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="Directory for watchlist, snapshots, and reports")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model name")
    parser.add_argument("--price-threshold", type=float, default=5.0, help="One-day move threshold that counts as unusual")
    parser.add_argument("--skip-ai", action="store_true", help="Run deterministic change detection without Gemini")
    parser.add_argument("--email", action="store_true", help="Send the generated report via Gmail API")
    parser.add_argument("--add", action="append", default=[], help="Add a NYSE or Nasdaq ticker to the local watchlist")
    parser.add_argument("--remove", action="append", default=[], help="Remove a ticker from the local watchlist")
    parser.add_argument("--list", action="store_true", help="Print the current watchlist and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir)

    if args.add or args.remove:
        return update_watchlist(state_dir, args.add, args.remove)

    if args.list:
        return print_watchlist(state_dir)

    return run_monitor(
        state_dir=state_dir,
        model=args.model,
        price_threshold_pct=args.price_threshold,
        skip_ai=args.skip_ai,
        send_email=args.email,
    )


if __name__ == "__main__":
    raise SystemExit(main())
