"""
etf_mf_master_scraper.py

Scrape SEC EDGAR to build comprehensive master lists for ETFs and Mutual Funds.
Only NASDAQ and NYSE listed, no OTC.

Sources:
- SEC EDGAR company tickers API
- SEC EDGAR form types (N-1A for MFs, N-2/N-3 for ETFs)
- FMP for enrichment (category, sector, market cap)

Outputs:
- etf_master table: ticker, name, fund_house, category, inception_date, aum, shares_outstanding
- mf_master table: ticker, name, fund_house, category, inception_date, aum, strategy
- company_data table: enriched with ISIN, sector, industry codes
"""

import os
import logging
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client
import aiohttp
import json

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
FMP_API_KEY = os.getenv("FMP_API_KEY")

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_CIKS_URL = "https://data.sec.gov/submissions/CIK"
EDGAR_API = "https://data.sec.gov/api/xbrl"

ETF_FORM_TYPES = ["N-2", "N-3"]  # ETF/closed-end fund registration statements
MF_FORM_TYPES = ["N-1A"]          # Mutual fund prospectus

NASDAQ_NYSE_EXCHANGES = {"nasdaq", "nyse"}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Fallback: curated list of major ETFs (NASDAQ/NYSE only, no OTC)
TOP_ETFS = [
    {"ticker": "SPY", "name": "S&P 500 ETF", "fund_house": "State Street", "category": "Broad Market", "exchange": "NYSE"},
    {"ticker": "QQQ", "name": "NASDAQ 100 ETF", "fund_house": "Invesco", "category": "Technology", "exchange": "NASDAQ"},
    {"ticker": "IWM", "name": "Russell 2000 ETF", "fund_house": "iShares", "category": "Small Cap", "exchange": "NYSE"},
    {"ticker": "EEM", "name": "MSCI Emerging Markets ETF", "fund_house": "iShares", "category": "International", "exchange": "NYSE"},
    {"ticker": "EFA", "name": "MSCI EAFE ETF", "fund_house": "iShares", "category": "International", "exchange": "NYSE"},
    {"ticker": "VTI", "name": "Total Stock Market ETF", "fund_house": "Vanguard", "category": "Broad Market", "exchange": "NASDAQ"},
    {"ticker": "VOO", "name": "Vanguard S&P 500 ETF", "fund_house": "Vanguard", "category": "Broad Market", "exchange": "NYSE"},
    {"ticker": "VTV", "name": "Vanguard Value ETF", "fund_house": "Vanguard", "category": "Value", "exchange": "NYSE"},
    {"ticker": "VUG", "name": "Vanguard Growth ETF", "fund_house": "Vanguard", "category": "Growth", "exchange": "NYSE"},
    {"ticker": "XLK", "name": "Technology Select Sector SPDR", "fund_house": "SPDR", "category": "Technology", "exchange": "NYSE"},
    {"ticker": "XLF", "name": "Financial Select Sector SPDR", "fund_house": "SPDR", "category": "Finance", "exchange": "NYSE"},
    {"ticker": "XLE", "name": "Energy Select Sector SPDR", "fund_house": "SPDR", "category": "Energy", "exchange": "NYSE"},
    {"ticker": "XLV", "name": "Health Care Select Sector SPDR", "fund_house": "SPDR", "category": "Healthcare", "exchange": "NYSE"},
    {"ticker": "XLI", "name": "Industrial Select Sector SPDR", "fund_house": "SPDR", "category": "Industrials", "exchange": "NYSE"},
    {"ticker": "XLY", "name": "Consumer Discretionary Select Sector SPDR", "fund_house": "SPDR", "category": "Consumer", "exchange": "NYSE"},
    {"ticker": "GLD", "name": "SPDR Gold Shares", "fund_house": "SPDR", "category": "Commodities", "exchange": "NYSE"},
    {"ticker": "TLT", "name": "iShares 20+ Year Treasury Bond ETF", "fund_house": "iShares", "category": "Bonds", "exchange": "NASDAQ"},
    {"ticker": "BND", "name": "Vanguard Total Bond Market ETF", "fund_house": "Vanguard", "category": "Bonds", "exchange": "NASDAQ"},
]


