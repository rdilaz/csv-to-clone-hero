"""Transactional email for long-job completion notifications.

Purposely tiny and provider-swappable (resend | smtp | log). Email is OPT-IN
and only used to notify when a long job finishes -- it is never an account
identity and never used for sign-in.

A missing API key is not an error for the request path: completion email is
best-effort by design.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from csv_to_clonehero.config import NotificationsConfig

log = logging.getLogger(__name__)


class EmailSender:
    def __init__(self, config: NotificationsConfig | None = None) -> None:
        self.config = config or NotificationsConfig()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def send(self, subject: str, body: str, to: str) -> bool:
        """Send one notification. Returns True when accepted (or no-op log)."""
        if not self.enabled or not str(to or "").strip():
            return False
        provider = (self.config.provider or "log").strip().lower()
        if provider == "resend":
            return self._send_resend(subject, body, to)
        if provider == "smtp":
            return self._send_smtp(subject, body, to)
        return self._send_log(subject, body, to)

    def _send_resend(self, subject: str, body: str, to: str) -> bool:
        api_key = self.config.effective_api_key
        if not api_key:
            return self._send_log(subject, body, to)
        try:
            response = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": self.config.from_address,
                    "to": [to],
                    "subject": subject,
                    "text": body,
                },
                timeout=20.0,
            )
            response.raise_for_status()
            return True
        except Exception:  # noqa: BLE001 - best-effort
            log.warning("completion email to %s failed", _mask(to), exc_info=True)
            return False

    def _send_smtp(self, subject: str, body: str, to: str) -> bool:
        if not self.config.smtp_host:
            return self._send_log(subject, body, to)
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.config.from_address
            msg["To"] = to
            msg.set_content(body)
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=20) as smtp:
                if self.config.smtp_use_tls:
                    smtp.starttls()
                user = ""
                if self.config.smtp_user_env:
                    import os

                    user = os.environ.get(self.config.smtp_user_env, "")
                if user:
                    import os

                    password = os.environ.get(self.config.smtp_password_env, "")
                    smtp.login(user, password)
                smtp.send_message(msg)
            return True
        except Exception:  # noqa: BLE001 - best-effort
            log.warning("completion email to %s failed", _mask(to), exc_info=True)
            return False

    def _send_log(self, subject: str, body: str, to: str) -> bool:
        log.info("email notification (to=%s): %s", _mask(to), subject)
        return True


def _mask(address: str) -> str:
    """Tiny obfuscation (local part) without over-engineering logging."""
    if "@" not in address:
        return "*"
    local, _, domain = address.partition("@")
    return f"{local[0] if local else '*'}...@{domain}"


# Convenience instance for workers that do not need DI.
def build_sender(config) -> EmailSender:
    notifications = getattr(config, "notifications", None)
    return EmailSender(notifications or NotificationsConfig())