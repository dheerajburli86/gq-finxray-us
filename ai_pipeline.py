import os
import json
import re
import time
import requests
import logging
import logging.handlers

# Rotating logger for AI pipeline — tracks token usage
logger = logging.getLogger("ai_pipeline")
if not logger.handlers:
    import os as _os
    _os.makedirs("logs", exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        "logs/ai_pipeline.log", maxBytes=5*1024*1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = "google/gemini-2.5-flash"

# -- AI Mode disabled for now (DeepInfra key pulled) — using keyword-based RAW mode.
# Flip back to False once DEEPINFRA_API_KEY is restored in Railway.
RAW_MODE = False

from Prompt_P2_GibberishChecker import get_prompt as gibberish_prompt
from Prompt_V3_RelevanceCheck import get_prompt as relevance_prompt
from Prompt_V1_SummaryValidation import get_prompt as validation_prompt
from Prompt_V2_SimilarityCheck import get_prompt as similarity_prompt
from Prompt_C1_ImpactClassification import get_prompt as impact_prompt


# ── DeepInfra caller ─────────────────────────────────────────────────────────
def call_deepinfra(prompt, retries=2, max_tokens=1000):
    """Call DeepInfra API with Gemini 3.5 Flash."""
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
                data = r.json()
                text = (data["choices"][0]["message"]["content"] or "").strip()
                # Strip <think>...</think> reasoning tokens
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                # Log token usage for cost monitoring
                usage = data.get("usage", {})
                logger.info(f"[DEEPINFRA] model={DEEPINFRA_MODEL} prompt_tokens={usage.get('prompt_tokens',0)} completion_tokens={usage.get('completion_tokens',0)} total={usage.get('total_tokens',0)}")
                return text
            else:
                logger.warning(f"[DEEPINFRA] Attempt {attempt+1} failed: {r.status_code} {r.text[:100]}")
        except Exception as e:
            print(f"[DEEPINFRA] Attempt {attempt+1} error: {e}")
        time.sleep(2 ** attempt)
    return None


# ── Summary quality helpers ───────────────────────────────────────────────────
BAD_START_KEYWORDS = [
    "this content", "the following", "this document", "this filing",
    "this report", "this article", "this press release", "this announcement",
    "this form", "this exhibit", "note:", "summary:", "overview:",
    "the company has filed", "pursuant to", "in accordance with"
]

def clean_summary(text):
    if not text:
        return text
    text = re.sub(r"^summary[\s\-:]+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\(\d+\s*words?\)", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"word\s*count[\s:]+\d+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s{2,}", " ", text).strip()
    if text and not text[0].isupper():
        text = text[0].upper() + text[1:]
    return text

def standardize_numbers(text):
    """
    Standardize large numbers in summary text to readable format.
    e.g. 1000000000 -> $1B, 50000000 -> $50M, 1500000 -> $1.5M
    Also standardizes written numbers: "1 billion" -> "$1B"
    """
    if not text:
        return text
    import re

    # Written forms: "1.5 billion" -> "$1.5B", "500 million" -> "$500M"
    text = re.sub(
        r'\$?\s*(\d+(?:\.\d+)?)\s*billion',
        lambda m: f"${float(m.group(1)):.1f}B".replace('.0B', 'B'),
        text, flags=re.IGNORECASE
    )
    text = re.sub(
        r'\$?\s*(\d+(?:\.\d+)?)\s*million',
        lambda m: f"${float(m.group(1)):.0f}M",
        text, flags=re.IGNORECASE
    )
    text = re.sub(
        r'\$?\s*(\d+(?:\.\d+)?)\s*trillion',
        lambda m: f"${float(m.group(1)):.1f}T".replace('.0T', 'T'),
        text, flags=re.IGNORECASE
    )

    # Raw large numbers: 1000000000 -> $1B etc
    def replace_large_number(m):
        n = float(m.group(0).replace(',', ''))
        if n >= 1_000_000_000:
            return f"${n/1_000_000_000:.1f}B".replace('.0B', 'B')
        elif n >= 1_000_000:
            return f"${n/1_000_000:.0f}M"
        elif n >= 1_000:
            return f"${n/1_000:.0f}K"
        return m.group(0)

    text = re.sub(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', replace_large_number, text)

    return text


def count_words(text):
    return len(text.split()) if text else 0

def starts_with_bad_keyword(text):
    if not text:
        return False
    return any(text.lower().strip().startswith(kw) for kw in BAD_START_KEYWORDS)

def last_sentence_incomplete(text):
    if not text:
        return False
    text = text.strip()
    if text.endswith((".", "!", "?")):
        return False
    incomplete_endings = [
        " and", " or", " but", " with", " for", " of", " to",
        " in", " on", " at", " by", " from", " as", " the", " a",
        " an", " its", " their", " this", " that", " which",
        " including", " such", " while", " after", " before",
    ]
    return any(text.lower().endswith(e) for e in incomplete_endings) or text[-1] not in ".!?"

def validate_summary_quality(summary):
    if not summary:
        return False, "empty"
    summary = clean_summary(summary)
    wc = count_words(summary)
    if wc > 75:
        return False, "too_long"
    if wc < 5:
        return False, "too_short"
    if starts_with_bad_keyword(summary):
        return False, "bad_start"
    if last_sentence_incomplete(summary):
        return False, "incomplete"
    return True, "ok"

def trim_to_word_limit(text, limit=75):
    words = text.split()
    if len(words) <= limit:
        return text
    truncated = " ".join(words[:limit])
    last_punct = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if last_punct > 20:
        return truncated[:last_punct + 1].strip()
    return truncated.strip()


# ── S.1 — Primary summarisation ───────────────────────────────────────────────
def summarise_s1(company_name, raw_text, filing_type=""):
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
    return call_deepinfra(prompt, max_tokens=500)


# ── S.3 — Fallback re-summarisation ──────────────────────────────────────────
def summarise_s3(company_name, raw_text, reason=""):
    target_words = min(50, 1024 // 6)
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
    return call_deepinfra(prompt, max_tokens=500)


# ── Master summarise with quality validation loop ─────────────────────────────
def summarise(company_name, raw_text, filing_type=""):
    """
    S.1 → clean → validate → if fail → S.3 → clean → validate → best available
    """
    raw = summarise_s1(company_name, raw_text, filing_type)
    if not raw:
        print(f"[SUMMARY] S.1 returned nothing, trying S.3...")
        raw = summarise_s3(company_name, raw_text, "s1_empty")
    if not raw:
        return None

    summary = clean_summary(raw)
    summary = standardize_numbers(summary)
    if count_words(summary) > 75:
        summary = trim_to_word_limit(summary, 75)
        print(f"[SUMMARY] Trimmed to word limit")

    is_valid, reason = validate_summary_quality(summary)
    if is_valid:
        print(f"[SUMMARY] S.1 passed ({count_words(summary)} words)")
        return summary

    print(f"[SUMMARY] S.1 failed ({reason}), trying S.3...")
    raw_s3 = summarise_s3(company_name, raw_text, reason)
    if not raw_s3:
        print(f"[SUMMARY] S.3 empty, using S.1 as fallback")
        return summary if count_words(summary) >= 5 else None

    summary_s3 = clean_summary(raw_s3)
    summary_s3 = standardize_numbers(summary_s3)
    if count_words(summary_s3) > 75:
        summary_s3 = trim_to_word_limit(summary_s3, 75)

    is_valid_s3, reason_s3 = validate_summary_quality(summary_s3)
    if is_valid_s3:
        print(f"[SUMMARY] S.3 passed ({count_words(summary_s3)} words)")
        return summary_s3

    # Both failed — use best available
    print(f"[SUMMARY] Both failed, using best available")
    best = summary_s3 if count_words(summary_s3) > count_words(summary) else summary
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

def get_recent_summaries(ticker, limit=3):
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


# ── RAW MODE processor ────────────────────────────────────────────────────────
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

    # Step 3: Summarisation with quality pipeline
    summary = summarise(company_name, raw_text, filing_type)
    if not summary:
        print(f"[DISCARDED] Summarisation failed -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print(f"[SUMMARY] {summary[:100]}... ({count_words(summary)} words)")

    # Step 4: Summary validation (V.1)
    validation_result = parse_json_response(call_deepinfra(validation_prompt(summary)))
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

    store_summary(filing_id=filing_id, ticker=ticker, summary=summary,
                  impact=impact, event_type=filing_type)
    store_alert(ticker=ticker, summary=summary, impact=impact, source=source,
                filing_type=filing_type, extra=extra)
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} alert stored")


# ── Main pipeline runner ──────────────────────────────────────────────────────
def run_pipeline():
    mode = "RAW" if RAW_MODE else f"AI (DeepInfra - {DEEPINFRA_MODEL})"
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
