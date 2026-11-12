"""Executable architecture invariants.

Each test encodes one rule from CLAUDE.md's "Non-negotiables" so a violation
fails fast with an explanation of the rule and the fix, instead of relying on
whoever (or whatever) is editing having read the prose.

Everything here is stdlib `ast`/`pathlib`/`tomllib` plus introspection of the
FastAPI app and Pydantic models. Checks locate things by behavior (any module
under `src/marketplace`, any route carrying a role tag, any model matching a
naming family) rather than by hard-coded file, so splitting `api.py` into a
package of routers does not silently turn them vacuous. Each family asserts it
found something to check for the same reason.

Rules that cannot be checked statically are intentionally absent; the README
for this file is the table in the tooling report, not wishful thinking here.
"""

import ast
import re
import tomllib
import typing
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel

from marketplace import entities, models
from marketplace.api import app

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "marketplace"
PKG = "marketplace"


# ---------------------------------------------------------------- helpers


def _py_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "static" not in p.relative_to(SRC).parts)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([PKG, *parts])


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> Iterator[tuple[str, int]]:
    """Absolute dotted module names a file imports, anywhere (also inside
    functions, which is how lazy imports sneak past a top-of-file scan)."""
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            yield target, node.lineno
            for alias in node.names:  # `from . import db` imports the submodule
                yield f"{target}.{alias.name}", node.lineno


