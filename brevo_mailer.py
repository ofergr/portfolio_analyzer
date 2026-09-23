"""Brevo (transactional email HTTP API) mailer for the portfolio analyzer project.

Sends over HTTPS (api.brevo.com:443) instead of raw SMTP, since outbound SMTP
(ports 587/465) is blocked on this host's network as of 2026-09-18.

.env keys:
    BREVO_API_KEY  - API key from Brevo Settings -> SMTP & API
    SENDER_EMAIL   - the verified Brevo sender address
    RECIPIENTS     - comma-separated recipient list
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 5
RETRY_BACKOFF = 15  # seconds, multiplied by the attempt number

load_dotenv(ENV_PATH)


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_email_config() -> dict[str, object]:
    return {
        "sender_email": os.getenv("SENDER_EMAIL", "").strip(),
        "api_key": os.getenv("BREVO_API_KEY", "").strip(),
        "recipients": _csv_env("RECIPIENTS"),
    }


def send_email_via_brevo(
    subject: str,
    html_content: str,
    recipients: Iterable[str] | None = None,
    plain_text: str | None = None,
) -> dict[str, object]:
    config = load_email_config()
    sender_email = str(config["sender_email"])
    api_key = str(config["api_key"])
    recipient_list = list(recipients if recipients is not None else config["recipients"])

    if not sender_email:
        raise RuntimeError("SENDER_EMAIL is not configured in .env")
    if not api_key:
        raise RuntimeError(
            "BREVO_API_KEY is not configured in .env. Create one at "
            "https://app.brevo.com/settings/keys/api"
        )
    if not recipient_list:
        raise RuntimeError("RECIPIENTS is not configured in .env")

    payload = {
        "sender": {"email": sender_email},
        "to": [{"email": recipient} for recipient in recipient_list],
        "subject": subject,
        "htmlContent": html_content,
        "textContent": plain_text or "This message contains an HTML report.",
    }
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                BREVO_SEND_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            )
            if response.status_code >= 500:
                raise requests.HTTPError(
                    f"Brevo server error {response.status_code}: {response.text}"
                )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Brevo rejected the send ({response.status_code}): {response.text}"
                )
            break
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_BACKOFF * attempt
            print(
                f"Brevo send attempt {attempt}/{MAX_ATTEMPTS} failed ({exc}); "
                f"retrying in {delay}s"
            )
            time.sleep(delay)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_error if last_error else RuntimeError("Brevo email send failed")

    return {
        "sender": sender_email,
        "recipients": recipient_list,
        "count": len(recipient_list),
    }


def brevo_setup_status() -> dict[str, object]:
    config = load_email_config()
    return {
        "env_path": str(ENV_PATH),
        "sender_email_configured": bool(config["sender_email"]),
        "api_key_configured": bool(config["api_key"]),
        "recipient_count": len(config["recipients"]),
    }
