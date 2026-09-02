# Gmail Email Setup

This project sends the daily portfolio report through Gmail SMTP using an
**App Password**. There is no OAuth flow and no Google Cloud project — the App
Password does not expire the way OAuth refresh tokens do.

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Create a Gmail App Password

1. Enable **2-Step Verification** on the sender Gmail account.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create a new App Password (name it anything, e.g. `portfolio-monitor`).
4. Copy the 16-character password.

## 3. Fill in `.env`

Edit `.env` in this folder:

```env
GEMINI_API_KEY=your_gemini_api_key_here
SENDER_EMAIL=your-email@gmail.com
GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
RECIPIENTS=first@example.com,second@example.com
```

Notes:
- `SENDER_EMAIL` is the Gmail account that sends (and logs in to) SMTP.
- `GMAIL_APP_PASSWORD` — spaces are stripped automatically, so paste as-is.
- `RECIPIENTS` is comma-separated and can include your own address.

## 4. Verify the config

```bash
python3 authenticate_gmail.py
```

Prints the setup status and reports whether the config is complete.

## 5. Send the report

```bash
python3 portfolio_monitor.py --email
```

Runs the monitor, builds the HTML report, and sends it via
`smtp.gmail.com:587` to the addresses in `RECIPIENTS`.

## Troubleshooting

- **`GMAIL_APP_PASSWORD is not configured`** — add it to `.env`.
- **`535 ... Username and Password not accepted`** — 2-Step Verification is off,
  or the App Password is wrong/revoked. Generate a new one.
- **`SENDER_EMAIL` / `RECIPIENTS` errors** — fill those keys in `.env`.
