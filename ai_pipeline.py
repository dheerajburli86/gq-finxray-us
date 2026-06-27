import os
import json
import re
import time
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


# ── DeepInfra caller ─────────────────────────────────────────────────────────
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
    for attempt in range(retries):
        try:
            r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"].strip()
                # Strip <think>...</think> reasoning tokens
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                return text
            else:
                print(f"[DEEPINFRA] Attempt {attempt+1} failed: {r.status_code} {r.text[:100]}")
        except Exception as e:
            print(f"[DEEPINFRA] Attempt {attempt+1} error: {e}")
        time.sleep(2 ** attempt)
    return None


# ── Summary cleaning & validation ─────────────────────────────────────────────
BAD_START_KEYWORDS = [
    "this content", "the following", "this document", "this filing",
    "this report", "this article", "this press release", "this announcement",
    "this form", "this exhibit", "note:", "summary:", "overview:",
    "the company has filed", "pursuant to", "in accordance with"
]

def clean_summary(text):
    """
    Step 1: Remove known prefixes and artifacts.
    - Strip 'Summary:' or 'Summary -' from the start
    - Remove extra whitespace
    - Remove word count mentions like '(50 words)'
    """
    if not text:
        return text

    # Remove 'Summary:' or 'Summary -' from start (regex)
    text = re.sub(r"^summary[\s\-:]+", "", text, flags=re.IGNORECASE).strip()

    # Remove word count mentions like "(48 words)" or "Word count: 50"
    text = re.sub(r"\(\d+\s*words?\)", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"word\s*count[\s:]+\d+", "", text, flags=re.IGNORECASE).strip()

    # Remove extra spaces
    text = re.sub(r"\s{2,}", " ", text).strip()

    # Capitalise first letter
    if text and not text[0].isupper():
        text = text[0].upper() + text[1:]

    return text


def count_words(text):
    return len(text.split()) if text else 0


def starts_with_bad_keyword(text):
    """Check if summary starts with a disqualifying phrase."""
    if not text:
        return False
    text_lower = text.lower().strip()
    return any(text_lower.startswith(kw) for kw in BAD_START_KEYWORDS)


def last_sentence_incomplete(text):
    """
    Check if the last sentence appears to be cut mid-way.
    Heuristic: ends without . ! ? and the last word doesn't look like
    a complete thought (e.g. ends with a conjunction or preposition).
    """
    if not text:
        return False
    text = text.strip()
    if text.endswith((".", "!", "?")):
        return False
    # If it ends mid-sentence (no terminal punctuation), flag it
    incomplete_endings = [
        " and", " or", " but", " with", " for", " of", " to",
        " in", " on", " at", " by", " from", " as", " the", " a",
        " an", " its", " their", " this", " that", " which",
        " including", " such", " while", " after", " before",
    ]
    text_lower = text.lower()
    return any(text_lower.endswith(e) for e in incomplete_endings) or not text[-1] in ".!?"


def validate_summary_quality(summary):
    """
    Returns (is_valid, reason) tuple.
    Checks word count, bad starts, incomplete sentences.
    """
    if not summary:
        return False, "empty"

    summary = clean_summary(summary)
    word_count = count_words(summary)

    if word_count > 75:
        return False, "too_long"

    if word_count < 5:
        return False, "too_short"

    if starts_with_bad_keyword(summary):
        return False, "bad_start"

    if last_sentence_incomplete(summary):
        return False, "incomplete"

    return True, "ok"


def trim_to_word_limit(text, limit=75):
    """
    If text exceeds word limit, trim to last complete sentence within limit.
    """
    words = text.split()
    if len(words) <= limit:
        return text

    # Take first `limit` words, then find last complete sentence
    truncated = " ".join(words[:limit])
    # Find last sentence-ending punctuation
    last_punct = max(
        truncated.rfind("."),
        truncated.rfind("!"),
        truncated.rfind("?")
    )
    if last_punct > 20:  # ensure we have meaningful content
        return truncated[:last_punct + 1].strip()
    return truncated.strip()


# ── Primary summarise (S.1) ───────────────────────────────────────────────────
def summarise_s1(company_name, raw_text, filing_type=""):
    """
    S.1 — Primary summarisation prompt.
    News uses S.1.N style (news-focused), Announcements use S.1.A (filing-focused).
    """
    is_news = filing_type in ("NEWS",)

    if is_news:
        prompt = f"""You are a financial news analyst writing concise alerts for retail investors.

Summarise the following news article in 40-50 words.
Write in plain English. Focus on what happened and why it matters to investors.
Do not start with "This article", "The article", "Summary:", or similar phrases.
Do not use first person. Do not mention word count. End with a complete sentence.
Company/Topic: {company_name}

Article:
{raw_text[:6000]}

Return only the summary text. No preamble. No labels."""
    else:
        prompt = f"""You are a financial analyst writing concise alerts for retail investors.

Summarise the following SEC filing in 40-50 words.
Write in plain English. Focus on what happened and why it matters to investors.
Do not start with "This filing", "This document", "Summary:", or similar phrases.
Do not use first person. Do not mention word count. End with a complete sentence.
Company: {company_name}

Filing text:
{raw_text[:8000]}

Return only the summary text. No preamble. No labels."""

    return call_deepinfra(prompt, max_tokens=200)


