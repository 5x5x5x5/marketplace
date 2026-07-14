"""Real-user auth: mail port, signup/login/sessions, verify + reset flows."""

import pytest
from fastapi.testclient import TestClient

from marketplace.mail import ConsoleEmailSender, RecordingEmailSender, get_mail_sender, use_sender
from tests.conftest import TEST_PASSWORD


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
