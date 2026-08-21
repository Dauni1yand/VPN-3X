"""Client for the main server's internal API (see server/app/api/routes).
Authenticates with the shared `internal_api_key`, per PLAN.md section 4."""

from __future__ import annotations

import httpx

from bot.config import settings


class ServerAPIClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.server_api_url,
            headers={"X-API-Key": settings.internal_api_key},
            timeout=15.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_nodes(self) -> list[dict]:
        resp = await self._http.get("/nodes")
        resp.raise_for_status()
        return resp.json()

    async def get_user_client(self, telegram_id: int) -> dict | None:
        """None when the user has no live config -- a normal state (never
        subscribed, or it ran out), not an error worth raising over."""
        resp = await self._http.get(f"/clients/by-user/{telegram_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def add_node(
        self,
        name: str,
        ip: str,
        panel_base_url: str,
        panel_login: str,
        panel_password: str,
        country: str | None,
        admin_telegram_id: int,
    ) -> dict:
        resp = await self._http.post(
            "/nodes",
            json={
                "name": name,
                "ip": ip,
                "panel_base_url": panel_base_url,
                "panel_login": panel_login,
                "panel_password": panel_password,
                "country": country,
                "admin_telegram_id": admin_telegram_id,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_node(self, node_id: str, admin_telegram_id: int) -> None:
        resp = await self._http.delete(f"/nodes/{node_id}", params={"admin_telegram_id": admin_telegram_id})
        resp.raise_for_status()

    async def bootstrap_node(
        self, name: str, ip: str, ssh_password: str, country: str | None, admin_telegram_id: int, ssh_user: str = "root"
    ) -> dict:
        resp = await self._http.post(
            "/nodes/bootstrap",
            json={
                "name": name,
                "ip": ip,
                "ssh_user": ssh_user,
                "ssh_password": ssh_password,
                "country": country,
                "admin_telegram_id": admin_telegram_id,
            },
            # Returns as soon as the job is queued; the install itself runs
            # in the worker, so no long client-side timeout is needed.
        )
        resp.raise_for_status()
        return resp.json()

    async def provision_inbound(self, node_id: str, admin_telegram_id: int) -> dict:
        resp = await self._http.post(f"/nodes/{node_id}/inbound", params={"admin_telegram_id": admin_telegram_id})
        resp.raise_for_status()
        return resp.json()

    async def rotate_sni(self, node_id: str, admin_telegram_id: int) -> dict:
        resp = await self._http.post(
            f"/nodes/{node_id}/inbound/rotate-sni", params={"admin_telegram_id": admin_telegram_id}
        )
        resp.raise_for_status()
        return resp.json()

    async def create_admin_client(
        self, user_telegram_id: int, duration_seconds: int, target_node_id: str, admin_telegram_id: int
    ) -> dict:
        resp = await self._http.post(
            "/clients/admin",
            json={
                "user_telegram_id": user_telegram_id,
                "duration_seconds": duration_seconds,
                "target_node_id": target_node_id,
                "admin_telegram_id": admin_telegram_id,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def migrate_client(self, client_id: str, admin_telegram_id: int, target_node_id: str | None = None) -> dict:
        resp = await self._http.post(
            f"/clients/{client_id}/migrate",
            json={"admin_telegram_id": admin_telegram_id, "target_node_id": target_node_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_setting(self, key: str) -> str:
        resp = await self._http.get(f"/settings/{key}")
        resp.raise_for_status()
        return resp.json()["value"]

    async def list_settings(self) -> dict[str, str]:
        resp = await self._http.get("/settings")
        resp.raise_for_status()
        return resp.json()

    async def set_setting(self, key: str, value: str, admin_telegram_id: int) -> dict:
        resp = await self._http.put(f"/settings/{key}", json={"value": value, "admin_telegram_id": admin_telegram_id})
        resp.raise_for_status()
        return resp.json()

    async def connect_cloudflare(
        self, record_name: str, server_ip: str | None, admin_telegram_id: int
    ) -> dict:
        resp = await self._http.post(
            "/cloudflare/connect",
            json={"record_name": record_name, "server_ip": server_ip, "admin_telegram_id": admin_telegram_id},
            timeout=30,  # a few outbound calls to Cloudflare's API in sequence
        )
        resp.raise_for_status()
        return resp.json()

    async def grant_ad_view(self, telegram_id: int, ad_type: str, provider_impression_id: str) -> dict:
        resp = await self._http.post(
            "/subscriptions/ad-view",
            json={
                "user_telegram_id": telegram_id,
                "ad_type": ad_type,
                "provider_impression_id": provider_impression_id,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def confirm_payment(
        self, telegram_id: int, provider_invoice_id: str, plan_code: str, amount: str, currency: str
    ) -> dict:
        resp = await self._http.post(
            "/subscriptions/payments/confirm",
            json={
                "user_telegram_id": telegram_id,
                "provider_invoice_id": provider_invoice_id,
                "plan_code": plan_code,
                "amount": amount,
                "currency": currency,
            },
        )
        resp.raise_for_status()
        return resp.json()


server_api = ServerAPIClient()
