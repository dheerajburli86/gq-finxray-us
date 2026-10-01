"""
main.py
GQ FinXray US — process entry point: scheduler, AI pipeline, delivery loop.

THREE THREADS
-------------
  scheduler  — every poller, on its own cadence, across two worker pools
  pipeline   — drains PENDING raw_filings through ai_pipeline.py
  delivery   — fans finished alerts out per user (runs on the main thread)

TIMEZONE
--------
The process runs on US/Eastern. `schedule`'s .at() matches LOCAL time, and every
market time in this file is an ET wall-clock time, so the two have to agree. The
old version scheduled in UTC with a comment saying so, which silently drifted by
an hour at every DST transition and put the "midday" heatmap at 9:30am half the
year. Setting TZ here makes .at("12:30") mean 12:30 ET all year round.
"""

import os
import time

# Must run before `schedule` (or anything else) reads the clock.
os.environ.setdefault("TZ", "America/New_York")
if hasattr(time, "tzset"):
    time.tzset()

import asyncio
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import schedule
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
# THE BUG THIS FIXES. There was no basicConfig anywhere in the runtime path —
# every call in the repo sits inside an `if __name__ == "__main__"` block, which
# never executes under `python main.py`. So the root logger had no handler and
# Python's last-resort handler only emitted WARNING and above. Every logger.info
# in the codebase was silent, including delivery.py's per-cycle
# "sent=/failed=/no_audience=" line — the single number that would have shown
# that market-wide alerts were being built and then dropped. The only output
# anyone ever saw was raw print() from the SEC pollers, which is why the logs
# looked like SEC polling was the only thing running.
logging.basicConfig(
    level=os.getenv("GQ_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Third-party libraries are chatty at INFO and drown out our own lines.
for noisy in ("httpx", "httpcore", "hpack", "telegram", "asyncio", "urllib3", "PIL"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("main")

# ── Latency budget ────────────────────────────────────────────────────────────
# End-to-end delay is the sum of three queue drains: SEC poll -> AI pipeline ->
# delivery fan-out. Each drain is a single indexed Supabase query when its queue
# is empty, so tightening the idle gaps costs queries, not tokens or API quota.
#
# Both loops now only sleep when their queue came back EMPTY (see run_pipeline
# and delivery_loop below), so these are idle-poll intervals, not per-batch
# pauses. An idle tick is one indexed query returning zero rows; the bulk
# expiry UPDATEs both loops run are separately throttled to once a minute, so
# dropping 3s -> 1s triples a cheap read and adds no writes.
PIPELINE_IDLE_SECONDS = int(os.getenv("GQ_PIPELINE_IDLE_SECONDS", "1"))
DELIVERY_IDLE_SECONDS = int(os.getenv("GQ_DELIVERY_IDLE_SECONDS", "1"))
# 8-K and Form 4 are the time-critical ones (material events, insider trades).
SEC_FAST_POLL_SECONDS = int(os.getenv("GQ_SEC_POLL_SECONDS", "15"))
# S-1 is Feature 8's early-warning half and now DOES produce alerts, so this is
# a real detection delay. Off the fast lane only because it is market-wide.
SEC_S1_POLL_MINUTES = int(os.getenv("GQ_SEC_S1_POLL_MINUTES", "5"))

from delivery import deliver_pending_alerts as delivery_deliver
from ai_pipeline import run_pipeline as process_with_ai

# ── Feature 1 / 5 / 8-trigger: SEC EDGAR ──────────────────────────────────────
from edgar_poller_async import (poll_sec_8k, poll_sec_form4, poll_sec_10q,
                                poll_sec_10k, poll_sec_s1, load_cik_map,
                                ensure_cik_map)
# ── Feature 2: Company & Sector News ──────────────────────────────────────────
from news_poller import poll_all_news
from fmp_poller import poll_fmp_news, poll_fmp_events      # Features 2, 4, 5
# ── Feature 4: EPS surprise (the other half of the earnings feature) ──────────
from earnings_alerts import poll_earnings_for_tickers
# ── Feature 3: Result Snapshot ────────────────────────────────────────────────
from result_snapshot import process_pending_snapshots
# ── Feature 5: Large block/bulk trades ────────────────────────────────────────
from large_trades_poller import run_large_trades_poller
# ── Feature 6: Technical Alerts ───────────────────────────────────────────────
from technical_poller import run_technical_poller
# ── Feature 7: ETF Flow ───────────────────────────────────────────────────────
from etf_flow_poller import run_etf_flow_poller
# ── Feature 8: IPO Deep Dive ──────────────────────────────────────────────────
from ipo_poller import run_ipo_poller
# ── Feature 9: Sector Heatmap ─────────────────────────────────────────────────
from heatmap_generator import (run_sector_heatmap_midday, run_sector_heatmap_afternoon,
                               run_sector_heatmap_weekly, run_sector_heatmap_monthly)
# ── Feature 10: Earnings Call Transcripts ─────────────────────────────────────
from earnings_transcript_poller import run_earnings_transcript_poller
# ── Feature 11: Analyst Ratings & Price Targets ───────────────────────────────
from analyst_ratings_poller import poll_analyst_ratings
# ── Feature 12: Macro & Policy Digest + scheduled market reports ──────────────
from macro_policy_roundup import run_macro_policy_roundup
from market_reports import (send_premarket_report, send_market_open_report,
                            send_midday_report, send_market_close_report,
                            send_afterhours_report)
# ── Feature 13: Watchlist Heatmap ─────────────────────────────────────────────
from watchlist_heatmap import (run_watchlist_heatmap_midday,
                               run_watchlist_heatmap_eod)


# ── Delivery ──────────────────────────────────────────────────────────────────
async def deliver_pending_alerts():
    """Dispatch to delivery.py, which owns per-user routing, watchlist filtering,
    message rendering, alert_run_log and payload_log.

    Returns how many alerts left the queue, so delivery_loop can drain a backlog
    back-to-back instead of sleeping between batches.
    """
    try:
        return await delivery_deliver() or 0
    except Exception as e:
        logger.error("Delivery failed: %s", e)
        return 0


# ── Scheduler ─────────────────────────────────────────────────────────────────
# Sized for the worst alignment, not the average one. Fifteen non-SEC jobs run
# on this pool and their intervals (60s, 15m, 30m, 45m, 60m) all divide an hour,
# so at the top of every hour six or more fire in the same tick — at 6 workers
# the rest queued behind whichever slow FMP job took a worker first. FMP has no
# global rate limiter (only per-call 429 backoff), so this is deliberately 8 and
# not higher: enough to clear the hourly pile-up, not enough to turn a burst
# into a quota problem.
_JOB_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="job")
# SEC EDGAR gets its own lane. Sharing one pool meant the latency-critical 8-K
# and Form 4 polls queued behind whatever slow FMP/Massive/news/technical job
# happened to hold the workers — the heatmap and transcript jobs are minutes
# long, and six of them at once stalled SEC polling completely. A separate
# executor means an SEC tick never waits on a non-SEC job.
_SEC_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="sec")
_JOB_RUNNING = {}
_JOB_LOCK = threading.Lock()


def job(fn, pool=None):
    """
    Hand a scheduled job to a pool instead of running it inline.

    schedule.run_pending() executes jobs on the calling thread, so without this
    a 90-second technical_poller would hold up the 15-second SEC poll behind it.
    The running-set guard drops a tick if the previous run of that same job has
    not finished, which stops fast schedules from stacking work faster than it
    drains.
    """
    name = getattr(fn, "__name__", str(fn))

    def _submit():
        with _JOB_LOCK:
            if _JOB_RUNNING.get(name):
                logger.debug("Skipping %s — previous run still active", name)
                return
            _JOB_RUNNING[name] = True

        def _run():
            started = time.monotonic()
            try:
                fn()
            except Exception as e:
                logger.error("[SCHEDULER] %s failed: %s", name, e)
                traceback.print_exc()
            finally:
                took = time.monotonic() - started
                if took > 30:
                    logger.info("[SCHEDULER] %s took %.1fs", name, took)
                with _JOB_LOCK:
                    _JOB_RUNNING[name] = False

        (pool or _JOB_POOL).submit(_run)

    _submit.__name__ = f"job_{name}"
    return _submit


def sec_job(fn):
    """Schedule on the dedicated SEC lane so filings never queue behind FMP."""
    return job(fn, pool=_SEC_POOL)


def run_scheduler():
    logger.info("[SCHEDULER] Starting (timezone=%s)", time.tzname[0])
    load_cik_map()

    # Warm start in parallel — a serial block delayed the first scheduled tick
    # by however long the slowest poller took.
    for warm in (poll_sec_8k, poll_sec_form4, poll_sec_10q, poll_sec_10k):
        sec_job(warm)()
    for warm in (poll_all_news, poll_fmp_news, run_technical_poller,
                 run_etf_flow_poller, poll_analyst_ratings):
        job(warm)()

    # ── Features 1 & 5 — SEC EDGAR, the fast lane ─────────────────────────────
    # Every SEC form polls at the same fast interval. SEC EDGAR is where these
    # filings originate — FMP/Massive are downstream resellers reading this same
    # feed and republishing minutes later — so any gap here is latency we choose
    # to add to the one source that has the news first.
    #
    # Cost: SEC's fair-access policy allows 10 req/s. Four feeds at 15s is 16
    # requests/MINUTE, ~3% of the allowance, and each tick is one conditional
    # feed read that returns nothing when nothing has been filed.
    schedule.every(SEC_FAST_POLL_SECONDS).seconds.do(sec_job(poll_sec_8k))
    schedule.every(SEC_FAST_POLL_SECONDS).seconds.do(sec_job(poll_sec_form4))
    schedule.every(SEC_FAST_POLL_SECONDS).seconds.do(sec_job(poll_sec_10q))
    schedule.every(SEC_FAST_POLL_SECONDS).seconds.do(sec_job(poll_sec_10k))

    # ── S-1 — Feature 8's early-warning half ──────────────────────────────────
    # A company filing an S-1 is the first public signal it intends to go
    # public, and it lands here weeks to months before the deal appears on any
    # IPO calendar. EDGAR is the ONLY source for that leading edge: FMP's
    # ipos-calendar lists a deal once it is scheduled and priced, which is a
    # different (later) event. So this interval is a real detection delay.
    #
    # These rows used to be stored status="IPO_PENDING" and read by nothing —
    # the pipeline selects "PENDING", and ipo_poller resolved its S-1 link live
    # from FMP rather than from the table. They now enter the pipeline like any
    # other filing and route market-wide under source=SEC_IPO.
    #
    # Off the fast lane, not because it is unimportant but because it is the one
    # market-wide SEC poll: it cannot filter by watchlist (a pre-IPO registrant
    # is on nobody's), so a cold start fetches ~100 bodies (EDGAR_FEED_COUNT) at
    # sec_client's 8 req/s ceiling — about 13 seconds, which overran a 15s tick
    # and logged "Skipping poll_sec_s1 — previous run still active".
    #
    # 5 minutes is affordable because the expensive part is now bounded: the CIK
    # dedup in poll_edgar_generic_async drops amendments BEFORE any body is
    # fetched, so a steady-state tick costs one feed read plus a body only for
    # genuinely new registrants — a handful a day, not per tick.
    schedule.every(SEC_S1_POLL_MINUTES).minutes.do(sec_job(poll_sec_s1))

    # The CIK map is fetched once, at boot, from a rate-limited endpoint, at the
    # exact moment every other poller is also starting. When SEC answered 429 to
    # it three times on 2026-09-09 the loader gave up and the process ran its
    # whole life with an empty map — which silently disables every
    # watchlist-scoped SEC feature, because a filing whose CIK will not resolve
    # is indistinguishable from a filing for a company nobody watches. This is a
    # no-op once loaded, so it costs one boolean check per tick.
    schedule.every(10).minutes.do(sec_job(ensure_cik_map))

    # ── Feature 3 — Result Snapshot ───────────────────────────────────────────
    # The 10-Q/10-K poll only files the filing; this turns it into an alert, so
    # its interval adds directly on top of the poll's. It now also picks up 8-K
    # Item 2.02 earnings releases, which land weeks earlier than the 10-Q.
    # Moved off the SEC pool: it is a Supabase + XBRL/FMP job, not an EDGAR feed
    # read, and it was occupying an SEC worker every 30 seconds.
    schedule.every(30).seconds.do(job(process_pending_snapshots))

    # ── Feature 2 — News ──────────────────────────────────────────────────────
    schedule.every(60).seconds.do(job(poll_all_news))
    schedule.every(15).minutes.do(job(poll_fmp_news))

    # ── Features 4 & 5 — FMP events + large trades ────────────────────────────
    # De-prioritized relative to SEC: FMP re-surfaces the same material events
    # EDGAR already caught, and caught faster. These cover what EDGAR structurally
    # cannot — forward earnings calendars and off-exchange block prints.
    schedule.every(30).minutes.do(job(poll_fmp_events))
    # PAUSED: Feature 5 (Large Trades) — uncomment to re-enable
    # schedule.every(30).minutes.do(job(run_large_trades_poller))

    # Feature 4's OTHER half: the EPS surprise itself, not just the heads-up
    # that earnings are due. feature_map already listed EARNINGS_MISS and
    # EARNINGS_BEAT as Feature 4 filing types "emitted by earnings_alerts.py" —
    # but nothing ever called that module, and it carried an undefined-variable
    # bug that would have failed every insert if anything had. Both fixed.
    #
    # Hourly is affordable at any watchlist size: /stable/earnings-calendar has
    # no per-symbol filter, so this is ONE market-wide call per poll that is then
    # indexed by ticker locally — not one call per name. Actual EPS lands within
    # hours of the close, and the poller looks back 24h, so nothing is missed
    # between ticks; the interval only decides how quickly a surprise surfaces.
    schedule.every(60).minutes.do(job(poll_earnings_for_tickers))

    # ── Feature 6 — Technical Alerts ──────────────────────────────────────────
    schedule.every(45).minutes.do(job(run_technical_poller))

    # ── Feature 7 — ETF Flow ──────────────────────────────────────────────────
    schedule.every(60).minutes.do(job(run_etf_flow_poller))

    # ── Feature 8 — IPO Deep Dive ─────────────────────────────────────────────
    schedule.every().day.at("08:00").do(job(run_ipo_poller))

    # ── Feature 10 — Earnings Call Transcripts ────────────────────────────────
    schedule.every(30).minutes.do(job(run_earnings_transcript_poller))

    # ── Feature 11 — Analyst Ratings & Price Targets ──────────────────────────
    # FMP's consensus endpoints return current state, not an event feed, so the
    # poll interval IS the detection delay: a downgrade published one minute
    # after a tick waited the rest of the interval. Two-hourly made that up to
    # 119 minutes on a HIGH-impact alert.
    #
    # POLLING FASTER CANNOT DUPLICATE. _evaluate() compares the live snapshot
    # against _latest_prior_alert() — the last alert STORED for that ticker, not
    # the last poll — so an unchanged consensus is silently "nochange" however
    # often it is read. Four times the polls is four times the reads, not four
    # times the alerts.
    #
    # Cost is one FMP call plus one indexed Supabase lookup per watched ticker,
    # paced 0.1s apart inside the poller, and the _JOB_RUNNING guard drops a
    # tick if the previous pass is still running — so a watchlist too large to
    # finish in 30 minutes degrades to "as often as it can" instead of stacking.
    schedule.every(30).minutes.do(job(poll_analyst_ratings))

    # ── Feature 12 — Macro & Policy Digest + market reports ───────────────────
    # All ET wall-clock. Market hours are 09:30–16:00 ET.
    schedule.every().day.at("08:15").do(job(run_macro_policy_roundup))
    schedule.every().day.at("09:15").do(job(send_premarket_report))
    schedule.every().day.at("09:35").do(job(send_market_open_report))
    schedule.every().day.at("13:00").do(job(send_midday_report))
    schedule.every().day.at("16:05").do(job(send_market_close_report))
    schedule.every().day.at("16:45").do(job(send_afterhours_report))

    # ── Feature 9 — Sector Heatmap ────────────────────────────────────────────
    # Cadence mirrors the India FinXray spec in
    # DATA_COLLECTION_SOURCES_AND_PROCESSING_SUMMARY.md §6.1 (daily midday +
    # just-after-close, weekly ~70min after close, monthly ~2h after close),
    # translated from IST market hours to ET ones. The weekly and monthly jobs
    # self-gate on the NYSE calendar and no-op unless the run date really is the
    # last trading day of its week / month.
    schedule.every().day.at("12:30").do(job(run_sector_heatmap_midday))
    schedule.every().day.at("16:01").do(job(run_sector_heatmap_afternoon))
    schedule.every().day.at("17:10").do(job(run_sector_heatmap_weekly))
    schedule.every().day.at("18:00").do(job(run_sector_heatmap_monthly))

    # ── Feature 13 — Watchlist Heatmap ────────────────────────────────────────
    # Same two daily slots, offset from the sector heatmap so a user does not
    # receive two images in the same minute.
    schedule.every().day.at("12:45").do(job(run_watchlist_heatmap_midday))
    schedule.every().day.at("16:20").do(job(run_watchlist_heatmap_eod))

    logger.info("[SCHEDULER] %d jobs registered across 13 features", len(schedule.jobs))
    while True:
        schedule.run_pending()
        time.sleep(1)


# ── AI pipeline thread ────────────────────────────────────────────────────────
def run_pipeline():
    """
    Drain PENDING raw_filings continuously.

    process_with_ai() returns the number of filings it handled, so a backlog
    drains back-to-back instead of one batch per idle gap; it only sleeps when
    the queue came back empty, and an empty check is a single indexed query.
    """
    logger.info("[PIPELINE] Starting")
    while True:
        worked = False
        try:
            worked = bool(process_with_ai())
        except Exception as e:
            logger.error("[PIPELINE] %s", e)
        if not worked:
            time.sleep(PIPELINE_IDLE_SECONDS)


# ── Delivery loop ─────────────────────────────────────────────────────────────
async def delivery_loop():
    """
    Last hop before the user's phone. An empty cycle is one indexed query.

    THE BACKLOG BUG THIS FIXES. This slept DELIVERY_IDLE_SECONDS after every
    cycle, including cycles that did work. delivery.py settles at most
    BATCH_LIMIT (100) alerts per call, so a backlog drained at 100 alerts per
    (cycle + idle gap) with the gap added for no reason — the queue was known
    to be non-empty at that exact moment. The pipeline loop below already only
    sleeps when its queue comes back empty; this now matches it.

    The return value counts alerts that LEFT the queue, not alerts examined, so
    a cycle that only defers in-flight retries reports 0 and idles rather than
    spinning on rows it cannot settle yet.
    """
    logger.info("[DELIVERY] Starting")
    while True:
        settled = 0
        try:
            settled = await deliver_pending_alerts()
        except Exception as e:
            logger.error("[DELIVERY] %s", e)
        if not settled:
            await asyncio.sleep(DELIVERY_IDLE_SECONDS)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    logger.info("GQ FinXray US starting — SEC EDGAR + FMP + Massive + RSS + AI + Telegram")
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_pipeline, daemon=True).start()
    await delivery_loop()


if __name__ == "__main__":
    asyncio.run(main())
