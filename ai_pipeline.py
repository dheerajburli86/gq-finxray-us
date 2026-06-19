import os
import json
import time
import requests as http_requests
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import re

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

DEEPINFRA_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
MODEL = "google/gemini-2.5-flash"

from Prompt_P2_GibberishChecker import get_prompt as gibberish_prompt
from Prompt_V2_SimilarityCheck import get_prompt as similarity_prompt
from Prompt_C1_ImpactClassification import get_prompt as impact_prompt

def call_llm(prompt, retries=3, max_tokens=1000):
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
                    "max_tokens": max_tokens,
                    "temperature": 0.1
                },
                timeout=30
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"].strip()
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

def force_complete_summary(summary, company_name, raw_text):
    """
    Pass 2 — always runs after Pass 1.
    Takes the generated summary and either confirms it is complete
    or rewrites the ending to ensure it ends properly with investor significance.
    This is not a fallback — it runs on EVERY summary.
    """
    prompt = f"""You are a financial editor reviewing a summary about {company_name}.

Your job is to check if the summary below is complete and ends with a clear investor significance sentence. Then return a polished final version.

RULES:
1. If the summary is complete and already ends with investor significance — return it exactly as is with no changes.
2. If it ends mid-sentence, mid-phrase, or without investor significance — fix the ending using details from the source text.
3. The final sentence must explain why this matters to investors or what it signals for the stock.
4. Total word count must be 55-70 words.
5. The final character MUST be a full stop.
6. Never say "this filing" or "this article".
7. Return ONLY the final summary — no preamble, no labels, no explanation.

Summary to review:
{summary}

Source text for reference:
{raw_text[:3000]}

Return ONLY the final polished summary. Last character MUST be a full stop."""
    result = call_llm(prompt, max_tokens=600)
    return result

def is_complete_summary(text):
    """
    Basic sanity check only — catches catastrophic failures.
    The force_complete_summary step handles quality.
    """
    if not text or len(text.strip()) < 10:
        return False
    if text.strip().rstrip()[-1] not in ".!?":
        return False
    if len(text.split()) < 30:
        return False
    return True

def is_news_relevant(company_name, raw_text):
    text = raw_text.lower()
    non_financial_phrases = [
        "summer cabin", "should i pay", "my child was given",
        "dear abby", "horoscope", "recipe for", "movie review",
        "celebrity", "fashion week", "sports score", "nfl score",
        "nba score", "nhl score", "mlb score", "soccer score",
        "dating advice", "relationship advice", "health tips",
        "weight loss", "diet plan", "workout routine",
        "travel guide", "vacation tips", "restaurant review",
        "book review", "tv show", "film review", "music review",
        "crossword", "sudoku", "puzzle", "lottery results"
    ]
    for phrase in non_financial_phrases:
        if phrase in text:
            print(f"[RELEVANCE] Discarded — matched: {phrase}")
            return False
    return True

def summarise_announcement(company_name, raw_text):
    prompt = f"""Your task is to summarize the provided document specifically focusing on the company: {company_name}.

PURPOSE: The summary must help investors understand significant developments related to {company_name}.

CONTENT RULES:
1. Include ONLY details directly relevant to {company_name} and its investors.
2. Exclude any information unrelated to {company_name}.
3. Only use factual information explicitly mentioned in the original document.
4. Do not add interpretations, opinions, or recommendations.

FORMAT:
- Single paragraph, no line breaks.
- 55-70 words.
- Do not start with the company name.
- Do not say "this filing" or "this document".

WHAT TO EXTRACT BY TYPE:
- Earnings: revenue, net income, EPS, year-over-year change, guidance.
- Leadership change: who left, their role, replacement name, effective date.
- Acquisition: both companies, deal value, strategic reason, expected close.
- Government grant: exact dollar amount, awarding body, purpose.
- Clinical trial: drug name, phase, result, patient count.
- Annual meeting: key votes, outcomes, directors elected.
- Regulatory/legal: what changed, who it affects, financial exposure.

IMPORTANT: Write 2-3 sentences of facts, then end with one sentence explaining why this matters to investors.

Document:
{raw_text[:8000]}

Return ONLY the summary. Nothing else."""
    return call_llm(prompt, max_tokens=800)

def summarise_form4(company_name, raw_text):
    prompt = f"""Summarize this SEC Form 4 insider transaction for {company_name} in 55-70 words.

Extract: insider full name, exact title, bought or sold, exact share count, price per share, total dollar value, date, shares owned after.

End with one sentence on what this signals for investors.

Single paragraph. No line breaks. Final character must be a full stop.

Form 4 data:
{raw_text[:8000]}

Return ONLY the summary."""
    return call_llm(prompt, max_tokens=800)