def _is_mod(name: str, *prefixes: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _modules_named(*stems: str) -> list[Path]:
    """`pricing.py` or a `pricing/` package, wherever it lives under src."""
    found: list[Path] = []
    for path in _py_files():
        rel = path.relative_to(SRC)
        top = rel.parts[0].removesuffix(".py")
        if top in stems:
            found.append(path)
    return found


def _fmt(violations: Iterable[str]) -> str:
    return "\n  ".join(violations)


def _flatten(annotation: object) -> Iterator[object]:
    yield annotation
    for arg in typing.get_args(annotation):
        yield from _flatten(arg)


def _model_classes(annotation: object) -> Iterator[type[BaseModel]]:
    for item in _flatten(annotation):
        if isinstance(item, type) and issubclass(item, BaseModel):
            yield item


def _reachable_models(model: type[BaseModel]) -> Iterator[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    stack = [model]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        yield current
        for field in current.model_fields.values():
            stack.extend(_model_classes(field.annotation))


def _field_names(model: type[BaseModel]) -> set[str]:
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        for alias in (field.alias, field.validation_alias, field.serialization_alias):
            if isinstance(alias, str):
                names.add(alias)
    return names


def _api_routes() -> list[APIRoute]:
    # FastAPI >= 0.162 keeps included routers nested; iter_route_contexts walks them.
    routes: list[APIRoute] = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.original_route
        if isinstance(route, APIRoute):
            assert ctx.path == route.path, "an include-time prefix would make route.path wrong here"
            routes.append(route)
    return routes


def _route_role(route: APIRoute) -> str | None:
    tags = set(route.tags)
    for role in ("buyer", "seller", "catalog", "admin"):
        if role in tags:
            return role
    return None


def _routes_response_models(role: str) -> Iterator[tuple[APIRoute, type[BaseModel]]]:
    for route in _api_routes():
        if _route_role(route) == role and route.response_model is not None:
            for model in _model_classes(route.response_model):
                yield route, model


def _body_field_names(route: APIRoute) -> set[str]:
    """Every name a client can put in the body: model fields (with aliases)
    plus individually-declared form/body parameters."""
    names: set[str] = set()
    for param in route.dependant.body_params:
        models_ = list(_model_classes(param.field_info.annotation))
        if models_:
            for model in models_:
                names |= _field_names(model)
        else:
            names.update({param.name, param.alias})
    return names


def _tokens(name: str) -> set[str]:
    return set(name.lower().split("_"))


# ------------------------------------------------- identity & authentication


def test_request_bodies_never_carry_buyer_or_seller_id() -> None:
    """INVARIANT: identity comes from the authenticated principal, never a request body.

    Fix: delete the `buyer_id`/`seller_id` field from the request model and
    take the id from the auth dependency (`Claims.sub`) in the handler. A body
    field here lets any caller impersonate another user.
    """
    checked = 0
    bad: list[str] = []
    for route in _api_routes():
        names = _body_field_names(route)
        checked += bool(names)
        for forbidden in sorted(names & {"buyer_id", "seller_id"}):
            bad.append(f"{sorted(route.methods or ())} {route.path}: body field {forbidden!r}")
    assert checked > 10, "found almost no request bodies; route discovery is broken"
    assert not bad, f"request bodies must not accept caller-supplied identity:\n  {_fmt(bad)}"


def test_no_token_minting_or_second_identity_verifier() -> None:
    """INVARIANT: identity resolves through `auth_sessions` rows only.

    `mint_token` / `MARKETPLACE_SECRET` are gone for good, and no JWT/HMAC
    verifier may be added beside the session lookup. Fix: create an
    `AuthSession` row via the existing login/signup flow instead.
    """
    banned_names = {"mint_token", "MARKETPLACE_SECRET", "marketplace_secret"}
    banned_modules = ("jwt", "jose", "itsdangerous", "authlib")
    bad: list[str] = []
    for path in _py_files():
        rel = path.relative_to(ROOT)
        for node in ast.walk(_parse(path)):
            ident = None
            if isinstance(node, ast.Name):
                ident = node.id
            elif isinstance(node, ast.Attribute):
                ident = node.attr
            elif isinstance(node, ast.FunctionDef):
                ident = node.name
            if ident in banned_names:
                bad.append(f"{rel}:{getattr(node, 'lineno', 0)}: {ident}")
        for name, lineno in _imports(path):
            if _is_mod(name, *banned_modules):
                bad.append(f"{rel}:{lineno}: import {name}")
    assert not bad, f"second identity mechanism introduced:\n  {_fmt(bad)}"


def test_credential_columns_are_hash_only() -> None:
    """INVARIANT: passwords are argon2 hashes; session and email-verification/reset
    tokens are stored sha256-only, and email tokens are single-use (`used_at`).

    Fix: store `token_hash` / `password_hash`, never the raw value, and keep a
    `used_at` column on any single-use token table.
    """
    raw_names = {"password", "token", "raw_token", "session_token", "api_key", "secret"}
    bad = [
        f"{table.name}.{col.name}"
        for table in entities.Base.metadata.tables.values()
        for col in table.columns
        if col.name.lower() in raw_names
    ]
    assert not bad, f"raw credential columns (store a hash instead): {bad}"
    tables = entities.Base.metadata.tables
    assert "token_hash" in tables["auth_sessions"].c, "sessions must store token_hash"
    assert "token_hash" in tables["email_tokens"].c, "email tokens must store token_hash"
    assert "used_at" in tables["email_tokens"].c, "email tokens must be single-use (used_at)"
    assert "password_hash" in tables["users"].c


def test_password_hash_assignments_use_the_argon2_helper() -> None:
    """INVARIANT: passwords are stored only as argon2 hashes via `auth.hash_password`,
    and token columns only ever receive a hashed value.

    Fix: pass `hash_password(...)` for `password_hash=` and `_hash_token(raw)`
    for `token_hash=`. Never `hashlib.md5/sha1/sha256` a password, and never
    store the raw token.
    """
    weak = {"md5", "sha1", "sha224", "blake2b", "blake2s"}
    bad: list[str] = []
    for path in _py_files():
        rel = path.relative_to(ROOT)
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "hashlib"
                    and func.attr in weak
                ):
                    bad.append(f"{rel}:{node.lineno}: hashlib.{func.attr} (weak hash)")
                for kw in node.keywords:
                    hashed_name = isinstance(kw.value, ast.Name) and "hash" in kw.value.id
                    if (
                        kw.arg == "password_hash"
                        and not _calls(kw.value, "hash_password")
                        and not hashed_name
                    ):
                        bad.append(f"{rel}:{kw.value.lineno}: password_hash not from hash_password")
                    if (
                        kw.arg == "token_hash"
                        and not _calls(kw.value, "_hash_token", "hash_token")
                        and not hashed_name
                    ):
                        bad.append(f"{rel}:{kw.value.lineno}: token_hash is not a hash call")
    assert not bad, f"credential storage rule broken:\n  {_fmt(bad)}"


def _calls(node: ast.AST, *names: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id in names) or (
        isinstance(func, ast.Attribute) and func.attr in names
    )


def test_session_cookie_is_httponly_and_samesite() -> None:
    """INVARIANT: cookie sessions are the same `auth_sessions` rows as the API, set
    HttpOnly + SameSite=Lax.

    Fix: every `set_cookie` call must pass `httponly=True` and a `samesite`
    value; a script-readable or cross-site-sent session cookie is an XSS/CSRF hole.
    """
    seen = 0
    bad: list[str] = []
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr != "set_cookie":
                    continue
                seen += 1
                kws = {kw.arg: kw.value for kw in node.keywords}
                httponly = kws.get("httponly")
                if not (isinstance(httponly, ast.Constant) and httponly.value is True):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: httponly must be True")
                if "samesite" not in kws:
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: samesite missing")
    assert seen, "no set_cookie call found; if cookie sessions moved, update this test"
    assert not bad, "\n  ".join(bad)


def _depends_on(dependant: Any, target: object) -> bool:
    return any(dep.call is target or _depends_on(dep, target) for dep in dependant.dependencies)


def test_every_page_post_requires_csrf_except_the_allowlist() -> None:
    """INVARIANT: every authenticated page form carries a session-bound CSRF token.

    Every POST route served by the `pages` package must depend on
    `pages.csrf.require_csrf`, except the unauthenticated forms named in
    `CSRF_EXEMPT_POSTS` (they have no session to bind a token to). Fix: add
    `dependencies=[Depends(require_csrf)]` to the route and render
    `_csrf.html` inside its form; extending the allowlist is a security
    decision, not a fix.
    """
    from marketplace.pages.csrf import CSRF_EXEMPT_POSTS, require_csrf

    page_posts = [
        r
        for r in _api_routes()
        if "POST" in (r.methods or ()) and r.endpoint.__module__.startswith(f"{PKG}.pages")
    ]
    assert len(page_posts) > 10, "no page POST routes found; route discovery is broken"
    paths = {r.path for r in page_posts}
    assert paths >= CSRF_EXEMPT_POSTS, (
        f"allowlist names unknown routes: {CSRF_EXEMPT_POSTS - paths}"
    )
    bad = [
        r.path
        for r in page_posts
        if r.path not in CSRF_EXEMPT_POSTS and not _depends_on(r.dependant, require_csrf)
    ]
    assert not bad, f"page POST without the CSRF dependency:\n  {_fmt(bad)}"


# ------------------------------------------------- information asymmetry


def test_model_family_field_denylists() -> None:
    """INVARIANT: information asymmetry is enforced by the model layer.

    Buyer-facing views never declare `seller_payout`/`margin`; seller-facing
    views never declare `buyer_price`; public catalog views never declare
    payout/margin/capacity/email/`payments_ready`. Fix: remove the field from
    the view (or add a separate admin view); never widen a shared view.
    """
    families: dict[str, tuple[tuple[str, ...], set[str]]] = {
        "Buyer": (("Buyer",), {"seller_payout", "margin", "clawback_amount"}),
        "Seller": (("Seller",), {"buyer_price", "refund_amount"}),
    }
    bad: list[str] = []
    counts: dict[str, int] = {}
    for name, cls in vars(models).items():
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            continue
        if not cls.__module__.startswith("marketplace.models"):
            continue
        for family, (prefixes, forbidden) in families.items():
            if name.startswith(prefixes) and name.endswith(("View", "Out")):
                counts[family] = counts.get(family, 0) + 1
                for field in sorted(_field_names(cls) & forbidden):
                    bad.append(f"{name} ({family.lower()}-facing) declares {field!r}")
        if name.startswith("Catalog"):
            counts["Catalog"] = counts.get("Catalog", 0) + 1
            for field in sorted(_field_names(cls)):
                low = field.lower()
                if any(w in low for w in ("payout", "margin", "capacity", "email")) or (
                    low == "payments_ready"
                ):
                    bad.append(f"{name} (public catalog) declares {field!r}")
    assert counts.get("Buyer") and counts.get("Seller") and counts.get("Catalog"), counts
    assert not bad, f"view leaks the other side's number:\n  {_fmt(bad)}"


@pytest.mark.parametrize(
    ("role", "forbidden"),
    [
        ("buyer", {"seller_payout", "margin"}),
        ("seller", {"buyer_price"}),
        (
            "catalog",
            {
                "seller_payout",
                "payout",
                "margin",
                "capacity",
                "payments_ready",
                "email",
                "buyer_price",
            },
        ),
    ],
)
def test_role_routes_do_not_return_the_other_sides_fields(role: str, forbidden: set[str]) -> None:
    """INVARIANT: buyer endpoints return buyer views, seller endpoints seller views,
    `/v1/catalog/*` the public views, with nothing the other side must not see.

    Checked on what each route actually declares as `response_model`, including
    nested models, so a route wired to the wrong view (or a view that nests an
    admin model) is caught regardless of how the models are named.
    Fix: return the correct role-scoped view from the handler.
    """
    checked = 0
    bad: list[str] = []
    for route, model in _routes_response_models(role):
        for nested in _reachable_models(model):
            checked += 1
            for field in sorted(_field_names(nested) & forbidden):
                bad.append(f"{route.path} -> {model.__name__}.{nested.__name__}.{field}")
    assert checked, f"no {role} routes with response models found"
    assert not bad, f"{role}-facing route exposes forbidden field:\n  {_fmt(bad)}"


def test_role_routes_declare_a_typed_response_model() -> None:
    """INVARIANT: never hand-build a dict; ORM entities never leave the API layer.

    Every buyer/seller/catalog/admin route must declare a `response_model`, it
    must not contain `Any` (a free-form payload bypasses the view split), and
    it must not be (or nest) an SQLAlchemy entity. Fix: define a Pydantic view
    in `models.py` and return it.
    """
    bad: list[str] = []
    checked = 0
    for route in _api_routes():
        role = _route_role(route)
        if role is None:
            continue
        checked += 1
        rm = route.response_model
        if rm is None:
            bad.append(f"{route.path}: no response_model")
            continue
        flat = list(_flatten(rm))
        if role != "admin" and any(item is Any for item in flat):
            bad.append(f"{route.path}: free-form Any in response_model")
        if any(isinstance(i, type) and issubclass(i, entities.Base) for i in flat):
            bad.append(f"{route.path}: ORM entity used as response_model")
    assert checked > 30
    assert not bad, f"response typing rule broken:\n  {_fmt(bad)}"


def test_models_module_does_not_touch_the_db_layer() -> None:
    """INVARIANT: ORM entities never leave the API layer; `models.py` is DTOs only.

    Fix: keep SQLAlchemy and `entities`/`db` out of the Pydantic view module;
    map ORM rows to views in the API layer.
    """
    bad: list[str] = []
    for path in _modules_named("models"):
        for name, lineno in _imports(path):
            if _is_mod(name, "sqlalchemy", f"{PKG}.entities", f"{PKG}.db", f"{PKG}.repo"):
                bad.append(f"{path.relative_to(ROOT)}:{lineno}: imports {name}")
    assert not bad, f"models must not depend on the persistence layer:\n  {_fmt(bad)}"


def test_money_fields_are_decimal_not_float() -> None:
    """INVARIANT: money is `Decimal`, serialized as JSON strings.

    Any view/request field whose name says it's money (price, payout, amount,
    margin, fee(s), revenue, ...) must not be annotated `float`. Fix: use
    `Decimal` and quantize with `models.to_money`; only the pricing pipeline's
    ratios stay float.
    """
    money_tokens = {"price", "payout", "payouts", "amount", "margin", "fee", "fees", "revenue"}
    bad: list[str] = []
    checked = 0
    for name, cls in vars(models).items():
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            continue
        if not cls.__module__.startswith("marketplace.models"):
            continue
        for field_name, field in cls.model_fields.items():
            if _tokens(field_name) & money_tokens:
                checked += 1
                if any(item is float for item in _flatten(field.annotation)):
                    bad.append(f"{name}.{field_name}: float")
    assert checked > 10
    assert not bad, f"money must be Decimal:\n  {_fmt(bad)}"


# ----------------------------------------------- listings, pricing, ownership


def test_listing_request_models_have_no_price_or_ownership_flags() -> None:
    """INVARIANT: listings never carry prices; `Listing.hidden` is admin-owned and
    `Listing.active` seller-owned.

    Seller listing bodies must have no price-like field and must not accept
    `hidden`; admin bodies must not accept `active`. Fix: prices come from the
    platform pipelines; the moderation flag is set only by the admin
    hide/unhide endpoints and the on/off toggle only by the seller.
    """
    price_tokens = {
        "price",
        "payout",
        "amount",
        "cost",
        "fee",
        "fees",
        "rate",
        "margin",
        "currency",
    }
    bad: list[str] = []
    seen_listing_body = False
    for route in _api_routes():
        if "listings" not in route.path or not route.dependant.body_params:
            continue
        names = _body_field_names(route)
        role = _route_role(route)
        if role == "seller":
            seen_listing_body = True
            for n in sorted(names):
                if _tokens(n) & price_tokens:
                    bad.append(f"{route.path}: price-like body field {n!r}")
            if "hidden" in names:
                bad.append(f"{route.path}: seller body accepts 'hidden' (admin-owned)")
        if role == "admin" and "active" in names:
            bad.append(f"{route.path}: admin body accepts 'active' (seller-owned)")
    assert seen_listing_body, "no seller listing write route found"
    table = entities.Base.metadata.tables["listings"]
    for col in table.columns:
        if _tokens(col.name) & price_tokens:
            bad.append(f"listings.{col.name}: listings must not store prices")
    assert not bad, f"listing rule broken:\n  {_fmt(bad)}"


_PURE_FORBIDDEN_INTERNAL = (
    "db",
    "entities",
    "repo",
    "api",
    "pages",
    "auth",
    "catalog",
    "notifications",
    "mail",
    "payments",
    "idempotency",
    "observability",
    "services",
    "routers",
)


def test_pricing_and_matching_core_is_pure() -> None:
    """INVARIANT: `pricing` and `matching` operate on config snapshots, never on the
    DB session or ORM rows.

    They may not import sqlalchemy, fastapi/starlette, or the db/entities/repo
    (or any I/O-layer) modules. Fix: have `repo.py` load the data into the
    snapshot types in `config.py` and pass those in.
    """
    pure = _modules_named("pricing", "matching", "config")
    assert {p.stem for p in pure} >= {"pricing", "matching"}, pure
    forbidden = (
        "sqlalchemy",
        "fastapi",
        "starlette",
        "pydantic_settings",
        *(f"{PKG}.{m}" for m in _PURE_FORBIDDEN_INTERNAL),
    )
    bad = [
        f"{p.relative_to(ROOT)}:{lineno}: imports {name}"
        for p in pure
        for name, lineno in _imports(p)
        if _is_mod(name, *forbidden)
    ]
    assert not bad, f"pure core must not depend on I/O layers:\n  {_fmt(bad)}"


# ---------------------------------------------------------- import direction


def test_only_the_provider_module_imports_the_stripe_sdk() -> None:
    """INVARIANT: providers are only reached through `payments/port.py`.

    `import stripe` is allowed in `payments/stripe_provider.py` only. Fix: add
    the operation to `PaymentProvider` in `port.py`, implement it in both
    `fake.py` and `stripe_provider.py`, and call it through `get_provider()`.
    """
    allowed = SRC / "payments" / "stripe_provider.py"
    bad = [
        f"{p.relative_to(ROOT)}:{lineno}: imports {name}"
        for p in _py_files()
        if p != allowed
        for name, lineno in _imports(p)
        if _is_mod(name, "stripe")
    ]
    assert allowed.exists()
    assert not bad, f"the stripe SDK is confined to stripe_provider.py:\n  {_fmt(bad)}"


def test_app_code_reaches_providers_only_through_the_port() -> None:
    """INVARIANT: providers are only reached through `payments/port.py`; selection
    is `payments.get_provider`.

    Nothing outside the `payments` package may import `payments.fake` or
    `payments.stripe_provider` (tests may; they script the singleton). Fix:
    import `get_provider` from `marketplace.payments` and the types from
    `marketplace.payments.port`.
    """
    bad = [
        f"{p.relative_to(ROOT)}:{lineno}: imports {name}"
        for p in _py_files()
        if "payments" not in p.relative_to(SRC).parts[:1]
        for name, lineno in _imports(p)
        if _is_mod(name, f"{PKG}.payments.fake", f"{PKG}.payments.stripe_provider")
    ]
    assert not bad, f"bypassing the payment port:\n  {_fmt(bad)}"


def test_import_direction_is_one_way() -> None:
    """INVARIANT: `pages -> (db, auth, catalog, entities, models)` never imports the
    API module; `notifications` never imports `api`/`pages`; nothing but the
    app assembler may import `api` (that is a cycle).

    Fix: move the shared piece into a module both sides can import (as
    `catalog.py` did), never import `api` from a lower layer.
    """
    lower = {
        "pages",
        "notifications",
        "auth",
        "catalog",
        "db",
        "repo",
        "entities",
        "models",
        "mail",
        "idempotency",
        "observability",
        "settings",
        "pricing",
        "matching",
        "config",
        "payments",
        "errors",
        "services",
        "routers",
    }
    bad: list[str] = []
    for path in _py_files():
        top = path.relative_to(SRC).parts[0].removesuffix(".py")
        for name, lineno in _imports(path):
            if top in lower and _is_mod(name, f"{PKG}.api"):
                bad.append(f"{path.relative_to(ROOT)}:{lineno}: {top} imports {name}")
            if top == "notifications" and _is_mod(name, f"{PKG}.pages"):
                bad.append(f"{path.relative_to(ROOT)}:{lineno}: notifications imports {name}")
    assert _modules_named("pages") and _modules_named("notifications")
    assert not bad, f"import cycle risk:\n  {_fmt(bad)}"


def _services_layer() -> list[Path]:
    """The framework-free logic layer: `services/`, plus the two modules that
    already played that role before it existed (`catalog`, `errors`)."""
    found = _modules_named("services", "catalog", "errors")
    assert any(p.relative_to(SRC).parts[0] == "services" for p in found), found
    return found


def test_services_import_no_web_framework_and_no_upper_layer() -> None:
    """INVARIANT: domain logic lives in service modules that hold no app and import
    no web framework, so the JSON routers and the HTML pages can both call them.

    `services/*`, `catalog` and `errors` may not import fastapi/starlette, nor
    `api`, `routers`, `pages` or `auth` (identity arrives as a plain argument),
    and may not mention `HTTPException`. Fix: raise an `errors.DomainError`
    subclass (api.py maps it to HTTP) and take ids/roles as parameters.
    """
    forbidden = (
        "fastapi",
        "starlette",
        f"{PKG}.api",
        f"{PKG}.routers",
        f"{PKG}.pages",
        f"{PKG}.auth",
    )
    bad: list[str] = []
    for path in _services_layer():
        rel = path.relative_to(ROOT)
        for name, lineno in _imports(path):
            if _is_mod(name, *forbidden):
                bad.append(f"{rel}:{lineno}: imports {name}")
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Name) and node.id == "HTTPException":
                bad.append(f"{rel}:{node.lineno}: HTTPException")
    assert not bad, f"service layer depends on the web layer:\n  {_fmt(bad)}"


