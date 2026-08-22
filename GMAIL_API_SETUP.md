# Gmail API Setup

This project can send the daily portfolio report through the Gmail API.

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Fill in `.env`

Edit `.env` in this folder:

```env
GEMINI_API_KEY=your_gemini_api_key_here
SENDER_EMAIL=your-email@gmail.com
RECIPIENTS=first@example.com,second@example.com
```

Notes:
- `SENDER_EMAIL` should be the Gmail account that will send the report.
- `RECIPIENTS` can include your own Gmail address.

## 3. Create Google Cloud credentials

As of August 22, 2026, the standard Gmail API desktop-app flow is:

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project.
3. Enable the [Gmail API](https://developers.google.com/workspace/gmail/api/quickstart/python).
4. Configure the Google Auth platform / OAuth consent screen.
5. Create an OAuth client of type `Desktop app`.
6. Download the client file and save it in this folder as `credentials.json`.

You do not need to change anything special inside Gmail itself for the API path beyond using a Gmail-enabled Google account.

## 4. Run one-time authentication

```bash
python3 authenticate_gmail.py
```

This will:
- open a browser,
- ask you to sign in to the sender Gmail account,
- ask for permission to send mail,
- create `token.json` in this folder.

If you authenticate on another machine, copy both `credentials.json` and `token.json` back into this project folder.

## 5. Send the report

```bash
python3 portfolio_monitor.py --email
```

That will:
- run the monitor,
- build the HTML report,
- send it through Gmail API to the addresses in `RECIPIENTS`.

## Files used

- `credentials.json`: downloaded from Google Cloud Console
- `token.json`: created after OAuth login
- `.env`: sender and recipient configuration

## SMTP vs Gmail API

This project now uses Gmail API, not Gmail SMTP.

That means:
- you do not need a Gmail App Password,
- you do not need to enable “less secure apps,”
- you do need the Google Cloud OAuth setup above.

## Troubleshooting

If sending fails:

1. Confirm `credentials.json` exists in this folder.
2. Confirm `token.json` exists in this folder.
3. Confirm `.env` has `SENDER_EMAIL` and `RECIPIENTS`.
4. If the token is stale or revoked, delete `token.json` and run:

```bash
python3 authenticate_gmail.py
```
