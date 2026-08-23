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
# How long 3x-ui gets to open its listener after a restart. `x-ui restart`
# returns once systemd has started the unit, not once the panel is actually
# accepting connections, and on a small VPS the gap is more than a few
# seconds -- especially on first boot, when xray is also starting.
PANEL_START_TIMEOUT_SECONDS = 90

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
    # Panel credentials as the installer actually applied them, read back from
    # /etc/x-ui/install-result.env rather than assumed. 3x-ui does not always
    # honour every requested value, and the API token is only ever printed
    # once at creation.
    panel_login: str
    panel_password: str
    panel_api_token: str | None


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
) -> str | None:
    """Reads one `name: value` line out of `x-ui setting -show true`.

    Returns None when the line is absent. It deliberately does NOT fall back
    to a caller-supplied default: doing that hid a real failure for a long
    time. `/usr/bin/x-ui` is a management shell script whose argument
    dispatcher has no `setting` case, so `x-ui setting -show true` prints its
    help menu and exits 0. Every field then "defaulted" to what we had asked
    for, and the bootstrap went on believing the panel sat on a port and path
    it had never actually been moved to.
    """

    match = re.search(
        rf"(?m)^\s*{re.escape(name)}:\s*(.+?)\s*$",
        output,
    )

    if not match:
        return None

    return match.group(1).strip()


async def _find_xui_binary(conn: asyncssh.SSHClientConnection) -> str:
    """Locates the x-ui Go binary.

    `/usr/bin/x-ui` is the interactive management script, not the binary --
    it understands start/stop/restart/status and prints its help menu for
    anything else, including `setting` and `migrate`, while still exiting 0.
    The installer itself always calls "${xui_folder}/x-ui setting ...", so
    that is the path that actually applies configuration.
    """

    result = await conn.run(
        "for p in /usr/local/x-ui/x-ui /usr/local/x-ui/bin/x-ui /opt/x-ui/x-ui; do "
        '[ -x "$p" ] && echo "$p" && exit 0; done; exit 1',
        check=False,
        timeout=30,
        input="",
    )

    path = str(result.stdout or "").strip().splitlines()

    if result.exit_status != 0 or not path:
        raise NodeBootstrapError(
            "could not find the x-ui binary on the node "
            "(looked in /usr/local/x-ui, /opt/x-ui) -- "
            "the 3x-ui install did not lay out as expected"
        )

    return path[0]


def _parse_install_result(output: str) -> dict[str, str]:
    """Parses /etc/x-ui/install-result.env, which the 3x-ui installer writes
    with `printf '%q'` per value so the file stays safely source-able.

    This is the only place the panel's API token is ever exposed: the panel
    stores just a SHA-256 hash, so a token not captured here can never be
    recovered -- only replaced.
    """

    values: dict[str, str] = {}

    for raw in output.splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()

        if not key.startswith("XUI_"):
            continue

        # Undo printf %q. For the alphanumeric values the installer generates
        # this is a no-op, but a pinned password may legitimately contain
        # characters that got escaped.
        try:
            parsed = shlex.split(value)
        except ValueError:
            parsed = []

        values[key] = parsed[0] if parsed else value.strip().strip("'\"")

    return values


def _normalize_web_base_path(value: str) -> str:

    value = value.strip()

    if not value or value == "/":
        return "/"

    return "/" + value.strip("/") + "/"


def _panel_url(
    host: str,
    port: int,
    path: str,
    scheme: str = "http",
) -> str:

    display_host = host

    if ":" in host and not host.startswith("["):
        display_host = f"[{host}]"

    return f"{scheme}://{display_host}:{port}{path}"


