import os
import json
import re
import time
import hashlib
import requests
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = "google/gemini-2.5-flash"

# -- AI Mode enabled via DeepInfra (paid) -------------------------------------
RAW_MODE = False

from Prompt_P2_GibberishChecker import get_prompt as gibberish_prompt
from Prompt_V3_RelevanceCheck import get_prompt as relevance_prompt
from Prompt_V1_SummaryValidation import get_prompt as validation_prompt
from Prompt_V2_SimilarityCheck import get_prompt as similarity_prompt
from Prompt_C1_ImpactClassification import get_prompt as impact_prompt
from Prompt_S1N_NewsSummarization import get_prompt as s1n_prompt
from Prompt_S1A_AnnouncementSummarization import get_prompt as s1a_prompt
from Prompt_S1T_TranscriptSummarization import get_prompt as s1t_prompt
from feature_map import resolve_feature


# ── Word-count escalation ladder ──────────────────────────────────────────────
MIN_WORDS = 70
STARTING_TARGET = 75
TARGET_STEP = 5
MAX_TARGET = 100

TRANSCRIPT_CHAR_LIMIT = 12000
FILING_CHAR_LIMIT = 8000
NEWS_CHAR_LIMIT = 6000


# ── Token usage tracking (per filing currently being processed) ──────────────
_token_usage = {"input": 0, "output": 0, "calls": 0}


def _reset_token_usage():
    _token_usage["input"] = 0
    _token_usage["output"] = 0
    _token_usage["calls"] = 0


def _record_token_usage(usage):
    if not usage:
        return
    _token_usage["input"] += usage.get("prompt_tokens", 0) or 0
    _token_usage["output"] += usage.get("completion_tokens", 0) or 0
    _token_usage["calls"] += 1


def get_token_usage():
    """Snapshot of accumulated tokens since the last _reset_token_usage()."""
    return dict(_token_usage)


# ── Deduplication helpers ─────────────────────────────────────────────────────
def generate_alert_hash(ticker, source, filing_type, summary_snippet):
    """Generate a unique hash for an alert to detect duplicates.
    
    Uses ticker + source + filing_type + first 500 chars of summary
    to create a unique identifier for deduplication.
    """
    unique_str = f"{ticker}#{source}#{filing_type}#{summary_snippet[:500]}"
    return hashlib.sha256(unique_str.encode()).hexdigest()


def check_alert_exists(ticker, source, filing_type, summary):
    """Check if an identical alert already exists in the database."""
    try:
        alert_hash = generate_alert_hash(ticker, source, filing_type, summary)
        result = supabase.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("source", source) \
            .eq("filing_type", filing_type) \
            .execute()
        
        if result.data:
            # Compare with existing summaries to find semantic duplicates
            for existing_alert in result.data:
                existing_id = existing_alert.get("id")
                existing_summary = existing_alert.get("summary", "")
                
                # Check if summaries are very similar (>80% match on first 300 chars)
                existing_snippet = existing_summary[:300]
                new_snippet = summary[:300]
                
                if existing_snippet.lower() == new_snippet.lower():
                    print(f"[DUPLICATE CHECK] Alert exists with ID {existing_id}")
                    return True
        
        return False
    except Exception as e:
        print(f"[WARNING] Could not check for duplicates: {e}")
        return False


def check_raw_filing_exists(ticker, source, filing_type, raw_text_hash=None):
    """Check if a raw filing already exists to prevent duplicate processing."""
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("source", source) \
            .eq("filing_type", filing_type) \
            .eq("status", "PENDING") \
            .execute()
        
        if result.data and len(result.data) > 1:
            # Multiple pending filings for same ticker/source/type = duplicate
            print(f"[RAW FILING DUPLICATE] Found {len(result.data)} pending {filing_type} for {ticker}")
            return True
        
        return False
    except Exception as e:
        print(f"[WARNING] Could not check raw filings: {e}")
        return False


# ── DeepInfra caller ─────────────────────────────────────────────────────────
MIN_CALL_GAP_SECONDS = 2.0
MAX_RATE_LIMIT_RETRIES = 5
_last_call_at = [0.0]


def _pace_before_call():
    elapsed = time.monotonic() - _last_call_at[0]
    if elapsed < MIN_CALL_GAP_SECONDS:
        time.sleep(MIN_CALL_GAP_SECONDS - elapsed)


