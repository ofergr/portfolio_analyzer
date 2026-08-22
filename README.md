# Start Your Own

This folder lets you run the trading experiment on your own computer. It contains two small scripts and the CSV files they produce.

Run the commands below from the repository root. The scripts automatically
save their CSV data inside this folder.

## Overview

 **Install dependencies:**
   ```bash
   # Recommended: Use a virtual environment
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   
   pip install -r requirements.txt
   ```

**Processing Portfolio:**
   ```bash
   # ALWAYS include a CSV file of history

   python trading_script.py --data-dir "Start Your Own"
   ```

**To Save Prior Days:**
   ```bash

   # Save data with specific date
   python trading_script.py --asof 2025-08-27 --data-dir "Start Your Own"
   ```

**Generate performance graphs:**
   ```bash
   python "Start Your Own/Generate_Graph.py"
   ```

**Standalone monitor with Yahoo + Gemini:**
   ```bash
   pip install -r requirements.txt

   # Put your keys/settings in .env
   # GEMINI_API_KEY=your_key_here
   # SENDER_EMAIL=your-email@gmail.com
   # RECIPIENTS=your-email@gmail.com

   # Add NYSE or Nasdaq symbols to the watchlist
   python portfolio_monitor.py --add IBM --add KO

   # Review saved symbols
   python portfolio_monitor.py --list

   # Run the monitor
   python portfolio_monitor.py

   # Optionally email the report through Gmail API
   python portfolio_monitor.py --email
   ```

The monitor script is standalone and does not read this repo's portfolio CSV.
It keeps its own local watchlist, validates `--add` symbols against Yahoo
Finance, and only accepts tickers that appear to be listed on NYSE or Nasdaq.

For Gmail API setup, see [GMAIL_API_SETUP.md](/Users/oferg/work/mycode/portfolio_analyzer/GMAIL_API_SETUP.md).

### Argument Table for 'Generate_Graph.py'

| Argument            | Type   | Default          | Description                                                        |
|---------------------|--------|------------|--------------------------------------------------------------------------|
| `--start-date`      | str    | Start date in CSV| Start date in `YYYY-MM-DD` format                                  |
| `--end-date`        | str    | End date in CSV| End date in `YYYY-MM-DD` format                                      |
| `--start-equity`    | float  | 100.0   | Baseline to index both series (default 100)                                 |
| `--output`          | str    | —       | Optional path to save the chart (`.png` / `.jpg` / `.pdf`)                  |

## ProcessPortfolio.py

### IMPORTANT

Always run the program after the market closes at 4:00 PM EST, otherwise it will default to using the previous day’s data.

Because the program relies on past data, orders for a given day are generated after that day’s trading session and must be placed on the following trading day. This prevents lookahead bias. For example, When I receive orders from ChatGPT, I run the program and input the orders the close the day after.  

This script updates your portfolio and logs trades.

**Information**
   - The program uses past data from 'chatgpt_portfolio_update.csv' to automatically grab today's portfolio.
   - If 'chatgpt_portfolio_update.csv' is empty (meaning no past trading days logged), you will required to enter your starting cash.
   - From here, you can set up your portfolio or make any changes.
   - The script asks if you want to record manual buys or sells.
   - After you hit 'Enter' all calculations for the day are made.
   - Results are saved to `chatgpt_portfolio_update.csv` and any trades are added to `chatgpt_trade_log.csv`.
   - In the terminal, daily results are printed. Copy and paste results into the LLM.
To automate prompts, check out the [Automation Guide](https://github.com/LuckyOne7777/ChatGPT-Micro-Cap-Experiment/blob/main/Other/AUTOMATION_README.md)
## Generate_Graph.py

This script draws a graph of your portfolio versus the S&P 500.

**Program will ALWAYS use 'Start Your Own/chatgpt_portfolio_update.csv' for data.**

1. **Ensure you have portfolio data**
   - Run `ProcessPortfolio.py` at least once so `chatgpt_portfolio_update.csv` has data.

2. **Run the graph script**
   ```bash
   python "Start Your Own/Generate_Graph.py" --start-equity 100
   ```
   
3. **View the chart**
   - A window opens showing your portfolio value vs. S&P 500. Results will be adjusted for baseline equity.

All of this is still VERY NEW, so there are bugs. Please reach out if you find an issue or have a question.

Both scripts are designed for beginners, feel free to experiment and modify them as you learn.
