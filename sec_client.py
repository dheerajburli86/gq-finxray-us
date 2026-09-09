"""
Async HTTP client for SEC EDGAR with per-poll session lifecycle.

edgar_poller_async.py calls:
    await sec_client.get_json(url)
    await sec_client.get_text(url)
    await sec_client.gather_limited(<generator of coroutines>)
    await sec_client.close_session()

Each asyncio.run() poll creates its own session to avoid event loop binding issues.
Requests within a poll share the session for connection pooling.
"""

import asyncio
import logging
import os
import random
import threading
import time
from collections import deque

import aiohttp

logger = logging.getLogger(__name__)

USER_AGENT = os.getenv("SEC_USER_AGENT", "GQuants FinXray admin@gquants.com")

# Concurrency and REQUEST RATE are different limits and SEC enforces the rate
# one. Its fair-access policy is 10 requests/second per requester; exceeding it
# gets the whole IP throttled, which would cost far more latency than it saves.
# So allow a healthy number of sockets in flight (bursts of filings pipeline
# properly) and let the token bucket below hold the actual issue rate under
# SEC's ceiling. Raising SEC_MAX_CONCURRENCY alone can never breach the policy
# because every request still passes the rate limiter first.
MAX_CONCURRENCY = int(os.getenv("SEC_MAX_CONCURRENCY", "20"))
MAX_RPS = float(os.getenv("SEC_MAX_RPS", "8"))
REQUEST_TIMEOUT = int(os.getenv("SEC_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("SEC_MAX_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("SEC_RETRY_DELAY", "5"))

# ── Request-rate limiter ─────────────────────────────────────────────────────
# Shared across every event loop and thread, because SEC counts requests per
# requester, not per loop -- and each poll runs in its own asyncio.run() on the
# SEC thread pool, so a per-loop limiter would let five concurrent pollers each
# believe they had the full budget.
_rate_lock = threading.Lock()
_request_times = deque()


async def _await_rate_slot():
    while True:
        with _rate_lock:
            now = time.monotonic()
            while _request_times and (now - _request_times[0]) >= 1.0:
                _request_times.popleft()
            if len(_request_times) < MAX_RPS:
                _request_times.append(now)
                return
            wait = 1.0 - (now - _request_times[0]) + 0.01
        await asyncio.sleep(min(max(wait, 0.01), 1.0))


# ── Session reuse ────────────────────────────────────────────────────────────
# One session per event loop, not one per REQUEST. Building a ClientSession and
# TCPConnector for every call meant a fresh TCP connect plus TLS handshake on
# every single fetch -- roughly 100-300ms of pure setup that a keep-alive
# connection pays once. A poll that fetches 30 filing documents was doing 30
# handshakes. It also made the connector's `limit` meaningless: each session
# had its own limit of 5 and there was one session per request, so nothing was
# ever actually bounded.
#
# Keyed by loop id because aiohttp binds a session to the loop that created it,
# and every poll runs under its own asyncio.run(); a single global session
# would raise "Event loop is closed" on the second poll.
_sessions: dict[int, aiohttp.ClientSession] = {}


def _get_session() -> aiohttp.ClientSession:
    loop = asyncio.get_running_loop()
    key = id(loop)
    session = _sessions.get(key)
    if session is None or session.closed:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            connector=aiohttp.TCPConnector(
                limit=MAX_CONCURRENCY, ttl_dns_cache=300, force_close=False),
        )
        _sessions[key] = session
    return session


async def close_session():
    """
    Close this loop's session. Call before the loop ends -- a session outlives
    its loop otherwise and aiohttp warns about the unclosed connector.
    """
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        return
    session = _sessions.pop(key, None)
    if session and not session.closed:
        await session.close()


async def _fetch(url: str, as_json: bool):
    """Single GET, rate-limited, over this loop's shared keep-alive session."""
    session = _get_session()

    for attempt in range(MAX_RETRIES):
        await _await_rate_slot()
        try:
            async with session.get(url) as resp:
                if resp.status in (429, 503):
                    delay = RETRY_DELAY + random.random()
                    logger.warning("[SEC] %s on %s — backing off %.1fs",
                                   resp.status, url, delay)
                    await asyncio.sleep(delay)
                    continue

                if resp.status != 200:
                    logger.warning("[SEC] HTTP %s for %s", resp.status, url)
                    return None

                if as_json:
                    return await resp.json(content_type=None)
                return await resp.text()

        except asyncio.TimeoutError:
            logger.warning("[SEC] timeout on %s (attempt %d/%d)", url, attempt + 1, MAX_RETRIES)
        except aiohttp.ClientError as e:
            logger.warning("[SEC] client error on %s: %s", url, e)
        except Exception as e:
            logger.warning("[SEC] error on %s: %s", url, e)
            return None

        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_DELAY + random.random())

    logger.error("[SEC] giving up on %s after %d attempts", url, MAX_RETRIES)
    return None


async def get_json(url: str):
    """GET and parse as JSON."""
    return await _fetch(url, as_json=True)


async def get_text(url: str):
    """GET and return text."""
    return await _fetch(url, as_json=False)


async def gather_limited(coros, limit: int | None = None):
    """Run coroutines concurrently with optional limit."""
    if limit is None:
        limit = MAX_CONCURRENCY

    sem = asyncio.Semaphore(limit)

    async def _bounded(coro):
        async with sem:
            return await coro

    return await asyncio.gather(
        *(_bounded(c) for c in coros),
        return_exceptions=True,
    )


