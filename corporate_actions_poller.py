"""
corporate_actions_poller.py
GQ FinXray US — Corporate actions alerts from 8-K filings. Feature 12.

Covers:
- Dividend announcements (rate, ex-date, record date, pay date)
- Stock splits (ratio, effective date)
- Share buybacks (amount, timeline)
- Mergers & acquisitions (deal value, terms)
- Director/officer appointments & resignations
- Material agreements
- Bankruptcy/insolvency
- Debt issuance/repayment

Extracts from 8-K Item 2.x (creation/repayment of obligations)
and Item 5.x (costs associated with exit/disposal).
Stores in alerts table with source=SEC_8K_ACTIONS.
"""

import os
import logging
import re
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# Corporate action keywords
DIVIDEND_KEYWORDS = ["dividend", "cash distribution", "special dividend", "quarterly dividend"]
SPLIT_KEYWORDS = ["stock split", "split ratio", "reverse split"]
BUYBACK_KEYWORDS = ["share repurchase", "buyback", "repurchase program"]
MERGER_KEYWORDS = ["merger", "acquisition", "acquired by", "business combination"]
DIRECTOR_KEYWORDS = ["director", "officer", "appointment", "resignation", "retired"]
DEBT_KEYWORDS = ["notes", "bonds", "debt offering", "credit facility", "loan"]


def get_all_stocks():
    """Fetch all US stocks (NYSE + NASDAQ) from the stocks table."""
    try:
        result = supabase.table("stocks").select("ticker").execute()
        return [row["ticker"] for row in result.data if row.get("ticker")]
    except Exception as e:
        logger.error(f"[ACTIONS] Failed to fetch stocks list: {e}")
        return []


def action_alert_exists(ticker, action_type, announcement_date):
    """Check if corporate action alert already exists."""
    try:
        result = supabase.table("alerts").select("id").eq(
            "ticker", ticker
        ).eq("source", "SEC_8K_ACTIONS").contains(
            "extra", {"action_type": action_type, "announcement_date": announcement_date}
        ).limit(1).execute()
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"[ACTIONS] Dedup check failed: {e}")
        return False


def classify_action(text: str):
    """Classify corporate action type from 8-K text."""
    text_lower = text.lower()

    for keyword in DIVIDEND_KEYWORDS:
        if keyword in text_lower:
            return "DIVIDEND"
    for keyword in SPLIT_KEYWORDS:
        if keyword in text_lower:
            return "STOCK_SPLIT"
    for keyword in BUYBACK_KEYWORDS:
        if keyword in text_lower:
            return "BUYBACK"
    for keyword in MERGER_KEYWORDS:
        if keyword in text_lower:
            return "MERGER_ACQUISITION"
    for keyword in DIRECTOR_KEYWORDS:
        if keyword in text_lower:
            return "EXECUTIVE_CHANGE"
    for keyword in DEBT_KEYWORDS:
        if keyword in text_lower:
            return "DEBT_ACTION"

    return "OTHER_ACTION"


def extract_dividend_details(text: str):
    """Extract dividend rate and dates from text."""
    details = {}

    # Dividend rate/amount pattern
    rate_match = re.search(r'\$\s*(\d+\.\d+)\s+per\s+share|(\d+\.\d+)%', text, re.IGNORECASE)
    if rate_match:
        details['rate'] = rate_match.group(1) or rate_match.group(2)

    # Ex-dividend date
    ex_match = re.search(r'ex(?:-|dividend)?\s*date.*?(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})', text, re.IGNORECASE)
    if ex_match:
        details['ex_date'] = ex_match.group(1)

    # Record date
    rec_match = re.search(r'record\s+date.*?(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})', text, re.IGNORECASE)
    if rec_match:
        details['record_date'] = rec_match.group(1)

    # Payment date
    pay_match = re.search(r'(?:payment|pay)\s+date.*?(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})', text, re.IGNORECASE)
    if pay_match:
        details['pay_date'] = pay_match.group(1)

    return details


def extract_split_ratio(text: str):
    """Extract stock split ratio."""
    # Pattern: 3:1 split, 2-for-1 split, etc.
    split_match = re.search(r'(\d+)(?::|[:-]for|[:-]to)\s*(\d+)\s*split', text, re.IGNORECASE)
    if split_match:
        return f"{split_match.group(1)}:{split_match.group(2)}"
    return None


def extract_merger_value(text: str):
    """Extract deal value from merger/acquisition text."""
    # Pattern: $X billion, $X million
    value_match = re.search(r'\$\s*(\d+(?:\.\d+)?)\s*(?:billion|million|b|m)', text, re.IGNORECASE)
    if value_match:
        return value_match.group(1)
    return None


