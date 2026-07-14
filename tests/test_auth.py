"""Real-user auth: mail port, signup/login/sessions, verify + reset flows."""

from marketplace.mail import ConsoleEmailSender, RecordingEmailSender, get_mail_sender, use_sender


def test_mail_sender_swap_roundtrip() -> None:
    recorder = RecordingEmailSender()
    previous = use_sender(recorder)
    try:
        assert get_mail_sender() is recorder
        get_mail_sender().send("a@b.test", "hi", "body")
        assert recorder.sent == [("a@b.test", "hi", "body")]
    finally:
        use_sender(previous)
    assert isinstance(get_mail_sender(), ConsoleEmailSender)


def test_console_sender_logs_instead_of_sending() -> None:
    # The dev adapter must never raise — it only logs.
    ConsoleEmailSender().send("a@b.test", "subject", "body")


def test_auth_tables_registered() -> None:
    from marketplace.entities import Base

    assert {"users", "auth_sessions", "email_tokens"} <= set(Base.metadata.tables)
