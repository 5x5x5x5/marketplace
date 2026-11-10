"""Service-layer domain errors render exactly like the HTTPException they replaced.

Routes used to raise `HTTPException` directly; services now raise
`errors.DomainError` subclasses and `api.py` maps them in one handler. These
tests pin that the mapping is wire-identical (status, JSON body, headers) and
that it is the handler actually installed on the app.
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from marketplace import api, errors

_ALL = [
    errors.ForbiddenError,
    errors.NotFoundError,
    errors.ConflictError,
    errors.GoneError,
    errors.UnprocessableError,
    errors.InternalError,
    errors.ProviderUnavailableError,
    errors.OverloadedError,
]


def test_app_installs_the_domain_error_handler() -> None:
    handler = api.app.exception_handlers[errors.DomainError]
    assert handler is api._domain_error_response  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("cls", _ALL)
def test_domain_error_renders_like_http_exception(cls: type[errors.DomainError]) -> None:
    probe = FastAPI()
    probe.add_exception_handler(
        errors.DomainError,
        api._domain_error_response,  # pyright: ignore[reportPrivateUsage]
    )
    headers = {"Retry-After": "1"}

    def domain() -> None:
        raise cls("something specific", headers=headers)

    def http() -> None:
        raise HTTPException(
            status_code=cls.status_code, detail="something specific", headers=headers
        )

    probe.add_api_route("/domain", domain)
    probe.add_api_route("/http", http)
    client = TestClient(probe, raise_server_exceptions=False)
    got, want = client.get("/domain"), client.get("/http")
    assert got.status_code == want.status_code == cls.status_code
    assert got.json() == want.json() == {"detail": "something specific"}
    assert dict(got.headers) == dict(want.headers)


def test_status_codes_are_the_ones_routes_used() -> None:
    assert {cls.__name__: cls.status_code for cls in _ALL} == {
        "ForbiddenError": 403,
        "NotFoundError": 404,
        "ConflictError": 409,
        "GoneError": 410,
        "UnprocessableError": 422,
        "InternalError": 500,
        "ProviderUnavailableError": 502,
        "OverloadedError": 503,
    }
