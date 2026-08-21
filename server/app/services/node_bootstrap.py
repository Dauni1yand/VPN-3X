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

import asyncio

import asyncssh

# Pinned to v3.6.0 specifically because threexui_client.py's whole
# login/session-cookie flow (see its docstring) is designed around that
# version's auth behavior -- installing a different version here would
# silently provision nodes the rest of the codebase can't actually talk to.
# TODO(Etap 0 R&D): confirm v3.6.0 is still installable via this script
# (tag names can be pruned/renamed upstream) before the first real deploy.
THREEXUI_VERSION = "v3.6.0"
THREEXUI_INSTALL_URL = "https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"

PANEL_PORT = 2053  # 3x-ui's own default management port

# Per-command cap. Generous because `apt-get update` plus the 3x-ui
# installer genuinely take minutes on a small VPS -- but finite, so a
# command that hangs fails the job instead of pinning it forever.
COMMAND_TIMEOUT_SECONDS = 600
# A host that silently drops packets (closed security group, wrong IP)
# would otherwise sit in TCP retry for a very long time.
CONNECT_TIMEOUT_SECONDS = 20
LOGIN_TIMEOUT_SECONDS = 60

# Every remote command runs through this: apt on Ubuntu happily opens an
# interactive dialog (service-restart prompt, changed-config-file prompt)
# and, over an SSH exec channel with stdin attached and no TTY, that dialog
# waits forever -- which is exactly how a bootstrap "just hangs". Forcing
# noninteractive mode and keeping the packaged config on conflict removes
# both prompts; stdin is separately pointed at /dev/null in _run so
# anything that still tries to read gets EOF immediately rather than
# blocking.
NONINTERACTIVE = (
    "export DEBIAN_FRONTEND=noninteractive "
    "APT_LISTCHANGES_FRONTEND=none "
    "NEEDRESTART_MODE=a; "
)
APT_OPTS = '-o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"' 


class NodeBootstrapError(RuntimeError):
    pass


async def _run(conn: asyncssh.SSHClientConnection, command: str) -> asyncssh.SSHCompletedProcess:
    try:
        result = await conn.run(
            command,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            # EOF instead of a blocked read for anything that prompts.
            stdin=asyncssh.DEVNULL,
        )
    except asyncio.TimeoutError as exc:
        raise NodeBootstrapError(
            f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {command}"
        ) from exc
    if result.exit_status != 0:
        # stderr can be a whole apt log; keep the tail, which is where the
        # actual reason lives.
        detail = str(result.stderr or "").strip()[-800:]
        raise NodeBootstrapError(f"command failed (exit {result.exit_status}): {command}\n{detail}")
    return result


async def bootstrap_node(
    *,
    ssh_host: str,
    panel_login: str,
    panel_password: str,
    panel_port: int = PANEL_PORT,
    ssh_user: str = "root",
    ssh_port: int = 22,
    ssh_password: str | None = None,
    ssh_private_key: str | None = None,
) -> None:
    """Installs 3x-ui on `ssh_host` and sets it to the panel credentials
    the caller already persisted on the Node row.

    Credentials are passed in rather than generated here so the Node row
    can exist (and be visible in the bot) before this slow work starts --
    see workers/tasks.py bootstrap_node_job.

    Host key verification is intentionally skipped (`known_hosts=None`):
    this is a one-shot bootstrap connection for a box we've never talked to
    before and won't SSH into again afterwards (everything else goes over
    the 3x-ui HTTP API) -- trust-on-first-use isn't meaningfully improved by
    pinning a key we'd never check again.
    """

    if not ssh_password and not ssh_private_key:
        raise ValueError("bootstrap_node needs ssh_password or ssh_private_key")

    connect_kwargs: dict = {
        "username": ssh_user,
        "port": ssh_port,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "login_timeout": LOGIN_TIMEOUT_SECONDS,
    }
    if ssh_private_key:
        connect_kwargs["client_keys"] = [asyncssh.import_private_key(ssh_private_key)]
    else:
        connect_kwargs["password"] = ssh_password

    try:
        conn_ctx = asyncssh.connect(ssh_host, **connect_kwargs)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the admin as-is
        raise NodeBootstrapError(f"couldn't connect over SSH: {exc}") from exc

    try:
        async with conn_ctx as conn:
            await _run(conn, NONINTERACTIVE + "apt-get update -y -qq")
            await _run(conn, NONINTERACTIVE + f"apt-get install -y -qq {APT_OPTS} curl sudo tar tzdata")
            # Downloaded first, then run from a file: piping into `bash -s`
            # leaves the script's stdin attached to the pipe, so a prompt
            # inside it reads from curl's output instead of getting EOF.
            await _run(
                conn,
                NONINTERACTIVE
                + f"curl -fsSL {THREEXUI_INSTALL_URL} -o /tmp/3xui-install.sh"
                + f" && bash /tmp/3xui-install.sh {THREEXUI_VERSION}",
            )
            await _run(
                conn,
                f"x-ui setting -username '{panel_login}' -password '{panel_password}' -port {panel_port}",
            )
            await _run(conn, "x-ui restart")
    except NodeBootstrapError:
        raise
    except asyncio.TimeoutError as exc:
        raise NodeBootstrapError(f"SSH connection timed out to {ssh_host}") from exc
    except Exception as exc:  # noqa: BLE001 -- auth failure, refused, unreachable...
        raise NodeBootstrapError(f"SSH to {ssh_host} failed: {exc}") from exc
