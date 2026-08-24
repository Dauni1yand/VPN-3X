"""Turns a bare Ubuntu VPS into a Remnawave node, over SSH.

Much smaller than the 3x-ui bootstrap it replaces, and for a structural
reason rather than a stylistic one. Installing 3x-ui meant running its
installer, then discovering which port and web path it had actually chosen,
polling for its panel to answer, reading credentials back out of a file it
wrote, and minting an API token -- because each VPS ran its own panel with
its own state. A Remnawave node runs one container with two environment
variables and holds no state of its own: the panel pushes it a config and
that is the whole contract.

What survives from the old bootstrap is the part that was never about the
panel: choosing the REALITY dest by measuring, from the node itself, which
candidate host answers a TLS 1.3 handshake fastest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import asyncssh

from app.core.config import settings
from app.services.node_bootstrap import (
    APT_OPTS,
    CONNECT_TIMEOUT_SECONDS,
    LOGIN_TIMEOUT_SECONDS,
    NONINTERACTIVE,
    NodeBootstrapError,
    _pick_node_sni,
    _run,
)

NODE_DIR = "/opt/remnanode"

# The container has to pull an image before it can listen, so this is a
# download, not a start-up.
NODE_START_TIMEOUT_SECONDS = 180


@dataclass
class RemnawaveBootstrapResult:
    node_port: int
    sni: str


def build_node_compose(*, node_port: int, secret_key: str) -> str:
    """The node's docker-compose.yml.

    `network_mode: host` is not optional: the node serves VLESS on 443 on
    the host's own address, and the panel's config speaks in host ports.

    SECRET_KEY is the panel's public key, from /api/keygen. It is what lets
    the node trust the config pushed to it, and it is the only secret this
    function puts on the VPS.
    """

    return f"""services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: remnawave/node:latest
    restart: always
    network_mode: host
    environment:
      - NODE_PORT={node_port}
      - SECRET_KEY={secret_key}