def summarise_news(company_name, raw_text):
    prompt = f"""Summarize this news article focusing on {company_name} in 55-70 words.

Include only facts directly relevant to {company_name}. Include analyst names and firms if mentioned. Include specific figures — prices, percentages, targets.

End with one sentence explaining why this matters to investors.

Single paragraph. No line breaks. Final character must be a full stop.

Article:
{raw_text[:8000]}

Return ONLY the summary."""
    return call_llm(prompt, max_tokens=800)

def summarise(company_name, raw_text, filing_type=""):
    if filing_type == "NEWS":
        return summarise_news(company_name, raw_text)
    elif filing_type == "4":
        return summarise_form4(company_name, raw_text)
    else:
        return summarise_announcement(company_name, raw_text)

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

def store_alert(ticker, summary, impact, source, filing_type=None, extra=None, filing_url=""):
    try:
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": source,
            "filing_type": filing_type,
            "delivered": False,
            "extra": extra or {},
            "filing_url": filing_url
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
    filing_url   = filing.get("filing_url", "")

    print(f"\n[PROCESSING] {filing_type} — {company_name} ({ticker})")

    # ── Step 1: Gibberish check ───────────────────────────────
    gibberish_response = call_llm(gibberish_prompt(raw_text[:3000]))
    gibberish_result = parse_json_response(gibberish_response)
    if gibberish_result.get("is_gibberish") == True:
        print(f"[DISCARDED] Gibberish — {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS] Gibberish check")

    # ── Step 2: Relevance check for news only ─────────────────
    if filing_type == "NEWS":
        relevant = is_news_relevant(company_name, raw_text)
        if not relevant:
            print(f"[DISCARDED] Not relevant — {ticker}")
            update_filing_status(filing_id, "DISCARDED")
            return
        print(f"[PASS] Relevance check")
    else:
        print(f"[SKIP] Relevance check")

    # ── Step 3: Pass 1 — Generate summary ────────────────────
    if filing_type == "NEWS" and company_name in ("MARKET", "SPY", "QQQ", "DIA", "UNKNOWN"):
        summarise_name = "the US stock market and major indices"
    else:
        summarise_name = company_name

    summary = summarise(summarise_name, raw_text, filing_type)
    if not summary or len(summary.strip()) < 10:
        print(f"[DISCARDED] Summarisation failed — {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[PASS 1] {len(summary.split())} words — {summary[:80]}...")

    # ── Step 4: Pass 2 — Force complete ──────────────────────
    # Runs on EVERY summary — not just incomplete ones
    completed = force_complete_summary(summary, summarise_name, raw_text)
    if completed and is_complete_summary(completed):
        summary = completed
        print(f"[PASS 2] {len(summary.split())} words — complete ✅")
    elif is_complete_summary(summary):
        print(f"[PASS 2] Kept Pass 1 — {len(summary.split())} words ✅")
    else:
        print(f"[DISCARDED] Both passes failed — {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return

    # ── Step 5: Impact classification ────────────────────────
    cur_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    impact_response = call_llm(impact_prompt(company_name, summary, cur_date))
    impact_result = parse_json_response(impact_response)
    impact = impact_result.get("impact", "LOW").upper()
    if impact not in ("HIGH", "MEDIUM", "LOW"):
        impact = "LOW"
    print(f"[IMPACT] {impact}")

    # ── Step 6: Semantic deduplication ───────────────────────
    recent_summaries = get_recent_summaries(ticker)
    for old_summary in recent_summaries:
        similarity_response = call_llm(similarity_prompt(old_summary, summary))
        similarity_result = parse_json_response(similarity_response)
        if similarity_result.get("is_similar") == "True":
            print(f"[DISCARDED] Duplicate — {ticker}")
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
        extra=extra,
        filing_url=filing_url
    )
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} — {impact} alert stored ✅")

def run_pipeline():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking for PENDING filings...")
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        result = supabase.table("raw_filings") \
            .select("*") \
            .eq("status", "PENDING") \
            .gte("created_at", cutoff) \
            .order("created_at", desc=True) \
            .limit(25) \
            .execute()

        filings = result.data
        if not filings:
            print("No PENDING filings found.")
            return

        print(f"Found {len(filings)} PENDING filings — processing...")
        for filing in filings:
            process_filing(filing)
            time.sleep(0.5)

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")

if __name__ == "__main__":
    run_pipeline()