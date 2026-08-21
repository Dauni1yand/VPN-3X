"""Async client for a 3x-ui node."""

from __future__ import annotations

import httpx

from app.core.security import decrypt_secret


class ThreeXUIAuthError(Exception):
    pass


class ThreeXUIAPIError(Exception):
    pass


class ThreeXUIClient:

    def __init__(
        self,
        base_url: str,
        login: str,
        password: str,
        timeout: float = 20.0,
    ) -> None:

        base_url = base_url.rstrip("/") + "/"

        self._base_url = base_url
        self._login = login
        self._password = password

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
            },
        )

        self._authenticated = False
        self._csrf_token: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login_request(self) -> None:

        # Login itself does not require an existing CSRF token.
        response = await self._http.post(
            "/login",
            data={
                "username": self._login,
                "password": self._password,
            },
        )

        response.raise_for_status()

        try:
            body = response.json()
        except ValueError as exc:
            raise ThreeXUIAuthError(
                f"3x-ui returned non-JSON login response: "
                f"{response.text[:500]}"
            ) from exc

        if not body.get("success", False):

            raise ThreeXUIAuthError(
                f"3x-ui login failed: "
                f"{body.get('msg') or body}"
            )

        # Modern 3x-ui requires this token for cookie-based
        # unsafe requests.
        csrf_response = await self._http.get(
            "/csrf-token"
        )

        csrf_response.raise_for_status()

        try:
            csrf_body = csrf_response.json()
        except ValueError as exc:
            raise ThreeXUIAuthError(
                "3x-ui returned invalid CSRF response"
            ) from exc

        csrf_token = csrf_body.get("obj")

        if not isinstance(csrf_token, str) or not csrf_token:
            raise ThreeXUIAuthError(
                f"3x-ui did not return a CSRF token: "
                f"{csrf_body}"
            )

        self._csrf_token = csrf_token

        self._http.headers["X-CSRF-Token"] = csrf_token

        self._authenticated = True

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:

        if not self._authenticated:
            await self._login_request()

        response = await self._http.request(
            method,
            path,
            **kwargs,
        )

        # Session or CSRF token may have expired.
        if response.status_code in (401, 403):

            self._authenticated = False
            self._csrf_token = None

            self._http.headers.pop(
                "X-CSRF-Token",
                None,
            )

            await self._login_request()

            response = await self._http.request(
                method,
                path,
                **kwargs,
            )

        response.raise_for_status()

        return response

    @staticmethod
    def _body(response: httpx.Response) -> dict:

        try:
            body = response.json()
        except ValueError as exc:
            raise ThreeXUIAPIError(
                f"3x-ui returned invalid JSON: "
                f"{response.text[:1000]}"
            ) from exc

        if not body.get("success", False):

            raise ThreeXUIAPIError(
                body.get("msg")
                or body.get("message")
                or str(body)
            )

        return body

    async def list_inbounds(self) -> list[dict]:

        response = await self._request(
            "GET",
            "/panel/api/inbounds/list",
        )

        body = self._body(response)

        obj = body.get("obj", [])

        if not isinstance(obj, list):
            raise ThreeXUIAPIError(
                f"Unexpected inbounds response: {body}"
            )

        return obj

    async def add_inbound(
        self,
        payload: dict,
    ) -> dict:

        response = await self._request(
            "POST",
            "/panel/api/inbounds/add",
            json=payload,
        )

        body = self._body(response)

        obj = body.get("obj")

        if not isinstance(obj, dict):
            raise ThreeXUIAPIError(
                f"3x-ui did not return created inbound: {body}"
            )

        if not obj.get("id"):
            raise ThreeXUIAPIError(
                f"3x-ui returned inbound without id: {body}"
            )

        return obj

    async def update_inbound(
        self,
        inbound_id: int,
        payload: dict,
    ) -> dict:

        response = await self._request(
            "POST",
            f"/panel/api/inbounds/update/{inbound_id}",
            json=payload,
        )

        body = self._body(response)

        obj = body.get("obj")

        if not isinstance(obj, dict):
            raise ThreeXUIAPIError(
                f"3x-ui returned invalid update response: {body}"
            )

        return obj

    async def add_client(
        self,
        inbound_id: int,
        client: dict,
    ) -> dict:

        response = await self._request(
            "POST",
            "/panel/api/clients/add",
            json={
                "client": client,
                "inboundIds": [inbound_id],
            },
        )

        return self._body(response)

    async def delete_client(
        self,
        inbound_id: int,
        client_uuid: str,
    ) -> dict:

        response = await self._request(
            "POST",
            f"/panel/api/inbounds/"
            f"{inbound_id}/delClient/"
            f"{client_uuid}",
        )

        return self._body(response)

    async def get_client_traffic_by_email(
        self,
        email: str,
    ) -> dict:

        response = await self._request(
            "GET",
            f"/panel/api/inbounds/"
            f"getClientTraffics/{email}",
        )

        body = self._body(response)

        return body.get("obj", {})

    async def get_online_clients(self) -> list[str]:

        response = await self._request(
            "POST",
            "/panel/api/clients/onlines",
        )

        body = self._body(response)

        obj = body.get("obj", [])

        if not isinstance(obj, list):
            return []

        return obj


_client_cache: dict[
    str,
    tuple[str, ThreeXUIClient],
] = {}


def get_pooled_client(node) -> ThreeXUIClient:

    fingerprint = (
        f"{node.panel_base_url}|"
        f"{node.panel_login}|"
        f"{node.panel_password_encrypted}"
    )

    cached = _client_cache.get(node.id)

    if cached is not None:
        cached_fingerprint, client = cached

        if cached_fingerprint == fingerprint:
            return client

    client = ThreeXUIClient(
        base_url=node.panel_base_url,
        login=node.panel_login,
        password=decrypt_secret(
            node.panel_password_encrypted
        ),
    )

    _client_cache[node.id] = (
        fingerprint,
        client,
    )

    return client