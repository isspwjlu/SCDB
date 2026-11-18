"""Survive a network outage without losing the work in flight.

The host is a laptop on a connection that drops: Wi-Fi sleeps, a VPN tunnel
re-resolves, the uplink flaps. Every layer that touches the network needs to
distinguish "this failed" from "the network is gone", because the second must
be waited out rather than retried immediately or given up on.

Three pieces:

* :func:`wait_for_network` — block until a host answers, with a cheap TCP
  connect probe rather than a full HTTP request.
* :class:`NetworkResilient` — wraps an awaitable that may raise a transport
  error, retrying with capped exponential backoff plus jitter until the
  deadline. Jitter matters: without it, every concurrent worker wakes on the
  same schedule and stampedes the moment connectivity returns.
* :func:`with_network_retry` — the common case, wrapping one call.

The retry budget is deliberately long (15 minutes by default). A ingest loop
that is waiting on the network is not doing useful work anyway, and giving up
after 30 seconds means a two-minute outage silently discards a paper.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
from typing import Awaitable, Callable, TypeVar

import httpx

log = logging.getLogger(__name__)

T = TypeVar("T")

# Transport failures that mean "the network or the server is unreachable",
# as opposed to a bad request that will fail identically on retry.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
    ConnectionError,
    socket.gaierror,
    OSError,
)

DEFAULT_DEADLINE_S = 900.0
DEFAULT_INITIAL_BACKOFF_S = 2.0
DEFAULT_MAX_BACKOFF_S = 60.0
_PROBE_INTERVAL_S = 5.0


def _probe_host(host: str = "api.openalex.org", port: int = 443) -> bool:
    """True when a TCP connect to ``host`` succeeds.

    A connect-only probe is cheap enough to run every few seconds and does not
    depend on any HTTP service being healthy, only on the network path being
    up — which is what a machine that lost Wi-Fi needs to know.
    """
    try:
        with socket.create_connection((host, port), timeout=4):
            return True
    except OSError:
        return False


async def wait_for_network(
    *,
    max_wait_s: float = 1800.0,
    interval_s: float = _PROBE_INTERVAL_S,
    host: str = "api.openalex.org",
) -> bool:
    """Block until the network answers, or ``max_wait_s`` elapses.

    Returns True when connectivity was regained, False on timeout. The caller
    decides what to do then; typically it keeps looping rather than exiting,
    because a machine left running overnight should start working again when
    the link comes back.
    """
    waited = 0.0
    announced = False
    # ``max_wait_s <= 0`` means "wait indefinitely". An unattended collector
    # must survive an outage of any length: a 30-minute cap meant a night-time
    # dropout ended ingestion until a person noticed.
    while max_wait_s <= 0 or waited < max_wait_s:
        if await asyncio.to_thread(_probe_host, host):
            if announced:
                log.info("network restored after %.0fs", waited)
            return True
        if not announced:
            log.warning("network unreachable — pausing ingestion until it returns")
            announced = True
        await asyncio.sleep(interval_s)
        waited += interval_s
    log.error("network still down after %.0fs; giving up on this call", waited)
    return False


async def with_network_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    description: str = "",
    deadline_s: float = DEFAULT_DEADLINE_S,
    initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S,
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
) -> T | None:
    """Run ``factory`` again while it fails on the network.

    ``factory`` is a callable rather than a coroutine so each attempt starts
    a fresh request. Returns None when the deadline passes, which callers must
    handle — a discarded paper is better than a dead process, but it should be
    logged.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    backoff = initial_backoff_s
    attempt = 0
    while True:
        attempt += 1
        try:
            return await factory()
        except NETWORK_ERRORS as exc:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.error(
                    "giving up on %s after %d attempts and %.0fs: %s",
                    description or "operation", attempt, deadline_s, exc,
                )
                return None
            # Jitter spreads the retries so concurrent workers do not all wake
            # at once when the link comes back and stampede the API.
            sleep_for = min(backoff, remaining) * (0.7 + 0.6 * random.random())
            log.warning(
                "%s failed (attempt %d): %s — retrying in %.0fs",
                description or "operation", attempt, exc, sleep_for,
            )
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, max_backoff_s)