def test_lower_layers_never_import_services_or_routers() -> None:
    """INVARIANT: the pure core and the persistence/outbox layers sit below the
    services; services sit below routers and pages; only `api` mounts routers.

    `pricing`/`matching`/`config`/`repo`/`notifications`/`db`/`entities`/
    `models`/`mail`/`payments` may not import `services` or `routers`; `pages`
    may import `services` but never `routers`; a service never imports a
    router. Fix: move the shared piece down a layer instead of importing up.
    """
    below_services = {
        "pricing",
        "matching",
        "config",
        "repo",
        "notifications",
        "db",
        "entities",
        "models",
        "mail",
        "payments",
        "settings",
        "errors",
        "catalog",
        "idempotency",
        "observability",
        "auth",
    }
    bad: list[str] = []
    for path in _py_files():
        top = path.relative_to(SRC).parts[0].removesuffix(".py")
        rel = path.relative_to(ROOT)
        for name, lineno in _imports(path):
            if top in below_services and _is_mod(name, f"{PKG}.services", f"{PKG}.routers"):
                bad.append(f"{rel}:{lineno}: {top} imports {name}")
            if top in {"pages", "services"} and _is_mod(name, f"{PKG}.routers"):
                bad.append(f"{rel}:{lineno}: {top} imports {name}")
    assert not bad, f"layering broken:\n  {_fmt(bad)}"


