"""Bootstrap a bare Ubuntu VPS into a fully usable VPN-3X node."""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
from dataclasses import dataclass

import asyncssh


THREEXUI_VERSION = "v3.6.0"
THREEXUI_INSTALL_URL = (
    "https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"
)

PANEL_PORT = 2053

COMMAND_TIMEOUT_SECONDS = 900
CONNECT_TIMEOUT_SECONDS = 20
LOGIN_TIMEOUT_SECONDS = 60

SNI_CANDIDATES = (
    "www.microsoft.com",
    "www.apple.com",
    "dl.google.com",
    "www.cloudflare.com",
    "aws.amazon.com",
    "www.swift.org",
)

NONINTERACTIVE = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "export APT_LISTCHANGES_FRONTEND=none; "
    "export NEEDRESTART_MODE=a; "
)

APT_OPTS = (
    '-o Dpkg::Options::="--force-confdef" '
    '-o Dpkg::Options::="--force-confold"'
)


class NodeBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    panel_base_url: str
    panel_port: int
    panel_web_base_path: str
    sni: str


async def _run(
    conn: asyncssh.SSHClientConnection,
    command: str,
    *,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> asyncssh.SSHCompletedProcess:

    try:
        result = await conn.run(
            command,
            check=False,
            timeout=timeout,
            input="",
        )
    except asyncio.TimeoutError as exc:
        raise NodeBootstrapError(
            f"remote command timed out after {timeout}s: {command}"
        ) from exc

    if result.exit_status != 0:
        stderr = str(result.stderr or "").strip()
        stdout = str(result.stdout or "").strip()

        detail = (stderr or stdout)[-2000:]

        raise NodeBootstrapError(
            f"remote command failed "
            f"(exit {result.exit_status}): {command}\n{detail}"
        )

    return result


def _extract_setting(
    output: str,
    name: str,
    default: str = "",
) -> str:

    match = re.search(
        rf"(?m)^\s*{re.escape(name)}:\s*(.+?)\s*$",
        output,
    )

    if not match:
        return default

    return match.group(1).strip()


def _normalize_web_base_path(value: str) -> str:

    value = value.strip()

    if not value or value == "/":
        return "/"

    return "/" + value.strip("/") + "/"


def _panel_url(
    host: str,
    port: int,
    path: str,
) -> str:

    display_host = host

    if ":" in host and not host.startswith("["):
        display_host = f"[{host}]"

    return f"http://{display_host}:{port}{path}"


async def _pick_node_sni(
    conn: asyncssh.SSHClientConnection,
) -> str:

    for host in SNI_CANDIDATES:

        command = (
            "curl -fsSIL "
            "--http1.1 "
            "--tlsv1.3 "
            "--connect-timeout 5 "
            "--max-time 10 "
            f"https://{shlex.quote(host)}/ >/dev/null"
        )

        try:
            await _run(
                conn,
                command,
                timeout=15,
            )
        except NodeBootstrapError:
            continue

        return host

    raise NodeBootstrapError(
        "the node could not complete a TLS 1.3 handshake "
        "with any configured REALITY SNI"
    )


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
) -> BootstrapResult:

    if not ssh_password and not ssh_private_key:
        raise NodeBootstrapError(
            "bootstrap_node needs ssh_password or ssh_private_key"
        )

    if not panel_login or not panel_password:
        raise NodeBootstrapError(
            "3x-ui panel credentials cannot be empty"
        )

    # Every node gets its own hidden web path.
    web_base_path = f"vpn3x-{secrets.token_hex(10)}"

    connect_kwargs: dict = {
        "username": ssh_user,
        "port": ssh_port,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "login_timeout": LOGIN_TIMEOUT_SECONDS,
    }

    if ssh_private_key:

        try:
            connect_kwargs["client_keys"] = [
                asyncssh.import_private_key(ssh_private_key)
            ]
        except Exception as exc:
            raise NodeBootstrapError(
                f"invalid SSH private key: {exc}"
            ) from exc

    else:
        connect_kwargs["password"] = ssh_password

    try:

        async with asyncssh.connect(
            ssh_host,
            **connect_kwargs,
        ) as conn:

            # --------------------------------------------------------
            # Ubuntu dependencies
            # --------------------------------------------------------

            await _run(
                conn,
                NONINTERACTIVE +
                "apt-get update -y -qq",
            )

            await _run(
                conn,
                NONINTERACTIVE +
                f"apt-get install -y -qq {APT_OPTS} "
                "curl sudo tar tzdata openssl ca-certificates",
            )

            # --------------------------------------------------------
            # Check panel port
            # --------------------------------------------------------

            port_check = await conn.run(
                (
                    "ss -ltnH | "
                    f"awk '$4 ~ /:{panel_port}$/ "
                    "{{found=1}} "
                    "END {{exit found ? 0 : 1}}'"
                ),
                check=False,
                timeout=15,
                input="",
            )

            if port_check.exit_status == 0:

                raise NodeBootstrapError(
                    f"TCP port {panel_port} is already occupied "
                    f"on {ssh_host}"
                )

            # --------------------------------------------------------
            # Download 3x-ui installer
            # --------------------------------------------------------

            installer = "/tmp/vpn3x-3xui-install.sh"

            await _run(
                conn,
                (
                    f"curl -fsSL "
                    f"{shlex.quote(THREEXUI_INSTALL_URL)} "
                    f"-o {installer} && "
                    f"chmod 700 {installer}"
                ),
                timeout=120,
            )

            # --------------------------------------------------------
            # Unattended 3x-ui installation
            # --------------------------------------------------------

            install_command = (
                NONINTERACTIVE
                + "export XUI_NONINTERACTIVE=1; "
                + f"export XUI_USERNAME={shlex.quote(panel_login)}; "
                + f"export XUI_PASSWORD={shlex.quote(panel_password)}; "
                + f"export XUI_PANEL_PORT={panel_port}; "
                + f"export XUI_WEB_BASE_PATH={shlex.quote(web_base_path)}; "
                + "export XUI_SSL_MODE=none; "
                + f"bash {installer} {THREEXUI_VERSION}"
            )

            await _run(
                conn,
                install_command,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )

            # --------------------------------------------------------
            # Enable service
            # --------------------------------------------------------

            await _run(
                conn,
                "systemctl enable --now x-ui",
            )

            await _run(
                conn,
                "x-ui migrate",
                timeout=120,
            )

            # --------------------------------------------------------
            # Force credentials / port
            # --------------------------------------------------------

            await _run(
                conn,
                (
                    "x-ui setting "
                    f"-username {shlex.quote(panel_login)} "
                    f"-password {shlex.quote(panel_password)} "
                    f"-port {panel_port}"
                ),
            )

            # Try to force our known web path.
            # Older builds may reject this option, so we don't fail here.
            path_result = await conn.run(
                (
                    "x-ui setting "
                    f"-webBasePath {shlex.quote(web_base_path)}"
                ),
                check=False,
                timeout=60,
                input="",
            )

            _ = path_result

            await _run(
                conn,
                "x-ui restart",
                timeout=120,
            )

            await asyncio.sleep(3)

            # --------------------------------------------------------
            # Read actual 3x-ui settings
            # --------------------------------------------------------

            settings_result = await _run(
                conn,
                "x-ui setting -show true",
                timeout=60,
            )

            settings_output = str(
                settings_result.stdout or ""
            )

            actual_port = int(
                _extract_setting(
                    settings_output,
                    "port",
                    str(panel_port),
                )
            )

            actual_path = _normalize_web_base_path(
                _extract_setting(
                    settings_output,
                    "webBasePath",
                    web_base_path,
                )
            )

            if actual_port != panel_port:

                raise NodeBootstrapError(
                    f"3x-ui ignored requested panel port "
                    f"{panel_port}; actual port is {actual_port}"
                )

            # --------------------------------------------------------
            # Verify panel locally
            # --------------------------------------------------------

            local_url = (
                f"http://127.0.0.1:"
                f"{actual_port}"
                f"{actual_path}"
            )

            await _run(
                conn,
                (
                    "curl -fsS "
                    "--max-time 15 "
                    f"{shlex.quote(local_url)} "
                    ">/dev/null"
                ),
                timeout=30,
            )

            # --------------------------------------------------------
            # UFW
            # --------------------------------------------------------

            ufw = await conn.run(
                "command -v ufw",
                check=False,
                timeout=10,
                input="",
            )

            if ufw.exit_status == 0:

                status = await conn.run(
                    "ufw status",
                    check=False,
                    timeout=10,
                    input="",
                )

                if "Status: active" in str(
                    status.stdout
                ):

                    await _run(
                        conn,
                        f"ufw allow {ssh_port}/tcp",
                    )

                    await _run(
                        conn,
                        f"ufw allow {panel_port}/tcp",
                    )

                    await _run(
                        conn,
                        "ufw allow 443/tcp",
                    )

            # --------------------------------------------------------
            # Probe REALITY SNI FROM THE NODE
            # --------------------------------------------------------

            sni = await _pick_node_sni(conn)

            return BootstrapResult(
                panel_base_url=_panel_url(
                    ssh_host,
                    actual_port,
                    actual_path,
                ),
                panel_port=actual_port,
                panel_web_base_path=actual_path,
                sni=sni,
            )

    except NodeBootstrapError:
        raise

    except asyncio.TimeoutError as exc:

        raise NodeBootstrapError(
            f"SSH connection timed out to {ssh_host}"
        ) from exc

    except (asyncssh.Error, OSError) as exc:

        raise NodeBootstrapError(
            f"SSH to {ssh_host} failed: {exc}"
        ) from exc

    except Exception as exc:

        raise NodeBootstrapError(
            f"node bootstrap failed on {ssh_host}: {exc}"
        ) from exc