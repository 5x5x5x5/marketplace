"""Provider selection. STRIPE_SECRET_KEY set → Stripe (added in a later task);
unset → the deterministic fake. The fake is a module singleton so scripted test
state and the app see the same instance."""

from .fake import FakeProvider
from .port import PaymentProvider

fake_provider = FakeProvider()


def get_provider() -> PaymentProvider:
    return fake_provider
