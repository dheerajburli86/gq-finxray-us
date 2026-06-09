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

def call_llm(prompt, retries=3, max_tokens=1500):
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

def is_complete_summary(text):
    """
    Check if summary is complete and meaningful:
    - Must end with . ! or ?
    - Must have at least 40 words
    - Must not end mid-number or mid-phrase
    """
    if not text or len(text.strip()) < 10:
        return False

    stripped = text.rstrip()

    # Must end with sentence-ending punctuation
    if stripped[-1] not in ".!?":
        return False

    # Must have at least 40 words
    words = stripped.split()
    if len(words) < 40:
        return False

    # Check the full text for incomplete endings — preposition at end before period
    # Remove the final punctuation and check last word
    text_no_punct = stripped.rstrip(".!?").rstrip()
    last_words = text_no_punct.split()
    if not last_words:
        return False

    last_word = last_words[-1].lower().rstrip(",")
    second_last = last_words[-2].lower().rstrip(",") if len(last_words) >= 2 else ""

    # Incomplete if ends with preposition or article
    dangling_words = {"on", "the", "a", "an", "and", "or", "but", "that",
                      "with", "by", "in", "of", "to", "for", "as", "at",
                      "from", "into", "than", "about", "over", "after"}
    if last_word in dangling_words:
        return False

    # Incomplete if last word is a number preceded by a preposition
    # e.g. "at a price of $227" or "sold 436,2"
    clean_last = last_word.replace(",", "").replace("$", "").replace(".", "")
    if clean_last.isnumeric() and second_last in {"of", "at", "for", "worth", "than", "about", "to", "from"}:
        return False

    # Incomplete if last word looks like a partial number (e.g. "436,2" without trailing digits)
    if re.search(r'\d+,\d{1,2}$', last_word):
        return False

    return True

def summarise(company_name, raw_text):
    prompt = f"""You are a senior financial analyst writing a stock market intelligence alert for investors.

Company: {company_name}

TASK: Read the source text and write a high-quality summary of what happened and why it matters to investors.

FORMAT: Write exactly 3 complete sentences. Total word count: 50-70 words.

ABSOLUTE RULES — ALL MUST BE FOLLOWED WITHOUT EXCEPTION:
1. Every sentence must be 100% grammatically complete — never cut off mid-word, mid-number, or mid-phrase.
2. The final character of your entire response MUST be a full stop.
3. Never truncate numbers. Write $115,489,662 in full — never as "$115" or "$115 million" if the source says "$115,489,662".
4. Only use numbers that appear in the source text. Never invent figures.
5. Write in plain English — a retail investor with no financial background must understand it.
6. Do not start with the company name as the first word.
7. Do not use phrases like "this filing", "this article", "the company announced that".
8. Make the summary genuinely useful — answer: what happened, what are the specific details, and what does it mean for the stock or investors?

WHAT TO EXTRACT BY FILING TYPE:
- Earnings: revenue figure, net income or EPS, year-over-year change (better or worse), forward guidance if present.
- Insider trade: insider's full name and exact title, action (bought/sold), exact share count, price per share, total dollar value, date.
- Leadership change: who left and their exact role, who replaced them (or "replacement not named"), effective date.
- Acquisition/merger: both company names, deal value, strategic rationale, expected close date.
- Government grant or contract: exact dollar amount, awarding body, purpose, what it enables the company to do.
- Analyst call: firm name, previous rating, new rating, new price target, core reason for the change.
- Clinical trial: drug/therapy name, trial phase and name, key result (success/failure/milestone), patient numbers, what it means for commercialisation.
- Exploration/mining update: project name, location, specific milestone achieved (e.g. drill pad construction begun), timeline, investor significance.
- Regulatory/legal: what specifically changed, who it affects, financial exposure or benefit.
- General operational update: the most specific facts available — names, locations, percentages, deadlines.

QUALITY STANDARD — your summary must answer all three of these questions:
1. What specifically happened? (not vague — include names and numbers)
2. What are the key details? (financials, timeline, people involved)
3. Why does it matter to someone holding or considering this stock?

A summary that says "the company reported results and reaffirmed guidance" is POOR.
A summary that says "net sales fell 4% to $2.4 billion, adjusted EPS dropped 32% to $0.50, yet the company reaffirmed full-year 2026 guidance suggesting confidence in recovery" is GOOD.

EXAMPLES OF GOOD SUMMARIES:

Earnings example:
"Campbell's reported Q3 fiscal 2026 net sales of $2.4 billion, down 4% year-over-year, with adjusted EPS declining 32% to $0.50 due to cost pressures. Reported EPS improved to $0.41 and EBIT rose to $239 million. Despite near-term margin headwinds, the company reaffirmed its full-year fiscal 2026 guidance, signaling management confidence in annual targets."

Insider trade example:
"Jorie L. Novacek, Senior Vice President and Controller, purchased 207 shares of Incentive Compensation Deferral Plan Share Credits on June 5, 2026. Each share was acquired at $227.22, bringing the total investment to $46,956. This transaction increases Ms. Novacek's direct ownership, a signal of continued insider confidence in the company's outlook."

Government grant example:
"The U.S. Department of Energy reinstated a $115,489,662 grant to fund construction of a lithium hydroxide processing facility, with the DOE contributing $57,744,831 and the company providing matching funds. The facility is central to domestic EV battery supply chain development. Reinstatement removes a key financial uncertainty and strengthens the company's path to full commercial production."

Source text:
{raw_text[:8000]}

Return ONLY the summary. No preamble. No labels. No explanation. Just 3 complete sentences ending with a full stop."""
    return call_llm(prompt, max_tokens=1500)

