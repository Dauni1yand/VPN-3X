"""Cloudflare API client -- creates/updates a proxied DNS record for the
main server and sets a sane SSL mode, so connecting Cloudflare can be
triggered from inside the bot (/connectcloudflare) instead of requiring
shell access to the box.

Deliberately does NOT touch the host firewall (unlike
scripts/setup_cloudflare.sh's optional --lock-firewall): this code runs
inside the server's own container, which doesn't share the host's network
namespace, so `ufw` commands issued from here would affect the container,
not the actual machine -- that part stays a script the admin runs on the
host directly.
"""

from __future__ import annotations

import httpx

_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    pass


class CloudflareClient:
    def __init__(self, api_token: str, zone_id: str) -> None:
        self._zone_id = zone_id
        self._http = httpx.AsyncClient(
            base_url=_API_BASE, headers={"Authorization": f"Bearer {api_token}"}, timeout=15.0
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict:
        body = resp.json()
        if not body.get("success"):
            raise CloudflareError(f"Cloudflare API error: {body.get('errors')}")
        return body

    async def verify_zone(self) -> str:
        resp = await self._http.get(f"/zones/{self._zone_id}")
        return self._unwrap(resp)["result"]["name"]

    async def upsert_dns_record(self, record_name: str, server_ip: str) -> dict:
        existing = await self._http.get(
            f"/zones/{self._zone_id}/dns_records", params={"type": "A", "name": record_name}
        )
        existing_result = self._unwrap(existing)["result"]
        record_id = existing_result[0]["id"] if existing_result else None

        payload = {"type": "A", "name": record_name, "content": server_ip, "ttl": 1, "proxied": True}
        if record_id:
            resp = await self._http.put(f"/zones/{self._zone_id}/dns_records/{record_id}", json=payload)
        else:
            resp = await self._http.post(f"/zones/{self._zone_id}/dns_records", json=payload)
        return self._unwrap(resp)["result"]

    async def set_ssl_mode(self, mode: str = "full") -> None:
        resp = await self._http.patch(f"/zones/{self._zone_id}/settings/ssl", json={"value": mode})
        self._unwrap(resp)


async def detect_public_ip() -> str:
    """Best-effort auto-detect of the main server's own public IPv4 --
    used when the admin doesn't pass one explicitly. Works because the
    container's egress IP is the host's public IP in the standard
    port-mapped deployment install.sh sets up (no separate NAT layer)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get("https://api.ipify.org")
            resp.raise_for_status()
            return resp.text.strip()
    except httpx.HTTPError as exc:
        raise CloudflareError(
            f"couldn't auto-detect this server's public IP ({exc}) -- pass it explicitly instead"
        ) from exc


async def connect_cloudflare(api_token: str, zone_id: str, record_name: str, server_ip: str) -> dict:
    client = CloudflareClient(api_token, zone_id)
    try:
        try:
            zone_name = await client.verify_zone()
            record = await client.upsert_dns_record(record_name, server_ip)
            await client.set_ssl_mode("full")
        except (httpx.HTTPError, ValueError) as exc:
            # Anything below the "got a response with success:false" level
            # that _unwrap already turns into CloudflareError -- DNS
            # failure, connection refused/reset, timeout, or a non-JSON
            # response body (`.json()` raises a ValueError subclass) -- so
            # the route can map this to a clean 502 instead of an uncaught
            # 500.
            raise CloudflareError(f"couldn't reach Cloudflare's API: {exc}") from exc
    finally:
        await client.aclose()
    return {
        "zone_name": zone_name,
        "record_name": record_name,
        "server_ip": server_ip,
        "proxied": record.get("proxied", False),
    }
