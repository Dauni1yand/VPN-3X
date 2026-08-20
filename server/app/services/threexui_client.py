"""Thin async client for a node's 3x-ui panel.

Per PLAN.md section 4: the main server authenticates with the panel's
login/password (session cookie), not a per-node API token, because part of
the 3x-ui v3.6.0 API (e.g. /panel/api/openapi.json) requires an authenticated
session rather than a token.

IMPORTANT: the exact endpoint paths below are the standard 3x-ui panel API
paths as of v3.6.0, but MUST be verified against a real panel instance during
Etap 0 (R&D) before relying on them in production -- panel forks/versions
have shifted route names before, and we should not assume they are stable
without a live check against the pinned 3x-ui version.
"""

from __future__ import annotations

import httpx

from app.core.security import decrypt_secret


class ThreeXUIAuthError(Exception):
    pass


class ThreeXUIClient:
    def __init__(self, base_url: str, login: str, password: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._login = login
        self._password = password
        # A single AsyncClient keeps the session cookie across requests.
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)
        self._authenticated = False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login_request(self) -> None:
        resp = await self._http.post(
            "/login", data={"username": self._login, "password": self._password}
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", False):
            raise ThreeXUIAuthError(f"3x-ui login failed: {body}")
        self._authenticated = True

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._authenticated:
            await self._login_request()

        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code == 401:
            # Session expired -- relogin once and retry.
            await self._login_request()
            resp = await self._http.request(method, path, **kwargs)

        resp.raise_for_status()
        return resp

    async def list_inbounds(self) -> list[dict]:
        resp = await self._request("GET", "/panel/api/inbounds/list")
        return resp.json().get("obj", [])

    async def add_inbound(self, payload: dict) -> dict:
        resp = await self._request("POST", "/panel/api/inbounds/add", json=payload)
        return resp.json().get("obj", {})

    async def update_inbound(self, inbound_id: int, payload: dict) -> dict:
        """Used to rotate an inbound's REALITY dest/SNI in place, without
        recreating it (and therefore without losing its existing clients)."""
        resp = await self._request("POST", f"/panel/api/inbounds/update/{inbound_id}", json=payload)
        return resp.json().get("obj", {})

    async def add_client(self, inbound_id: int, client_settings: dict) -> dict:
        resp = await self._request(
            "POST",
            "/panel/api/inbounds/addClient",
            json={"id": inbound_id, "settings": client_settings},
        )
        return resp.json()

    async def delete_client(self, inbound_id: int, client_uuid: str) -> dict:
        resp = await self._request(
            "POST", f"/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}"
        )
        return resp.json()

    async def get_client_traffic_by_email(self, email: str) -> dict:
        resp = await self._request("GET", f"/panel/api/inbounds/getClientTraffics/{email}")
        return resp.json().get("obj", {})

    async def get_online_clients(self) -> list[str]:
        """Live online status, used by the health-check worker."""
        resp = await self._request("POST", "/panel/api/inbounds/onlines")
        return resp.json().get("obj", [])


# node.id -> (credential fingerprint, client). Per PLAN.md section 4: "не
# токен ноды" -- login/password with a session cookie, reused across calls
# rather than re-logging in every time (the previous per-call
# construct-then-aclose pattern meant e.g. the health-check worker
# re-authenticated to every node every single minute). Keyed on a
# fingerprint of the node's current credentials/URL, not just node.id, so a
# password rotation or URL change transparently gets a fresh client instead
# of reusing a stale, now-wrong one.
_client_cache: dict[str, tuple[str, "ThreeXUIClient"]] = {}


def get_pooled_client(node) -> "ThreeXUIClient":
    fingerprint = f"{node.panel_base_url}|{node.panel_login}|{node.panel_password_encrypted}"
    cached = _client_cache.get(node.id)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    client = ThreeXUIClient(
        base_url=node.panel_base_url,
        login=node.panel_login,
        password=decrypt_secret(node.panel_password_encrypted),
    )
    _client_cache[node.id] = (fingerprint, client)
    return client