def test_routers_hand_the_session_to_services() -> None:
    """INVARIANT: routers are thin (parse, call one service, return); the domain
    work, including every session read and write, happens in `services/`.

    No `session.<method>(...)` call in a router module: a router only passes
    the session along. Fix: move the query or mutation into the service the
    route calls (a page will want the same logic).
    """
    routers = [p for p in _py_files() if p.relative_to(SRC).parts[0] == "routers"]
    assert routers, "no routers package found"
    bad: list[str] = []
    for path in routers:
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"session", "db", "sess"}
            ):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: session.{node.func.attr}")
    assert not bad, f"router touches the session directly:\n  {_fmt(bad)}"


def test_api_module_is_app_assembly_only() -> None:
    """INVARIANT: `api.py` assembles the app (middleware, lifespan, error mapping,
    router mounting, the CommitRoute check) and holds no routers or handlers
    beyond `/healthz`.

    Fix: put a new route in the matching `routers/` module and its logic in a
    service; never grow a router back inside `api.py`.
    """
    (api_path,) = [p for p in _py_files() if p.relative_to(SRC) == Path("api.py")]
    bad: list[str] = []
    for node in ast.walk(_parse(api_path)):
        if _calls(node, "APIRouter"):
            bad.append(f"api.py:{getattr(node, 'lineno', 0)}: APIRouter(...)")
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"get", "post", "put", "patch", "delete"}
                    and node.name != "healthz"
                ):
                    bad.append(f"api.py:{node.lineno}: route {node.name}")
    assert not bad, f"api.py is app assembly only:\n  {_fmt(bad)}"


