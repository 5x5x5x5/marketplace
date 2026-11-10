"""Thin async client for /v1/admin. The only module that touches the token."""

from typing import Any

import httpx

DETAIL_LIMIT = 500


class ApiError(Exception):
    """An API or transport failure, already phrased for an agent to read."""


def _detail(resp: httpx.Response) -> str:
    try:
        body: Any = resp.json()
    except ValueError:
        return resp.reason_phrase
    detail: Any = body.get("detail", body) if isinstance(body, dict) else body  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(detail, str):
        text = detail
    else:
        # Validation errors arrive as a list of dicts; compact them.
        text = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}"  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
            if isinstance(e, dict)
            else str(e)  # pyright: ignore[reportUnknownArgumentType]
            for e in (detail if isinstance(detail, list) else [detail])  # pyright: ignore[reportUnknownVariableType]
        )
    return text[:DETAIL_LIMIT]


class AdminClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=timeout,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.request(method, path, params=clean, json=json)
        except httpx.HTTPError as exc:
            # Type name only: exception text can embed request details.
            raise ApiError(
                f"cannot reach the marketplace API ({type(exc).__name__}); is it running?"
            ) from None
        if resp.status_code >= 400:
            hint = ""
            if resp.status_code in (401, 403):
                hint = " (admin token missing, expired or not an admin session)"
            raise ApiError(f"API returned {resp.status_code}{hint}: {_detail(resp)}")
        return resp.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any = None) -> Any:
        return await self.request("POST", path, json=json)

    async def put(self, path: str, json: Any) -> Any:
        return await self.request("PUT", path, json=json)

    async def aclose(self) -> None:
        await self._http.aclose()