# ── Re-summarise (S.3) ────────────────────────────────────────────────────────
def summarise_s3(company_name, raw_text, reason=""):
    """
    S.3 — Fallback re-summarisation with stricter constraints.
    Used when S.1 output fails quality checks.
    Target word count: min(75, char_limit / (avg_word_length + 1))
    char_limit = 1024 - chars used by template, avg_word_length = 5
    """
    target_words = min(50, 1024 // 6)  # ~50 words target

    prompt = f"""You are a financial analyst. Write a {target_words}-word summary of the following content.

Rules:
- Exactly {target_words} words or fewer
- Must end with a complete sentence ending in . ! or ?
- Do not start with "This", "The following", "Summary:", "Note:" or similar
- Plain English only
- Focus on the most important fact for an investor
- No first person

Company: {company_name}

Content:
{raw_text[:5000]}

Return only the summary. Nothing else."""

    return call_deepinfra(prompt, max_tokens=150)


# ── Master summarise function with validation loop ────────────────────────────
def summarise(company_name, raw_text, filing_type=""):
    """
    Full summarisation pipeline with quality validation.
    Flow: S.1 → clean → validate → if fail → S.3 → clean → validate → accept or discard
    """
    # Attempt 1: Primary summarisation (S.1)
    raw_summary = summarise_s1(company_name, raw_text, filing_type)
    if not raw_summary:
        print(f"[SUMMARY] S.1 returned nothing, trying S.3...")
        raw_summary = summarise_s3(company_name, raw_text, "s1_empty")

    if not raw_summary:
        return None

    # Clean the output
    summary = clean_summary(raw_summary)

    # Trim if over word limit
    if count_words(summary) > 75:
        summary = trim_to_word_limit(summary, 75)
        print(f"[SUMMARY] Trimmed to word limit")

    # Validate quality
    is_valid, reason = validate_summary_quality(summary)

    if is_valid:
        print(f"[SUMMARY] S.1 passed validation ({count_words(summary)} words)")
        return summary

    print(f"[SUMMARY] S.1 failed validation ({reason}), retrying with S.3...")

    # Attempt 2: Re-summarise (S.3)
    raw_summary_s3 = summarise_s3(company_name, raw_text, reason)
    if not raw_summary_s3:
        # Last resort: use S.1 output even if imperfect
        print(f"[SUMMARY] S.3 returned nothing, using S.1 output as-is")
        return summary if count_words(summary) >= 5 else None

    summary_s3 = clean_summary(raw_summary_s3)

    if count_words(summary_s3) > 75:
        summary_s3 = trim_to_word_limit(summary_s3, 75)

    is_valid_s3, reason_s3 = validate_summary_quality(summary_s3)

    if is_valid_s3:
        print(f"[SUMMARY] S.3 passed validation ({count_words(summary_s3)} words)")
        return summary_s3

    # Both failed — use whichever is less bad
    print(f"[SUMMARY] Both S.1 and S.3 failed validation, using best available")
    s1_words = count_words(summary)
    s3_words = count_words(summary_s3)

    # Prefer S.3 if it has more content, otherwise S.1
    best = summary_s3 if s3_words > s1_words else summary
    return best if count_words(best) >= 5 else None


# ── Other pipeline helpers ────────────────────────────────────────────────────
def parse_json_response(text):
    if not text:
        return {}
    try:
        clean = text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except:
        return {}


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


# ── RAW MODE processor (kept as fallback) ─────────────────────────────────────
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
    store_alert(ticker=ticker, summary=summary, impact=impact, source=source,
                filing_type=filing_type, extra=extra)
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} alert stored (raw mode)")


# ── AI MODE processor ─────────────────────────────────────────────────────────
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

    # Step 3: Summarisation (with built-in quality validation loop)
    summary = summarise(company_name, raw_text, filing_type)
    if not summary:
        print(f"[DISCARDED] Summarisation failed -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[SUMMARY] {summary[:100]}... ({count_words(summary)} words)")

    # Step 4: Summary validation (existing V.1 prompt check)
    validation_response = call_deepinfra(validation_prompt(summary))
    validation_result = parse_json_response(validation_response)
    if validation_result.get("issues_detected") == "True":
        corrected = validation_result.get("corrected_summary", "").strip()
        if corrected:
            corrected = clean_summary(corrected)
            if count_words(corrected) > 75:
                corrected = trim_to_word_limit(corrected, 75)
            summary = corrected
            print(f"[CORRECTED] Summary fixed by V.1 ({count_words(summary)} words)")
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

    store_summary(filing_id=filing_id, ticker=ticker, summary=summary,
                  impact=impact, event_type=filing_type)
    store_alert(ticker=ticker, summary=summary, impact=impact, source=source,
                filing_type=filing_type, extra=extra)
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} alert stored")


# ── Main pipeline runner ──────────────────────────────────────────────────────
def run_pipeline():
    mode = "RAW" if RAW_MODE else "AI (DeepInfra - Gemini 2.5 Flash)"
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
