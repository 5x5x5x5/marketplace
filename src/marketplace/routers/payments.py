"""The provider webhook sink (`/v1/payments/webhook`). Request parsing and
signature checks stay here; dedup and apply run on a worker thread in
`services.webhooks.process_webhook`, never on the event loop."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from ..db import CommitRoute
from ..payments.port import PaymentError, WebhookSignatureError
from ..services.webhooks import process_webhook as _process_webhook
from ..settings import settings
from .deps import ProviderDep

payments_router = APIRouter(prefix="/v1/payments", tags=["payments"], route_class=CommitRoute)


@payments_router.post("/webhook")
async def payments_webhook(request: Request, provider: ProviderDep) -> dict[str, str]:
    """Provider event sink. Unauthenticated by design — authenticity comes from
    the provider's signature, verified in parse_webhook. Duplicates no-op.

    The fake provider verifies nothing, so without Stripe the route does not
    exist (404) unless `settings.allow_unsigned_webhooks` is on."""
    if provider.name == "fake" and not settings.allow_unsigned_webhooks:
        raise HTTPException(status_code=404, detail="Not Found")
    payload = await request.body()
    try:
        event = provider.parse_webhook(payload, request.headers.get("stripe-signature"))
    except WebhookSignatureError:
        raise HTTPException(status_code=400, detail="invalid webhook signature") from None
    except (PaymentError, ValueError, KeyError):
        raise HTTPException(status_code=400, detail="malformed webhook payload") from None
    return await asyncio.to_thread(_process_webhook, event)
