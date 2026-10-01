"""
gquants_format_converter.py — GQ FinXray US
Convert SEC EDGAR XBRL, FMP, and raw data to GQuants frontend format.

Every alert now carries structured JSON that renders on the frontend instead of
raw links to SEC filings or FMP endpoints. The format matches the India finxray
schema documented in XBRL_STRUCTURED_CONTENT_REFERENCE.md.

Key types for US market:
  - fr: Financial results (10-Q/10-K)
  - it: Insider trading (Form 4)
  - ipo: IPO details (S-1)
  - earning_calls: Earnings transcripts
  - tradingview: Technical indicators

Each converter takes raw source data and emits a payload suitable for
information_version.payload or direct alert.extra[].
"""

import logging
import os
from datetime import datetime
from urllib.parse import quote
from typing import Any, Dict, Optional, List

logger = logging.getLogger(__name__)


def _format_currency(value: float | None, unit: str = "USD") -> str:
    """
    Format a currency value.

    Branches on magnitude, not on the signed value, so a loss scales the same
    way a profit does: -5.2e9 renders "-$5.20B", not "$-5,200,000,000.00".
    Loss-making quarters are common and were previously mangled.
    """
    if value is None:
        return "-"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if value < 0 else ""
    mag = abs(value)
    if mag >= 1_000_000_000:
        return f"{sign}${mag / 1_000_000_000:.2f}B"
    if mag >= 1_000_000:
        return f"{sign}${mag / 1_000_000:.2f}M"
    if mag >= 1_000:
        return f"{sign}${mag / 1_000:.2f}K"
    return f"{sign}${mag:,.2f}"


def _pct_change(current: float | None, previous: float | None) -> str:
    """Format percentage change. Returns 'Unch' if no change, 'Up X%' or 'Down X%'."""
    if current is None or previous is None:
        return "-"
    if previous == 0:
        return "Up Inf" if current > 0 else "Down Inf" if current < 0 else "Unch"
    pct = ((current - previous) / abs(previous)) * 100
    if pct == 0:
        return "Unch"
    return f"Up {pct:.2f}%" if pct > 0 else f"Down {abs(pct):.2f}%"


# ── Financial Results (10-Q / 10-K) ───────────────────────────────────────────
def xbrl_to_financial_results(
    ticker: str,
    cik: str,
    quarters: List[Dict[str, Any]],
    company_name: str,
    form_type: str
) -> Dict[str, Any]:
    """
    Convert SEC XBRL quarterly data to GQuants `fr` format.

    Args:
        ticker: Stock symbol (e.g., "AAPL")
        cik: SEC CIK number
        quarters: List of quarters from sec_financials.get_income_statement()
        company_name: Full company name
        form_type: "10-Q" or "10-K"

    Returns:
        Dict with type="fr" and structured financial data ready for frontend
    """
    if not quarters or len(quarters) < 2:
        return {}

    quarters = sorted(quarters, key=lambda q: q.get("date", ""), reverse=True)
    latest = quarters[0]
    prev = quarters[1]
    yoy = quarters[4] if len(quarters) >= 5 else None

    period = latest.get("period", "")
    date_str = latest.get("date", "")
    cal_year = latest.get("calendarYear", "")
    label = f"{period} FY{cal_year}".replace("  ", " ").strip()
    if not label:
        label = f"Latest ({date_str})"

    return {
        "type": "fr",
        "name": company_name,
        "general_information": {
            "scrip_code": cik,
            "ticker": ticker,
            "form_type": form_type,
            "isin": None,
        },
        "overview": [
            {"category": "FORM TYPE", "details": form_type},
            {"category": "REPORTING PERIOD", "details": label},
            {"category": "PERIOD END", "details": date_str},
        ],
        "column_names": {
            "item": "Metric",
            "latest_qtr_value": label,
            "prev_qtr_value": f"Previous ({prev.get('date', '')})",
            "qoq_change": "Q-o-Q Change",
            "yoy_change": "Y-o-Y Change",
        },
        "financial_overview": _build_financial_rows(latest, prev, yoy),
        "_source": "SEC_XBRL",
        "_metadata": {
            "fetched_at": datetime.now().isoformat(),
            "cik": cik,
            "ticker": ticker,
        }
    }