def test_services_and_pages_import_without_the_app() -> None:
    """INVARIANT: `pages.py` never imports `api.py`, and the services it will call
    are importable without dragging in the app or the JSON routers.

    Checked at runtime in a fresh interpreter, so a transitive import (a
    service importing a module that imports `api`) fails too. Fix: break the
    chain by moving the shared piece into a lower module.
    """
    import subprocess
    import sys

    services = sorted(
        _module_name(p) for p in _py_files() if p.relative_to(SRC).parts[0] == "services"
    )
    code = (
        "import importlib, sys\n"
        f"for m in {[f'{PKG}.pages', *services]!r}:\n"
        "    importlib.import_module(m)\n"
        f"leaked = sorted(m for m in sys.modules if m == '{PKG}.api'"
        f" or m.startswith('{PKG}.routers'))\n"
        "print(','.join(leaked))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert len(services) > 5, services
    assert out == "", f"importing pages/services pulled in: {out}"


def test_catalog_queries_live_in_catalog_module() -> None:
    """INVARIANT: `catalog.py` is the single source of catalog visibility truth.

    The `visible_listings_stmt` / `listing_bookable` helpers must be defined
    exactly once, and the page layer must consume them rather than redefine
    them. Fix: extend `catalog.py` for a new consumer; never copy the filter.
    """
    defs: dict[str, list[str]] = {}
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.FunctionDef) and node.name in {
                "visible_listings_stmt",
                "listing_bookable",
                "seller_publicly_visible",
                "indicative_buyer_price",
            }:
                defs.setdefault(node.name, []).append(str(path.relative_to(ROOT)))
    assert len(defs) == 4, f"expected the four catalog helpers, found {sorted(defs)}"
    dup = {k: v for k, v in defs.items() if len(v) != 1}
    assert not dup, f"catalog helper defined more than once (drift risk): {dup}"
    assert all(v[0].endswith("catalog.py") or "catalog" in v[0] for v in defs.values()), defs