def store_corporate_action_alert(ticker, action_type, announcement_date, summary, details):
    """Store corporate action alert."""
    try:
        # Determine impact
        impact_map = {
            "DIVIDEND": "MEDIUM",
            "STOCK_SPLIT": "MEDIUM",
            "BUYBACK": "MEDIUM",
            "MERGER_ACQUISITION": "HIGH",
            "EXECUTIVE_CHANGE": "LOW",
            "DEBT_ACTION": "MEDIUM",
            "OTHER_ACTION": "LOW",
        }
        impact = impact_map.get(action_type, "MEDIUM")

        alert_data = {
            "ticker": ticker,
            "source": "SEC_8K_ACTIONS",
            "filing_type": "CORPORATE_ACTION",
            "summary": summary,
            "impact": impact,
            "delivered": False,
            "extra": tag_extra({
                "action_type": action_type,
                "announcement_date": announcement_date,
                "details": details,
            })
        }

        supabase.table("alerts").insert(alert_data).execute()
        logger.info(f"[ACTIONS] {ticker} {action_type}: {summary[:80]}")

    except Exception as e:
        logger.error(f"[ACTIONS] Failed to store alert for {ticker}: {e}")


def poll_corporate_actions():
    """
    Query recent 8-K filings and extract corporate actions.
    This is called after SEC 8-K poller completes.
    """
    try:
        # Query pending 8-K filings not yet processed for actions
        result = supabase.table("raw_filings").select("*").eq(
            "filing_type", "8-K"
        ).eq("actions_extracted", False).limit(100).execute()

        filings = result.data
        if not filings:
            logger.info("[ACTIONS] No pending 8-K filings for action extraction")
            return

        logger.info(f"[ACTIONS] Processing {len(filings)} 8-K filings...")

        for filing in filings:
            try:
                ticker = filing.get("ticker", "")
                filing_date = filing.get("filing_date", "")
                filing_text = filing.get("filing_text", "")
                filing_id = filing.get("id")

                if not all([ticker, filing_text]):
                    continue

                # Classify action
                action_type = classify_action(filing_text)
                if action_type == "OTHER_ACTION":
                    # Skip non-material actions
                    supabase.table("raw_filings").update(
                        {"actions_extracted": True}
                    ).eq("id", filing_id).execute()
                    continue

                # Check dedup
                if action_alert_exists(ticker, action_type, filing_date):
                    supabase.table("raw_filings").update(
                        {"actions_extracted": True}
                    ).eq("id", filing_id).execute()
                    continue

                # Extract details based on action type
                details = {}
                summary = ""

                if action_type == "DIVIDEND":
                    details = extract_dividend_details(filing_text)
                    rate = details.get('rate', 'N/A')
                    ex_date = details.get('ex_date', 'N/A')
                    summary = f"Dividend announced: ${rate} per share, ex-date {ex_date}"

                elif action_type == "STOCK_SPLIT":
                    ratio = extract_split_ratio(filing_text)
                    details['split_ratio'] = ratio or 'N/A'
                    summary = f"Stock split announced: {ratio or 'details in filing'}"

                elif action_type == "BUYBACK":
                    amount_match = re.search(r'\$\s*(\d+(?:\.\d+)?)\s*(?:billion|million)', filing_text, re.IGNORECASE)
                    details['amount'] = amount_match.group(1) if amount_match else 'N/A'
                    summary = f"Share repurchase program announced: ${details['amount']}"

                elif action_type == "MERGER_ACQUISITION":
                    deal_value = extract_merger_value(filing_text)
                    details['deal_value'] = deal_value or 'N/A'
                    summary = f"M&A transaction announced: ${deal_value} deal"

                elif action_type == "EXECUTIVE_CHANGE":
                    # Extract name if available
                    name_match = re.search(r'(?:Mr\.|Ms\.|Dr\.)\s+([A-Za-z\s]+)', filing_text)
                    details['person'] = name_match.group(1) if name_match else 'Unknown'
                    summary = f"Executive change: {details['person']}"

                elif action_type == "DEBT_ACTION":
                    amount_match = re.search(r'\$\s*(\d+(?:\.\d+)?)\s*(?:billion|million)', filing_text, re.IGNORECASE)
                    details['amount'] = amount_match.group(1) if amount_match else 'N/A'
                    summary = f"Debt action: ${details['amount']} transaction"

                # Store alert
                store_corporate_action_alert(ticker, action_type, filing_date, summary, details)

                # Mark as processed
                supabase.table("raw_filings").update(
                    {"actions_extracted": True}
                ).eq("id", filing_id).execute()

            except Exception as e:
                logger.error(f"[ACTIONS] Error processing filing: {e}")
                continue

        logger.info("[ACTIONS] Corporate actions extraction complete")

    except Exception as e:
        logger.error(f"[ACTIONS] Failed to process filings: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    poll_corporate_actions()
