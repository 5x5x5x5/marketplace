"""Environment configuration. The token is read once and never rendered."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

TOKEN_VAR = "MARKETPLACE_ADMIN_TOKEN"
URL_VAR = "MARKETPLACE_API_URL"
WRITES_VAR = "MARKETPLACE_MCP_ENABLE_WRITES"
_TRUTHY = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str
    # repr=False: a stray log of the settings object must not leak the token.
    token: str = field(repr=False)
    writes_enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        token = e.get(TOKEN_VAR, "").strip()
        if not token:
            raise ConfigError(
                f"{TOKEN_VAR} is not set. Log in as an admin (POST /v1/auth/login with "
                "role admin, or run scripts/seed.py) and export the session token."
            )
        base_url = e.get(URL_VAR, "http://localhost:8000").strip().rstrip("/")
        writes = e.get(WRITES_VAR, "").strip().lower() in _TRUTHY
        return cls(base_url=base_url, token=token, writes_enabled=writes)
