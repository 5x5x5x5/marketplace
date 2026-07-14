"""Outbound-email port — the seam between the app and mail delivery.

The console adapter logs instead of sending, keeping dev/tests turnkey; forks
plug in SES/Resend/Postmark behind the same protocol. The notifications phase
builds on this port.
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from .settings import settings

logger = logging.getLogger("marketplace.mail")


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    """Dev adapter: the 'sent' mail lands in the log."""

    def send(self, to: str, subject: str, body: str) -> None:
        logger.info("email to=%s subject=%r body=%r", to, subject, body)


class RecordingEmailSender:
    """Test double: captures sends so tests read tokens from the port, not logs."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


class SmtpEmailSender:
    """Real delivery via any provider's SMTP endpoint. Stdlib only; STARTTLS
    then LOGIN when configured, plain relay (e.g. Mailpit) when not."""

    def __init__(
        self, host: str, port: int, username: str, password: str, starttls: bool, from_addr: str
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._starttls = starttls
        self._from_addr = from_addr

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._from_addr
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


def _default_sender() -> EmailSender:
    if settings.smtp_host:
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            starttls=settings.smtp_starttls,
            from_addr=settings.mail_from,
        )
    return ConsoleEmailSender()


_active: EmailSender = _default_sender()


def get_mail_sender() -> EmailSender:
    return _active


def use_sender(sender: EmailSender) -> EmailSender:
    """Swap the active sender (tests, forks); returns the previous one."""
    global _active
    previous = _active
    _active = sender
    return previous