# ----------------------------------------------------------------- templates


def test_templates_never_disable_autoescape_or_mark_safe() -> None:
    """INVARIANT: Jinja autoescape is a security boundary.

    No template may use `|safe` or an `autoescape false` block, and no Python
    module may build `Markup`/`autoescape=False`. Fix: render user data as
    plain `{{ value }}`; if rich text is ever needed, sanitize server-side
    into a dedicated, reviewed helper and add a test for it.
    """
    bad: list[str] = []
    templates = [
        p for p in SRC.rglob("*") if p.is_file() and p.suffix in {".html", ".jinja", ".j2"}
    ]
    assert templates, "no templates found"
    pattern = re.compile(
        r"\|\s*safe\b|autoescape\s+(false|off)|\|\s*(mark_safe|forceescape)\b", re.I
    )
    for tpl in templates:
        for i, line in enumerate(tpl.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                bad.append(f"{tpl.relative_to(ROOT)}:{i}: {line.strip()}")
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (
                        kw.arg == "autoescape"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is False
                    ):
                        bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: autoescape=False")
                if _calls(node, "Markup", "mark_safe"):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: Markup(...)")
    assert not bad, f"autoescape boundary broken:\n  {_fmt(bad)}"


# ---------------------------------------------------- concurrency & commits


def test_no_process_level_locks() -> None:
    """INVARIANT: concurrency is the DB's job.

    No `threading`/`asyncio`/`multiprocessing` Lock, RLock or Semaphore in
    `src/marketplace`. Fix: use `session.get(..., with_for_update=True)` or
    `select(...).with_for_update()` on the row being protected; a process lock
    is wrong the moment there are two workers.
    """
    lock_names = {"Lock", "RLock", "Semaphore", "BoundedSemaphore", "Condition"}
    lock_modules = {"threading", "asyncio", "multiprocessing", "anyio"}
    bad: list[str] = []
    for path in _py_files():
        tree = _parse(path)
        imported_locks: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in lock_modules:
                for alias in node.names:
                    if alias.name in lock_names:
                        imported_locks.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in lock_names
                and isinstance(node.value, ast.Name)
                and node.value.id in lock_modules
            ) or (isinstance(node, ast.Name) and node.id in imported_locks):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not bad, f"process-level lock found (use row locks):\n  {_fmt(bad)}"


def test_get_session_teardown_does_not_commit() -> None:
    """INVARIANT: commits happen in `db.CommitRoute` before the response is sent,
    never in the `get_session` teardown (read-your-writes race, finding F2).

    Fix: leave `get_session` as open/yield/close; if a handler must persist
    something itself, commit inside the handler.
    """
    found = False
    bad: list[str] = []
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.FunctionDef) and node.name == "get_session":
                found = True
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in {"commit", "flush"}:
                        bad.append(f"{path.relative_to(ROOT)}:{sub.lineno}: .{sub.attr}")
    assert found, "get_session not found; update this test if the dependency was renamed"
    assert not bad, f"commit in get_session teardown:\n  {_fmt(bad)}"


def _except_type_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in nodes
        if isinstance(n, (ast.Name, ast.Attribute))
    }