"""


async def bootstrap_remnawave_node(
    *,
    ssh_host: str,
    secret_key: str,
    node_port: int | None = None,
    panel_address: str | None = None,
    ssh_user: str = "root",
    ssh_port: int = 22,
    ssh_password: str | None = None,
    ssh_private_key: str | None = None,
) -> RemnawaveBootstrapResult:

    if not ssh_password and not ssh_private_key:
        raise NodeBootstrapError("bootstrap needs ssh_password or ssh_private_key")

    if not secret_key:
        raise NodeBootstrapError("panel did not provide a SECRET_KEY for the node")

    node_port = node_port or settings.remnawave_node_port
    panel_address = panel_address or settings.remnawave_panel_address

    connect_kwargs: dict = {
        "username": ssh_user,
        "port": ssh_port,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "login_timeout": LOGIN_TIMEOUT_SECONDS,
    }

    if ssh_private_key:
        try:
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(ssh_private_key)]
        except Exception as exc:
            raise NodeBootstrapError(f"invalid SSH private key: {exc}") from exc
    else:
        connect_kwargs["password"] = ssh_password

    try:
        async with asyncssh.connect(ssh_host, **connect_kwargs) as conn:
            await _install_docker(conn)
            await _write_node_compose(conn, node_port=node_port, secret_key=secret_key)
            await _start_node(conn)
            await _wait_for_node_port(conn, node_port)
            await _configure_firewall(
                conn, node_port=node_port, panel_address=panel_address, ssh_port=ssh_port
            )

            sni = await _pick_node_sni(conn)

    except NodeBootstrapError:
        raise
    except asyncssh.Error as exc:
        raise NodeBootstrapError(f"SSH к {ssh_host} не удался: {exc}") from exc

    return RemnawaveBootstrapResult(node_port=node_port, sni=sni)


async def _install_docker(conn: asyncssh.SSHClientConnection) -> None:
    """Installs Docker if it is not already there.

    Checked first rather than installed unconditionally: re-running the
    convenience script on a box that already has Docker from the distro
    repos can leave two conflicting installs.
    """

    probe = await conn.run("command -v docker >/dev/null 2>&1 && docker compose version", check=False)
    if probe.exit_status == 0:
        return

    await _run(
        conn,
        f"{NONINTERACTIVE} apt-get update -qq && "
        f"{NONINTERACTIVE} apt-get install -y -qq {APT_OPTS} ca-certificates curl ufw",
    )
    await _run(conn, "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh")
    await _run(conn, f"{NONINTERACTIVE} sh /tmp/get-docker.sh")

    verify = await conn.run("docker compose version", check=False)
    if verify.exit_status != 0:
        raise NodeBootstrapError(
            "Docker установился, но `docker compose` не работает: "
            f"{str(verify.stderr or verify.stdout)[-500:]}"
        )


async def _write_node_compose(
    conn: asyncssh.SSHClientConnection, *, node_port: int, secret_key: str
) -> None:
    """Writes the compose file with the secret in it.

    Via stdin, not an argument: the key would otherwise appear in the
    process list and in the shell history of anyone auditing the box.
    """

    await _run(conn, f"mkdir -p {NODE_DIR} && chmod 700 {NODE_DIR}")

    compose = build_node_compose(node_port=node_port, secret_key=secret_key)

    result = await conn.run(
        f"cat > {NODE_DIR}/docker-compose.yml && chmod 600 {NODE_DIR}/docker-compose.yml",
        input=compose,
        check=False,
    )
    if result.exit_status != 0:
        raise NodeBootstrapError(
            f"не удалось записать {NODE_DIR}/docker-compose.yml: "
            f"{str(result.stderr or '')[-500:]}"
        )


async def _start_node(conn: asyncssh.SSHClientConnection) -> None:
    # `up -d` pulls the image, which on a cold box is most of the wait.
    await _run(conn, f"cd {NODE_DIR} && docker compose up -d", timeout=NODE_START_TIMEOUT_SECONDS)


async def _wait_for_node_port(conn: asyncssh.SSHClientConnection, node_port: int) -> None:
    """Waits until the container is actually listening.

    Returning before this and letting the panel register the node instead
    turns a slow image pull into "node registered, permanently
    disconnected", with nothing in the panel saying why.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + NODE_START_TIMEOUT_SECONDS

    while True:
        listening = await conn.run(
            f"ss -ltnH 'sport = :{node_port}' | head -1", check=False
        )
        if str(listening.stdout or "").strip():
            return

        if loop.time() >= deadline:
            break

        await asyncio.sleep(3)

    logs = await conn.run(f"cd {NODE_DIR} && docker compose logs --tail 40", check=False)
    state = await conn.run(f"cd {NODE_DIR} && docker compose ps", check=False)

    raise NodeBootstrapError(
        f"remnawave-node не начал слушать порт {node_port} за "
        f"{NODE_START_TIMEOUT_SECONDS}s.\n\n"
        f"docker compose ps:\n{str(state.stdout or '').strip()[-1000:]}\n\n"
        f"логи:\n{str(logs.stdout or logs.stderr or '').strip()[-2000:]}"
    )


async def _configure_firewall(
    conn: asyncssh.SSHClientConnection,
    *,
    node_port: int,
    panel_address: str | None,
    ssh_port: int = 22,
) -> None:
    """Opens what clients need and closes what only the panel needs.

    NODE_PORT carries the panel's config pushes and is not a client port.
    Left open to the internet it is an unauthenticated-looking control
    surface on every node, so it is restricted to the panel's address. When
    we do not know that address the rule cannot be written safely, and the
    honest thing is to leave the port closed rather than open it to
    everyone -- a node that cannot be reached by the panel fails loudly,
    which an exposed control port does not.
    """

    # SSH first, and always: enabling ufw with a default-deny policy before
    # allowing the port we arrived on locks us out of the box mid-bootstrap.
    # Both 22 and the configured port, since a box moved off 22 still often
    # has tooling pointed at it.
    for port in {22, int(ssh_port)}:
        await _run(conn, f"ufw allow {port}/tcp || true")

    await _run(conn, "ufw allow 443/tcp || true")

    if panel_address:
        await _run(
            conn,
            f"ufw allow from {panel_address} to any port {node_port} proto tcp || true",
        )

    await _run(conn, "ufw --force enable || true")
