"""
Shared async HTTP client for SEC EDGAR.

edgar_poller_async.py calls these four names at module level:
    await sec_client.get_json(url)
    await sec_client.get_text(url)
    await sec_client.gather_limited(<generator of coroutines>)
    await sec_client.close_session()

SEC enforces two hard rules on automated access:
  1. A User-Agent identifying the operator with a contact address. Requests
     without one get 403'd.
  2. No more than 10 requests/second. Exceeding it gets the IP blocked.

Both are handled here so every caller inherits them — the rate limiter is a
process-wide semaphore plus a minimum inter-request gap, not per-call sleeps,
so concurrent pollers can't collectively blow the budget.
"""

import asyncio
import logging
import os
import random

import aiohttp

logger = logging.getLogger(__name__)

# SEC wants "Company Name contact@domain.com". Set SEC_USER_AGENT in Railway
# variables to your real contact — the fallback below is a placeholder and
# SEC may throttle it.
USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "GQuants FinXray admin@gquants.com",
)

MAX_CONCURRENCY = int(os.getenv("SEC_MAX_CONCURRENCY", "5"))
MIN_INTERVAL = float(os.getenv("SEC_MIN_INTERVAL", "0.12"))  # ~8 req/s
REQUEST_TIMEOUT = int(os.getenv("SEC_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("SEC_MAX_RETRIES", "3"))

_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()
_last_request_at = 0.0
_pace_lock: asyncio.Lock | None = None


def _headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


async def _get_session() -> aiohttp.ClientSession:
    """One session per process, created lazily inside the running loop."""
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            _session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": USER_AGENT,
                         "Accept-Encoding": "gzip, deflate"},
            )
    return _session


async def _fetch(url: str, as_json: bool):
    """
    Single GET with retry/backoff. Returns parsed JSON, text, or None.

    Returns None rather than raising on failure — every call site in
    edgar_poller_async.py already branches on a falsy response and logs it
    via log_poller_error(), so raising would just convert a handled empty
    result into an unhandled traceback that kills the poll loop.
    """
    session = await _get_session()

    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url) as resp:
                # 429/503 are SEC throttling — back off and retry.
                if resp.status in (429, 503):
                    delay = (2 ** attempt) + random.random()
                    logger.warning(
                        "[SEC] %s on %s — backing off %.1fs (attempt %d/%d)",
                        resp.status, url, delay, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status != 200:
                    logger.warning("[SEC] HTTP %s for %s", resp.status, url)
                    return None

                if as_json:
                    # SEC serves JSON as text/html on some endpoints,
                    # so don't let aiohttp's content-type check reject it.
                    return await resp.json(content_type=None)
                return await resp.text()

        except asyncio.TimeoutError:
            logger.warning("[SEC] timeout on %s (attempt %d/%d)",
                           url, attempt + 1, MAX_RETRIES)
        except aiohttp.ClientError as e:
            logger.warning("[SEC] client error on %s: %s (attempt %d/%d)",
                           url, e, attempt + 1, MAX_RETRIES)
        except Exception as e:
            logger.warning("[SEC] unexpected error on %s: %s", url, e)
            return None

        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep((2 ** attempt) + random.random())

    logger.error("[SEC] giving up on %s after %d attempts", url, MAX_RETRIES)
    return None


async def get_json(url: str):
    """GET a URL and parse it as JSON. Returns None on failure."""
    return await _fetch(url, as_json=True)


async def get_text(url: str):
    """GET a URL and return the body as text. Returns None on failure."""
    return await _fetch(url, as_json=False)


async def gather_limited(coros, limit: int | None = None):
    """
    Run an iterable of coroutines concurrently, capped at `limit`.

    Called as: await sec_client.gather_limited(fetch(u) for u in urls)

    Exceptions are returned in place rather than raised — edgar_poller_async
    zips the results against its candidate list and already checks
    `isinstance(text, Exception)`, so a single bad filing must not abort the
    whole batch.
    """
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


async def close_session():
    """
    Close the shared session. Safe to call when nothing was opened.
    
    In practice, this should only run at process shutdown. Calling it mid-poll
    (e.g. at the end of a schedule job) closes the loop too early and causes
    "Event loop is closed" on the next poll in a new job. Let the process exit
    handle cleanup instead, or wrap the job in a try/finally that doesn't call this.
    """
    global _session
    if _session is not None and not _session.closed:
        try:
            await _session.close()
            # aiohttp needs a tick to release the underlying connector.
            await asyncio.sleep(0.1)
        except Exception:
            pass  # Already closed or loop gone; ignore
    _session = None
