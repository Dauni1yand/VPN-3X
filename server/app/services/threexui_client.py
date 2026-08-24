"""Async client for a node's 3x-ui panel.

Auth: an API token is used when the node has one, otherwise a login session.

The token path is strongly preferred. In 3x-ui, `checkAPIAuth` accepts
`Authorization: Bearer <token>` and sets `api_authed`, and CSRFMiddleware
short-circuits on `api_authed` -- so a token needs no cookie, no CSRF token
and no re-login. The installer mints one at "install"/admin scope and writes
it to /etc/x-ui/install-result.env, which node_bootstrap reads back.

The session fallback exists only for nodes connected manually with just a
login/password. Note the ordering there: /login is itself behind
CSRFMiddleware and ValidateCSRFToken returns false when the session carries
no token, so the CSRF token must be fetched *before* posting to /login.
"""

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
        api_token: str | None = None,
        timeout: float = 20.0,
    ) -> None:

        base_url = base_url.rstrip("/") + "/"

        self._base_url = base_url
        self._login = login
        self._password = password
        self._api_token = api_token

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
            },
        )

        if api_token:
            self._http.headers["Authorization"] = f"Bearer {api_token}"

        self._authenticated = bool(api_token)
        self._csrf_token: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login_request(self) -> None:
        """Establishes a panel session. Only used when the node has no API
        token -- see the module docstring."""

        if self._api_token:
            # Nothing to do: the Bearer header is already set and the panel
            # treats it as authenticated on every request.
            self._authenticated = True
            return

        # Must come first: POST /login runs through CSRFMiddleware, and
        # ValidateCSRFToken fails closed when the session has no token yet.
        csrf_response = await self._http.get("/csrf-token")
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

        if response.status_code in (401, 403):

            if self._api_token:
                # The token was rejected. It is read from the node's
                # /etc/x-ui/install-result.env, which on a re-bootstrapped
                # box can be left over from an earlier install, so this is
                # recoverable rather than fatal: drop the token and fall
                # back to the login/password we also hold. Cleared first so
                # _login_request takes the session branch, and permanently
                # so every later call on this client stops presenting a
                # credential the panel has already refused.
                self._api_token = None
                self._http.headers.pop("Authorization", None)

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

    async def health(self) -> None:
        """Cheapest authenticated round trip the panel offers -- /list/slim
        returns inbounds without their client arrays or traffic stats, so a
        node with thousands of clients doesn't ship all of it every minute
        just to prove the panel is alive."""

        response = await self._request(
            "GET",
            "/panel/api/inbounds/list/slim",
        )

        self._body(response)

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

    async def get_client_links(
        self,
        email: str,
    ) -> list[str]:
        """The share links the node itself generates for this client.

        This is the panel's own generator (internal/sub/service.go,
        genVlessLink), reading the same inbound row xray is configured from,
        so a link it returns cannot disagree with what the node actually
        serves. That is the whole reason to prefer it over composing the URI
        here from our own copy of the parameters.

        The address in the link comes from the Host header of this request
        (controller.resolveHost), so it is whatever host we dialled the panel
        on -- the node's public IP, since panel_base_url is built from it.
        """

        response = await self._request(
            "GET",
            f"/panel/api/clients/links/{email}",
        )

        obj = self._body(response).get("obj") or []

        return [str(link) for link in obj if isinstance(link, str)] if isinstance(obj, list) else []

    async def get_inbound(
        self,
        inbound_id: int,
    ) -> dict:

        response = await self._request(
            "GET",
            f"/panel/api/inbounds/get/{inbound_id}",
        )

        obj = self._body(response).get("obj")

        if not isinstance(obj, dict):
            raise ThreeXUIAPIError(
                f"3x-ui returned no inbound #{inbound_id}: {obj!r}"
            )

        return obj

    async def delete_client(
        self,
        email: str,
    ) -> dict:
        """Clients are addressed by email, not by inbound id + UUID: 3x-ui
        exposes /panel/api/clients/del/{email} and has no
        /panel/api/inbounds/{id}/delClient/{uuid} route."""

        response = await self._request(
            "POST",
            f"/panel/api/clients/del/{email}",
        )

        return self._body(response)

    async def list_xray_versions(self) -> list[str]:
        """Xray-core releases the panel is willing to install, newest first.

        3x-ui filters this list to >= v26.6.27, so it can move xray forward
        but never below that floor -- worth knowing before assuming a
        downgrade is available as a remedy.
        """

        response = await self._request(
            "GET",
            "/panel/api/server/getXrayVersion",
        )

        body = self._body(response)

        obj = body.get("obj", [])

        return [str(v) for v in obj] if isinstance(obj, list) else []

    async def install_xray(self, version: str) -> None:
        """Downloads and switches the node to `version` (e.g. "v26.7.28"),
        restarting xray as part of it. Minutes, not seconds: the panel
        fetches the release from GitHub."""

        response = await self._request(
            "POST",
            f"/panel/api/server/installXray/{version}",
            timeout=300,
        )

        self._body(response)

    async def restart_xray(self) -> None:

        response = await self._request(
            "POST",
            "/panel/api/server/restartXrayService",
        )

        self._body(response)

    async def get_xray_logs(self, count: int = 50) -> list[str]:
        """Recent xray-core log lines from the node.

        The one place a REALITY handshake failure is visible: a client the
        server rejects is silently proxied to `dest` instead, so from the
        outside a refused config is indistinguishable from a working one.
        """

        response = await self._request(
            "POST",
            f"/panel/api/server/xraylogs/{int(count)}",
        )

        body = self._body(response)

        obj = body.get("obj", [])

        return [str(line) for line in obj] if isinstance(obj, list) else []

    async def get_client_traffic_by_email(
        self,
        email: str,
    ) -> dict:

        response = await self._request(
            "GET",
            f"/panel/api/clients/traffic/{email}",
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
        f"{node.panel_password_encrypted}|"
        f"{node.panel_api_token_encrypted}"
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
        api_token=(
            decrypt_secret(node.panel_api_token_encrypted)
            if node.panel_api_token_encrypted
            else None
        ),
    )

    _client_cache[node.id] = (
        fingerprint,
        client,
    )

    return client
