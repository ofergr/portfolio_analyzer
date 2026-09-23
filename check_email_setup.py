#!/usr/bin/env python3
"""Email setup check for portfolio_analyzer.

This project sends mail via the Brevo transactional email HTTP API (not
SMTP, since outbound SMTP is blocked on this host's network).
Setup steps:
  1. Create a free Brevo account: https://app.brevo.com
  2. Verify SENDER_EMAIL as a sender in Brevo (Settings -> Senders).
  3. Create an API key: https://app.brevo.com/settings/keys/api
  4. Put these in .env:
       SENDER_EMAIL=you@example.com
       BREVO_API_KEY=xkeysib-xxxxxxxxxxxxxxxx
       RECIPIENTS=a@example.com,b@example.com
"""

from __future__ import annotations

import json

from brevo_mailer import brevo_setup_status


def main() -> int:
    status = brevo_setup_status()
    print(json.dumps(status, indent=2))
    ok = status["sender_email_configured"] and status["api_key_configured"] and status["recipient_count"]
    if not ok:
        print("\nIncomplete. See the setup steps in this file's docstring.")
        return 1
    print("\nBrevo email config looks complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
