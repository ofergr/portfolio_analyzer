#!/usr/bin/env python3
"""One-time Gmail API OAuth setup for portfolio_analyzer."""

from __future__ import annotations

from gmail_mailer import CREDENTIALS_PATH, save_token_from_oauth_flow


def main() -> int:
    if not CREDENTIALS_PATH.exists():
        print(f"credentials.json not found: {CREDENTIALS_PATH}")
        print("Download an OAuth Desktop App client from Google Cloud Console and place it here.")
        return 1

    token_path = save_token_from_oauth_flow()
    print(f"Gmail authentication successful. Token saved to: {token_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
