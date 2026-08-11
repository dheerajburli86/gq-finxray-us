"""
main.py
GQ FinXray US — process entry point.

Runs three things: a scheduler thread driving every poller, a pipeline thread
turning raw content into alerts, and an async delivery loop fanning those alerts
out to users based on their watchlists.

FIXES IN THIS REVISION
----------------------
* **Every scheduled job is wrapped in `safe_job`.** `schedule` does not catch
  exceptions raised by a job — it lets them propagate out of `run_pending()`,
  which killed the scheduler thread and silently stopped every poller while the
  process kept running and looked healthy. One unguarded FMP 429 inside
  `run_etf_xray` was enough to do it.

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
from heatmap_generator import (run_sector_heatmap_midday, run_sector_heatmap_afternoon,
                               run_sector_heatmap_weekly, run_sector_heatmap_monthly)
from watchlist_heatmap import run_watchlist_heatmap_midday, run_watchlist_heatmap_eod
from analyst_ratings_poller import poll_analyst_ratings
from macro_policy_roundup import run_macro_policy_roundup
from market_reports import (send_premarket_report, send_market_open_report,
                            send_midday_report, send_market_close_report,
                            send_afterhours_report)


# ── Crash isolation ───────────────────────────────────────────────────────────
def log_job_error(job_name, exc):
    try:
        supabase.table("poller_error_log").insert({
            "poller_name": job_name,
            "job_name": job_name,
            "error_message": str(exc)[:1000],
            "error_traceback": traceback.format_exc()[:4000],
        }).execute()
    except Exception:
        pass  # Never let error logging itself break the scheduler.


def safe_job(fn, name=None):
    """
    Wrap a scheduled job so a failure inside it can never take down the
    scheduler thread. This is the single most important line of defence in this
    file: `schedule.run_pending()` re-raises whatever a job raises, and that
    exception unwinds the whole `while True` loop.
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
            if elapsed > 120:
                logger.warning("[JOB SLOW] %s took %.0fs", job_name, elapsed)

    wrapped.__name__ = f"safe_{job_name}"
    return wrapped


# ── Scheduler ─────────────────────────────────────────────────────────────────
def run_scheduler():
    logger.info("[SCHEDULER] Starting (timezone=%s)", os.environ.get("TZ"))

    safe_job(load_cik_map, "load_cik_map")()

    # ── SEC EDGAR (Feature 1) ────────────────────────────────────────────────
    schedule.every(30).seconds.do(safe_job(poll_sec_8k, "poll_sec_8k"))
    schedule.every(30).seconds.do(safe_job(poll_sec_form4, "poll_sec_form4"))
    schedule.every(5).minutes.do(safe_job(poll_sec_10q, "poll_sec_10q"))
    schedule.every(5).minutes.do(safe_job(poll_sec_10k, "poll_sec_10k"))
    schedule.every(10).minutes.do(safe_job(poll_sec_s1, "poll_sec_s1"))

    # ── News (Feature 2) ─────────────────────────────────────────────────────
    schedule.every(60).seconds.do(safe_job(poll_all_news, "poll_all_news"))
    schedule.every(10).minutes.do(safe_job(poll_fmp_news, "poll_fmp_news"))

    # ── Results, earnings calendar, insider (Features 3, 4, 5) ───────────────
    schedule.every(30).minutes.do(safe_job(process_pending_snapshots, "result_snapshot"))
    schedule.every(60).minutes.do(safe_job(poll_fmp_events, "poll_fmp_events"))

    # ── Technical (Feature 6) — every 30 min inside market hours ─────────────
    schedule.every(30).minutes.do(safe_job(run_technical_poller, "technical_poller"))

    # ── ETF flow (Feature 7) ─────────────────────────────────────────────────
    schedule.every(60).minutes.do(safe_job(run_etf_flow_poller, "etf_flow_poller"))

    # ── IPO (Feature 8) ──────────────────────────────────────────────────────
    schedule.every().day.at("08:00").do(safe_job(run_ipo_poller, "ipo_poller"))

    # ── Transcripts (Feature 11) ─────────────────────────────────────────────
    schedule.every(30).minutes.do(safe_job(run_earnings_transcript_poller, "transcript_poller"))

    # ── Analyst ratings (Feature 12) ─────────────────────────────────────────
    schedule.every(120).minutes.do(safe_job(poll_analyst_ratings, "analyst_ratings"))

    # ── Heatmaps (Features 9, 14) — staggered so they never collide ──────────
    schedule.every().day.at("13:00").do(safe_job(run_sector_heatmap_midday, "sector_heatmap_midday"))
    schedule.every().day.at("15:00").do(safe_job(run_sector_heatmap_afternoon, "sector_heatmap_afternoon"))
    schedule.every().day.at("17:10").do(safe_job(run_sector_heatmap_weekly, "sector_heatmap_weekly"))
    schedule.every().day.at("18:00").do(safe_job(run_sector_heatmap_monthly, "sector_heatmap_monthly"))
    schedule.every().day.at("13:05").do(safe_job(run_watchlist_heatmap_midday, "watchlist_heatmap_midday"))
    schedule.every().day.at("16:30").do(safe_job(run_watchlist_heatmap_eod, "watchlist_heatmap_eod"))

    # ── ETF Xray + macro digest (Features 10, 13) ────────────────────────────
    schedule.every().day.at("09:00").do(safe_job(run_etf_xray, "etf_xray"))
    schedule.every().day.at("19:00").do(safe_job(run_macro_policy_roundup, "macro_roundup"))

    # ── Market reports (market-wide, respects /settings market off) ──────────
    schedule.every().day.at("09:25").do(safe_job(send_premarket_report, "premarket_report"))
    schedule.every().day.at("09:35").do(safe_job(send_market_open_report, "market_open_report"))
    schedule.every().day.at("12:30").do(safe_job(send_midday_report, "midday_report"))
    schedule.every().day.at("16:05").do(safe_job(send_market_close_report, "market_close_report"))
    schedule.every().day.at("16:45").do(safe_job(send_afterhours_report, "afterhours_report"))

    # Kick the fast pollers once so a fresh deploy is not silent for 30 seconds.
    for fn, name in ((poll_sec_8k, "poll_sec_8k"), (poll_sec_form4, "poll_sec_form4"),
                     (poll_all_news, "poll_all_news"), (poll_fmp_news, "poll_fmp_news")):
        safe_job(fn, name)()

    logger.info("[SCHEDULER] %d jobs registered", len(schedule.get_jobs()))

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            # Belt and braces. safe_job should make this unreachable, but if a
            # job is ever registered unwrapped, the scheduler must still survive.
            logger.error("[SCHEDULER] Unhandled error in run_pending: %s", e)
            log_job_error("scheduler_loop", e)
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

    threading.Thread(target=run_scheduler, daemon=True, name="scheduler").start()
    threading.Thread(target=run_pipeline_thread, daemon=True, name="pipeline").start()

    await delivery_loop(interval_seconds=30)


if __name__ == "__main__":
    asyncio.run(main())
