"""
analyst_ratings_poller.py
GQ FinXray US — Feature 12: Analyst Ratings & Price Targets.

WHAT THIS EMITS
---------------
source="FMP_ANALYST", filing_type="ANALYST_RATING", one `alerts` row per genuine
change, delivered=False. delivery.py routes it to the users watching that ticker.
This poller never touches Telegram.

THE HARD PART: CHANGE vs RESTATEMENT
------------------------------------
FMP's consensus endpoints return the *current* state of street coverage, not an
event feed. Polling them every two hours and alerting on whatever comes back
would send the same "consensus is Buy, target $214" message twelve times a day.

So every run compares against the most recent FMP_ANALYST alert already stored
for that ticker and emits only when something actually moved:

  * the consensus rating LABEL changed (Hold -> Buy), or
  * the consensus price target moved more than TARGET_MOVE_PCT.

The 5% floor exists because the consensus target drifts by a few tenths of a
percent whenever any one of thirty analysts refreshes a model. That is not news.
A 5% move in the *average* of the whole street requires either several revisions
or one large one, which is.

FIRST OBSERVATION OF A TICKER
-----------------------------
There is nothing to compare against, so there is no change to report — but with
nothing written, the next run has no baseline either and the ticker can never
alert. The poller therefore writes a baseline row with delivered=True, which
seeds the comparison without ever reaching a user. It is marked
extra["baseline"]=True so it is obvious in the table what it is.

HEADLINES
---------
This poller does not go through ai_pipeline, so nothing else would populate
extra["headline"] and alert_formatter would render a bare paragraph. The
headline is composed here from the same numbers as the summary — it can never
assert something the summary does not.
"""

import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

ET = ZoneInfo("America/New_York")

SOURCE = "FMP_ANALYST"
FILING_TYPE = "ANALYST_RATING"

# Below this, a move in the consensus target is one analyst nudging a model.
TARGET_MOVE_PCT = 5.0
# Above this, the whole street has repriced the name — treat it like a rating change.
HIGH_IMPACT_TARGET_MOVE_PCT = 15.0

# Politeness gap between tickers. Two FMP calls per ticker, tens of tickers.
TICKER_GAP_SECONDS = 0.1

# Ordering used only to decide whether a label change reads as an upgrade or a
# downgrade. Synonyms are mapped onto the same rung because different brokers'
# vocabularies get aggregated into FMP's `consensus` field.
_RATING_RANK = {
    "STRONG SELL": 1, "SELL": 2, "UNDERPERFORM": 2, "UNDERWEIGHT": 2, "REDUCE": 2,
    "HOLD": 3, "NEUTRAL": 3, "MARKET PERFORM": 3, "EQUAL WEIGHT": 3, "SECTOR PERFORM": 3,
    "BUY": 4, "OUTPERFORM": 4, "OVERWEIGHT": 4, "ACCUMULATE": 4, "MODERATE BUY": 4,
    "STRONG BUY": 5,
}


# ── Small helpers ─────────────────────────────────────────────────────────────
def _norm_label(value):
    """'  strong  buy ' -> 'Strong Buy'. Returns '' for anything unusable."""
    if not value or not isinstance(value, str):
        return ""
    return " ".join(value.strip().split()).title()


def _rank(label):
    return _RATING_RANK.get((label or "").upper().strip(), 0)


