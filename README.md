# Portfolio Analyzer

![Portfolio Analyzer dashboard illustration](assets/portfolio-dashboard.svg)

A standalone Python script that monitors a personal stock watchlist, collects market context from Yahoo Finance, asks Gemini what materially changed, and optionally emails a copy-ready daily report through the Gmail API.

The project does not read any portfolio CSV. The watchlist is created and maintained with `--add` and `--remove`.

## What It Does

- Validates tickers against Yahoo Finance before adding them.
- Allows NYSE and Nasdaq equities.
- Stores the watchlist locally in `portfolio_monitor_state/watchlist.json`.
- Collects price, volume, fundamentals, news, earnings dates, SEC filing, analyst, insider, valuation, technical, sector, and corporate action context.
- Detects explicit breaks above or below the 50-day moving average.
- Uses Gemini to summarize what changed without making buy/sell/hold recommendations.
- Sends a single copy-ready HTML email with light color emphasis for readability.

## Setup

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` in the project folder:

```env
GEMINI_API_KEY=your_gemini_api_key_here
SENDER_EMAIL=your-email@gmail.com
RECIPIENTS=first@example.com,second@example.com
```

`GEMINI_API_KEY` is required for AI analysis. If it is missing, the script falls back to deterministic summaries.

`SENDER_EMAIL` and `RECIPIENTS` are required only when using `--email`.

## Gmail API

Email sending uses the Gmail API, not SMTP.

You need two local files:

- `credentials.json`: OAuth desktop client downloaded from Google Cloud Console.
- `token.json`: created after running the one-time Gmail OAuth flow.

Run:

```bash
python3 authenticate_gmail.py
```

For the full setup flow, see [GMAIL_API_SETUP.md](/Users/oferg/work/mycode/portfolio_analyzer/GMAIL_API_SETUP.md).

These files are secrets and are ignored by Git:

- `.env`
- `credentials.json`
- `token.json`
- `client_secret*.json`

## Watchlist Commands

Add tickers:

```bash
python3 portfolio_monitor.py --add RTX --add SNDK
```

Remove a ticker:

```bash
python3 portfolio_monitor.py --remove RTX
```

List current tickers:

```bash
python3 portfolio_monitor.py --list
```

Adding the same ticker more than once is safe. The watchlist is saved without duplicates.

## Run The Monitor

Print a report in the terminal:

```bash
python3 portfolio_monitor.py
```

Send the report by email:

```bash
python3 portfolio_monitor.py --email
```

Run without Gemini:

```bash
python3 portfolio_monitor.py --skip-ai
```

Use a different Gemini model:

```bash
python3 portfolio_monitor.py --model gemini-3.7-flash
```

Change the one-day price move threshold:

```bash
python3 portfolio_monitor.py --price-threshold 4
```

## Report Format

The email is HTML, but the report body is intentionally one continuous text block so it can be copied into another AI chat.

The HTML version adds light visual emphasis:

- Ticker names and priority values are colored by urgency.
- Section headers are bold and colored.
- Positive percentages are green.
- Negative percentages are red.

The plain-text email body remains plain text.

## Detectors

The script currently collects these detector groups from Yahoo Finance / yfinance:

- `Analyst Revisions`: EPS revisions, EPS trends, recommendation summaries, price targets, upgrades, and downgrades.
- `Insider Transactions`: recent insider transaction and purchase data when Yahoo provides it.
- `SEC Filings`: recent SEC filing metadata exposed by Yahoo.
- `Valuation Changes`: valuation metrics and analyst target gap versus the latest close.
- `Technical Indicators`: 20 DMA, 50 DMA, 50 DMA breaks, RSI, volatility, and distance from yearly high/low.
- `Sector Performance`: 5-day relative move versus a sector ETF proxy.
- `Corporate Actions`: recent dividends and splits.
- `News Relevance`: lightweight headline classification by topic.

Yahoo coverage varies by ticker. Some detector groups may be empty for some symbols.

## Local State

Generated state lives under:

```text
portfolio_monitor_state/
```

It contains:

- `watchlist.json`: saved tickers.
- `snapshots/`: prior fundamental snapshots used for change detection.
- `raw/`: latest structured context per ticker.
- `reports/`: saved report payloads.

This directory is ignored by Git.

## Server Cron Example

Example cron for running at 14:00 Israel time, Sunday through Friday:

```cron
CRON_TZ=Asia/Jerusalem
0 14 * * 0-5 /root/scripts/portfolio_analyzer/.venv/bin/python /root/scripts/portfolio_analyzer/portfolio_monitor.py --email >> /root/scripts/portfolio_analyzer/cron.log 2>&1
```