def call_deepinfra(prompt, retries=3, max_tokens=1000):
    """Call DeepInfra API with Gemini 2.5 Flash."""
    headers = {
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPINFRA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }

    normal_attempt = 0
    rate_limit_attempt = 0

    while True:
        _pace_before_call()
        try:
            r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=30)
        except Exception as e:
            _last_call_at[0] = time.monotonic()
            normal_attempt += 1
            print(f"[DEEPINFRA] Attempt {normal_attempt} error: {e}")
            if normal_attempt >= retries:
                return None
            time.sleep(2 ** normal_attempt)
            continue

        _last_call_at[0] = time.monotonic()

        if r.status_code == 200:
            resp = r.json()
            text = (resp["choices"][0]["message"]["content"] or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            _record_token_usage(resp.get("usage"))
            return text

        if r.status_code == 429:
            rate_limit_attempt += 1
            if rate_limit_attempt > MAX_RATE_LIMIT_RETRIES:
                print(f"[DEEPINFRA] Rate limited (429) persisted after {MAX_RATE_LIMIT_RETRIES} extended waits -- giving up")
                return None
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else min(15 * rate_limit_attempt, 60)
            print(f"[DEEPINFRA] Rate limited (429) -- waiting {wait:.0f}s before retry {rate_limit_attempt}/{MAX_RATE_LIMIT_RETRIES}")
            time.sleep(wait)
            continue

        normal_attempt += 1
        print(f"[DEEPINFRA] Attempt {normal_attempt} failed: {r.status_code} {r.text[:100]}")
        if normal_attempt >= retries:
            return None
        time.sleep(2 ** normal_attempt)


# ── Summary quality helpers ───────────────────────────────────────────────────
BAD_START_KEYWORDS = [
    "this content", "the following", "this document", "this filing",
    "this report", "this article", "this press release", "this announcement",
    "this is a summary", "this text discusses", "this page", "this section",
    "according to the document", "based on the document"
]

BAD_END_KEYWORDS = [
    "more details", "additional information", "further details", "read more",
    "for more information", "learn more", "see", "check", "view", "read",
    "visit", "find out", "discover", "explore"
]


def clean_summary(summary):
    """Strip filler patterns from start/end."""
    for keyword in BAD_START_KEYWORDS:
        pat = re.compile(re.escape(keyword), re.IGNORECASE)
        summary = pat.sub("", summary).strip()
    for keyword in BAD_END_KEYWORDS:
        pat = re.compile(re.escape(keyword) + r"[,.\s]*$", re.IGNORECASE)
        summary = pat.sub("", summary).strip()
    return summary.strip()


def count_words(text):
    """Count words in a string."""
    return len(text.split())


def classify_failure(summary, max_target):
    """Classify why a summary fails quality checks."""
    words = count_words(summary)
    if words < MIN_WORDS:
        return f"too_short({words})"
    if words > max_target:
        return f"too_long({words})"
    return None


def standardize_numbers(text):
    """Standardize number formatting."""
    return text


def parse_json_response(text):
    """Parse JSON from a response, handling markdown code fences."""
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n", "", text)
        text = re.sub(r"\n```$", "", text)
    try:
        return json.loads(text)
    except:
        return {}


def store_flagged_summary(filing_id, ticker, company_name, final_summary, 
                          failure_reason, attempts, source, filing_type):
    """Store summary that failed quality checks for manual review."""
    try:
        supabase.table("flagged_summaries").insert({
            "filing_id": filing_id,
            "ticker": ticker,
            "company_name": company_name,
            "summary": final_summary,
            "failure_reason": failure_reason,
            "attempts": attempts,
            "source": source,
            "filing_type": filing_type,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        print(f"[ERROR] Failed to store flagged summary: {e}")


def get_recent_summaries(ticker, hours=24):
    """Get recent summaries for a ticker to check for duplicates."""
    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = supabase.table("alerts") \
            .select("summary") \
            .eq("ticker", ticker) \
            .gte("created_at", cutoff.isoformat()) \
            .execute()
        return [r.get("summary", "") for r in result.data]
    except Exception as e:
        print(f"[WARNING] Could not fetch recent summaries: {e}")
        return []


def summarise(company_name, raw_text, filing_type, sub_summary, 
              filing_id=None, ticker=None, source=None):
    """Summarize content with retry escalation logic."""
    attempt_log = []
    target = STARTING_TARGET
    
    for attempt_num in range(1, 7):
        prompt_func = {
            "NEWS": s1n_prompt,
            "EARNINGS_TRANSCRIPT": s1t_prompt,
        }.get(filing_type, s1a_prompt)
        
        char_limit = {
            "EARNINGS_TRANSCRIPT": TRANSCRIPT_CHAR_LIMIT,
            "NEWS": NEWS_CHAR_LIMIT,
        }.get(filing_type, FILING_CHAR_LIMIT)
        
        prompt = prompt_func(company_name, raw_text[:char_limit], sub_summary, target)
        summary = call_deepinfra(prompt)
        
        if not summary:
            attempt_log.append({
                "attempt": attempt_num,
                "target": target,
                "failure": "api_error"
            })
            continue
        
        summary = clean_summary(summary)
        failure = classify_failure(summary, target)
        
        if not failure:
            return summary, attempt_num
        
        attempt_log.append({
            "attempt": attempt_num,
            "target": target,
            "summary": summary[:100],
            "failure": failure
        })
        
        if target < MAX_TARGET:
            target = min(target + TARGET_STEP, MAX_TARGET)
        else:
            break
    
    store_flagged_summary(
        filing_id=filing_id, ticker=ticker, company_name=company_name,
        final_summary=summary if summary else "",
        failure_reason="failed_all_attempts",
        attempts=attempt_log,
        source=source or "UNKNOWN",
        filing_type=filing_type
    )
    return None, len(attempt_log)


def store_summary(filing_id, ticker, summary, impact, event_type):
    """Store processed summary."""
    try:
        result = supabase.table("summaries").insert({
            "filing_id": filing_id,
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "event_type": event_type,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        return result.data[0].get("id") if result.data else None
    except Exception as e:
        print(f"[ERROR] Failed to store summary: {e}")
        return None


def store_alert(ticker, summary, impact, source, filing_type, extra=None, summary_id=None):
    """Store alert with deduplication check."""
    try:
        # CHECK FOR DUPLICATE BEFORE STORING
        if check_alert_exists(ticker, source, filing_type, summary):
            print(f"[DUPLICATE ALERT] Skipping duplicate alert for {ticker}")
            return
        
        merged_extra = dict(extra or {})
        merged_extra["summary_id"] = summary_id
        
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": source,
            "filing_type": filing_type,
            "extra": merged_extra,
            "delivered": False,
            "summary_id": summary_id,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        print(f"[ALERT READY] {impact} -- {ticker}: {summary[:80]}...")
    except Exception as e:
        print(f"[ERROR] Failed to store alert: {e}")


def update_filing_status(filing_id, status):
    try:
        supabase.table("raw_filings") \
            .update({"status": status}) \
            .eq("id", filing_id) \
            .execute()
    except Exception as e:
        print(f"[ERROR] Failed to update status: {e}")


# ── AI MODE processor ─────────────────────────────────────────────────────────
def process_filing(filing):
    filing_id    = filing["id"]
    ticker       = filing.get("ticker", "UNKNOWN")
    company_name = filing.get("company_name", "UNKNOWN")
    raw_text     = filing.get("raw_text", "")
    filing_type  = filing.get("filing_type", "")
    source       = filing.get("source", "SEC_EDGAR")
    extra        = filing.get("extra") or {}
    sub_summary  = extra.get("title", "")

    print(f"\n[PROCESSING] {filing_type} -- {company_name} ({ticker}) [source={source}]")

    # Check if duplicate raw filing exists
    if check_raw_filing_exists(ticker, source, filing_type):
        print(f"[DISCARDED] Duplicate raw filing -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return

    _reset_token_usage()

    # Step 1: Gibberish check
    gibberish_result = parse_json_response(call_deepinfra(gibberish_prompt(raw_text[:3000])))
    if gibberish_result.get("is_gibberish") == True:
        print(f"[DISCARDED] Gibberish -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS] Gibberish check")

    # Step 2: Relevance check
    relevance_result = parse_json_response(call_deepinfra(relevance_prompt(company_name, raw_text[:3000])))
    if relevance_result.get("is_relevant") in ("False", False):
        print(f"[DISCARDED] Not relevant to {company_name}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS] Relevance check")

    # Step 3: Summarisation
    summary, summarization_attempts = summarise(company_name, raw_text, filing_type, sub_summary,
                                                 filing_id=filing_id, ticker=ticker, source=source)
    if not summary:
        print(f"[FLAGGED, NOT SENT] {ticker} -- see flagged_summaries for review")
        update_filing_status(filing_id, "FLAGGED_FOR_REVIEW")
        return
    print(f"[SUMMARY] {summary[:100]}... ({count_words(summary)} words)")

    # Step 4: Summary validation (V.1)
    validation_result = parse_json_response(call_deepinfra(validation_prompt(summary)))
    if validation_result.get("issues_detected") == "True":
        corrected = validation_result.get("corrected_summary", "").strip()
        if not corrected:
            print(f"[FLAGGED] V.1 detected issues with no correction provided -- {ticker}")
            store_flagged_summary(
                filing_id=filing_id, ticker=ticker, company_name=company_name,
                final_summary=summary, failure_reason="v1_issues_no_correction",
                attempts=[{"target": None, "summary": summary, "failure": "v1_issues_no_correction"}],
                source=source, filing_type=filing_type
            )
            update_filing_status(filing_id, "FLAGGED_FOR_REVIEW")
            return
        corrected = standardize_numbers(clean_summary(corrected))
        failure = classify_failure(corrected, MAX_TARGET)
        if failure:
            print(f"[FLAGGED] V.1-corrected summary still fails ({failure}) -- {ticker}")
            store_flagged_summary(
                filing_id=filing_id, ticker=ticker, company_name=company_name,
                final_summary=corrected, failure_reason=f"v1_correction_{failure}",
                attempts=[{"stage": "v1_correction", "words": count_words(corrected), "failure": failure}],
                source=source, filing_type=filing_type
            )
            update_filing_status(filing_id, "FLAGGED_FOR_REVIEW")
            return
        summary = corrected
        print(f"[CORRECTED] Summary fixed by V.1 ({count_words(summary)} words)")
    print(f"[PASS] Validation check")

    # Step 5: Impact classification
    cur_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    impact_result = parse_json_response(call_deepinfra(impact_prompt(company_name, summary, cur_date)))
    impact = impact_result.get("impact", "LOW").upper()
    if impact not in ("HIGH", "MEDIUM", "LOW"):
        impact = "LOW"
    print(f"[IMPACT] {impact}")

    # Step 6: Semantic deduplication
    for old_summary in get_recent_summaries(ticker):
        sim_result = parse_json_response(call_deepinfra(similarity_prompt(old_summary, summary)))
        if sim_result.get("is_similar") == "True":
            print(f"[DISCARDED] Duplicate -- {ticker}")
            update_filing_status(filing_id, "DISCARDED")
            return
    print(f"[PASS] Deduplication check")

    usage = get_token_usage()
    extra = dict(extra or {})
    extra["summarization_attempts"] = summarization_attempts
    extra["input_tokens"] = usage["input"]
    extra["output_tokens"] = usage["output"]
    extra["total_tokens"] = usage["input"] + usage["output"]
    extra["llm_calls"] = usage["calls"]

    summary_id = store_summary(filing_id=filing_id, ticker=ticker, summary=summary,
                                impact=impact, event_type=filing_type)
    
    # THIS IS THE KEY FIX: store_alert now includes duplicate checking
    store_alert(ticker=ticker, summary=summary, impact=impact, source=source,
                filing_type=filing_type, extra=extra, summary_id=summary_id)
    
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} alert stored ({summarization_attempts} attempt(s), "
          f"{usage['input']}+{usage['output']} tokens in+out)")


# ── Main pipeline runner ──────────────────────────────────────────────────────
def run_pipeline():
    mode = f"AI (DeepInfra - {DEEPINFRA_MODEL})"
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking for PENDING filings... [{mode} MODE]")
    try:
        result = supabase.table("raw_filings") \
            .select("*") \
            .eq("status", "PENDING") \
            .order("created_at") \
            .limit(10) \
            .execute()

        filings = result.data
        if not filings:
            print("No PENDING filings found.")
            return

        print(f"Found {len(filings)} PENDING filings -- processing...")
        for filing in filings:
            process_filing(filing)
            time.sleep(1)

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")


if __name__ == "__main__":
    run_pipeline()
