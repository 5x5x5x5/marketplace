"""Runnable end-to-end demo — the whole lifecycle, headless, no server needed.

    uv run python scripts/demo.py

Uses an in-process TestClient against a throwaway SQLite database, so it needs
no Postgres and no running server. Mirror these calls against a real deployment
by swapping the client for httpx pointed at your host.
"""

import os
import tempfile
import time
from decimal import Decimal
from uuid import UUID

# Point at a throwaway DB before importing the app.
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{tempfile.mkdtemp()}/demo.db")
os.environ.setdefault("ADMIN_EMAIL", "admin@demo.test")
os.environ.setdefault("ADMIN_PASSWORD", "demo-admin-password")
os.environ.setdefault("NOTIFY_DRAIN_SECONDS", "1")  # fast maintenance-loop ticks for act 13
# Real env vars outrank .env in pydantic-settings: pin these empty so a
# developer's .env (with a real Stripe key) can never flip this demo from the
# fake provider onto the live API.
os.environ.setdefault("STRIPE_SECRET_KEY", "")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "")

import email_validator
from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace import api
from marketplace.db import SessionLocal, init_db
from marketplace.entities import Payment
from marketplace.mail import RecordingEmailSender, use_sender
from marketplace.models import PaymentStatus
from marketplace.payments import fake_provider

# This script uses reserved *.test addresses (RFC 2606); email-validator's
# EmailStr backing rejects them as "special-use" domains unless told this is a
# test environment. This is the library's own documented switch for it — see
# tests/conftest.py for the same override.
email_validator.TEST_ENVIRONMENT = True


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signup(c: TestClient, email: str, role: str) -> dict[str, object]:
    resp = c.post(
        "/v1/auth/signup",
        json={
            "email": email,
            "password": "demo-password-1",
            "role": role,
            "display_name": email.split("@")[0],
        },
    )
    assert resp.status_code == 201, f"signup {email} failed: {resp.status_code} {resp.text}"
    return resp.json()


def main() -> None:
    init_db()
    with TestClient(api.app) as c:
        _run(c)


