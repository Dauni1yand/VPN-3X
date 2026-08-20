"""SSH-bootstraps a bare Ubuntu VPS into a running node: installs 3x-ui
(pinned to a known version -- see PLAN.md risk on 3x-ui API changes) and
sets our own admin credentials on the panel, rather than trusting whatever
3x-ui generates on its own.

Assumes root SSH access to the box, which is the common case for a freshly
rented VPS (password or a private key, both supported). Does not touch the
database -- returns the credentials/port for the caller to store on a Node
row and hand to node_provisioner.provision_default_inbound.

NOT exercised against a real VPS (nothing reachable like that from this
environment) -- the install.sh invocation and the `x-ui setting` flags are
based on documented/known 3x-ui usage, not a verified live run. Treat this
the same as the 3x-ui HTTP API: verify against a real box in Etap 0 R&D
before relying on it in production.
"""

from __future__ import annotations

import secrets

import asyncssh

# Pin instead of always grabbing "latest" -- see PLAN.md risk: 3x-ui's API
# has changed under us before (v3.6.0 started requiring a session instead of
# a token). TODO(Etap 0 R&D): confirm this is still the recommended/latest
# stable tag before first real deploy.
THREEXUI_VERSION = "v2.6.3"
THREEXUI_INSTALL_URL = "https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"

PANEL_PORT = 2053  # 3x-ui's own default management port
COMMAND_TIMEOUT_SECONDS = 300


class NodeBootstrapError(RuntimeError):
    pass


async def _run(conn: asyncssh.SSHClientConnection, command: str) -> asyncssh.SSHCompletedProcess:
    result = await conn.run(command, check=False, timeout=COMMAND_TIMEOUT_SECONDS)
    if result.exit_status != 0:
        raise NodeBootstrapError(f"command failed (exit {result.exit_status}): {command}\n{result.stderr}")
    return result


async def bootstrap_node(
    *,
    ssh_host: str,
    ssh_user: str = "root",
    ssh_port: int = 22,
    ssh_password: str | None = None,
    ssh_private_key: str | None = None,
) -> tuple[str, str, int]:
    """Installs 3x-ui on `ssh_host` and returns (panel_login,
    panel_password, panel_port) to store on the Node row.

    Host key verification is intentionally skipped (`known_hosts=None`):
    this is a one-shot bootstrap connection for a box we've never talked to
    before and won't SSH into again afterwards (everything else goes over
    the 3x-ui HTTP API) -- trust-on-first-use isn't meaningfully improved by
    pinning a key we'd never check again.
    """

    if not ssh_password and not ssh_private_key:
        raise ValueError("bootstrap_node needs ssh_password or ssh_private_key")

    connect_kwargs: dict = {"username": ssh_user, "port": ssh_port, "known_hosts": None}
    if ssh_private_key:
        connect_kwargs["client_keys"] = [asyncssh.import_private_key(ssh_private_key)]
    else:
        connect_kwargs["password"] = ssh_password

    panel_login = "admin"
    panel_password = secrets.token_urlsafe(24)

    async with asyncssh.connect(ssh_host, **connect_kwargs) as conn:
        await _run(conn, "apt-get update -y && apt-get install -y curl sudo")
        # Piped into `bash -s` rather than the `bash <(curl ...)` form the
        # 3x-ui docs show -- process substitution needs an interactive-ish
        # bash and is less reliable over a plain SSH exec channel.
        await _run(conn, f"curl -Ls {THREEXUI_INSTALL_URL} | bash -s -- {THREEXUI_VERSION}")
        await _run(
            conn,
            f"x-ui setting -username '{panel_login}' -password '{panel_password}' -port {PANEL_PORT}",
        )
        await _run(conn, "x-ui restart")

    return panel_login, panel_password, PANEL_PORT
