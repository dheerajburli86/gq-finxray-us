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

import aiohttp

logger = logging.getLogger(__name__)

USER_AGENT = os.getenv("SEC_USER_AGENT", "GQuants FinXray admin@gquants.com")
MAX_CONCURRENCY = int(os.getenv("SEC_MAX_CONCURRENCY", "5"))
REQUEST_TIMEOUT = int(os.getenv("SEC_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("SEC_MAX_RETRIES", "3"))

# Thread-local session per asyncio task/loop
_current_session: aiohttp.ClientSession | None = None


async def _get_or_create_session() -> aiohttp.ClientSession:
    """Get or create a session for this poll run."""
    global _current_session
    if _current_session is None or _current_session.closed:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        _current_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            connector=aiohttp.TCPConnector(limit=MAX_CONCURRENCY, force_close=False),
        )
    return _current_session


async def _fetch(url: str, as_json: bool):
    """Single GET with retry/backoff."""
    session = await _get_or_create_session()

    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url) as resp:
                if resp.status in (429, 503):
                    delay = (2 ** attempt) + random.random()
                    logger.warning("[SEC] %s on %s — backing off %.1fs", resp.status, url, delay)
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
            await asyncio.sleep((2 ** attempt) + random.random())

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


async def close_session():
    """Close the session for this poll run."""
    global _current_session
    if _current_session is not None and not _current_session.closed:
        try:
            await _current_session.close()
            await asyncio.sleep(0.1)
        except Exception:
            pass
    _current_session = None