def _run(c: TestClient) -> None:
    admin_resp = c.post(
        "/v1/auth/login",
        json={"email": "admin@demo.test", "password": "demo-admin-password", "role": "admin"},
    )
    assert admin_resp.status_code == 200, (
        f"admin login failed: {admin_resp.status_code} {admin_resp.text}"
    )
    admin = bearer(admin_resp.json()["token"])

    alice_signup = signup(c, "buyer@demo.test", "buyer")
    alice = bearer(alice_signup["token"])
    alice_id = alice_signup["user"]["id"]

    carol_signup = signup(c, "seller@demo.test", "seller")
    carol = bearer(carol_signup["token"])
    carol_id = carol_signup["user"]["id"]

    sid = "rideshare"

    print("1. Admin configures a service type + a surge pipeline")
    c.put(
        f"/v1/admin/config/service_types/{sid}",
        json={"base_buyer_price": 20, "base_seller_payout": 14},
        headers=admin,
    )
    c.put(
        f"/v1/admin/config/pipelines/{sid}",
        json={"buyer": ["surge_by_demand_ratio"], "seller": []},
        headers=admin,
    )
    c.put("/v1/admin/config/margin_floor", json={"absolute": 3}, headers=admin)

    print("2. Seller Carol (capacity 2) onboards for payments, then posts availability")
    c.put(f"/v1/admin/sellers/{carol_id}", json={"capacity": 2}, headers=admin)
    onboard = c.post("/v1/seller/payments/onboard", headers=carol).json()
    print(f"   seller onboarded: payments_ready={onboard['payments_ready']}")
    c.post("/v1/seller/availability", json={"service_type_id": sid}, headers=carol)

    print("3. Buyer Alice requests a quote")
    quote = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    print(f"   quote buyer_price = {quote['buyer_price']}")

    print("4. Alice creates a job — the platform offers it to a seller")
    job = c.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=alice).json()
    job_id = job["id"]
    print(f"   job status = {job['status']}")

    print("5. Carol sees the offer (no buyer_price) and accepts — charge succeeds inline")
    offer = c.get("/v1/seller/offers", headers=carol).json()[0]
    print(f"   offer seller_payout = {offer['seller_payout']}  (buyer_price hidden)")
    accepted = c.post(f"/v1/seller/offers/{offer['id']}/accept", headers=carol).json()
    print(f"   job status = {accepted['status']}  (fake provider charges instantly)")

    print("6. Carol completes the job — transaction booked, payout transferred")
    tx = c.post(f"/v1/seller/jobs/{job_id}/complete", headers=carol).json()
    print(f"   margin (platform spread) = {tx['margin']}")

    print("7. Alice reviews Carol")
    c.post(f"/v1/jobs/{job_id}/review", json={"rating": 5, "comment": "great"}, headers=alice)

    print("7b. Carol reviews Alice back — buyer aggregate is display-only")
    c.post(
        f"/v1/seller/jobs/{job_id}/review", json={"rating": 5, "comment": "prompt"}, headers=carol
    )
    profile = c.get("/v1/profile", headers=alice).json()
    print(f"   alice rating = {profile['rating']} ({profile['rating_count']} review)")
    assert profile["rating"] == 5.0

    print("8. Admin margin summary")
    summary = c.get("/v1/admin/margins/summary", headers=admin).json()
    print(f"   {summary}")

    # --- Act 2: async payment (what a real Stripe confirmation looks like) ---
    print("9. Alice orders again; this time the charge comes back PENDING")
    fake_provider.next_charge_status = PaymentStatus.PENDING
    quote2 = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    job2 = c.post("/v1/jobs", json={"quote_id": quote2["id"]}, headers=alice).json()
    job2_id = job2["id"]
    offer2 = c.get("/v1/seller/offers", headers=carol).json()[0]
    accepted2 = c.post(f"/v1/seller/offers/{offer2['id']}/accept", headers=carol).json()
    print(f"   job status = {accepted2['status']}  (capacity held while awaiting payment)")

    view2 = c.get(f"/v1/jobs/{job2_id}", headers=alice).json()
    print(f"   payment_status={view2['payment_status']}  client_secret={view2['client_secret']}")

    print("10. The provider confirms the charge out-of-band; its webhook lands")
    with SessionLocal() as s:
        provider_payment_id = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(job2_id))
        )
    webhook = c.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_demo_1",
            "kind": "payment_succeeded",
            "object_id": provider_payment_id,
        },
    ).json()
    print(f"   webhook response = {webhook}")
    view2 = c.get(f"/v1/jobs/{job2_id}", headers=alice).json()
    print(f"   job status after webhook = {view2['status']}")

    print("11. Carol completes job 2 — payout transfers to the seller")
    c.post(f"/v1/seller/jobs/{job2_id}/complete", headers=carol)
    payouts = c.get("/v1/admin/payouts", headers=admin).json()
    payout2 = next(p for p in payouts if p["job_id"] == job2_id)
    print(f"   payout status = {payout2['status']}")

    print("12. Alice checks her own identity")
    me = c.get("/v1/auth/me", headers=alice).json()
    print(f"   me: id={me['id']} email={me['email']} role={me['role']}")

    # --- Act 3: notifications, delivered by the maintenance loop itself ---
    print("13. Alice orders once more; the offer email arrives via the loop (no manual drain)")
    outbox = RecordingEmailSender()
    previous_sender = use_sender(outbox)
    quote3 = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    job3 = c.post("/v1/jobs", json={"quote_id": quote3["id"]}, headers=alice).json()
    assert job3["status"] == "pending", job3
    deadline = time.time() + 15
    offer_mails: list[tuple[str, str, str]] = []
    while time.time() < deadline and not offer_mails:
        time.sleep(0.5)
        offer_mails = [m for m in outbox.sent if "New offer" in m[1]]
    use_sender(previous_sender)
    assert offer_mails, "maintenance loop did not deliver the offer email within 15s"
    print(f"   loop delivered: to={offer_mails[0][0]} subject={offer_mails[0][1]!r}")

    # --- Act 4: disputes (arbitration over the escrowed money) ---
    print("14. Alice disputes job 1; the admin resolves it partially")
    dispute = c.post(
        f"/v1/jobs/{job_id}/dispute",
        json={"reason": "arrived late, partial service"},
        headers=alice,
    ).json()
    resolved = c.post(
        f"/v1/admin/disputes/{dispute['id']}/resolve",
        json={"refund_amount": "6.00", "clawback_amount": "4.00", "note": "split the difference"},
        headers=admin,
    ).json()
    summary2 = c.get("/v1/admin/margins/summary", headers=admin).json()
    print("   resolved: refund=6.00 clawback=4.00")
    print(f"   margin gross={summary2['platform_margin']} net={summary2['platform_margin_net']}")

    assert onboard["payments_ready"] is True
    assert view2["status"] == "accepted"
    assert payout2["status"] == "paid"
    assert me["id"] == alice_id
    assert me["email"] == "buyer@demo.test"
    assert resolved["status"] == "resolved"
    assert summary2["platform_margin_net"] != summary2["platform_margin"]

    # --- Act 5: moderation (report -> takedown -> suspension -> reinstate) ---
    print("15. Carol discovers Alice's review of her own job, then reports it")
    job_reviews = c.get(f"/v1/seller/jobs/{job_id}/reviews", headers=carol).json()
    review = next(r for r in job_reviews if r["kind"] == "review")
    review_id = review["id"]
    print(f"   seller-side discovery: kind={review['kind']} rating={review['rating']}")
    report = c.post(
        "/v1/reports",
        json={"target_kind": "review", "target_id": review_id, "reason": "abusive language"},
        headers=carol,
    ).json()
    print(f"   report status = {report['status']}")

    print("16. Admin hides the comment (rating and aggregates stay)")
    hidden = c.post(f"/v1/admin/reviews/buyer/{review_id}/hide", headers=admin).json()
    print(f"   comment_hidden = {hidden['comment_hidden']}")

    print("17. Admin suspends Alice — acquisition blocked, reads still fine")
    c.post(f"/v1/admin/users/{alice_id}/suspend", json={"reason": "abuse"}, headers=admin)
    blocked = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice)
    print(f"   new quote -> {blocked.status_code} {blocked.json()['detail']}")
    assert blocked.status_code == 403

    print("18. Reinstate + resolve the report (no automatic actions either way)")
    c.post(f"/v1/admin/users/{alice_id}/reinstate", headers=admin)
    resolved_report = c.post(
        f"/v1/admin/reports/{report['id']}/resolve",
        json={"status": "actioned", "note": "comment hidden"},
        headers=admin,
    ).json()
    print(f"   report -> {resolved_report['status']}")
    assert resolved_report["status"] == "actioned"

    # --- Act 6: notification preferences (mute the nudge, money mail stays) ---
    print("19. Carol mutes offer_received — a new job matches silently")
    mute_resp = c.put(
        "/v1/notification-preferences", json={"muted": ["offer_received"]}, headers=carol
    )
    assert mute_resp.status_code == 200, mute_resp.text

    def offer_mail_count() -> int:
        rows = c.get("/v1/admin/notifications", headers=admin).json()
        return len([n for n in rows if n["kind"] == "offer_received"])

    before = offer_mail_count()
    q = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    c.post("/v1/jobs", json={"quote_id": q["id"]}, headers=alice)
    assert offer_mail_count() == before, "muted offer still mailed"
    offers = c.get("/v1/seller/offers", headers=carol).json()
    print(f"   offer mails unchanged ({before}); in-app offers visible: {len(offers)}")
    assert offers, "offer should still exist in-app"

    print("20. Carol unmutes — the next offer mails again")
    unmute_resp = c.put("/v1/notification-preferences", json={"muted": []}, headers=carol)
    assert unmute_resp.status_code == 200, unmute_resp.text
    q = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    c.post("/v1/jobs", json={"quote_id": q["id"]}, headers=alice)
    assert offer_mail_count() == before + 1
    print("   offer mail queued after unmute")

    # --- Act 7: fee-aware margin (the summary matches the bank account) ---
    print("21. Fees: the margin summary is net of the provider's estimated cut")
    s3 = c.get("/v1/admin/margins/summary", headers=admin).json()
    fees = Decimal(s3["fees_estimated"])
    net = Decimal(s3["platform_margin_net_of_fees"])
    assert fees > 0, s3
    assert net == Decimal(s3["platform_margin"]) + Decimal(s3["adjustments_net"]) - fees, s3
    print(f"   fees_estimated={fees}  margin gross={s3['platform_margin']}  net_of_fees={net}")

    print(
        "\nAll asserts passed: onboarding ready, async accept resolved via webhook, "
        "payout paid, offer email loop-delivered, moderation loop closed, "
        "notification mute/unmute enforced at enqueue, margin reported net of provider fees."
    )


if __name__ == "__main__":
    main()