def test_page_handlers_roll_back_before_answering_a_domain_error() -> None:
    """INVARIANT: a page form handler that catches a `DomainError` rolls the
    session back before it answers.

    A page POST answers 303, which is under 400, so `CommitRoute` commits; a
    handler that caught the error and redirected without rolling back would
    commit whatever the service wrote before it raised (the JSON API discards
    that by answering 4xx).

    Fix: answer through `pages.common.redirect_after_error`, or call
    `session.rollback()` in the `except` body, or re-raise. Only POST/PUT/
    PATCH/DELETE handlers are checked: a GET handler writes nothing.
    """
    from marketplace import errors

    domain = {
        name
        for name, obj in vars(errors).items()
        if isinstance(obj, type) and issubclass(obj, errors.DomainError)
    }
    assert {"DomainError", "NotFoundError"} <= domain
    writes = {"post", "put", "patch", "delete"}
    checked = 0
    bad: list[str] = []
    for path in sorted((SRC / "pages").glob("*.py")):
        for fn in ast.walk(_parse(path)):
            if not isinstance(fn, ast.FunctionDef):
                continue
            is_write = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in writes
                for d in fn.decorator_list
            )
            if not is_write:
                continue
            handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
            # A handler nested in another handler runs after the outer one's
            # rollback (which is itself checked), so it is covered by it.
            nested = {
                id(inner)
                for outer in handlers
                for stmt in outer.body
                for inner in ast.walk(stmt)
                if isinstance(inner, ast.ExceptHandler)
            }
            for handler in handlers:
                if id(handler) in nested or not _except_type_names(handler) & domain:
                    continue
                checked += 1
                ok = any(
                    isinstance(n, ast.Raise)
                    or (isinstance(n, ast.Call) and _calls_named(n, "redirect_after_error"))
                    or (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "rollback"
                    )
                    for stmt in handler.body
                    for n in ast.walk(stmt)
                )
                if not ok:
                    bad.append(f"{path.relative_to(ROOT)}:{handler.lineno} in {fn.name}()")
    assert checked, "no DomainError handlers found in pages/; update this test if they moved"
    assert not bad, (
        "page handler catches a DomainError without rolling back (a 303 would commit the "
        "service's partial writes); use redirect_after_error, session.rollback(), or "
        f"re-raise:\n  {_fmt(bad)}"
    )


def _calls_named(call: ast.Call, name: str) -> bool:
    f = call.func
    return (isinstance(f, ast.Name) and f.id == name) or (
        isinstance(f, ast.Attribute) and f.attr == name
    )


def test_every_route_uses_commit_route() -> None:
    """INVARIANT: every router sets `route_class=CommitRoute`.

    `db._assert_commit_routes` enforces this at import; this test pins that the
    guard still covers every route (a new router, page or health route must
    opt in) so weakening the guard itself fails here. Fix: construct the
    router with `route_class=CommitRoute`.
    """
    from marketplace.db import CommitRoute

    exempt = {"/healthz"}
    routes = _api_routes()
    assert len(routes) > 50
    bad = [r.path for r in routes if not isinstance(r, CommitRoute) and r.path not in exempt]
    assert not bad, f"routes without CommitRoute: {bad}"


def test_sync_db_work_stays_off_the_event_loop() -> None:
    """INVARIANT: webhook (and all) DB work stays off the event loop.

    Inside an `async def` body, no direct `session`/`db` method call that hits
    the database; sync work must go through `asyncio.to_thread` /
    `run_in_threadpool` in a plain `def`. Fix: move the logic into a sync
    function and await `asyncio.to_thread(fn, ...)`.
    """
    db_methods = {
        "execute",
        "scalar",
        "scalars",
        "get",
        "commit",
        "flush",
        "add",
        "delete",
        "query",
    }
    session_names = {"session", "db", "sess"}
    bad: list[str] = []
    async_defs = 0
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            async_defs += 1
            stack: list[ast.AST] = list(node.body)
            while stack:
                sub = stack.pop()
                if isinstance(sub, ast.FunctionDef | ast.Lambda):
                    continue  # nested sync function: runs wherever it's dispatched
                stack.extend(ast.iter_child_nodes(sub))
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in db_methods
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id in session_names
                ):
                    call = f"{sub.func.value.id}.{sub.func.attr}"
                    bad.append(f"{path.relative_to(ROOT)}:{sub.lineno}: {call}")
    assert async_defs, "no async functions found"
    assert not bad, f"blocking DB call on the event loop:\n  {_fmt(bad)}"


def test_async_webhook_handlers_offload_to_a_thread() -> None:
    """INVARIANT: webhook DB work stays off the event loop.

    An `async def` route whose path ends in `/webhook` must hand its work to
    `asyncio.to_thread` / `run_in_threadpool` (or be a plain `def`, which
    Starlette already runs in the threadpool). Fix: keep the DB work in a sync
    function and `await asyncio.to_thread(fn, ...)` from the coroutine.
    """
    import inspect
    import textwrap

    checked = 0
    bad: list[str] = []
    for route in _api_routes():
        if not route.path.endswith("/webhook"):
            continue
        checked += 1
        if not inspect.iscoroutinefunction(route.endpoint):
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
        offloads = any(
            _calls(n, "to_thread", "run_in_threadpool", "run_sync") for n in ast.walk(tree)
        )
        if not offloads:
            bad.append(route.path)
    assert checked, "no webhook route found; update this test if it moved"
    assert not bad, f"async webhook handler does not offload: {bad}"


def test_booked_ledger_rows_are_never_updated_or_deleted_in_bulk() -> None:
    """INVARIANT: `Transaction` rows are immutable and `adjustments` is append-only.

    No `update(Transaction|Adjustment)` / `delete(Transaction|Adjustment)`
    statement anywhere. Fix: append a new `Adjustment` row (refund / clawback /
    chargeback_*) instead of editing history.
    """
    ledger = {"Transaction", "Adjustment"}
    bad: list[str] = []
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and _calls(node, "update", "delete")
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in ledger
            ):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.args[0].id}")
    assert not bad, f"ledger mutated:\n  {_fmt(bad)}"


# --------------------------------------------------- logging & notifications


_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_SENSITIVE_ATTRS = {"headers", "cookies", "query_string", "query_params", "authorization"}
_SENSITIVE_STRINGS = {"headers", "cookie", "authorization", "query_string", "body", "password"}


