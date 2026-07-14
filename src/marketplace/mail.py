"""Outbound-email port — the seam between the app and mail delivery.

The console adapter logs instead of sending, keeping dev/tests turnkey; forks
plug in SES/Resend/Postmark behind the same protocol. The notifications phase
builds on this port.
"""

import logging
from typing import Protocol

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


_active: EmailSender = ConsoleEmailSender()


def get_mail_sender() -> EmailSender:
    return _active


def use_sender(sender: EmailSender) -> EmailSender:
    """Swap the active sender (tests, forks); returns the previous one."""
    global _active
    previous = _active
    _active = sender
    return previous
