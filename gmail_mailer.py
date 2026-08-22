"""Gmail API mailer for the portfolio analyzer project."""

from __future__ import annotations

import base64
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
TOKEN_PATH = SCRIPT_DIR / "token.json"
CREDENTIALS_PATH = SCRIPT_DIR / "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

load_dotenv(ENV_PATH)


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_email_config() -> dict[str, object]:
    return {
        "sender_email": os.getenv("SENDER_EMAIL", "").strip(),
        "recipients": _csv_env("RECIPIENTS"),
    }


def load_gmail_credentials() -> Credentials:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    if not creds or not creds.valid:
        raise RuntimeError(
            "No valid Gmail API token found. Run 'python3 authenticate_gmail.py' first."
        )

    return creds


def build_gmail_service():
    from googleapiclient.discovery import build

    creds = load_gmail_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email_via_gmail_api(
    subject: str,
    html_content: str,
    recipients: Iterable[str] | None = None,
    plain_text: str | None = None,
) -> dict[str, object]:
    config = load_email_config()
    sender_email = str(config["sender_email"])
    recipient_list = list(recipients if recipients is not None else config["recipients"])

    if not sender_email:
        raise RuntimeError("SENDER_EMAIL is not configured in .env")
    if not recipient_list:
        raise RuntimeError("RECIPIENTS is not configured in .env")

    service = build_gmail_service()
    sent_to: list[str] = []

    for recipient in recipient_list:
        message = EmailMessage()
        message["To"] = recipient
        message["From"] = sender_email
        message["Subject"] = subject
        message.set_content(plain_text or "This message contains an HTML report.")
        message.add_alternative(html_content, subtype="html")

        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        body = {"raw": encoded}
        service.users().messages().send(userId="me", body=body).execute()
        sent_to.append(recipient)

    return {
        "sender": sender_email,
        "recipients": sent_to,
        "count": len(sent_to),
    }


def gmail_setup_status() -> dict[str, object]:
    config = load_email_config()
    return {
        "env_path": str(ENV_PATH),
        "credentials_json_exists": CREDENTIALS_PATH.exists(),
        "token_json_exists": TOKEN_PATH.exists(),
        "sender_email_configured": bool(config["sender_email"]),
        "recipient_count": len(config["recipients"]),
    }


def save_token_from_oauth_flow() -> Path:
    from google_auth_oauthlib.flow import InstalledAppFlow
    import webbrowser

    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. Download it from Google Cloud Console first."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    try:
        webbrowser.get()
        creds = flow.run_local_server(port=0)
    except webbrowser.Error:
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
        print("Open this URL in your browser and approve access:")
        print(auth_url)
        auth_code = input("Paste the authorization code here: ").strip()
        flow.fetch_token(code=auth_code)
        creds = flow.credentials
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return TOKEN_PATH
