"""Picks a "dest" domain for a node's REALITY inbound to impersonate.

REALITY needs a real, popular TLS 1.3 site the node can reach and that
reasonably matches locally-plausible traffic; xray then camouflages the
node's handshake as a connection to that site. Candidates are probed
concurrently with a real TLS handshake and the fastest reachable one wins.

TODO(Etap 1+): this probe should ideally run from the node itself over SSH,
since what matters is reachability/latency from the node's own network, not
from wherever the main server happens to sit. Running it centrally here is a
reasonable approximation until node bootstrap-over-SSH exists.
"""

from __future__ import annotations

import asyncio
import ssl
import time

CANDIDATE_SNIS = [
    "www.microsoft.com",
    "www.apple.com",
    "dl.google.com",
    "www.cloudflare.com",
    "aws.amazon.com",
    "www.swift.org",
]


class NoWorkingSNIError(RuntimeError):
    pass


async def _probe_one(host: str, timeout: float) -> float | None:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3

    start = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ctx, server_hostname=host), timeout=timeout
        )
    except Exception:
        return None

    elapsed_ms = (time.monotonic() - start) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 -- best-effort close, probe result already captured
        pass
    return elapsed_ms


async def pick_working_sni(candidates: list[str] | None = None, timeout: float = 5.0) -> str:
    """Probes `candidates` concurrently and returns the fastest one that
    completed a real TLS 1.3 handshake. Raises NoWorkingSNIError if none did."""
    candidates = candidates or CANDIDATE_SNIS
    results = await asyncio.gather(*(_probe_one(host, timeout) for host in candidates))

    reachable = [(host, ms) for host, ms in zip(candidates, results) if ms is not None]
    if not reachable:
        raise NoWorkingSNIError(f"none of {candidates} completed a TLS 1.3 handshake")

    reachable.sort(key=lambda pair: pair[1])
    return reachable[0][0]