def _build_financial_rows(
    latest: Dict[str, Any],
    prev: Dict[str, Any],
    yoy: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Build the financial_overview table rows from quarterly data."""
    rows = []
    metrics = [
        ("revenue", "Revenue (USD)"),
        ("grossProfit", "Gross Profit (USD)"),
        ("operatingIncome", "Operating Income (USD)"),
        ("netIncome", "Net Income (USD)"),
        ("eps", "Diluted EPS (USD)"),
        ("costOfRevenue", "Cost of Revenue (USD)"),
        ("operatingExpenses", "Operating Expenses (USD)"),
    ]
    for key, label in metrics:
        latest_val = latest.get(key)
        prev_val = prev.get(key)
        yoy_val = yoy.get(key) if yoy else None

        row = {
            "item": label,
            "latest_qtr_value": _format_currency(latest_val) if isinstance(latest_val, (int, float)) else str(latest_val or "-"),
            "prev_qtr_value": _format_currency(prev_val) if isinstance(prev_val, (int, float)) else str(prev_val or "-"),
            "qoq_change": _pct_change(latest_val, prev_val),
        }
        if yoy_val is not None:
            row["yoy_change"] = _pct_change(latest_val, yoy_val)
        rows.append(row)

    return rows


# ── Insider Trading (Form 4) ──────────────────────────────────────────────────
def form4_to_insider_trading(
    ticker: str,
    insider_name: str,
    insider_title: str,
    transactions: List[Dict[str, Any]],
    company_name: str,
    filing_date: str
) -> Dict[str, Any]:
    """
    Convert SEC Form 4 insider trading to GQuants `it` format.

    Args:
        ticker: Stock symbol
        insider_name: Name of the insider
        insider_title: Their title at the company
        transactions: List of transactions parsed from Form 4
        company_name: Full company name
        filing_date: Date the Form 4 was filed

    Returns:
        Dict with type="it" and structured insider data
    """
    return {
        "type": "it",
        "name": company_name,
        "general_information": {
            "ticker": ticker,
            "insider_name": insider_name,
            "insider_title": insider_title,
            "filing_date": filing_date,
        },
        "column_names": {
            "security": "Security",
            "action": "Action",
            "shares": "Shares",
            "price": "Price (USD)",
            "value": "Value (USD)",
            "date": "Transaction Date",
            "held_after": "Held After",
        },
        "types": [
            {
                "transaction_type": "Non-Derivative",
                "table_data": [
                    t for t in transactions if t.get("security_type") != "Derivative"
                ] or [],
                "insider_trading_aggregate": {
                    "insider_name": insider_name,
                    "ticker": ticker,
                    "title": insider_title,
                    "total_transactions": len([t for t in transactions if t.get("security_type") != "Derivative"]),
                }
            },
            {
                "transaction_type": "Derivative",
                "table_data": [
                    t for t in transactions if t.get("security_type") == "Derivative"
                ] or [],
                "insider_trading_aggregate": {
                    "insider_name": insider_name,
                    "ticker": ticker,
                    "derivative_transactions": len([t for t in transactions if t.get("security_type") == "Derivative"]),
                }
            }
        ],
        "_source": "SEC_FORM4",
        "_metadata": {
            "fetched_at": datetime.now().isoformat(),
            "ticker": ticker,
            "filing_date": filing_date,
        }
    }


# ── IPO (S-1) ─────────────────────────────────────────────────────────────────
def s1_to_ipo(
    company_name: str,
    ticker: Optional[str],
    price_range: Optional[str],
    shares: Optional[str],
    deal_size: Optional[str],
    listing_date: Optional[str],
    form_link: str,
    cik: str,
    filing_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Convert SEC S-1 IPO filing to GQuants `ipo` format.

    Args:
        company_name: Name of the IPO company
        ticker: Proposed ticker (may be None pre-IPO)
        price_range: IPO price range, e.g. "$18-$21"
        shares: Shares offered, e.g. "10,000,000"
        deal_size: Total deal size, e.g. "$210M"
        listing_date: Expected listing date
        form_link: Link to SEC form
        cik: SEC CIK number

    Returns:
        Dict with type="ipo" and IPO details
    """
    return {
        "type": "ipo",
        "name": company_name,
        "ipo_details": {
            "general_details": {
                "company_name": company_name,
                "proposed_ticker": ticker or "TBD",
                "status": "Upcoming",
                "sec_cik": cik,
                "form_link": form_link,
                "form_type": "S-1",
            },
            "financial_report": {
                "price_range": price_range or "-",
                "shares_offered": shares or "-",
                "deal_size": deal_size or "-",
            },
            "general_details_timeline": {
                # The date the S-1 was actually filed, which is the whole point
                # of the field. This was datetime.now(), so every payload
                # claimed the registration happened on the day it was rendered —
                # for an S-1 filed months before the listing, off by months.
                # Falls back to today only when the caller genuinely has no
                # filing date to give.
                "filing_date": (filing_date or datetime.now().isoformat()).split("T")[0],
                "expected_listing_date": listing_date or "-",
            },
            "subscription_details": [],
        },
        "_source": "SEC_S1",
        "_metadata": {
            "fetched_at": datetime.now().isoformat(),
            "cik": cik,
            # Was "IPO_PENDING", a raw_filings status that no longer exists —
            # S-1 rows are stored PENDING now. This field describes the DEAL,
            # not our row, so it says what is true of the deal.
            "status": "Upcoming",
        }
    }


# ── Earnings Call Transcripts ──────────────────────────────────────────────────
def earnings_transcript(
    ticker: str,
    company_name: str,
    quarter: str,
    fiscal_year: str,
    transcript_text: str,
    fmp_link: str,
    filing_date: str
) -> Dict[str, Any]:
    """
    Convert earnings call transcript to GQuants `earning_calls` format.

    Args:
        ticker: Stock symbol
        company_name: Full company name
        quarter: Quarter label (e.g., "Q3")
        fiscal_year: Year (e.g., "2026")
        transcript_text: Full transcript text
        fmp_link: Link to transcript source
        filing_date: Filing date

    Returns:
        Dict with type="earning_calls" and transcript
    """
    # Truncate transcript if too long for frontend
    max_chars = 50000
    text_preview = transcript_text[:max_chars]
    if len(transcript_text) > max_chars:
        text_preview += f"\n\n[Transcript truncated; full text available at {fmp_link}]"

    return {
        "type": "earning_calls",
        "name": f"{company_name} {quarter} FY{fiscal_year} Earnings Call",
        "ticker": ticker,
        "period": f"{quarter} FY{fiscal_year}",
        "content": text_preview,
        "source_link": fmp_link,
        "filing_date": filing_date,
        "_source": "FMP_TRANSCRIPT",
        "_metadata": {
            "fetched_at": datetime.now().isoformat(),
            "ticker": ticker,
            "quarter": quarter,
            "fiscal_year": fiscal_year,
        }
    }


# ── Technical Alerts ──────────────────────────────────────────────────────────
def technical_alert(
    ticker: str,
    alert_type: str,
    value: float,
    threshold: float,
    period: str,
    source: str = "Massive"
) -> Dict[str, Any]:
    """
    Convert technical indicator to GQuants `tradingview` format.

    Args:
        ticker: Stock symbol
        alert_type: Type of alert (RSI_OVERBOUGHT, RSI_OVERSOLD, 52W_HIGH, etc.)
        value: Current indicator value
        threshold: Threshold that triggered the alert
        period: Time period (1D, 1W, 1M, etc.)
        source: Data source (Massive, FMP, etc.)

    Returns:
        Dict with technical alert configuration
    """
    alert_map = {
        "RSI_OVERBOUGHT": {"label": "RSI Overbought (>70)", "emoji": "🔴"},
        "RSI_OVERSOLD": {"label": "RSI Oversold (<30)", "emoji": "🟢"},
        "52W_HIGH": {"label": "52-Week High", "emoji": "📈"},
        "52W_LOW": {"label": "52-Week Low", "emoji": "📉"},
        "VOLUME_SPIKE": {"label": "Volume Spike", "emoji": "⚡"},
        "SMA200_CROSSOVER_UP": {"label": "200-SMA Bullish Cross", "emoji": "✅"},
        "SMA200_CROSSOVER_DOWN": {"label": "200-SMA Bearish Cross", "emoji": "❌"},
    }
    alert_info = alert_map.get(alert_type, {"label": alert_type, "emoji": "📊"})

    return {
        "type": "tradingview",
        "name": f"{ticker} — {alert_info['label']}",
        "ticker": ticker,
        "alert_type": alert_type,
        "alert_label": alert_info["label"],
        "emoji": alert_info["emoji"],
        "value": value,
        "threshold": threshold,
        "period": period,
        "source": source,
        "_source": "TECHNICAL",
        "_metadata": {
            "fetched_at": datetime.now().isoformat(),
            "ticker": ticker,
            "alert_type": alert_type,
        }
    }


# ── Wrapper: attach to alert.extra ────────────────────────────────────────────
def attach_structured_payload(alert_dict: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge a structured GQuants payload into an alert's extra field.

    Args:
        alert_dict: The alert dict to be inserted into Supabase
        payload: The structured payload (fr, it, ipo, etc.)

    Returns:
        alert_dict with payload merged into extra
    """
    if not payload:
        return alert_dict

    merged = dict(alert_dict)
    extra = merged.get("extra") or {}
    extra["structured_payload"] = payload
    merged["extra"] = extra
    return merged


def make_frontend_link(payload: Dict[str, Any], alert_id: str) -> str:
    """
    Build the link that renders this structured payload on the frontend.

    The base URL comes from GQUANTS_ALERT_BASE_URL. There is deliberately no
    default: the route is owned by the frontend team and guessing it would put
    a dead link in every alert. Returns "" when unset, and the caller omits the
    link line entirely rather than shipping a broken one.

    Example:
        GQUANTS_ALERT_BASE_URL=https://app.gquants.com/alerts
        -> https://app.gquants.com/alerts/<id>?type=fr&ticker=AAPL
    """
    base = os.getenv("GQUANTS_ALERT_BASE_URL", "").strip().rstrip("/")
    if not base or not alert_id:
        return ""
    payload_type = payload.get("type", "unknown")
    ticker = payload.get("ticker") or payload.get("name") or "MARKET"
    return f"{base}/{alert_id}?type={quote(str(payload_type))}&ticker={quote(str(ticker))}"
