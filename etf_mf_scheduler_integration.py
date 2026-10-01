"""
etf_mf_scheduler_integration.py

Entry points for adding ETF/MF features to main.py scheduler.

Add these to main.py around line 318 (Feature 12 section):

# Feature 10 — ETF Xray
schedule.every().day.at("08:00").do(job(run_etf_mf_scraper))      # Daily 8am ET
schedule.every().day.at("10:30").do(job(run_etf_mf_poller))       # Daily 10:30am ET
schedule.every().day.at("16:30").do(job(run_etf_mf_alerts))       # Daily 4:30pm ET

# Feature 14 — Mutual Funds
# (Uses same scraper + poller + alerts, tickers filtered from mf_master)
"""

import logging
from datetime import datetime
from dotenv import load_dotenv

from etf_mf_master_scraper import main as scrape_etf_mf_masters
from etf_mf_data_poller import poll_etf_mf_data
from etf_mf_alerts import generate_etf_mf_alerts

load_dotenv()

logger = logging.getLogger(__name__)


def run_etf_mf_scraper():
    """
    Feature 10/14: Build/update ETF and MF master lists from SEC EDGAR.
    Runs once daily, typically early morning.
    """
    logger.info("[SCHEDULER] Starting ETF/MF master list scraper")
    try:
        # This is async, so wrap it
        import asyncio
        asyncio.run(scrape_etf_mf_masters())
        logger.info("[SCHEDULER] ETF/MF scraper completed successfully")
    except Exception as e:
        logger.error(f"[SCHEDULER] ETF/MF scraper failed: {e}")


def run_etf_mf_poller():
    """
    Feature 10/14: Poll SEC EDGAR + FMP for all 7 data categories (Holdings, Managers, Expenses, etc).
    Runs daily mid-morning after market opens.
    """
    logger.info("[SCHEDULER] Starting ETF/MF data poller")
    try:
        import asyncio
        asyncio.run(poll_etf_mf_data())
        logger.info("[SCHEDULER] ETF/MF poller completed successfully")
    except Exception as e:
        logger.error(f"[SCHEDULER] ETF/MF poller failed: {e}")


def run_etf_mf_alerts():
    """
    Feature 10/14: Generate alerts for holdings changes, manager changes, expense increases.
    Runs daily after market close.
    """
    logger.info("[SCHEDULER] Starting ETF/MF alert generation")
    
    try:
        from supabase import create_client
        import os
        
        supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        
        # Get all ETFs and MFs to alert on
        etfs = supabase.table("etf_master").select("ticker").execute().data or []
        mfs = supabase.table("mf_master").select("ticker").execute().data or []
        all_tickers = [e.get("ticker") for e in etfs] + [m.get("ticker") for m in mfs]
        
        total_alerts = 0
        for ticker in all_tickers:
            if ticker:
                alert_count = generate_etf_mf_alerts(ticker)
                total_alerts += alert_count
        
        logger.info(f"[SCHEDULER] ETF/MF alerts completed. {total_alerts} alerts generated")
    except Exception as e:
        logger.error(f"[SCHEDULER] ETF/MF alert generation failed: {e}")


if __name__ == "__main__":
    # Test entry points
    # run_etf_mf_scraper()
    # run_etf_mf_poller()
    # run_etf_mf_alerts()
    pass
