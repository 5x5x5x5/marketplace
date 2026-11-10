# Test corpus for rules/invariants.yml. Run: semgrep --test --config .semgrep/rules .semgrep/tests
# Never imported or executed; annotations apply to the line that follows.
# pyright: basic
# ruff: noqa
from decimal import Decimal


# --- request-model-identity-field
# ruleid: marketplace-request-model-identity-field
class JobCreateRequest(BaseModel):
    buyer_id: str
    quote_id: str


# ruleid: marketplace-request-model-identity-field
class AdminSellerBody(BaseModel):
    note: str
    seller_id: str = ""


# ok: marketplace-request-model-identity-field
class QuoteOut(BaseModel):
    buyer_id: str


# ok: marketplace-request-model-identity-field
class QuoteRequest(BaseModel):
    service_type_id: str


# --- stripe-import-outside-provider
# ruleid: marketplace-stripe-import-outside-provider
import stripe

# ruleid: marketplace-stripe-import-outside-provider
from stripe import StripeClient

# ruleid: marketplace-stripe-import-outside-provider
import stripe.error

# ok: marketplace-stripe-import-outside-provider
from payments import get_provider


# --- python-autoescape-disabled
# ruleid: marketplace-python-autoescape-disabled
env = Environment(loader=loader, autoescape=False)

# ruleid: marketplace-python-autoescape-disabled
html = Markup(user_text)

# ruleid: marketplace-python-autoescape-disabled
html = mark_safe(user_text)

# ok: marketplace-python-autoescape-disabled
env = Environment(loader=loader, autoescape=True)


# --- log-sensitive-request-data
def handler(request, logger):
    # ruleid: marketplace-log-sensitive-request-data
    logger.info("got %s", request.headers)
    # ruleid: marketplace-log-sensitive-request-data
    logger.warning("auth=%s", request.headers.get("authorization"))
    # ruleid: marketplace-log-sensitive-request-data
    logger.debug("cookies %s", request.cookies)
    # ruleid: marketplace-log-sensitive-request-data
    logger.info("q=%s", request.url.query)
    # ok: marketplace-log-sensitive-request-data
    logger.info("%s %s", request.method, request.url.path)
    # ok: marketplace-log-sensitive-request-data
    sig = request.headers.get("stripe-signature")


# --- log-sensitive-extra-key
def access(logger, status):
    # ruleid: marketplace-log-sensitive-extra-key
    logger.info("x", extra={"path": "/", "headers": {"a": 1}})
    # ruleid: marketplace-log-sensitive-extra-key
    logger.info("x", extra={"authorization": "Bearer x"})
    # ok: marketplace-log-sensitive-extra-key
    logger.info("x", extra={"method": "GET", "path": "/", "status": status})


# --- secret-compared-with-equality
def check(supplied_token, stored_token, body, password, expected):
    # ruleid: marketplace-secret-compared-with-equality
    if supplied_token == stored_token:
        pass
    # ruleid: marketplace-secret-compared-with-equality
    if body.password != expected:
        pass
    # ruleid: marketplace-secret-compared-with-equality
    ok = signature == computed
    # ok: marketplace-secret-compared-with-equality
    if supplied_token is None:
        pass
    # ok: marketplace-secret-compared-with-equality
    if hmac.compare_digest(supplied_token, stored_token):
        pass
    # ok: marketplace-secret-compared-with-equality
    if kind == "token":
        pass


# --- raw-credential-persisted / assigned / hash column
def persist(db, raw, pw, user):
    # ruleid: marketplace-raw-credential-persisted
    db.add(AuthSession(user_id="u", token=raw))
    # ruleid: marketplace-raw-credential-persisted
    db.add(User(email="a", password=pw))
    # ruleid: marketplace-raw-credential-assigned
    user.password = pw
    # ruleid: marketplace-credential-hash-column-not-hashed
    db.add(EmailToken(user_id="u", token_hash=raw))
    # ruleid: marketplace-credential-hash-column-not-hashed
    db.add(User(email="a", password_hash=pw))
    # ok: marketplace-credential-hash-column-not-hashed
    db.add(AuthSession(user_id="u", token_hash=_hash_token(raw)))
    # ok: marketplace-credential-hash-column-not-hashed
    db.add(User(email="a", password_hash=hash_password(pw)))
    # ok: marketplace-raw-credential-persisted
    return SessionOut(token=raw, expires_at=None)
    # ok: marketplace-raw-credential-persisted
    client = SmtpEmailSender(password=pw)


# --- money-field-float
class BadView(BaseModel):
    # ruleid: marketplace-money-field-float
    buyer_price: float
    # ruleid: marketplace-money-field-float
    seller_payout: float | None
    # ruleid: marketplace-money-field-float
    fee_total: float = 0.0
    # ok: marketplace-money-field-float
    margin: Decimal
    # ok: marketplace-money-field-float
    take_rate: float
    # ok: marketplace-money-field-float
    rating: float | None
