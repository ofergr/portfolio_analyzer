#!/usr/bin/env python3
"""Gmail setup check for portfolio_analyzer.

This project now sends mail via Gmail SMTP with an App Password (no OAuth).
Setup steps:
  1. Enable 2-Step Verification on the SENDER_EMAIL account.
  2. Create an App Password: https://myaccount.google.com/apppasswords
  3. Put these in .env:
       SENDER_EMAIL=you@gmail.com
       GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
       RECIPIENTS=a@example.com,b@example.com
"""

from __future__ import annotations

import json

from gmail_mailer import gmail_setup_status


def main() -> int:
    status = gmail_setup_status()
    print(json.dumps(status, indent=2))
    ok = status["sender_email_configured"] and status["app_password_configured"] and status["recipient_count"]
    if not ok:
        print("\nIncomplete. See the setup steps in this file's docstring.")
        return 1
    print("\nGmail SMTP config looks complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
