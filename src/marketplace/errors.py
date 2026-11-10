"""Domain errors raised by the service layer.

Services never import FastAPI, so they cannot raise `HTTPException`. They
raise one of these instead, and `api.py` maps every `DomainError` to the
same JSON response an `HTTPException` with the same status code, `detail`
and headers would produce. That mapping is the only place a domain error
becomes HTTP; a caller that is not the JSON API (a page handler, a script)
catches `DomainError` and decides for itself.

Like an `HTTPException`, a raised `DomainError` never commits: `CommitRoute`
only commits responses under 400. A service that must persist something
ahead of an error commits explicitly before raising.
"""

from typing import ClassVar


class DomainError(Exception):
    """Base: an expected, client-visible failure with an HTTP status."""

    status_code: ClassVar[int] = 400

    def __init__(self, detail: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers


class ForbiddenError(DomainError):
    """The principal may not do this (suspended, not assigned, not a party)."""

    status_code = 403


class NotFoundError(DomainError):
    """The object does not exist, or is not visible to this principal."""

    status_code = 404


class ConflictError(DomainError):
    """The object is in the wrong state for this action, or a duplicate."""

    status_code = 409


class GoneError(DomainError):
    """The object existed but has expired or been consumed."""

    status_code = 410


class UnprocessableError(DomainError):
    """The request is well-formed but its values are not acceptable."""

    status_code = 422


class InternalError(DomainError):
    """A stored invariant does not hold (data that should be impossible)."""

    status_code = 500


class ProviderUnavailableError(DomainError):
    """The payment provider call failed; the caller may retry."""

    status_code = 502


class OverloadedError(DomainError):
    """A capacity backstop tripped; the caller may retry shortly."""

    status_code = 503
