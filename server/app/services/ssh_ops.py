"""SSH primitives shared by node bootstrap.

Extracted from the 3x-ui bootstrap when that was removed: none of this was
ever about which panel the node runs. Running a remote command and having
its failure carry the remote output, and choosing the REALITY dest by
measuring from the node itself rather than from wherever the main server
happens to sit, are properties of managing a VPS over SSH.
"""

from __future__ import annotations

import asyncio
import shlex

import asyncssh


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


async def run_remote(
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


async def pick_node_sni(
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

    result = await run_remote(conn, command, timeout=30)

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
