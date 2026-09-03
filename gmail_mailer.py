"""Gmail SMTP mailer for the portfolio analyzer project.

Uses a Gmail App Password over SMTP (smtp.gmail.com:587) instead of OAuth,
so there is no token to expire or revoke. Requires 2-Step Verification on the
sending account and an App Password from https://myaccount.google.com/apppasswords.

.env keys:
    SENDER_EMAIL        - the Gmail address to send from / log in as
    GMAIL_APP_PASSWORD  - 16-character App Password (spaces optional)
    RECIPIENTS          - comma-separated recipient list
"""

from __future__ import annotations

import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_SSL_PORT = 465
SMTP_TIMEOUT = 30
# Gmail occasionally answers a fresh connection with a transient
# "421 4.4.5 Server busy, try again later" - retry with backoff before giving up.
SMTP_MAX_ATTEMPTS = 5
SMTP_RETRY_BACKOFF = 15  # seconds, multiplied by the attempt number

load_dotenv(ENV_PATH)


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_email_config() -> dict[str, object]:
    return {
        "sender_email": os.getenv("SENDER_EMAIL", "").strip(),
        "app_password": os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip(),
        "recipients": _csv_env("RECIPIENTS"),
    }


def send_email_via_gmail_api(
    subject: str,
    html_content: str,
    recipients: Iterable[str] | None = None,
    plain_text: str | None = None,
) -> dict[str, object]:
    config = load_email_config()
    sender_email = str(config["sender_email"])
    app_password = str(config["app_password"])
    recipient_list = list(recipients if recipients is not None else config["recipients"])

    if not sender_email:
        raise RuntimeError("SENDER_EMAIL is not configured in .env")
    if not app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD is not configured in .env. Create one at "
            "https://myaccount.google.com/apppasswords"
        )
    if not recipient_list:
        raise RuntimeError("RECIPIENTS is not configured in .env")

    context = ssl.create_default_context()

    def _deliver() -> list[str]:
        sent_to: list[str] = []
        with _connect(context) as server:
            server.login(sender_email, app_password)
            for recipient in recipient_list:
                message = EmailMessage()
                message["To"] = recipient
                message["From"] = sender_email
                message["Subject"] = subject
                message.set_content(
                    plain_text or "This message contains an HTML report."
                )
                message.add_alternative(html_content, subtype="html")

                server.send_message(message)
                sent_to.append(recipient)
        return sent_to

    last_error: Exception | None = None
    for attempt in range(1, SMTP_MAX_ATTEMPTS + 1):
        try:
            sent_to = _deliver()
            break
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError) as exc:
            last_error = exc
            if attempt == SMTP_MAX_ATTEMPTS:
                raise
            delay = SMTP_RETRY_BACKOFF * attempt
            print(
                f"Gmail SMTP attempt {attempt}/{SMTP_MAX_ATTEMPTS} failed "
                f"({exc}); retrying in {delay}s"
            )
            time.sleep(delay)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_error if last_error else RuntimeError("Gmail SMTP send failed")

    return {
        "sender": sender_email,
        "recipients": sent_to,
        "count": len(sent_to),
    }


def _connect(context: ssl.SSLContext) -> smtplib.SMTP:
    """Open an authenticated-ready SMTP connection.

    Tries STARTTLS on 587 first, then falls back to implicit TLS on 465 - a
    busy Gmail frontend sometimes rejects one port but accepts the other.
    """
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
        server.starttls(context=context)
        return server
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError):
        return smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_SSL_PORT, timeout=SMTP_TIMEOUT, context=context
        )


def gmail_setup_status() -> dict[str, object]:
    config = load_email_config()
    return {
        "env_path": str(ENV_PATH),
        "sender_email_configured": bool(config["sender_email"]),
        "app_password_configured": bool(config["app_password"]),
        "recipient_count": len(config["recipients"]),
    }
