"""Talks to the Remnawave panel's REST API.

Remnawave replaces 3x-ui, but it is not a drop-in for it: 3x-ui is one panel
per VPS, so the main server held credentials for N panels and created an
inbound and a client on each. Remnawave is a single central panel that owns
every node, so there is exactly one set of credentials, and the objects are
different:

  * a **config profile** is an Xray config; its `inbounds` are what nodes
    actually serve,
  * an **internal squad** is a named bundle of inbounds,
  * a **node** is a VPS running the remnawave-node container, bound to one
    config profile and the subset of its inbounds it should serve,
  * a **user** is a person, not a per-node credential. Their access is the
    union of the squads they are in, and they get one subscription that
    covers all of it.

Base URL is the panel root; every route lives under /api. Auth is a Bearer
token minted in the panel's "API Tokens" section.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# The panel answers slowly while it pushes a config to every node, and node
# registration in particular waits on the node's own handshake.
DEFAULT_TIMEOUT = 30.0


class RemnawaveError(RuntimeError):
    """The panel refused a call, or answered something we cannot use."""


class RemnawaveClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        caddy_token: str | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        # The SDK appends /api when the configured URL lacks it, and admins
        # copy the URL from their browser either way. Accept both rather
        # than making a trailing path segment the difference between a
        # working deploy and 404s everywhere.
        if not base.endswith("/api"):
            base += "/api"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Panels behind the documented Caddy/nginx guard reject requests
            # that do not look like the panel's own frontend.
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "https",
        }
        if caddy_token:
            headers["X-Api-Key"] = caddy_token

        self._http = httpx.AsyncClient(base_url=base, headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ---------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RemnawaveError(
                f"панель Remnawave недоступна ({type(exc).__name__}: {exc})"
            ) from exc

        if response.status_code >= 400:
            # The panel reports real problems as a JSON body with a message;
            # surfacing that beats a bare status code, since it usually says
            # exactly which field it rejected.
            raise RemnawaveError(
                f"{method} {path} -> {response.status_code}: {_message_of(response)}"
            )

        if not response.content:
            return None

        try:
            body = response.json()
        except ValueError as exc:
            raise RemnawaveError(
                f"{method} {path} вернул не JSON: {response.text[:500]}"
            ) from exc

        # Everything is wrapped in {"response": ...}.
        return body.get("response", body) if isinstance(body, dict) else body

    # -- system -----------------------------------------------------------

    async def health(self) -> None:
        """Cheapest authenticated round trip that proves the token works."""
        await self._request("GET", "/config-profiles")

    async def get_pubkey(self) -> str:
        """The panel's public key, which each node needs as its SSL_CERT.

        This is what lets a node trust config pushed to it, and it is the
        one secret node bootstrap has to carry onto the VPS.
        """
        body = await self._request("GET", "/keygen")
        key = (body or {}).get("pubKey")
        if not key:
            raise RemnawaveError("панель не вернула pubKey для ноды")
        return str(key)

    # -- config profiles --------------------------------------------------

    async def create_config_profile(self, name: str, config: dict) -> dict:
        return await self._request(
            "POST", "/config-profiles", json={"name": name, "config": config}
        )

    async def update_config_profile(self, uuid: str, *, config: dict) -> dict:
        return await self._request(
            "PATCH", "/config-profiles", json={"uuid": uuid, "config": config}
        )

    async def get_config_profile(self, uuid: str) -> dict:
        return await self._request("GET", f"/config-profiles/{uuid}")

    async def list_config_profiles(self) -> list[dict]:
        body = await self._request("GET", "/config-profiles")
        return _as_list(body, "configProfiles")

    async def delete_config_profile(self, uuid: str) -> None:
        await self._request("DELETE", f"/config-profiles/{uuid}")

    async def get_profile_inbounds(self, uuid: str) -> list[dict]:
        body = await self._request("GET", f"/config-profiles/{uuid}/inbounds")
        return _as_list(body, "inbounds")

    # -- internal squads --------------------------------------------------

    async def create_internal_squad(self, name: str, inbounds: list[str]) -> dict:
        return await self._request(
            "POST", "/internal-squads", json={"name": name, "inbounds": inbounds}
        )

    async def update_internal_squad(self, uuid: str, inbounds: list[str]) -> dict:
        return await self._request(
            "PATCH", "/internal-squads", json={"uuid": uuid, "inbounds": inbounds}
        )

    async def list_internal_squads(self) -> list[dict]:
        body = await self._request("GET", "/internal-squads")
        return _as_list(body, "internalSquads")

    async def delete_internal_squad(self, uuid: str) -> None:
        await self._request("DELETE", f"/internal-squads/{uuid}")

    # -- nodes ------------------------------------------------------------

    async def create_node(
        self,
        *,
        name: str,
        address: str,
        port: int,
        config_profile_uuid: str,
        active_inbounds: list[str],
        country_code: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "name": name,
            "address": address,
            "port": port,
            "configProfile": {
                "activeConfigProfileUuid": config_profile_uuid,
                "activeInbounds": active_inbounds,
            },
            # Two letters or the panel's own placeholder; it validates the
            # length and rejects anything longer.
            "countryCode": (country_code or "XX").upper()[:2],
            "isTrafficTrackingActive": False,
        }
        return await self._request("POST", "/nodes", json=payload)

    async def get_node(self, uuid: str) -> dict:
        return await self._request("GET", f"/nodes/{uuid}")

    async def list_nodes(self) -> list[dict]:
        body = await self._request("GET", "/nodes")
        return body if isinstance(body, list) else _as_list(body, "nodes")

    async def update_node(self, uuid: str, **fields) -> dict:
        return await self._request("PATCH", "/nodes", json={"uuid": uuid, **fields})

    async def delete_node(self, uuid: str) -> None:
        await self._request("DELETE", f"/nodes/{uuid}")

    async def enable_node(self, uuid: str) -> dict:
        return await self._request("POST", f"/nodes/{uuid}/actions/enable")

    async def disable_node(self, uuid: str) -> dict:
        return await self._request("POST", f"/nodes/{uuid}/actions/disable")

    async def restart_node(self, uuid: str) -> None:
        await self._request("POST", f"/nodes/{uuid}/actions/restart")

    # -- users ------------------------------------------------------------

    async def create_user(
        self,
        *,
        username: str,
        expire_at: str,
        internal_squads: list[str],
        telegram_id: int | None = None,
        description: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "username": username,
            "expireAt": expire_at,
            "status": "ACTIVE",
            "activeInternalSquads": internal_squads,
            "trafficLimitStrategy": "NO_RESET",
        }
        if telegram_id is not None:
            payload["telegramId"] = telegram_id
        if description:
            payload["description"] = description
        return await self._request("POST", "/users", json=payload)

    async def update_user(self, uuid: str, **fields) -> dict:
        return await self._request("PATCH", "/users", json={"uuid": uuid, **fields})

    async def get_user(self, uuid: str) -> dict:
        return await self._request("GET", f"/users/{uuid}")

    async def get_user_by_username(self, username: str) -> dict | None:
        try:
            return await self._request("GET", f"/users/by-username/{username}")
        except RemnawaveError as exc:
            if "404" in str(exc):
                return None
            raise

    async def delete_user(self, uuid: str) -> None:
        await self._request("DELETE", f"/users/{uuid}")

    async def disable_user(self, uuid: str) -> dict:
        return await self._request("POST", f"/users/{uuid}/actions/disable")

    async def enable_user(self, uuid: str) -> dict:
        return await self._request("POST", f"/users/{uuid}/actions/enable")

    # -- subscription -----------------------------------------------------

    async def get_subscription_links(self, short_uuid: str) -> list[str]:
        """The raw share links behind a user's subscription.

        Same principle we settled on with 3x-ui: the panel generates the
        link from the same config it pushes to the node, so a link it
        returns cannot disagree with what the node serves. Composing one
        here would put a second implementation of the format back in the
        path that matters.

        /sub/{shortUuid} answers with the client's own format -- base64 of
        newline-separated links for a generic client -- so ask for the raw
        list instead of guessing the encoding.
        """

        body = await self._request(
            "GET",
            f"/sub/{short_uuid}/info",
        )
        links = (body or {}).get("links")
        return [str(link) for link in links] if isinstance(links, list) else []


def _message_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:500]
    return str(body)[:500]


def _as_list(body: Any, key: str) -> list[dict]:
    """Collection responses are sometimes a bare list and sometimes wrapped
    under a named key alongside a `total`."""

    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        items = body.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


_client: RemnawaveClient | None = None


def get_remnawave() -> RemnawaveClient:
    """The deployment's single panel client.

    Pooled for the same reason the 3x-ui client was: a fresh httpx client
    per call meant a new TCP and TLS handshake on every node listing the bot
    renders. Unlike that one there is nothing to key the pool on -- there is
    exactly one panel.
    """

    global _client

    if not settings.remnawave_base_url or not settings.remnawave_token:
        raise RemnawaveError(
            "Remnawave не настроен: задайте REMNAWAVE_BASE_URL и REMNAWAVE_TOKEN в .env"
        )

    if _client is None:
        _client = RemnawaveClient(
            settings.remnawave_base_url,
            settings.remnawave_token,
            caddy_token=settings.remnawave_caddy_token or None,
        )

    return _client


async def close_remnawave() -> None:
    global _client

    if _client is not None:
        await _client.aclose()
        _client = None