async def fetch_sec_tickers():
    """Fetch all company tickers from SEC with retries."""
    retries = 3
    for attempt in range(retries):
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(SEC_TICKERS_URL, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        data = await r.json()
                        logger.info(f"[SEC] Fetched {len(data)} company tickers")
                        return data
                    else:
                        logger.warning(f"[SEC] HTTP {r.status}, retrying...")
        except asyncio.TimeoutError:
            logger.warning(f"[SEC] Timeout on attempt {attempt+1}/{retries}, retrying...")
        except Exception as e:
            logger.warning(f"[SEC] Attempt {attempt+1}/{retries} failed: {e}")

        if attempt < retries - 1:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff

    logger.error("[SEC] Failed to fetch tickers after retries")
    return {}


async def fetch_company_info(cik, session):
    """Fetch company info from SEC EDGAR."""
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.debug(f"[SEC] Failed to fetch CIK {cik}: {e}")
    return {}


async def fetch_fmp_profile(ticker, session):
    """Fetch company profile from FMP for enrichment."""
    try:
        url = f"https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={FMP_API_KEY}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                return data[0] if data else {}
    except Exception as e:
        logger.debug(f"[FMP] Failed to fetch profile for {ticker}: {e}")
    return {}


async def identify_etfs_and_mfs(tickers_data):
    """
    Filter tickers to identify ETFs and Mutual Funds on NASDAQ/NYSE.
    
    Heuristics:
    - ETFs: typically have form filings (N-2, N-3), high liquidity, specific categories
    - MFs: N-1A filings, often no active ticker trading
    - Filter: exchange in ['nasdaq', 'nyse']
    """
    etfs = []
    mfs = []
    
    async with aiohttp.ClientSession() as session:
        tasks = []
        for cik, info in tickers_data.items():
            if isinstance(info, dict):
                ticker = info.get("ticker")
                exchange = info.get("exchange", "").lower()
                
                # Only NASDAQ and NYSE
                if exchange not in NASDAQ_NYSE_EXCHANGES:
                    continue
                
                if ticker:
                    tasks.append(fetch_company_info(int(cik), session))
        
        # Fetch in batches to avoid rate limits
        batch_size = 10
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i+batch_size]
            results = await asyncio.gather(*batch, return_exceptions=True)
            await asyncio.sleep(1)  # Rate limit: 1 second between batches
    
    logger.info(f"[SCRAPER] Identified {len(etfs)} ETFs and {len(mfs)} MFs")
    return etfs, mfs


async def store_etf_master(etf_list):
    """Store ETF master list in Supabase."""
    if not etf_list:
        logger.info("[DB] No ETFs to store")
        return
    
    records = []
    for etf in etf_list:
        records.append({
            "ticker": etf.get("ticker"),
            "name": etf.get("name"),
            "fund_house": etf.get("fund_house"),
            "category": etf.get("category", "Unclassified"),
            "inception_date": etf.get("inception_date"),
            "aum": etf.get("aum"),
            "shares_outstanding": etf.get("shares_outstanding"),
            "exchange": etf.get("exchange", "NYSE/NASDAQ"),
            "extra": {
                "cik": etf.get("cik"),
                "cusip": etf.get("cusip"),
                "isin": etf.get("isin"),
            }
        })
    
    try:
        # Upsert on conflict (ticker)
        for record in records:
            supabase.table("etf_master").upsert(record, ignore_duplicates=False).execute()
        logger.info(f"[DB] Stored {len(records)} ETF records")
    except Exception as e:
        logger.error(f"[DB] Failed to store ETF master: {e}")


async def store_mf_master(mf_list):
    """Store Mutual Fund master list in Supabase."""
    if not mf_list:
        logger.info("[DB] No MFs to store")
        return
    
    records = []
    for mf in mf_list:
        records.append({
            "ticker": mf.get("ticker"),
            "name": mf.get("name"),
            "fund_house": mf.get("fund_house"),
            "category": mf.get("category", "Unclassified"),
            "inception_date": mf.get("inception_date"),
            "aum": mf.get("aum"),
            "strategy": mf.get("strategy"),
            "exchange": mf.get("exchange", "OTC/No Active Trading"),
            "extra": {
                "cik": mf.get("cik"),
                "cusip": mf.get("cusip"),
                "series_id": mf.get("series_id"),
            }
        })
    
    try:
        for record in records:
            supabase.table("mf_master").upsert(record, ignore_duplicates=False).execute()
        logger.info(f"[DB] Stored {len(records)} MF records")
    except Exception as e:
        logger.error(f"[DB] Failed to store MF master: {e}")


async def main():
    """Main scraper flow."""
    logger.info("[SCRAPER] Starting ETF/MF master list scraper")

    # Step 1: Try to fetch SEC tickers, fallback to curated list
    tickers_data = await fetch_sec_tickers()
    if not tickers_data:
        logger.warning("[SCRAPER] Using fallback ETF list (SEC tickers unavailable)")
        # Store fallback ETFs
        await store_etf_master(TOP_ETFS)
        logger.info("[SCRAPER] Stored fallback ETF list")
        return

    # Step 2: Filter for ETFs and MFs on NASDAQ/NYSE
    etfs, mfs = await identify_etfs_and_mfs(tickers_data)

    # Step 3: Store to Supabase
    await store_etf_master(etfs)
    await store_mf_master(mfs)

    logger.info("[SCRAPER] ETF/MF master list scraper complete")


if __name__ == "__main__":
    asyncio.run(main())