def _num(value):
    """float() that returns None instead of raising, and treats 0 as 'no data'."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── Supabase reads/writes ─────────────────────────────────────────────────────
def _latest_prior_alert(ticker):
    """
    The most recent FMP_ANALYST alert for this ticker, or None.

    One query per ticker. With a watchlist of tens that is tens of cheap indexed
    lookups per run; batching them into a single `in_()` would risk PostgREST's
    1,000-row cap truncating an older ticker's newest row out of the result.
    """
    try:
        rows = (supabase.table("alerts")
                .select("id, extra, created_at")
                .eq("source", SOURCE)
                .eq("ticker", ticker)
                .order("created_at", desc=True)
                .limit(1)
                .execute()).data or []
        return rows[0] if rows else None
    except Exception as e:
        logger.error("[ANALYST] Prior-alert lookup failed for %s: %s", ticker, e)
        # Returning None here would look like "first observation" and write a
        # duplicate baseline. Raise a sentinel instead so the caller can skip.
        raise


def _company_names(tickers):
    """{TICKER: company_name} from the watchlists table, in one query."""
    names = {}
    tickers = sorted(tickers)
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        try:
            rows = (supabase.table("watchlists")
                    .select("ticker, company_name")
                    .in_("ticker", chunk)
                    .execute()).data or []
        except Exception as e:
            logger.error("[ANALYST] Company-name lookup failed: %s", e)
            continue
        for r in rows:
            t = (r.get("ticker") or "").upper()
            if t and r.get("company_name") and t not in names:
                names[t] = r["company_name"]
    return names


def _insert_alert(ticker, summary, impact, extra, delivered=False):
    try:
        # Link to FMP analyst estimates page
        filing_url = f"https://site.financialmodelingprep.com/analyst-estimates/{ticker.upper()}"

        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": SOURCE,
            "filing_type": FILING_TYPE,
            "extra": tag_extra(extra, SOURCE, FILING_TYPE),
            "delivered": delivered,
            "filing_url": filing_url,
        }).execute()
        return True
    except Exception as e:
        logger.error("[ANALYST] Failed to insert alert for %s: %s", ticker, e)
        log_poller_error("analyst_ratings_poller", "insert_alert", e, {"ticker": ticker})
        return False


# ── Snapshot ──────────────────────────────────────────────────────────────────
def _fetch_snapshot(ticker):
    """
    Current coverage state for one ticker: two FMP calls, individually guarded.

    Returns a dict, or None when neither call produced anything usable (the
    normal outcome for a micro-cap nobody covers — not an error).
    """
    grades = None
    targets = None

    try:
        grades = fmp_client.get_grades_consensus(ticker)
    except Exception as e:
        logger.warning("[ANALYST] grades-consensus failed for %s: %s", ticker, e)

    try:
        targets = fmp_client.get_price_target_consensus(ticker)
    except Exception as e:
        logger.warning("[ANALYST] price-target-consensus failed for %s: %s", ticker, e)

    grades = grades or {}
    targets = targets or {}

    counts = {
        "strong_buy": _int(grades.get("strongBuy")),
        "buy": _int(grades.get("buy")),
        "hold": _int(grades.get("hold")),
        "sell": _int(grades.get("sell")),
        "strong_sell": _int(grades.get("strongSell")),
    }
    analyst_count = sum(counts.values())

    snap = {
        "consensus": _norm_label(grades.get("consensus")),
        "analyst_count": analyst_count,
        "target_consensus": _num(targets.get("targetConsensus")),
        "target_median": _num(targets.get("targetMedian")),
        "target_high": _num(targets.get("targetHigh")),
        "target_low": _num(targets.get("targetLow")),
        **counts,
    }

    if not snap["consensus"] and snap["target_consensus"] is None:
        return None
    return snap


# ── Wording ───────────────────────────────────────────────────────────────────
def _build_summary(snap, prior, rating_changed, target_pct):
    """A readable sentence or two that names every number the alert is based on."""
    parts = []

    new_label = snap["consensus"]
    old_label = (prior or {}).get("consensus") or ""

    if rating_changed:
        parts.append(f"Analyst consensus moved to {new_label} from {old_label}.")
    elif new_label:
        parts.append(f"Analyst consensus stands at {new_label}.")

    target = snap["target_consensus"]
    count = snap["analyst_count"]
    coverage = f" across {count} analysts covering the stock" if count else ""

    if target is not None and target_pct is not None and abs(target_pct) >= 0.05:
        verb = "raised" if target_pct > 0 else "cut"
        parts.append(
            f"Average price target {verb} {abs(target_pct):.1f}% to ${target:,.2f}{coverage}."
        )
    elif target is not None:
        parts.append(f"Average price target is ${target:,.2f}{coverage}.")

    low, high = snap["target_low"], snap["target_high"]
    if low is not None and high is not None and high > low:
        parts.append(f"Street targets range from ${low:,.2f} to ${high:,.2f}.")

    if count:
        parts.append(
            f"Current split: {snap['strong_buy']} strong buy, {snap['buy']} buy, "
            f"{snap['hold']} hold, {snap['sell']} sell, {snap['strong_sell']} strong sell."
        )

    return " ".join(parts)


def _build_headline(snap, prior, rating_changed, target_pct):
    """
    4–10 words, Title Case, no ticker, no hype — same rules as Prompt_H1, applied
    deterministically because there is no LLM in this path.
    """
    new_label = snap["consensus"]
    old_rank = _rank((prior or {}).get("consensus"))
    new_rank = _rank(new_label)

    if rating_changed and new_label:
        if new_rank and old_rank and new_rank > old_rank:
            direction = "Upgraded"
        elif new_rank and old_rank and new_rank < old_rank:
            direction = "Downgraded"
        else:
            direction = "Revised"

        if target_pct is not None and target_pct >= TARGET_MOVE_PCT:
            return f"Consensus {direction} To {new_label} On Raised Targets"
        if target_pct is not None and target_pct <= -TARGET_MOVE_PCT:
            return f"Consensus {direction} To {new_label} As Targets Fall"
        return f"Analyst Consensus {direction} To {new_label}"

    if target_pct is not None:
        verb = "Lift" if target_pct > 0 else "Cut"
        return f"Analysts {verb} Average Price Target {abs(target_pct):.0f} Percent"

    return ""


# ── Change detection ──────────────────────────────────────────────────────────
def _evaluate(snap, prior):
    """
    Returns (should_alert, rating_changed, target_pct).

    target_pct is the percentage move in the consensus target, or None when
    either side is missing (a newly-covered name has no prior target — that is
    not a 100% move).
    """
    old_label = _norm_label((prior or {}).get("consensus"))
    new_label = snap["consensus"]
    rating_changed = bool(old_label and new_label and old_label != new_label)

    old_target = _num((prior or {}).get("target_consensus"))
    new_target = snap["target_consensus"]
    target_pct = None
    if old_target is not None and new_target is not None:
        target_pct = (new_target - old_target) / old_target * 100.0

    target_moved = target_pct is not None and abs(target_pct) > TARGET_MOVE_PCT
    return (rating_changed or target_moved), rating_changed, target_pct


def _impact(rating_changed, target_pct):
    if rating_changed:
        return "HIGH"
    if target_pct is not None and abs(target_pct) > HIGH_IMPACT_TARGET_MOVE_PCT:
        return "HIGH"
    return "MEDIUM"


# ── Per-ticker ────────────────────────────────────────────────────────────────
def _process_ticker(ticker, company_name):
    """Returns 'alert', 'baseline', 'nochange' or 'skip'."""
    snap = _fetch_snapshot(ticker)
    if not snap:
        return "skip"

    prior_row = _latest_prior_alert(ticker)
    prior = (prior_row or {}).get("extra") or {}

    # Nothing stored yet — record the reference point without alerting anyone.
    if not prior_row or (not prior.get("consensus") and prior.get("target_consensus") is None):
        baseline_extra = dict(snap)
        baseline_extra.update({
            "baseline": True,
            "company_name": company_name or ticker,
            "observed_at": datetime.now(ET).isoformat(),
        })
        target = snap["target_consensus"]
        target_txt = f"average target ${target:,.2f}" if target is not None else "no published target"
        summary = (
            f"Baseline analyst coverage recorded: consensus "
            f"{snap['consensus'] or 'not published'}, {target_txt} "
            f"across {snap['analyst_count']} analysts. No alert sent — this is the "
            f"reference point future rating and target changes are measured against."
        )
        # The return value MUST be checked. The baseline row is the only record
        # that this ticker has ever been observed; if the insert fails and we
        # still report success, the next run finds no prior row, writes another
        # baseline, and the ticker re-baselines forever without ever alerting.
        # Reporting the failure lets the caller count it and retry next cycle.
        if not _insert_alert(ticker, summary, "LOW", baseline_extra, delivered=True):
            logger.error("[ANALYST] Baseline insert failed for %s — will retry next run", ticker)
            return "error"
        return "baseline"

    should_alert, rating_changed, target_pct = _evaluate(snap, prior)
    if not should_alert:
        return "nochange"

    impact = _impact(rating_changed, target_pct)
    summary = _build_summary(snap, prior, rating_changed, target_pct)
    if not summary:
        # Nothing sayable means nothing worth sending, whatever the numbers did.
        return "nochange"

    headline = _build_headline(snap, prior, rating_changed, target_pct)

    extra = dict(snap)
    extra.update({
        "company_name": company_name or ticker,
        "prev_consensus": _norm_label(prior.get("consensus")) or None,
        "prev_target_consensus": _num(prior.get("target_consensus")),
        "target_move_pct": round(target_pct, 2) if target_pct is not None else None,
        "rating_changed": rating_changed,
        "observed_at": datetime.now(ET).isoformat(),
    })
    if headline:
        extra["headline"] = headline

    if _insert_alert(ticker, summary, impact, extra, delivered=False):
        logger.info("[ANALYST] %s %s — %s", ticker, impact, headline or summary[:60])
        return "alert"
    return "skip"


# ── Entry point ───────────────────────────────────────────────────────────────
def poll_analyst_ratings():
    """
    One full pass over the watchlist. Never raises — a scheduler calls this.
    """
    try:
        tickers = sorted(get_watched_tickers())
        if not tickers:
            logger.info("[ANALYST] No watchlisted tickers; nothing to poll.")
            return

        logger.info("[ANALYST] Polling analyst coverage for %d tickers", len(tickers))
        names = _company_names(set(tickers))

        stats = {"alert": 0, "baseline": 0, "nochange": 0, "skip": 0, "error": 0}
        for ticker in tickers:
            try:
                stats[_process_ticker(ticker, names.get(ticker))] += 1
            except Exception as e:
                stats["error"] += 1
                logger.error("[ANALYST] %s failed: %s", ticker, e)
                log_poller_error("analyst_ratings_poller", "process_ticker", e,
                                 {"ticker": ticker})
            time.sleep(TICKER_GAP_SECONDS)

        logger.info("[ANALYST] Done — alerts=%d baselines=%d unchanged=%d "
                    "no-coverage=%d errors=%d",
                    stats["alert"], stats["baseline"], stats["nochange"],
                    stats["skip"], stats["error"])

    except Exception as e:
        logger.exception("[ANALYST] Run failed: %s", e)
        log_poller_error("analyst_ratings_poller", "poll_analyst_ratings", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    poll_analyst_ratings()
