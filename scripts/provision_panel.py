"""Brings a fresh Remnawave panel to the point where we hold an API token.

Run by install.sh, inside the server container, so it is on the compose
network and has httpx. Prints the token on stdout and nothing else; every
other message goes to stderr, so the caller can capture it with `$(...)`.

The panel's first-run flow is: registration is open until the first account
exists, and after that it is closed forever. So this registers the admin,
logs in, and mints a token with the JWT -- and is idempotent, because a
re-run finds registration already closed and logs in with the credentials
install.sh stored.

Usage:
    provision_panel.py <base_url> <username> <password> [token_name]
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

# The panel migrates its own schema on first boot, so the API can 404 or
# refuse connections for a while after the container reports healthy.
# Overridable so a slow box can be given longer without editing this file.
READY_TIMEOUT_SECONDS = int(os.environ.get("REMNAWAVE_READY_TIMEOUT", "180"))
TOKEN_LIFETIME_DAYS = 3650


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def unwrap(response: httpx.Response) -> dict:
    body = response.json()
    return body.get("response", body) if isinstance(body, dict) else {}


async def wait_ready(client: httpx.AsyncClient) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READY_TIMEOUT_SECONDS
    last = ""

    while True:
        try:
            response = await client.get("/api/auth/status")
            if response.status_code < 400:
                return unwrap(response)
            last = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"

        if loop.time() >= deadline:
            raise SystemExit(
                f"панель не ответила за {READY_TIMEOUT_SECONDS}s. Последняя ошибка: {last}"
            )

        await asyncio.sleep(3)


async def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("usage: provision_panel.py <base_url> <username> <password> [name]")

    base_url, username, password = sys.argv[1], sys.argv[2], sys.argv[3]
    token_name = sys.argv[4] if len(sys.argv) > 4 else "vpn-3x"

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30) as client:
        log("Жду, пока панель поднимется...")
        status = await wait_ready(client)

        if status.get("isRegisterAllowed"):
            log("Регистрирую администратора панели...")
            response = await client.post(
                "/api/auth/register", json={"username": username, "password": password}
            )
            if response.status_code >= 400:
                raise SystemExit(f"регистрация не удалась: {response.text[:400]}")
            access_token = unwrap(response).get("accessToken")
        else:
            # Not an error: a re-run, or a panel someone already set up. Only
            # the credentials install.sh stored can get us back in.
            log("Администратор уже существует, вхожу...")
            response = await client.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
            if response.status_code >= 400:
                raise SystemExit(
                    "в панели уже есть администратор, но сохранённый пароль не подошёл. "
                    "Создайте токен вручную (Settings -> API Tokens) и впишите его "
                    f"в .env как REMNAWAVE_TOKEN. Ответ панели: {response.text[:300]}"
                )
            access_token = unwrap(response).get("accessToken")

        if not access_token:
            raise SystemExit("панель не вернула accessToken")

        log("Выпускаю API-токен...")
        response = await client.post(
            "/api/tokens",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "name": token_name,
                "expiresInDays": TOKEN_LIFETIME_DAYS,
                # Full access: this token installs nodes, edits config
                # profiles, and creates users -- there is no narrower scope
                # that covers the job.
                "scopes": ["*"],
            },
        )
        if response.status_code >= 400:
            raise SystemExit(f"не удалось создать токен: {response.text[:400]}")

        token = unwrap(response).get("token")
        if not token:
            raise SystemExit("панель не вернула token")

        # stdout carries the token and nothing else.
        print(token)


if __name__ == "__main__":
    asyncio.run(main())