async def _panel_diagnostics(
    conn: asyncssh.SSHClientConnection,
    port: int,
    secrets_to_redact: tuple[str, ...] = (),
) -> str:
    """Collects why the panel isn't answering, from the node itself.

    Without this a failed bootstrap reports only "could not connect", which
    is the symptom for a crashed service, a service that never started, a
    port bound to a different address, and a panel still starting up -- four
    different problems needing four different fixes.
    """

    probes = (
        ("service state", "systemctl is-active x-ui 2>&1; systemctl is-enabled x-ui 2>&1"),
        # Every socket x-ui owns, not just the port we hoped for -- if the
        # panel came up somewhere else this is the line that says where.
        ("x-ui sockets", "ss -ltnp 2>/dev/null | grep x-ui || echo '(x-ui owns no listening socket)'"),
        # Via the binary, not /usr/bin/x-ui: the management script answers
        # unknown subcommands with its help menu, which is what this probe
        # used to report instead of the setting.
        (
            "panel settings",
            "for p in /usr/local/x-ui/x-ui /usr/local/x-ui/bin/x-ui /opt/x-ui/x-ui; do "
            '[ -x "$p" ] && "$p" setting -show true 2>&1 '
            "| grep -aiE 'port|webBasePath|listen|cert' | head -8 && exit 0; done; "
            "echo '(x-ui binary not found)'",
        ),
        # Targeted, not a blind tail: the bind line and any error are what
        # matter, and a truncated tail was cutting exactly those off.
        (
            "panel/bind log",
            "journalctl -u x-ui --no-pager -n 400 2>/dev/null "
            "| grep -aiE 'web server running|sub server running|error|fail|panic|bind' "
            "| tail -12 || echo '(no matching log lines)'",
        ),
    )

    sections: list[str] = []

    for label, command in probes:
        try:
            result = await conn.run(command, check=False, timeout=30, input="")
            output = (str(result.stdout or "") + str(result.stderr or "")).strip()
        except Exception as exc:  # noqa: BLE001 -- diagnostics must never mask the real error
            output = f"(probe failed: {type(exc).__name__}: {exc})"

        for secret in secrets_to_redact:
            if secret:
                output = output.replace(secret, "***")

        # Kept tight on purpose: this ends up in a Telegram alert, which is
        # hard-capped at 4096 characters. Overshoot and the send fails, so
        # the admin gets nothing at all instead of a truncated clue.
        sections.append(f"--- {label} ---\n{output[-700:]}")

    return "\n".join(sections)


async def _xui_listeners(
    conn: asyncssh.SSHClientConnection,
) -> list[tuple[str, int]]:
    """Every (host, port) x-ui currently listens on, as the node sees it.

    Parsed from `ss` rather than assumed from settings: the panel binds to
    `listenIP:port`, where listenIP may be a specific address and the port
    can be overridden by XUI_PORT in the service environment. Both make the
    stored port a poor guess at where the panel actually is.
    """

    result = await conn.run(
        "ss -ltnpH 2>/dev/null | grep x-ui",
        check=False,
        timeout=30,
        input="",
    )

    found: list[tuple[str, int]] = []

    for line in str(result.stdout or "").splitlines():
        fields = line.split()

        if len(fields) < 4:
            continue

        local = fields[3]
        host, _, port_text = local.rpartition(":")

        if not port_text.isdigit():
            continue

        host = host.strip("[]")

        # A wildcard bind is reachable over loopback; a specific one is only
        # reachable at that address.
        if host in ("*", "0.0.0.0", "::", ""):
            host = "127.0.0.1"

        found.append((host, int(port_text)))

    return found


# Any of these means "a web server answered here". 404 deliberately is not
# in the list: it means the socket is the panel's but the base path is wrong.
_PANEL_OK_STATUSES = ("200", "301", "302", "303", "307", "308", "401", "403")


async def _probe_panel(
    conn: asyncssh.SSHClientConnection,
    host: str,
    port: int,
    paths: tuple[str, ...],
) -> tuple[str, str] | None:
    """Finds the panel on one socket. Returns (scheme, path), or None.

    Both the scheme and the base path have to be discovered rather than
    assumed. 3x-ui serves TLS whenever a certificate is configured (-k
    accepts the self-signed one it generates for itself), and the base path
    it ends up serving is not necessarily the one we asked for.

    The status code is what decides, not curl's exit code: -f would collapse
    "wrong base path" (404) and "no panel here" into the same failure, and
    those need different responses -- try another path versus try another
    socket. Anything that is not a 404 is the panel answering, including the
    401/403 an authenticated route returns before login.
    """

    for scheme in ("http", "https"):
        for path in paths:
            url = f"{scheme}://{host}:{port}{path}"
            result = await conn.run(
                "curl -sk -o /dev/null -w '%{http_code}' "
                f"--max-time 10 {shlex.quote(url)}",
                check=False,
                timeout=30,
                input="",
            )

            status = str(result.stdout or "").strip()

            if status in _PANEL_OK_STATUSES:
                return scheme, path

    return None


