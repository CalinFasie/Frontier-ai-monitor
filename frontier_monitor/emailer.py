from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def send_if_configured(subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("EMAIL_FROM", "").strip() or user
    recipient = os.getenv("EMAIL_TO", "").strip()
    if not all([host, user, password, sender, recipient]):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    return True
