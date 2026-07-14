"""Real-user auth: mail port, signup/login/sessions, verify + reset flows."""

import pytest
from fastapi.testclient import TestClient

from marketplace.mail import ConsoleEmailSender, RecordingEmailSender, get_mail_sender, use_sender
from tests.conftest import TEST_PASSWORD, AuthFactory


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


def _signup(client: TestClient, role: str, email: str = "kim@example.test") -> dict[str, object]:
    r = client.post(
        "/v1/auth/signup",
        json={"email": email, "password": TEST_PASSWORD, "role": role, "display_name": "Kim"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_signup_returns_working_session(client: TestClient) -> None:
    body = _signup(client, "buyer")
    assert body["user"]["role"] == "buyer"  # pyright: ignore[reportIndexIssue]
    assert body["user"]["email_verified"] is False  # pyright: ignore[reportIndexIssue]
    header = {"Authorization": f"Bearer {body['token']}"}
    me = client.get("/v1/auth/me", headers=header).json()
    assert me["email"] == "kim@example.test"
    # And the session actually works against a domain endpoint:
    assert (
        client.post("/v1/quotes", json={"service_type_id": "x"}, headers=header).status_code == 404
    )  # unknown service, but authenticated


def test_signup_duplicate_email_role_conflicts(client: TestClient) -> None:
    _signup(client, "buyer")
    r = client.post(
        "/v1/auth/signup",
        json={
            "email": "kim@example.test",
            "password": TEST_PASSWORD,
            "role": "buyer",
            "display_name": "Kim2",
        },
    )
    assert r.status_code == 409


def test_same_email_may_register_both_roles(client: TestClient) -> None:
    buyer = _signup(client, "buyer")
    seller = _signup(client, "seller")
    assert buyer["user"]["id"] != seller["user"]["id"]  # pyright: ignore[reportIndexIssue]


def test_signup_cannot_create_admin(client: TestClient) -> None:
    r = client.post(
        "/v1/auth/signup",
        json={
            "email": "evil@example.test",
            "password": TEST_PASSWORD,
            "role": "admin",
            "display_name": "Evil",
        },
    )
    assert r.status_code == 422  # role is Literal[buyer, seller]


def test_login_and_wrong_password(client: TestClient) -> None:
    _signup(client, "buyer")
    ok = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": TEST_PASSWORD, "role": "buyer"},
    )
    assert ok.status_code == 200 and ok.json()["token"]
    bad = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": "wrong-password", "role": "buyer"},
    )
    unknown = client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.test", "password": TEST_PASSWORD, "role": "buyer"},
    )
    # Uniform 401: wrong password and unknown account are indistinguishable.
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json()


def test_logout_revokes_the_session(client: TestClient) -> None:
    body = _signup(client, "buyer")
    header = {"Authorization": f"Bearer {body['token']}"}
    assert client.post("/v1/auth/logout", headers=header).status_code == 200
    assert client.get("/v1/auth/me", headers=header).status_code == 401


def test_admin_bootstrap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from marketplace import auth as auth_module
    from marketplace.settings import settings as app_settings

    monkeypatch.setattr(app_settings, "admin_email", "root@example.test")
    monkeypatch.setattr(app_settings, "admin_password", "bootstrap-password-1")
    auth_module.bootstrap_admin()
    auth_module.bootstrap_admin()  # idempotent — second run must not raise or duplicate
    r = client.post(
        "/v1/auth/login",
        json={"email": "root@example.test", "password": "bootstrap-password-1", "role": "admin"},
    )
    assert r.status_code == 200
    header = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/v1/admin/transactions", headers=header).status_code == 200


def _extract_token(outbox: RecordingEmailSender, index: int = -1) -> str:
    # Mail bodies end with "token=<raw>" — the port carries it verbatim.
    body = outbox.sent[index][2]
    return body.rsplit("token=", 1)[1].strip()


def test_signup_sends_verification_and_verify_flips_flag(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    body = _signup(client, "buyer")
    assert len(mail_outbox.sent) == 1
    assert mail_outbox.sent[0][0] == "kim@example.test"
    token = _extract_token(mail_outbox)

    assert client.post("/v1/auth/verify", json={"token": token}).status_code == 200
    header = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/v1/auth/me", headers=header).json()["email_verified"] is True
    # Single-use: replay fails.
    assert client.post("/v1/auth/verify", json={"token": token}).status_code == 400


def test_password_reset_flow_revokes_sessions(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    body = _signup(client, "buyer")
    old_header = {"Authorization": f"Bearer {body['token']}"}
    r = client.post(
        "/v1/auth/password-reset/request", json={"email": "kim@example.test", "role": "buyer"}
    )
    assert r.status_code == 200
    token = _extract_token(mail_outbox)

    r = client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "brand-new-password-1"},
    )
    assert r.status_code == 200
    # Every pre-reset session is dead.
    assert client.get("/v1/auth/me", headers=old_header).status_code == 401
    # The new password works; the old one does not.
    ok = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": "brand-new-password-1", "role": "buyer"},
    )
    assert ok.status_code == 200
    stale = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": TEST_PASSWORD, "role": "buyer"},
    )
    assert stale.status_code == 401


def test_reset_request_never_confirms_account_existence(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    r = client.post(
        "/v1/auth/password-reset/request", json={"email": "ghost@example.test", "role": "buyer"}
    )
    assert r.status_code == 200  # identical to the exists case
    assert mail_outbox.sent == []  # but nothing was sent


def test_sweep_deletes_expired_sessions(client: TestClient, auth: AuthFactory) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from marketplace.db import SessionLocal
    from marketplace.entities import AuthSession

    auth("buyer", "sweepy")  # its session becomes the expired one
    with SessionLocal() as s:
        row = s.scalar(select(AuthSession))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)
        s.commit()
    # An EXPIRED principal 401s during dependency resolution — the endpoint
    # body (and its sweep) never runs. A different, live principal must
    # trigger maintenance.
    live = auth("seller", "sweeper")
    client.get("/v1/seller/offers", headers=live)  # list_offers calls _sweep
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(AuthSession)) == 1  # only the live one