def expand_summary(summary, company_name, raw_text):
    """Called when summary is incomplete — completes cut-off sentences and adds depth."""
    prompt = f"""You are a financial editor. The summary below about {company_name} has a problem — it either ends mid-sentence, ends mid-number, or is too short and vague.

YOUR JOB: Fix it so it becomes a complete, informative 3-sentence summary of 50-70 words.

STRICT RULES:
1. Keep every word and number from the original exactly as written — do not change any existing content.
2. Complete any sentence that was cut off using details from the source text below.
3. If the summary is under 40 words after fixing, add 1-2 more complete sentences with specific details from the source.
4. The final character of your response MUST be a full stop.
5. Never truncate a number — write it completely.
6. The summary must answer: what happened, key details, and why it matters to investors.
7. Do not say "this filing" or "this article".
8. Return only the completed summary — no preamble, no explanation.

Current summary (may be incomplete):
{summary}

Source text:
{raw_text[:4000]}

Return ONLY the fixed summary."""
    result = call_llm(prompt, max_tokens=1500)
    return result

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

    # ── Step 2: Relevance check skipped ──────────────────────
    print(f"[SKIP] Relevance check")

    # ── Step 3: Summarisation ─────────────────────────────────
    summary = summarise(company_name, raw_text)
    if not summary or len(summary.strip()) < 10:
        print(f"[DISCARDED] Summarisation failed — {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    word_count = len(summary.split())
    print(f"[SUMMARY] {word_count} words — {summary[:100]}...")

    # ── Step 4: Completeness and quality check ────────────────
    if not is_complete_summary(summary):
        print(f"[INCOMPLETE] {word_count} words — expanding...")
        expanded = expand_summary(summary, company_name, raw_text)
        if expanded and len(expanded.strip()) >= len(summary.strip()):
            summary = expanded
            print(f"[EXPANDED] {len(summary.split())} words — {summary[:100]}...")
        else:
            print(f"[WARNING] Expansion failed — keeping original")
    else:
        print(f"[COMPLETE] {word_count} words ✅")

    if not summary or len(summary.strip()) < 10:
        print(f"[DISCARDED] Summary unusable — {ticker}")
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
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
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