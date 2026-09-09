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
# Lowered 8 -> 5. SEC's published ceiling is 10/s per requester, but "requester"
# is the egress IP, which on a shared host is not ours alone — and production
# took a 429 on the very first request of the process, before our own bucket had
# issued anything worth throttling. 5/s keeps every feed comfortably fast (a
# poll is one feed read plus a body per new filing) while leaving real headroom
# for whatever else shares the address.
MAX_RPS = float(os.getenv("SEC_MAX_RPS", "5"))
REQUEST_TIMEOUT = int(os.getenv("SEC_TIMEOUT", "30"))
# Transport failures (timeout, connection reset). Throttling has its own budget.
MAX_RETRIES = int(os.getenv("SEC_MAX_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("SEC_RETRY_DELAY", "5"))
# A 429 clears by waiting, so it is worth waiting properly: 5s, 10s, 20s, 40s,
# 60s, 60s. The old code gave a throttled request three flat 5-second tries and
# then abandoned it, which is how the CIK map — fetched once, at boot, with no
# fallback — was lost for the entire life of the process.
MAX_THROTTLE_RETRIES = int(os.getenv("SEC_MAX_THROTTLE_RETRIES", "6"))
MAX_THROTTLE_BACKOFF = float(os.getenv("SEC_MAX_THROTTLE_BACKOFF", "60"))


def _retry_after_seconds(resp):
    """SEC's own Retry-After, when it sends one. Beats any guess we make."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), MAX_THROTTLE_BACKOFF))
    except (TypeError, ValueError):
        return None

# ── Request-rate limiter ─────────────────────────────────────────────────────
# Shared across every event loop and thread, because SEC counts requests per
# requester, not per loop -- and each poll runs in its own asyncio.run() on the
# SEC thread pool, so a per-loop limiter would let five concurrent pollers each
# believe they had the full budget.
_rate_lock = threading.Lock()
_request_times = deque()

# Shared "everybody wait" deadline, set whenever any caller is told 429.
#
# THE 429 STORM THIS FIXES. The token bucket above paces us to MAX_RPS, but
# it has no idea whether SEC is currently refusing us — and SEC counts per
# requester, across every poller and every event loop. Observed in production
# 2026-09-09 at boot: company_tickers.json 429'd three times while the 8-K,
# 10-Q, 10-K and Form 4 feeds each independently 429'd and retried, all inside
# fifteen seconds. Each caller backed off privately and then walked straight
# back into the same exhausted window, so the retries themselves kept the
# throttle alive and the CIK map never loaded. One shared deadline means the
# first 429 parks everyone until it plausibly clears.
_cooldown_until = [0.0]


def _set_cooldown(seconds: float):
    with _rate_lock:
        _cooldown_until[0] = max(_cooldown_until[0], time.monotonic() + seconds)


def _cooldown_remaining() -> float:
    with _rate_lock:
        return max(0.0, _cooldown_until[0] - time.monotonic())


async def _await_rate_slot():
    while True:
        with _rate_lock:
            now = time.monotonic()

            cooling = _cooldown_until[0] - now
            if cooling > 0:
                wait = cooling
            else:
                while _request_times and (now - _request_times[0]) >= 1.0:
                    _request_times.popleft()
                if len(_request_times) < MAX_RPS:
                    _request_times.append(now)
                    return
                wait = 1.0 - (now - _request_times[0]) + 0.01
        await asyncio.sleep(min(max(wait, 0.01), 5.0))


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

    attempt = 0
    throttle_attempt = 0

    while True:
        await _await_rate_slot()
        try:
            async with session.get(url) as resp:
                # A 429 is not a failure of this request, it is a statement
                # about the next few seconds for EVERY request. It therefore
                # gets its own retry budget (being throttled is recoverable by
                # waiting, unlike a 404), an exponential back-off rather than a
                # flat 5s that was far shorter than SEC's window, and it parks
                # every other in-flight caller behind the same deadline.
                if resp.status in (429, 503):
                    throttle_attempt += 1
                    if throttle_attempt > MAX_THROTTLE_RETRIES:
                        logger.error("[SEC] still %s after %d throttled attempts: %s",
                                     resp.status, MAX_THROTTLE_RETRIES, url)
                        return None
                    delay = _retry_after_seconds(resp) or min(
                        RETRY_DELAY * (2 ** (throttle_attempt - 1)), MAX_THROTTLE_BACKOFF)
                    delay += random.random()
                    _set_cooldown(delay)
                    logger.warning("[SEC] %s on %s — all SEC calls backing off %.1fs "
                                   "(throttle attempt %d/%d)",
                                   resp.status, url, delay, throttle_attempt,
                                   MAX_THROTTLE_RETRIES)
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

        # Transport failures keep their own, separate budget. Sharing one
        # counter with throttling is what let three 429s exhaust the retries
        # for a request that had not actually failed yet.
        attempt += 1
        if attempt >= MAX_RETRIES:
            logger.error("[SEC] giving up on %s after %d attempts", url, MAX_RETRIES)
            return None
        await asyncio.sleep(RETRY_DELAY + random.random())


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


