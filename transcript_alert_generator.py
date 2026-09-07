"""
transcript_alert_generator.py
GQ FinXray US — Generate alerts from earnings call transcripts. Feature 5 enhancement.

Wraps earnings_transcript_poller.py output and converts summaries into alerts.
Stores in alerts table with source=FMP_TRANSCRIPT, impact based on sentiment.
"""

import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def transcript_alert_exists(ticker, quarter, year):
    """Check if transcript alert already exists."""
    try:
        result = supabase.table("alerts").select("id").eq(
            "ticker", ticker
        ).eq("source", "FMP_TRANSCRIPT").contains(
            "extra", {"quarter": quarter, "year": year}
        ).execute()
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"[TRANSCRIPT] Dedup check failed: {e}")
        return False


def extract_sentiment_and_guidance(summary: str):
    """Extract sentiment (bullish/neutral/bearish) and forward guidance from summary."""
    summary_lower = summary.lower()

    # Bullish keywords
    bullish_keywords = ["strong", "growth", "improve", "expand", "accelerate", "upside", "beat", "exceed"]
    # Bearish keywords
    bearish_keywords = ["decline", "challenge", "headwind", "pressure", "weaken", "miss", "concern"]
    # Guidance keywords
    guidance_keywords = ["guidance", "outlook", "expect", "anticipate", "forecast", "target"]

    bullish_count = sum(1 for kw in bullish_keywords if kw in summary_lower)
    bearish_count = sum(1 for kw in bearish_keywords if kw in summary_lower)
    has_guidance = any(kw in summary_lower for kw in guidance_keywords)

    if bullish_count > bearish_count:
        sentiment = "BULLISH"
        impact = "HIGH"
    elif bearish_count > bullish_count:
        sentiment = "BEARISH"
        impact = "HIGH"
    else:
        sentiment = "NEUTRAL"
        impact = "MEDIUM"

    return sentiment, impact, has_guidance


def store_transcript_alert(ticker, quarter, year, summary, guidance_given=False):
    """Store earnings transcript alert."""
    try:
        sentiment, impact, has_guidance = extract_sentiment_and_guidance(summary)

        # Truncate summary to first 200 chars for alert
        summary_short = summary[:200] + "..." if len(summary) > 200 else summary

        alert_summary = f"Q{quarter} FY{year} Earnings Call: {sentiment}. {summary_short}"

        alert_data = {
            "ticker": ticker,
            "source": "FMP_TRANSCRIPT",
            "filing_type": "EARNINGS_TRANSCRIPT",
            "summary": alert_summary,
            "impact": impact,
            "delivered": False,
            "extra": tag_extra({
                "quarter": quarter,
                "year": year,
                "sentiment": sentiment,
                "guidance_given": guidance_given or has_guidance,
                "full_summary": summary,
            })
        }

        supabase.table("alerts").insert(alert_data).execute()
        logger.info(f"[TRANSCRIPT] {ticker} Q{quarter}FY{year}: {sentiment} ({impact}) — guidance: {has_guidance}")

    except Exception as e:
        logger.error(f"[TRANSCRIPT] Failed to store alert for {ticker}: {e}")


def generate_transcript_alerts():
    """
    Convert pending transcripts (from earnings_transcript_poller) into alerts.
    This is called after earnings_transcript_poller completes.
    """
    try:
        # Query pending transcripts (not yet alerted)
        result = supabase.table("transcript_summaries").select("*").eq(
            "alert_created", False
        ).limit(50).execute()

        transcripts = result.data
        if not transcripts:
            logger.info("[TRANSCRIPT] No pending transcripts to alert")
            return

        logger.info(f"[TRANSCRIPT] Processing {len(transcripts)} transcripts...")

        for transcript in transcripts:
            try:
                ticker = transcript.get("ticker", "")
                quarter = transcript.get("quarter", "")
                year = transcript.get("year", "")
                summary = transcript.get("summary", "")
                transcript_id = transcript.get("id")

                if not all([ticker, quarter, year, summary]):
                    continue

                # Check dedup
                if transcript_alert_exists(ticker, quarter, year):
                    # Mark as alerted without creating duplicate
                    supabase.table("transcript_summaries").update(
                        {"alert_created": True}
                    ).eq("id", transcript_id).execute()
                    continue

                # Store alert
                store_transcript_alert(ticker, quarter, year, summary)

                # Mark transcript as alerted
                supabase.table("transcript_summaries").update(
                    {"alert_created": True}
                ).eq("id", transcript_id).execute()

            except Exception as e:
                logger.error(f"[TRANSCRIPT] Error processing transcript: {e}")
                continue

        logger.info("[TRANSCRIPT] Transcript alert generation complete")

    except Exception as e:
        logger.error(f"[TRANSCRIPT] Failed to generate alerts: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    generate_transcript_alerts()
