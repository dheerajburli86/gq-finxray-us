"""
main.py
GQ FinXray US — process entry point.

Runs a set of independent scheduler lanes driving every poller, a pipeline
thread turning raw content into alerts, and an async delivery loop fanning those
alerts out to users based on their watchlists.

FIXES IN THIS REVISION
----------------------
* **The scheduler is split into independent lanes, one thread each.** Every job
  previously shared one `schedule` default scheduler on one thread, and
  `run_pending()` executes jobs SERIALLY. A single slow job therefore delayed
  every other feature in the process. `poll_sec_8k` and `poll_sec_form4` run on
  a 30-second interval and could each burn 75 seconds of wall clock inside
  `sec_get` when sec.gov timed out — so the two of them alone consumed more than
  the entire cycle, and technical, ETF, analyst, IPO, heatmap and roundup jobs
  simply never got their turn. Nothing crashed, nothing logged an error, and the
  process looked perfectly healthy while thirteen of fourteen features went
  silent. Lanes make a stall local to the lane that caused it.

* **`earnings_alerts` is registered.** `poll_earnings_for_tickers` existed and
  was imported but was never handed to the scheduler, so Feature 5 had no way to
  run at all.

* **Every scheduled job is wrapped in `safe_job`.** `schedule` does not catch
  exceptions raised by a job — it lets them propagate out of `run_pending()`,
  which killed the scheduler thread and silently stopped every poller while the
  process kept running and looked healthy. One unguarded FMP 429 inside
  `run_etf_xray` was enough to do it.

* **Slow jobs are now visible.** `safe_job` records anything over its lane's
  budget to `poller_error_log`, not just to a log line that dies with the
  container. A lane heartbeat proves liveness even when a lane has nothing to
  emit, which is the difference between "quiet" and "wedged".

* **The timezone is pinned to America/New_York at startup.** `schedule` fires on
  server local time while some jobs were formatting timestamps in UTC, which is
  how a slot scheduled for 09:30 ended up labelled `HEATMAP_DAILY_10` in
  production. Every time in this file is now unambiguously ET.

* **Delivery is watchlist-scoped.** `deliver_pending_alerts` moved to
  delivery.py, which routes each alert to the users who watch its ticker instead
  of blasting one shared channel. There is no longer a code path that sends a
  company-specific alert to someone who is not following that company.

* **The impact filter is gone from the query.** It was `.gte("impact","MEDIUM")`,
  which compares text alphabetically — "HIGH" sorts before "MEDIUM", so the
  filter dropped every HIGH alert while keeping MEDIUM. Impact is now ranked
  numerically, per user, against their own `min_impact` preference.
"""

import os
import time
import asyncio
import logging
import threading
import traceback

# Pin the process to Eastern time BEFORE anything reads the clock. Every schedule
# below is US market time; leaving this to the host's locale is how slot labels
# and market-hours guards drift apart.
os.environ["TZ"] = os.getenv("APP_TIMEZONE", "America/New_York")
try:
    time.tzset()
except AttributeError:
    pass  # Windows

import schedule
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gqfinxray")

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

from delivery import delivery_loop
from edgar_poller import (poll_sec_8k, poll_sec_form4, poll_sec_10q,
                          poll_sec_10k, poll_sec_s1, load_cik_map)
from news_poller import poll_all_news
from fmp_poller import poll_fmp_news, poll_fmp_events
from result_snapshot import process_pending_snapshots
from technical_poller import run_technical_poller
from ipo_poller import run_ipo_poller
from earnings_transcript_poller import run_earnings_transcript_poller
from news_roundup import run_etf_xray
from etf_flow_poller import run_etf_flow_poller
from earnings_alerts import poll_earnings_for_tickers
from heatmap_generator import (run_sector_heatmap_midday, run_sector_heatmap_afternoon,
                               run_sector_heatmap_weekly, run_sector_heatmap_monthly)
from watchlist_heatmap import run_watchlist_heatmap_midday, run_watchlist_heatmap_eod
from analyst_ratings_poller import poll_analyst_ratings
from macro_policy_roundup import run_macro_policy_roundup
from market_reports import (send_premarket_report, send_market_open_report,
                            send_midday_report, send_market_close_report,
                            send_afterhours_report)


# ── Crash isolation ───────────────────────────────────────────────────────────
def log_job_error(job_name, error, context=None):
    try:
        row = {
            "poller_name": job_name,
            "job_name": job_name,
            "error_message": str(error)[:1000],
            "error_traceback": (traceback.format_exc() if isinstance(error, BaseException)
                                else str(context or ""))[:4000],
        }
        supabase.table("poller_error_log").insert(row).execute()
    except Exception:
        pass  # Never let error logging itself break the scheduler.


