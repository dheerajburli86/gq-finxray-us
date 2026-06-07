import os
import json
import time
import requests as http_requests
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

DEEPINFRA_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
MODEL = "google/gemini-2.5-flash"

from Prompt_P2_GibberishChecker import get_prompt as gibberish_prompt
from Prompt_V3_RelevanceCheck import get_prompt as relevance_prompt
from Prompt_V1_SummaryValidation import get_prompt as validation_prompt
from Prompt_V2_SimilarityCheck import get_prompt as similarity_prompt
from Prompt_C1_ImpactClassification import get_prompt as impact_prompt

def call_gemini(prompt, retries=3):
    for attempt in range(retries):
        try:
            r = http_requests.post(
                DEEPINFRA_URL,
                headers={
                    "Authorization": f"Bearer {DEEPINFRA_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                    "temperature": 0.1
                },
                timeout=30
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"].strip()
                # Strip <think>...</think> reasoning tags
                if "<think>" in content:
                    if "</think>" in content:
                        content = content.split("</think>")[-1].strip()
                    else:
                        content = content.split("<think>")[0].strip()
                return content
            elif r.status_code == 429:
                wait = (attempt + 1) * 10
                print(f"[DEEPINFRA] Rate limited — waiting {wait}s")
                time.sleep(wait)
            else:
                print(f"[DEEPINFRA] Attempt {attempt+1} failed: {r.status_code} {r.text[:100]}")
                time.sleep(2 ** attempt)
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

Summarise the following filing in 40 to 60 words. You must write at least 40 words.
Write in plain English. Be specific — include names, numbers, amounts, percentages where available.
Focus on what happened, who was involved, and why it matters to investors.
Do not use first person. Do not mention word count. Do not use vague language.

For insider trades include: insider name, their role, whether they bought or sold, number of shares, dollar value if available.
For earnings include: revenue, EPS, whether beat or missed estimates, guidance if mentioned.
For leadership changes include: who left, who replaced them, effective date.
For acquisitions include: companies involved, deal value, strategic rationale.

Company: {company_name}

Filing text:
{raw_text[:8000]}

Return only the summary text, nothing else."""
    return call_gemini(prompt)

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

def store_alert(ticker, summary, impact, source, filing_type=None, extra=None):
    try:
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": source,
            "filing_type": filing_type,
            "delivered": False,
            "extra": extra or {}
        }).execute()
        print(f"[ALERT READY] {impact} — ${ticker}: {summary[:80]}...")
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

def process_filing(filing):
    filing_id    = filing["id"]
    ticker       = filing.get("ticker", "UNKNOWN")
    company_name = filing.get("company_name", "UNKNOWN")
    raw_text     = filing.get("raw_text", "")
    filing_type  = filing.get("filing_type", "")
    source       = filing.get("source", "SEC_EDGAR")
    extra        = filing.get("extra") or {}

    print(f"\n[PROCESSING] {filing_type} — {company_name} ({ticker})")

    # ── Step 1: Gibberish check ───────────────────────────────
    gibberish_response = call_gemini(gibberish_prompt(raw_text[:3000]))
    gibberish_result = parse_json_response(gibberish_response)
    if gibberish_result.get("is_gibberish") == True:
        print(f"[DISCARDED] Gibberish detected — {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS] Gibberish check")

    # ── Step 2: Relevance check ───────────────────────────────
    relevance_response = call_gemini(relevance_prompt(company_name, raw_text[:3000]))
    relevance_result = parse_json_response(relevance_response)
    if relevance_result.get("is_relevant") == "False" or \
       relevance_result.get("is_relevant") == False:
        print(f"[DISCARDED] Not relevant to {company_name}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS] Relevance check")

    # ── Step 3: Summarisation ─────────────────────────────────
    summary = summarise(company_name, raw_text)
    if not summary:
        print(f"[DISCARDED] Summarisation failed — {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[SUMMARY] {summary[:100]}...")

    # ── Step 4: Summary validation ────────────────────────────
    validation_response = call_gemini(validation_prompt(summary))
    validation_result = parse_json_response(validation_response)
    if validation_result.get("issues_detected") == "True":
        corrected = validation_result.get("corrected_summary", "").strip()
        if corrected:
            summary = corrected
            print(f"[CORRECTED] Summary fixed")
        else:
            print(f"[DISCARDED] Validation failed — {ticker}")
            update_filing_status(filing_id, "DISCARDED")
            return
    print(f"[PASS] Validation check")

    # ── Step 5: Impact classification ────────────────────────
    cur_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    impact_response = call_gemini(impact_prompt(company_name, summary, cur_date))
    impact_result = parse_json_response(impact_response)
    impact = impact_result.get("impact", "LOW").upper()
    if impact not in ("HIGH", "MEDIUM", "LOW"):
        impact = "LOW"
    print(f"[IMPACT] {impact}")

    # ── Step 6: Semantic deduplication ───────────────────────
    recent_summaries = get_recent_summaries(ticker)
    for old_summary in recent_summaries:
        similarity_response = call_gemini(similarity_prompt(old_summary, summary))
        similarity_result = parse_json_response(similarity_response)
        if similarity_result.get("is_similar") == "True":
            print(f"[DISCARDED] Duplicate alert — {ticker}")
            update_filing_status(filing_id, "DISCARDED")
            return
    print(f"[PASS] Deduplication check")

    # ── Store summary + alert ─────────────────────────────────
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
    print(f"[DONE] {ticker} — {impact} alert stored ✅")

def run_pipeline():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking for PENDING filings...")
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

        print(f"Found {len(filings)} PENDING filings — processing...")
        for filing in filings:
            process_filing(filing)
            time.sleep(1)

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")

if __name__ == "__main__":
    run_pipeline()