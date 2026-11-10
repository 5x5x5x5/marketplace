"""Server-rendered pages: the public storefront, account pages, and the buyer
and seller pages that book, pay, accept and complete.

`pages_router` is what `api.py` mounts. Every module's router uses
`CommitRoute`; pages call `services/` (and `catalog`, `auth`) and never
import `api` or `routers`."""

from fastapi import APIRouter
from fastapi.routing import APIRoute

from ..db import CommitRoute
from . import account, buyer, seller, storefront
from .interrupts import PageInterrupt, page_interrupt_handler

pages_router = APIRouter(route_class=CommitRoute, tags=["pages"])
for _module in (storefront, account, buyer, seller):
    pages_router.include_router(_module.router)

_PAGE_POSTS = [
    route
    for route in pages_router.routes
    if isinstance(route, APIRoute) and "POST" in route.methods
]


def is_page_post(path: str) -> bool:
    """True for a POST path served by the pages router. The idempotency
    middleware skips these: they answer with redirects and HTML, which its
    JSON replay store cannot represent, and their double-submit safety comes
    from the domain (single-use quotes, status guards, explicit target
    values), not from Idempotency-Key, which browsers never send."""
    return any(route.path_regex.match(path) for route in _PAGE_POSTS)


__all__ = ["PageInterrupt", "is_page_post", "page_interrupt_handler", "pages_router"]