def safe_job(fn, name=None, budget_seconds=120):
    """
    Wrap a scheduled job so a failure inside it can never take down the lane
    thread. This is the single most important line of defence in this file:
    `schedule.run_pending()` re-raises whatever a job raises, and that exception
    unwinds the whole `while True` loop.

    Overruns are recorded to `poller_error_log`, not just logged. A job that
    quietly takes four times its interval is the failure mode that took this
    system down — it produces no exception, no error row, and no alerts, and the
    only visible symptom is other features going silent.
    """
    job_name = name or getattr(fn, "__name__", "job")

    def wrapped(*args, **kwargs):
        started = time.time()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.error("[JOB FAILED] %s: %s", job_name, e)
            logger.debug(traceback.format_exc())
            log_job_error(job_name, e)
        finally:
            elapsed = time.time() - started
            if elapsed > budget_seconds:
                logger.warning("[JOB SLOW] %s took %.0fs (budget %ss)",
                               job_name, elapsed, budget_seconds)
                log_job_error(job_name,
                              f"Job overran its budget: {elapsed:.0f}s vs {budget_seconds}s",
                              context=f"{job_name} overran; this starves other jobs in the same lane.")

    wrapped.__name__ = f"safe_{job_name}"
    return wrapped


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler lanes
#
# `schedule.run_pending()` runs due jobs one after another on the calling
# thread. Anything that blocks — a socket read against sec.gov, twenty RSS feeds
# fetched in series, an LLM call — delays every other job registered on that
# same scheduler. Grouping jobs by how slow and how network-bound they are, and
# giving each group its own `schedule.Scheduler()` on its own thread, means a
# stalled SEC feed can no longer stop a heatmap from rendering.
#
#   sec     — five EDGAR feeds, externally rate-limited and prone to timeouts
#   news    — RSS/Atom sweep plus FMP news, dozens of third-party hosts
#   market  — everything driven by market data and by the clock
# ══════════════════════════════════════════════════════════════════════════════
LANE_BUDGETS = {"sec": 60, "news": 90, "market": 300}

_lane_schedulers = {}


def lane(name):
    """The `schedule.Scheduler` for a lane, created on first use."""
    if name not in _lane_schedulers:
        _lane_schedulers[name] = schedule.Scheduler()
    return _lane_schedulers[name]


def register(lane_name, spec, fn, job_name):
    """
    Register `fn` on a lane. `spec` is either ("every", n, unit) or ("at", "HH:MM").
    Every job goes through `safe_job` with the lane's budget — there is no code
    path in this file that registers a bare function.
    """
    sched = lane(lane_name)
    job = safe_job(fn, job_name, budget_seconds=LANE_BUDGETS.get(lane_name, 120))
    if spec[0] == "at":
        sched.every().day.at(spec[1]).do(job)
    else:
        _, n, unit = spec
        getattr(sched.every(n), unit).do(job)
    return job


def build_schedule():
    """Declare every job and which lane owns it. Returns nothing; mutates lanes."""

    # ── Lane: sec — SEC EDGAR (Feature 1) ────────────────────────────────────
    register("sec", ("every", 30, "seconds"), poll_sec_8k, "poll_sec_8k")
    register("sec", ("every", 30, "seconds"), poll_sec_form4, "poll_sec_form4")
    register("sec", ("every", 5, "minutes"), poll_sec_10q, "poll_sec_10q")
    register("sec", ("every", 5, "minutes"), poll_sec_10k, "poll_sec_10k")
    register("sec", ("every", 10, "minutes"), poll_sec_s1, "poll_sec_s1")

    # ── Lane: news — News (Feature 2) ────────────────────────────────────────
    register("news", ("every", 60, "seconds"), poll_all_news, "poll_all_news")
    register("news", ("every", 10, "minutes"), poll_fmp_news, "poll_fmp_news")

    # ── Lane: market — Results, earnings calendar, insider (Features 3, 4, 5) ─
    register("market", ("every", 30, "minutes"), process_pending_snapshots, "result_snapshot")
    register("market", ("every", 60, "minutes"), poll_fmp_events, "poll_fmp_events")
    # Feature 5 was imported but never registered — it could not run at all.
    register("market", ("every", 4, "hours"), poll_earnings_for_tickers, "earnings_alerts")

    # ── Technical (Feature 6) — every 30 min, guarded to market hours inside ──
    register("market", ("every", 30, "minutes"), run_technical_poller, "technical_poller")

    # ── ETF flow (Feature 7) ─────────────────────────────────────────────────
    register("market", ("every", 60, "minutes"), run_etf_flow_poller, "etf_flow_poller")

    # ── IPO (Feature 8) ──────────────────────────────────────────────────────
    register("market", ("at", "08:00"), run_ipo_poller, "ipo_poller")

    # ── Transcripts (Feature 11) ─────────────────────────────────────────────
    register("market", ("every", 30, "minutes"), run_earnings_transcript_poller, "transcript_poller")

    # ── Analyst ratings (Feature 12) ─────────────────────────────────────────
    register("market", ("every", 120, "minutes"), poll_analyst_ratings, "analyst_ratings")

    # ── Heatmaps (Features 9, 14) — staggered so they never collide ──────────
    register("market", ("at", "13:00"), run_sector_heatmap_midday, "sector_heatmap_midday")
    register("market", ("at", "15:00"), run_sector_heatmap_afternoon, "sector_heatmap_afternoon")
    register("market", ("at", "17:10"), run_sector_heatmap_weekly, "sector_heatmap_weekly")
    register("market", ("at", "18:00"), run_sector_heatmap_monthly, "sector_heatmap_monthly")
    register("market", ("at", "13:05"), run_watchlist_heatmap_midday, "watchlist_heatmap_midday")
    register("market", ("at", "16:30"), run_watchlist_heatmap_eod, "watchlist_heatmap_eod")

    # ── ETF Xray + macro digest (Features 10, 13) ────────────────────────────
    register("market", ("at", "09:00"), run_etf_xray, "etf_xray")
    register("market", ("at", "19:00"), run_macro_policy_roundup, "macro_roundup")

    # ── Market reports (market-wide, respects /settings market off) ──────────
    register("market", ("at", "09:25"), send_premarket_report, "premarket_report")
    register("market", ("at", "09:35"), send_market_open_report, "market_open_report")
    register("market", ("at", "12:30"), send_midday_report, "midday_report")
    register("market", ("at", "16:05"), send_market_close_report, "market_close_report")
    register("market", ("at", "16:45"), send_afterhours_report, "afterhours_report")


