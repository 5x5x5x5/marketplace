"""Compact projections of admin API payloads.

Each projection keeps what an operator acts on and drops the rest (buyer
client secrets, photo URLs, email addresses). Every user-authored free-text
field goes through `wrap`; nothing else is ever wrapped, so the marker means
exactly "a user wrote this".
"""

import json
from typing import Any

from marketplace_mcp.untrusted import NOTICE, wrap

Row = dict[str, Any]


def _pick(row: Row, keys: tuple[str, ...]) -> Row:
    return {k: row.get(k) for k in keys}


def job(r: Row) -> Row:
    return _pick(
        r,
        (
            "id",
            "status",
            "service_type_id",
            "buyer_id",
            "seller_id",
            "buyer_price",
            "payment_status",
            "created_at",
            "accepted_at",
            "completed_at",
        ),
    )


def payout(r: Row) -> Row:
    return _pick(
        r,
        (
            "id",
            "job_id",
            "seller_id",
            "amount",
            "currency",
            "status",
            "provider_transfer_id",
            "created_at",
        ),
    )


def transaction(r: Row) -> Row:
    return _pick(r, ("job_id", "buyer_price", "seller_payout", "margin", "completed_at"))


def dispute(r: Row) -> Row:
    out = _pick(
        r,
        (
            "id",
            "job_id",
            "source",
            "buyer_id",
            "status",
            "refund_amount",
            "clawback_amount",
            "created_at",
            "resolved_at",
        ),
    )
    out["reason"] = wrap(r.get("reason"))
    out["resolution_note"] = wrap(r.get("resolution_note"))
    return out


def report(r: Row) -> Row:
    out = _pick(
        r,
        ("id", "target_kind", "target_id", "reporter_id", "status", "created_at", "resolved_at"),
    )
    out["reason"] = wrap(r.get("reason"))
    out["resolution_note"] = wrap(r.get("resolution_note"))
    return out


def notification(r: Row) -> Row:
    out = _pick(r, ("id", "user_id", "kind", "status", "attempts", "created_at", "sent_at"))
    out["last_error"] = wrap(r.get("last_error"))
    return out


def webhook(r: Row) -> Row:
    out = _pick(
        r,
        (
            "id",
            "provider_event_id",
            "kind",
            "object_id",
            "status",
            "attempts",
            "received_at",
            "next_attempt_at",
        ),
    )
    # Provider/exception text, not operator-trusted: treat like user content.
    out["last_error"] = wrap(r.get("last_error"))
    return out


def listing(r: Row) -> Row:
    out = _pick(r, ("id", "service_type_id", "active", "hidden", "created_at", "updated_at"))
    out["title"] = wrap(r.get("title"))
    out["description"] = wrap(r.get("description"))
    return out


def moderation(r: Row) -> Row:
    out = _pick(r, ("id", "status", "suspended_at"))
    out["display_name"] = wrap(r.get("display_name"))
    out["suspended_reason"] = wrap(r.get("suspended_reason"))
    return out


def audit(r: Row) -> Row:
    return _pick(r, ("id", "actor", "action", "target", "created_at"))


def with_notice(data: Row) -> Row:
    """Attach the reminder only when the payload actually carries user text."""
    if "<<UNTRUSTED-USER-CONTENT" in json.dumps(data, default=str):
        return {"notice": NOTICE, **data}
    return data