def test_log_calls_never_carry_headers_cookies_bodies_or_queries() -> None:
    """INVARIANT: access logs (and every log line) never carry headers, bodies, or
    query strings; bearer tokens live in headers, reset tokens in bodies.

    Fix: log method/path/status/duration/request-id only. If you need a header
    value for logic, read it, but do not pass it to a logger or `extra=`.
    """
    bad: list[str] = []
    checked = 0
    for path in _py_files():
        if path.name == "mail.py":
            # The dev-only console adapter exists to print the email (links and
            # all) to the log; it is replaced by SMTP in any real deployment.
            continue
        for node in ast.walk(_parse(path)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
                and isinstance(node.func.value, ast.Name)
                and "log" in node.func.value.id.lower()
            ):
                continue
            checked += 1
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in _SENSITIVE_ATTRS:
                    bad.append(f"{path.relative_to(ROOT)}:{sub.lineno}: logs .{sub.attr}")
                elif (
                    isinstance(sub, ast.Constant)
                    and isinstance(sub.value, str)
                    and sub.value.lower() in _SENSITIVE_STRINGS
                ):
                    bad.append(f"{path.relative_to(ROOT)}:{sub.lineno}: logs key {sub.value!r}")
                elif isinstance(sub, ast.Name) and sub.id in {"body", "raw_token", "password"}:
                    bad.append(f"{path.relative_to(ROOT)}:{sub.lineno}: logs {sub.id}")
    assert checked > 5
    assert not bad, f"sensitive data reaches a log line:\n  {_fmt(bad)}"


def test_access_log_lives_in_observability_and_is_header_free() -> None:
    """INVARIANT: the access logger emits method/path/status/duration/request-id only.

    Fix: do not add fields to the `marketplace.access` log call; strip the query
    component from `path`.
    """
    obs = _modules_named("observability")
    assert obs
    allowed_extra = {"method", "path", "status", "duration_ms", "request_id"}
    bad: list[str] = []
    found = False
    for path in obs:
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "access_logger"
            ):
                found = True
                for kw in node.keywords:
                    if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                        for key in kw.value.keys:
                            if not (isinstance(key, ast.Constant) and key.value in allowed_extra):
                                shown = ast.unparse(key) if key else "**spread"
                                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: extra {shown}")
    assert found, "access_logger call not found; update this test if it moved"
    assert not bad, f"access log carries an unapproved field:\n  {_fmt(bad)}"


def test_mail_is_sent_only_from_known_modules() -> None:
    """INVARIANT: notifications are enqueue-only inside the domain transaction;
    sends happen only in `notifications.drain_once`.

    Today the only modules allowed to call a mail sender's `.send(...)` are
    `notifications` (the drain) and `auth` (the verification/reset link, whose
    raw token must reach the user once and is never persisted; documented
    exception). Fix: call `notifications.enqueue(...)` from endpoints.
    """
    allowed = {"notifications", "auth", "mail"}
    bad: list[str] = []
    for path in _py_files():
        top = path.relative_to(SRC).parts[0].removesuffix(".py")
        if top in allowed:
            continue
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send"
                and isinstance(node.func.value, ast.Name)
                and "mail" in node.func.value.id.lower()
            ):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: mail send outside outbox")
    assert not bad, f"mail sent outside the outbox:\n  {_fmt(bad)}"


# ------------------------------------------------------------- migrations


def test_migrations_do_not_import_the_app() -> None:
    """INVARIANT: migrations don't import app internals (they render `UTCDateTime`
    as plain `DateTime`).

    A revision must be reproducible forever; importing `marketplace` ties it to
    code that will change. Fix: inline the DDL types in the revision. (`env.py`
    legitimately imports `Base` metadata and is not a revision.)
    """
    versions = ROOT / "migrations" / "versions"
    files = sorted(versions.glob("*.py"))
    assert files, "no migration revisions found"
    bad: list[str] = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            lineno = 0
            if isinstance(node, ast.Import):
                names, lineno = [a.name for a in node.names], node.lineno
            elif isinstance(node, ast.ImportFrom):
                names, lineno = [node.module or ""], node.lineno
            for name in names:
                if _is_mod(name, PKG):
                    bad.append(f"{path.name}:{lineno}: imports {name}")
    assert not bad, f"migration imports app code:\n  {_fmt(bad)}"


# --------------------------------------------------------------- toolchain


def test_pyright_stays_strict() -> None:
    """INVARIANT: pyright strict across `src/` and `tests/`; do not drop to basic.

    Fix: fix the type error instead of loosening `pyproject.toml`.
    """
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pyright"]
    assert cfg["typeCheckingMode"] == "strict"
    assert {"src", "tests"} <= set(cfg["include"])
    assert not cfg.get("exclude"), "pyright must not exclude paths from strict checking"


def test_mcp_server_is_an_http_only_client() -> None:
    """INVARIANT: `marketplace_mcp` reaches the marketplace only over HTTP.

    Authentication, role checks, audit rows and commit ordering live in the
    admin API. A tool that imported the app or its database layer would skip
    all of them. Fix: call the `/v1/admin/*` endpoint through
    `marketplace_mcp.client` instead of importing from `marketplace`.
    """
    mcp_src = ROOT / "src" / "marketplace_mcp"
    bad: list[str] = []
    for path in sorted(mcp_src.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] in {PKG, "sqlalchemy"}:
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {name}")
    assert not bad, "marketplace_mcp must not import the app or the DB layer:\n" + "\n".join(bad)
