"""Hasura GraphQL helper for charger discovery."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp


class HasuraError(Exception):
    """Base Hasura error."""

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class HasuraAuthError(HasuraError):
    """Auth error for Hasura."""


class HasuraClient:
    """Simple GraphQL client for Hasura."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        url: str,
        token: str | None,
        request_timeout: int,
        verify_ssl: bool,
    ) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._token = token
        self._request_timeout = request_timeout
        self._verify_ssl = verify_ssl

    async def ping(self) -> None:
        """Validate Hasura endpoint and auth."""
        await self.query("query { __typename }")

    async def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.lstrip().startswith(("query", "{")) or re.search(r"\b(mutation|subscription)\b", query):
            raise HasuraError("Only read-only GraphQL queries are permitted")
        headers = {
            "Content-Type": "application/json",
        }
        if self._token:
            headers["x-hasura-admin-secret"] = self._token

        payload = {"query": query, "variables": variables or {}}

        timeout = aiohttp.ClientTimeout(total=self._request_timeout)
        try:
            async with self._session.post(
                self._url,
                json=payload,
                headers=headers,
                timeout=timeout,
                ssl=self._verify_ssl,
                allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    raise HasuraAuthError("Hasura authentication failed")
                if 300 <= resp.status < 400:
                    raise HasuraError("Hasura redirect refused")
                if resp.status >= 400:
                    raise HasuraError(f"Hasura HTTP error: {resp.status}")
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    raise HasuraError("Invalid Hasura response")
                if "errors" in data:
                    errors = data["errors"]
                    if isinstance(errors, list) and any(
                        isinstance(item, dict) and
                        isinstance(item.get("extensions"), dict) and
                        item.get("extensions", {}).get("code") in
                        ("access-denied", "invalid-jwt", "invalid-headers")
                        for item in errors
                    ):
                        raise HasuraAuthError("Hasura authentication failed")
                    raise HasuraError("Hasura query failed", errors=errors if isinstance(errors, list) else [])
                if not isinstance(data.get("data"), dict):
                    raise HasuraError("Missing Hasura data")
                return data
        except asyncio.TimeoutError as err:
            raise HasuraError("Hasura request timed out") from err
        except aiohttp.ClientError as err:
            raise HasuraError("Hasura transport error") from err
        except (ValueError, TypeError) as err:
            raise HasuraError("Invalid Hasura response") from err
