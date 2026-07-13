"""Provider selection. STRIPE_SECRET_KEY set → Stripe; unset → the deterministic
fake. The fake is a module singleton so scripted test state and the app see the
same instance; the Stripe client is built once, lazily."""

from ..settings import settings
from .fake import FakeProvider
from .port import PaymentProvider

fake_provider = FakeProvider()
_stripe_provider: PaymentProvider | None = None


def get_provider() -> PaymentProvider:
    global _stripe_provider
    if settings.stripe_secret_key:
        if _stripe_provider is None:
            from .stripe_provider import StripeProvider  # lazy: SDK only when configured

            _stripe_provider = StripeProvider(
                settings.stripe_secret_key, settings.stripe_webhook_secret
            )
        return _stripe_provider
    return fake_provider