async def _wait_for_panel(
    conn: asyncssh.SSHClientConnection,
    paths: tuple[str, ...],
    expected_port: int,
    *,
    timeout_seconds: int = PANEL_START_TIMEOUT_SECONDS,
    secrets_to_redact: tuple[str, ...] = (),
) -> tuple[str, int, str]:
    """Waits for the 3x-ui panel to answer and reports where it actually is.

    Returns (scheme, port, path). All three can differ from what was asked
    for: what the settings table says and what the process bound are not the
    same thing, the panel serves TLS whenever a certificate is configured,
    and the base path it ends up serving need not be the one we requested.

    `x-ui restart` returns as soon as systemd has started the unit, but the
    Go process still has to open its listener -- on a small VPS that gap is
    longer than a fixed sleep allows. A refused connection also fails
    instantly, so curl's --max-time gives no grace at all; the retry loop is
    what actually gives the panel time.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    # One restart partway through, in case the first `enable --now` raced the
    # installer finishing writing its config.
    retry_restart_at = loop.time() + timeout_seconds / 3
    restarted = False

    while True:
        listeners = await _xui_listeners(conn)

        # Prefer the port we asked for, then loopback, then anything x-ui
        # owns -- the panel is usually not the only socket it holds (the
        # subscription server has its own).
        candidates = sorted(
            listeners,
            key=lambda item: (item[1] != expected_port, item[0] != "127.0.0.1"),
        )

        for host, port in candidates:
            found = await _probe_panel(conn, host, port, paths)
            if found is not None:
                scheme, path = found
                return scheme, port, path

        now = loop.time()

        if now >= deadline:
            break

        if not restarted and now >= retry_restart_at:
            restarted = True
            await conn.run(
                "systemctl restart x-ui",
                check=False,
                timeout=60,
                input="",
            )

        await asyncio.sleep(3)

    diagnostics = await _panel_diagnostics(conn, expected_port, secrets_to_redact)

    seen = ", ".join(f"{h}:{p}" for h, p in listeners) or "none"

    tried = ", ".join(paths) or "(none)"

    raise NodeBootstrapError(
        f"3x-ui panel did not answer within {timeout_seconds}s. "
        f"Sockets x-ui was listening on: {seen} (expected port "
        f"{expected_port}). Base paths tried: {tried}.\n\n"
        f"{diagnostics}"
    )


async def _pick_node_sni(
    conn: asyncssh.SSHClientConnection,
) -> str:
    """Picks the REALITY dest by measuring, from the node itself, which
    candidate answers a TLS 1.3 handshake fastest.

    Latency matters here rather than just reachability: every REALITY
    connection makes the node handshake with this host, so the closest
    candidate is the one that costs users the least. Probes run in parallel
    on the node and the whole set is measured in one SSH round trip.
    """

    probes = " ".join(
        f"probe {shlex.quote(host)} &" for host in SNI_CANDIDATES
    )

    command = (
        "probe() { "
        "t=$(curl -fsSIL --http1.1 --tlsv1.3 "
        "--connect-timeout 5 --max-time 10 "
        "-o /dev/null -w '%{time_total}' \"https://$1/\" 2>/dev/null) "
        "&& echo \"$1 $t\"; "
        "}; "
        f"{probes} "
        "wait"
    )

    result = await _run(conn, command, timeout=30)

    ranked: list[tuple[float, str]] = []

    for line in str(result.stdout or "").splitlines():
        parts = line.split()

        if len(parts) != 2:
            continue

        try:
            ranked.append((float(parts[1]), parts[0]))
        except ValueError:
            continue

    if not ranked:
        raise NodeBootstrapError(
            "the node could not complete a TLS 1.3 handshake "
            "with any configured REALITY SNI"
        )

    ranked.sort()

    return ranked[0][1]

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

            # Everything below must go through the Go binary, never
            # /usr/bin/x-ui: that one is the management shell script, and it
            # silently prints its help menu (exit 0) for `setting` and
            # `migrate`, so those calls looked like they succeeded while
            # changing nothing at all.
            xui = shlex.quote(await _find_xui_binary(conn))

            await _run(
                conn,
                f"{xui} migrate",
                timeout=120,
            )

            # --------------------------------------------------------
            # Force credentials / port
            # --------------------------------------------------------

            await _run(
                conn,
                (
                    f"{xui} setting "
                    f"-username {shlex.quote(panel_login)} "
                    f"-password {shlex.quote(panel_password)} "
                    f"-port {panel_port} "
                    f"-webBasePath {shlex.quote(web_base_path)}"
                ),
            )

            # The panel binds to listenIP:port. A listenIP pinned to one
            # address means loopback is refused, which looks exactly like
            # "the panel never started" -- and the installer does pin it to
            # 127.0.0.1 in some SSL modes. Clear it so it binds every
            # interface; the firewall, not the bind address, is what keeps
            # the panel private. Tolerated if the build has no such flag.
            await conn.run(
                f'{xui} setting -listenIP ""',
                check=False,
                timeout=60,
                input="",
            )

            await _run(
                conn,
                "systemctl restart x-ui",
                timeout=120,
            )

            # --------------------------------------------------------
            # Read actual 3x-ui settings
            # --------------------------------------------------------

            settings_result = await conn.run(
                f"{xui} setting -show true",
                check=False,
                timeout=60,
                input="",
            )

            settings_output = str(settings_result.stdout or "")

            reported_port = _extract_setting(settings_output, "port")
            reported_path = _extract_setting(settings_output, "webBasePath")

            # The installer also records what it actually applied in
            # /etc/x-ui/install-result.env. That file is the only place the
            # API token is ever exposed -- the panel keeps just its SHA-256
            # hash, so a token not captured here can never be read back, only
            # replaced -- and it is a useful second opinion on port/path.
            install_result = await conn.run(
                "cat /etc/x-ui/install-result.env",
                check=False,
                timeout=30,
                input="",
            )

            applied: dict[str, str] = {}

            if install_result.exit_status == 0:
                applied = _parse_install_result(
                    str(install_result.stdout or "")
                )

            api_token = applied.get("XUI_API_TOKEN") or None

            actual_port = (
                int(reported_port)
                if reported_port and reported_port.isdigit()
                else panel_port
            )

            # --------------------------------------------------------
            # Find the panel and verify it locally
            # --------------------------------------------------------

            # Every base path worth trying, best guess first. None of these
            # is trusted: whichever one the panel actually answers on wins.
            # A wrong guess here is invisible -- it just 404s -- which is how
            # a panel that was up the whole time read as "never started".
            candidate_paths: list[str] = []

            for candidate in (
                reported_path,
                applied.get("XUI_WEB_BASE_PATH"),
                web_base_path,
                "/",
            ):
                if not candidate:
                    continue
                normalized = _normalize_web_base_path(candidate)
                if normalized not in candidate_paths:
                    candidate_paths.append(normalized)

            # Where the panel *is*, not where the settings say it should be.
            panel_scheme, actual_port, actual_path = await _wait_for_panel(
                conn,
                tuple(candidate_paths),
                actual_port,
                # The diagnostics quote the node's own logs; make sure the
                # panel password can't ride along into an alert row.
                secrets_to_redact=(panel_password,),
            )

            # --------------------------------------------------------
            # UFW
            # --------------------------------------------------------

            # A 3x-ui panel listens on 0.0.0.0 and is a known target, so it
            # must not stay reachable from the internet. Ubuntu ships ufw
            # installed but INACTIVE, which is why simply adding rules is not
            # enough -- an inactive ufw enforces nothing, and the panel port
            # stays wide open. Rules are added first and ufw is enabled last,
            # so the SSH session this runs over is already allowed by the
            # time enforcement starts.

            ufw = await conn.run(
                "command -v ufw",
                check=False,
                timeout=10,
                input="",
            )

            if ufw.exit_status == 0:

                # SSH first, and before enabling: getting this wrong locks
                # the admin out of their own server.
                await _run(
                    conn,
                    f"ufw allow {ssh_port}/tcp",
                )

                # 443 is the VLESS/REALITY port -- that one is the whole
                # point and has to be world-reachable.
                await _run(
                    conn,
                    "ufw allow 443/tcp",
                )

                # The 3x-ui panel is not. Only the main server ever calls
                # it, so scope it to the address we are connecting from
                # rather than leaving an admin panel exposed to the
                # internet. $SSH_CONNECTION's first field is the client
                # address as the node sees it, which is exactly the
                # origin the panel needs to accept.
                scoped = await conn.run(
                    (
                        'set -- $SSH_CONNECTION; src=$1; '
                        'if [ -n "$src" ]; then '
                        f'ufw allow from "$src" to any port {actual_port} proto tcp; '
                        "else exit 1; fi"
                    ),
                    check=False,
                    timeout=30,
                    input="",
                )

                if scoped.exit_status != 0:
                    # No SSH_CONNECTION to key off (unusual, but possible
                    # behind some proxies). Fall back to opening the port
                    # rather than locking ourselves out of the panel.
                    await _run(
                        conn,
                        f"ufw allow {actual_port}/tcp",
                    )

                status = await conn.run(
                    "ufw status",
                    check=False,
                    timeout=10,
                    input="",
                )

                if "Status: active" not in str(status.stdout):
                    # --force skips the interactive "may disrupt existing ssh
                    # connections" prompt, which would otherwise hang forever
                    # on a channel with no tty.
                    await _run(
                        conn,
                        "ufw --force enable",
                        timeout=60,
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
                    panel_scheme,
                ),
                panel_port=actual_port,
                panel_web_base_path=actual_path,
                sni=sni,
                panel_login=panel_login,
                panel_password=panel_password,
                panel_api_token=api_token,
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