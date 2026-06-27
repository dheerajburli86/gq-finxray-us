import os
import json
import time
import requests
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = "google/gemini-3.5-flash"

# -- AI Mode enabled via DeepInfra (paid) -------------------------------------
RAW_MODE = False

from Prompt_P2_GibberishChecker import get_prompt as gibberish_prompt
from Prompt_V3_RelevanceCheck import get_prompt as relevance_prompt
from Prompt_V1_SummaryValidation import get_prompt as validation_prompt
from Prompt_V2_SimilarityCheck import get_prompt as similarity_prompt
from Prompt_C1_ImpactClassification import get_prompt as impact_prompt


def call_deepinfra(prompt, retries=3, max_tokens=1000):
    """Call DeepInfra API with Gemini 2.0 Flash. Replaces call_gemini."""
    headers = {
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPINFRA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }
    for attempt in range(retries):
        try:
            r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                import re
                text = (r.json()["choices"][0]["message"]["content"] or "").strip()
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                return text
            else:
                print(f"[DEEPINFRA] Attempt {attempt+1} failed: {r.status_code} {r.text[:100]}")
        except Exception as e:
            print(f"[DEEPINFRA] Attempt {attempt+1} error: {e}")
        time.sleep(2 ** attempt)
    return None


def parse_json_response(text):
    if not text:
        return {}
    try:
        clean = text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except:
        return {}


def summarise(company_name, raw_text):
    prompt = f"""You are a financial analyst writing concise alerts for retail investors.

Summarise the following filing in exactly 50 words or less.
Write in plain English. Focus on what happened and why it matters to investors.
Do not use first person. Do not mention word count.
Company: {company_name}

Filing text:
{raw_text[:8000]}

Return only the summary text, nothing else."""
    return call_deepinfra(prompt, max_tokens=500)


def get_recent_summaries(ticker, limit=10):
    try:
        result = supabase.table("ai_summaries") \
            .select("summary") \
            .eq("ticker", ticker) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        return [r["summary"] for r in result.data if r.get("summary")]
    except:
        return []


def store_summary(filing_id, ticker, summary, impact, event_type):
    try:
        supabase.table("ai_summaries").insert({
            "filing_id": filing_id,
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "event_type": event_type
        }).execute()
    except Exception as e:
        print(f"[ERROR] Failed to store summary: {e}")


def store_alert(ticker, summary, impact, source, filing_type="", extra=None):
    try:
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": source,
            "filing_type": filing_type,
            "extra": extra or {},
            "delivered": False
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


# -- RAW MODE processor (kept as fallback) ------------------------------------
def process_filing_raw(filing):
    filing_id    = filing["id"]
    ticker       = filing.get("ticker", "UNKNOWN")
    company_name = filing.get("company_name", ticker)
    raw_text     = filing.get("raw_text", "")
    filing_type  = filing.get("filing_type", "")
    source       = filing.get("source", "SEC_EDGAR")
    extra        = filing.get("extra") or {}

    title = extra.get("title", "") or raw_text[:120].replace("\n", " ").strip()
    summary = title if title else f"{filing_type} filing from {company_name}"

    high_keywords = ["acquisition", "merger", "bankruptcy", "ceo", "resign",
                     "fraud", "sec investigation", "earnings", "guidance", "buyback"]
    medium_keywords = ["partnership", "contract", "agreement", "launch",
                       "appointed", "dividend", "expansion"]
    text_lower = raw_text.lower()
    if any(k in text_lower for k in high_keywords):
        impact = "HIGH"
    elif any(k in text_lower for k in medium_keywords):
        impact = "MEDIUM"
    else:
        impact = "LOW"

    print(f"[RAW MODE] {filing_type} -- {company_name} ({ticker}) -> {impact}")

    store_alert(
        ticker=ticker,
        summary=summary,
        impact=impact,
        source=source,
        filing_type=filing_type,
        extra=extra
    )
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} alert stored (raw mode)")


# -- AI MODE processor --------------------------------------------------------
def process_filing(filing):
    filing_id    = filing["id"]
    ticker       = filing.get("ticker", "UNKNOWN")
    company_name = filing.get("company_name", "UNKNOWN")
    raw_text     = filing.get("raw_text", "")
    filing_type  = filing.get("filing_type", "")
    source       = filing.get("source", "SEC_EDGAR")
    extra        = filing.get("extra") or {}

    print(f"\n[PROCESSING] {filing_type} -- {company_name} ({ticker})")

    # Step 1: Gibberish check
    gibberish_response = call_deepinfra(gibberish_prompt(raw_text[:3000]))
    gibberish_result = parse_json_response(gibberish_response)
    if gibberish_result.get("is_gibberish") == True:
        print(f"[DISCARDED] Gibberish detected -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS] Gibberish check")

    # Step 2: Relevance check
    relevance_response = call_deepinfra(relevance_prompt(company_name, raw_text[:3000]))
    relevance_result = parse_json_response(relevance_response)
    if relevance_result.get("is_relevant") == "False" or \
       relevance_result.get("is_relevant") == False:
        print(f"[DISCARDED] Not relevant to {company_name}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS] Relevance check")

    # Step 3: Summarisation
    summary = summarise(company_name, raw_text)
    if not summary:
        print(f"[DISCARDED] Summarisation failed -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[SUMMARY] {summary[:100]}...")

    # Step 4: Summary validation
    validation_response = call_deepinfra(validation_prompt(summary))
    validation_result = parse_json_response(validation_response)
    if validation_result.get("issues_detected") == "True":
        corrected = validation_result.get("corrected_summary", "").strip()
        if corrected:
            summary = corrected
            print(f"[CORRECTED] Summary fixed")
        else:
            print(f"[DISCARDED] Validation failed with no correction -- {ticker}")
            update_filing_status(filing_id, "DISCARDED")
            return
    print(f"[PASS] Validation check")

    # Step 5: Impact classification
    cur_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    impact_response = call_deepinfra(impact_prompt(company_name, summary, cur_date))
    impact_result = parse_json_response(impact_response)
    impact = impact_result.get("impact", "LOW").upper()
    if impact not in ("HIGH", "MEDIUM", "LOW"):
        impact = "LOW"
    print(f"[IMPACT] {impact}")

    # Step 6: Semantic deduplication
    recent_summaries = get_recent_summaries(ticker)
    for old_summary in recent_summaries:
        similarity_response = call_deepinfra(similarity_prompt(old_summary, summary))
        similarity_result = parse_json_response(similarity_response)
        if similarity_result.get("is_similar") == "True":
            print(f"[DISCARDED] Duplicate alert -- {ticker}")
            update_filing_status(filing_id, "DISCARDED")
            return
    print(f"[PASS] Deduplication check")

    store_summary(
        filing_id=filing_id,
        ticker=ticker,
        summary=summary,
        impact=impact,
        event_type=filing_type
    )
    store_alert(
        ticker=ticker,
        summary=summary,
        impact=impact,
        source=source,
        filing_type=filing_type,
        extra=extra
    )
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} alert stored")


# -- Main pipeline runner -----------------------------------------------------
def run_pipeline():
    mode = "RAW" if RAW_MODE else "AI (DeepInfra)"
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
            if RAW_MODE:
                process_filing_raw(filing)
            else:
                process_filing(filing)
            time.sleep(1)

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")


if __name__ == "__main__":
    run_pipeline()