# Run once at startup so a fresh deploy is not silent for a whole interval.
STARTUP_KICKS = {
    "sec": ((poll_sec_8k, "poll_sec_8k"), (poll_sec_form4, "poll_sec_form4")),
    "news": ((poll_all_news, "poll_all_news"), (poll_fmp_news, "poll_fmp_news")),
    "market": (),
}

HEARTBEAT_SECONDS = 300


def run_lane(lane_name):
    """
    Drive one lane forever. Each lane owns its own scheduler and its own thread,
    so a job that blocks here cannot delay a job in another lane.
    """
    sched = lane(lane_name)
    logger.info("[LANE %s] Starting with %d jobs (timezone=%s)",
                lane_name, len(sched.get_jobs()), os.environ.get("TZ"))

    # The CIK map has to exist before any EDGAR job resolves a ticker, and it is
    # a ~1MB download — it belongs inside the sec lane, not on the startup path
    # of the whole process where it would delay delivery coming up.
    if lane_name == "sec":
        safe_job(load_cik_map, "load_cik_map", budget_seconds=90)()

    for fn, job_name in STARTUP_KICKS.get(lane_name, ()):
        safe_job(fn, job_name, budget_seconds=LANE_BUDGETS.get(lane_name, 120))()

    last_beat = 0.0
    while True:
        try:
            sched.run_pending()
        except Exception as e:
            # Belt and braces. safe_job should make this unreachable, but if a
            # job is ever registered unwrapped, the lane must still survive.
            logger.error("[LANE %s] Unhandled error in run_pending: %s", lane_name, e)
            log_job_error(f"lane_{lane_name}", e)

        now = time.monotonic()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            # Proves the lane is turning over. Without this, a wedged lane and an
            # idle lane produce byte-identical logs.
            nxt = sched.next_run
            logger.info("[LANE %s] alive — %d jobs, next run %s",
                        lane_name, len(sched.get_jobs()), nxt)
        time.sleep(1)


# ── AI pipeline thread ────────────────────────────────────────────────────────
def run_pipeline_thread():
    logger.info("[PIPELINE] Starting")
    from ai_pipeline import run_pipeline
    while True:
        try:
            run_pipeline()
        except Exception as e:
            logger.error("[PIPELINE] Cycle failed: %s", e)
            log_job_error("ai_pipeline", e)
        time.sleep(60)


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║                   GQ FinXray US — starting                   ║
║   SEC EDGAR · FMP · Massive · News · AI · Telegram           ║
║   Delivery: watchlist-scoped, per user                       ║
╚══════════════════════════════════════════════════════════════╝
    """)

    build_schedule()

    for lane_name in ("sec", "news", "market"):
        threading.Thread(target=run_lane, args=(lane_name,),
                         daemon=True, name=f"lane-{lane_name}").start()
        logger.info("[BOOT] lane %s: %d jobs registered",
                    lane_name, len(lane(lane_name).get_jobs()))

    threading.Thread(target=run_pipeline_thread, daemon=True, name="pipeline").start()

    await delivery_loop(interval_seconds=30)


if __name__ == "__main__":
    asyncio.run(main())
