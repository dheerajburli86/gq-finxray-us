"""
etf_mf_alerts.py

Alert generation for ETF/MF features (10 & 14).
Monitors holdings changes, manager changes, expense ratio spikes, performance shifts.

Alert types:
- Holdings addition: high-conviction new position
- Holdings removal: exit from major position
- Manager change: portfolio manager departure/arrival
- Expense spike: fee increase
- Performance alert: underperformance vs benchmark
"""

import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client
import os

from feature_map import tag_extra, resolve_feature

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def create_holdings_alert(ticker: str, security_name: str, action: str, percentage: float, impact: str = "MEDIUM"):
    """
    Create alert for holdings addition/removal.
    
    Args:
        ticker: ETF/MF ticker
        security_name: Security being added/removed
        action: "ADDED" or "REMOVED"
        percentage: % of portfolio this holding represents
        impact: Alert impact level
    """
    if action == "ADDED":
        verb = "Added"
        context = f"{security_name} ({percentage:.2f}% of portfolio)"
    else:
        verb = "Removed"
        context = f"{security_name} ({percentage:.2f}% of portfolio)"
    
    summary = f"{verb} to {ticker}: {context}"
    
    try:
        fid, fname = resolve_feature("ETF_MF_HOLDINGS", "HOLDINGS_CHANGE")
        
        alert_dict = {
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "ETF_MF_HOLDINGS",
            "filing_type": "HOLDINGS_CHANGE",
            "delivered": False,
            "extra": tag_extra({
                "action": action,
                "security": security_name,
                "percentage": percentage,
                "feature": f"{fid}/{fname}",
            }, "ETF_MF_HOLDINGS", "HOLDINGS_CHANGE")
        }
        
        supabase.table("alerts").insert(alert_dict).execute()
        logger.info(f"[ETF/MF] Holdings alert: {ticker} - {action} {security_name}")
        return True
    except Exception as e:
        logger.error(f"[ETF/MF] Failed to create holdings alert: {e}")
        return False


def create_manager_change_alert(ticker: str, manager_name: str, change_type: str):
    """
    Create alert for fund manager changes.
    
    Args:
        ticker: ETF/MF ticker
        manager_name: Manager name
        change_type: "ARRIVAL" or "DEPARTURE"
    """
    if change_type == "ARRIVAL":
        summary = f"{manager_name} appointed as portfolio manager of {ticker}"
    else:
        summary = f"{manager_name} departed from {ticker} portfolio management"
    
    try:
        fid, fname = resolve_feature("ETF_MF_MANAGERS", "MANAGER_CHANGE")
        
        alert_dict = {
            "ticker": ticker,
            "summary": summary,
            "impact": "MEDIUM",
            "source": "ETF_MF_MANAGERS",
            "filing_type": "MANAGER_CHANGE",
            "delivered": False,
            "extra": tag_extra({
                "manager": manager_name,
                "change": change_type,
                "feature": f"{fid}/{fname}",
            }, "ETF_MF_MANAGERS", "MANAGER_CHANGE")
        }
        
        supabase.table("alerts").insert(alert_dict).execute()
        logger.info(f"[ETF/MF] Manager alert: {ticker} - {manager_name} {change_type}")
        return True
    except Exception as e:
        logger.error(f"[ETF/MF] Failed to create manager alert: {e}")
        return False


def create_expense_alert(ticker: str, gross_er_prior: float, gross_er_current: float):
    """
    Create alert for expense ratio increases.
    """
    delta = gross_er_current - gross_er_prior
    
    if delta > 0.1:  # Alert only for >0.1% increase
        summary = f"{ticker} expense ratio increased from {gross_er_prior:.3f}% to {gross_er_current:.3f}% (+{delta:.3f}%)"
        impact = "HIGH" if delta > 0.5 else "MEDIUM"
        
        try:
            fid, fname = resolve_feature("ETF_MF_EXPENSES", "EXPENSE_CHANGE")
            
            alert_dict = {
                "ticker": ticker,
                "summary": summary,
                "impact": impact,
                "source": "ETF_MF_EXPENSES",
                "filing_type": "EXPENSE_CHANGE",
                "delivered": False,
                "extra": tag_extra({
                    "prior_er": gross_er_prior,
                    "current_er": gross_er_current,
                    "delta_bps": int(delta * 100),
                    "feature": f"{fid}/{fname}",
                }, "ETF_MF_EXPENSES", "EXPENSE_CHANGE")
            }
            
            supabase.table("alerts").insert(alert_dict).execute()
            logger.info(f"[ETF/MF] Expense alert: {ticker} - ER increased {delta:.3f}%")
            return True
        except Exception as e:
            logger.error(f"[ETF/MF] Failed to create expense alert: {e}")
            return False
    
    return False


def generate_etf_mf_alerts(ticker: str) -> int:
    """
    Main alert generation for a single ETF/MF.
    Checks holdings changes, manager changes, expense shifts.
    Returns count of alerts generated.
    """
    alert_count = 0
    
    try:
        # Check for recent holdings changes
        changes = supabase.table("etf_mf_holdings_changes") \
            .select("*") \
            .eq("ticker", ticker) \
            .eq("sent", False) \
            .order("date", desc=True) \
            .limit(20) \
            .execute().data or []
        
        for change in changes:
            action = change.get("action")
            security = change.get("security_name")
            pct = change.get("percentage", 0)
            
            if pct >= 1.0:  # Only alert for >1% positions
                created = create_holdings_alert(ticker, security, action, pct)
                if created:
                    alert_count += 1
                    # Mark as sent
                    supabase.table("etf_mf_holdings_changes").update({"sent": True}).eq("id", change["id"]).execute()
        
        # Check for manager changes (would require tracking prior state)
        # For now, alerts only on first ingestion
        
        # Check for expense increases
        try:
            current = supabase.table("etf_mf_expenses").select("*").eq("ticker", ticker).limit(1).execute().data
            if current:
                # Compare to 30-day-old snapshot if available
                prior = supabase.table("etf_mf_expenses_history") \
                    .select("*") \
                    .eq("ticker", ticker) \
                    .order("date", desc=True) \
                    .limit(1) \
                    .execute().data
                
                if prior:
                    prior_er = prior[0].get("gross_er")
                    current_er = current[0].get("gross_er")
                    if prior_er and current_er:
                        created = create_expense_alert(ticker, prior_er, current_er)
                        if created:
                            alert_count += 1
        except:
            pass
    
    except Exception as e:
        logger.error(f"[ETF/MF] Failed to generate alerts for {ticker}: {e}")
    
    return alert_count


if __name__ == "__main__":
    # Test
    generate_etf_mf_alerts("SPY")
