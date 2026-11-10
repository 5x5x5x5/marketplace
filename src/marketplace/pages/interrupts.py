"""Page dependency failures that stop a request before its handler runs.

A dependency (login required, wrong role, a bad CSRF token) cannot return a
response, so it raises one of these; `api.py` registers
`page_interrupt_handler` for the base class. Raised before the handler, so
`CommitRoute` never commits anything for them."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


class PageInterrupt(Exception):  # noqa: N818 -- control flow, not an error
    def response(self, request: Request) -> Response:
        raise NotImplementedError


class LoginRequired(PageInterrupt):
    def response(self, request: Request) -> Response:
        return RedirectResponse("/login", status_code=303)


class PageForbidden(PageInterrupt):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def response(self, request: Request) -> Response:
        # No session in an exception handler, so the nav renders logged out;
        # the message says what went wrong.
        return templates.TemplateResponse(
            request,
            "error.html",
            {"claims": None, "current_user": None, "status_code": 403, "message": self.message},
            status_code=403,
            headers={"Referrer-Policy": "same-origin", "Cache-Control": "no-store"},
        )


def page_interrupt_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, PageInterrupt)  # registered for PageInterrupt only
    return exc.response(request)
