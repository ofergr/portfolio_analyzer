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
import json
import math
import os
from datetime import datetime, timezone
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


def to_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [to_json_safe(v) for v in value]
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
    history = instrument.history(period="3mo", interval="1d", auto_adjust=False)

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
        "recent_news": extract_news_items(raw_news),
        "source": {
            "provider": "Yahoo Finance via yfinance",
            "history_period": "3mo",
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

Ignore normal market noise.

Context:
{json.dumps(context, indent=2)}
""".strip()

    response = client.models.generate_content(model=model, contents=prompt)
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

    lines.extend(
        [
            "=" * 72,
            "This report is meant to surface what changed, not make the decision for you.",
            "Review the underlying filings, earnings materials, and company-specific news before acting.",
        ]
    )
    return "\n".join(lines)


def build_html_report(report_payload: dict[str, Any]) -> str:
    def priority_colors(priority: str) -> tuple[str, str, str]:
        mapping = {
            "HIGH": ("#8a1c1c", "#fde8e8", "#ef4444"),
            "MEDIUM": ("#92400e", "#fff4db", "#f59e0b"),
            "LOW": ("#14532d", "#e8f7ee", "#22c55e"),
        }
        return mapping.get(priority.upper(), ("#334155", "#eef2f7", "#94a3b8"))

    sections: list[str] = []
    high_count = 0
    med_count = 0
    low_count = 0

    for ticker in report_payload["tickers"]:
        item = report_payload["items"][ticker]
        analysis = item["analysis"]
        context = item["context"]
        priority = str(analysis.get("priority", "UNKNOWN")).upper()
        text_color, badge_bg, _ = priority_colors(priority)

        if priority == "HIGH":
            high_count += 1
        elif priority == "MEDIUM":
            med_count += 1
        else:
            low_count += 1

        company_name = context.get("company_name") or ticker
        exchange = context.get("exchange") or "Unknown exchange"
        sector = context.get("sector") or "Unknown sector"
        earnings_date = context.get("earnings_date") or "Not available"
        price_event = context.get("price_event", {})
        close = price_event.get("close")
        one_day = price_event.get("change_pct_1d")
        five_day = price_event.get("change_pct_5d")
        volume_ratio = price_event.get("volume_vs_20d_avg")

        news_items = []
        for news in context.get("recent_news", [])[:5]:
            title = news.get("title")
            publisher = news.get("publisher") or "Unknown"
            if title:
                news_items.append(f"{title} ({publisher})")

        snapshot_lines = [
            f"Company: {company_name}",
            f"Exchange: {exchange}",
            f"Sector: {sector}",
            f"Close: {close if close is not None else 'n/a'}",
            f"1D Move: {format_pct(one_day)}",
            f"5D Move: {format_pct(five_day)}",
            f"20D Volume Ratio: {format_ratio(volume_ratio)}",
            f"Next Earnings: {earnings_date}",
        ]

        brief_lines = [
            "SNAPSHOT",
            *snapshot_lines,
            "",
            "WHAT CHANGED",
            *(f"- {line}" for line in analysis.get("what_changed", [])),
            "",
            "WHAT TO REVIEW",
            *(f"- {line}" for line in analysis.get("what_to_review", [])),
            "",
            "RECENT NEWS",
            *(f"- {line}" for line in news_items),
        ]
        brief_text = "\n".join(line for line in brief_lines if line is not None).strip()
        if not analysis.get("what_changed"):
            brief_text = brief_text.replace("WHAT CHANGED\n", "WHAT CHANGED\n- None\n")
        if not analysis.get("what_to_review"):
            brief_text = brief_text.replace("WHAT TO REVIEW\n", "WHAT TO REVIEW\n- None\n")
        if not news_items:
            brief_text = brief_text.replace("RECENT NEWS", "RECENT NEWS\n- None")

        sections.append(
            f"""
            <section style="margin:0 0 24px 0;padding:24px;border:1px solid #e2e8f0;border-radius:18px;background:#ffffff;
                            box-shadow:0 10px 30px rgba(15,23,42,0.06);">
              <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:16px;">
                <div>
                  <div style="font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#64748b;margin-bottom:8px;">{exchange}</div>
                  <h2 style="margin:0;font-size:28px;line-height:1.1;color:#0f172a;">{ticker}</h2>
                  <div style="margin-top:6px;font-size:16px;color:#475569;">{company_name}</div>
                </div>
                <div style="padding:8px 14px;border-radius:999px;background:{badge_bg};color:{text_color};
                            font-size:12px;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;">
                  {priority}
                </div>
              </div>

              <div style="margin:0 0 18px 0;padding:18px;border-radius:16px;background:#fffaf0;border:1px solid #fde68a;">
                <div style="font-size:12px;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;color:#92400e;margin-bottom:10px;">Summary</div>
                <p style="margin:0;color:#3f3f46;line-height:1.72;font-size:15px;">{analysis.get("why_it_matters", "")}</p>
              </div>

              <div style="font-size:12px;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;color:#475569;margin-bottom:8px;">AI-Ready Brief</div>
              <pre style="margin:0;padding:16px 18px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;
                          font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;
                          font-size:13px;line-height:1.62;white-space:pre-wrap;word-break:break-word;color:#0f172a;">{brief_text}</pre>
            </section>
            """
        )

    generated_at = report_payload.get("generated_at", "")
    total = len(report_payload["tickers"])
    plain_text_hint = (
        "The block under each ticker is intentionally formatted as plain text so you can copy it directly into an AI chat."
    )
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
            <p style="margin:0;font-size:13px;line-height:1.6;opacity:0.82;max-width:760px;">
              {plain_text_hint}
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
                <div style="margin-top:4px;font-size:15px;font-weight:700;">High {high_count} · Med {med_count} · Low {low_count}</div>
              </div>
            </div>
          </div>

          {''.join(sections)}

          <div style="margin-top:18px;padding:18px 22px;border-radius:18px;background:#ffffff;border:1px solid #e2e8f0;color:#64748b;font-size:13px;line-height:1.65;">
            This automated report is designed to surface what changed, not to make the investment decision for you.
            Review the underlying filings, earnings materials, and company-specific news before acting.
          </div>
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
