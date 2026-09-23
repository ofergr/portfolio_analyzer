# Email Setup (Brevo)

This project sends the daily portfolio report through the **Brevo**
transactional email HTTP API (`api.brevo.com`, port 443). It previously used
Gmail SMTP, but outbound SMTP (ports 587/465) is blocked on this host's
network as of 2026-09-18, so sending moved to an HTTPS-based API instead.

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Create a Brevo account and API key

1. Sign up at <https://app.brevo.com> (free tier: 300 emails/day, no credit
   card required).
2. Verify your sender address under Settings -> Senders, Domains & Dedicated
   IPs (a 6-digit code is emailed to that address — no custom domain needed).
3. Create an API key under Settings -> SMTP & API -> API Keys.

## 3. Fill in `.env`

Edit `.env` in this folder:

```env
GEMINI_API_KEY=your_gemini_api_key_here
SENDER_EMAIL=your-verified-sender@example.com
BREVO_API_KEY=xkeysib-xxxxxxxxxxxxxxxx
RECIPIENTS=first@example.com,second@example.com
```

Notes:
- `SENDER_EMAIL` must be a sender verified in your Brevo account.
- `RECIPIENTS` is comma-separated and can include your own address.

## 4. Verify the config

```bash
python3 check_email_setup.py
```

Prints the setup status and reports whether the config is complete.

## 5. Send the report

```bash
python3 portfolio_monitor.py --email
```

Runs the monitor, builds the HTML report, and sends it via the Brevo API to
the addresses in `RECIPIENTS`.

## Troubleshooting

- **`BREVO_API_KEY is not configured`** — add it to `.env`.
- **`401`/`403` from Brevo** — the API key is wrong/revoked, or `SENDER_EMAIL`
  isn't a verified sender in your Brevo account.
- **New account not sending** — some new Brevo accounts need a one-time
  manual approval before the API will send; check the dashboard for an
  "under review" banner.
- **`SENDER_EMAIL` / `RECIPIENTS` errors** — fill those keys in `.env`.
