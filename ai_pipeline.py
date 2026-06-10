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
    if not text or len(text.strip()) < 10:
        return False
    stripped = text.rstrip()
    if stripped[-1] not in ".!?":
        return False
    words = stripped.split()
    if len(words) < 40:
        return False
    text_no_punct = stripped.rstrip(".!?").rstrip()
    last_words = text_no_punct.split()
    if not last_words:
        return False
    last_word = last_words[-1].lower().rstrip(",")
    second_last = last_words[-2].lower().rstrip(",") if len(last_words) >= 2 else ""
    dangling_words = {"on", "the", "a", "an", "and", "or", "but", "that",
                      "with", "by", "in", "of", "to", "for", "as", "at",
                      "from", "into", "than", "about", "over", "after"}
    if last_word in dangling_words:
        return False
    clean_last = last_word.replace(",", "").replace("$", "").replace(".", "")
    if clean_last.isnumeric() and second_last in {"of", "at", "for", "worth", "than", "about", "to", "from"}:
        return False
    if re.search(r'\d+,\d{1,2}$', last_word):
        return False
    return True

def is_news_relevant(company_name, raw_text):
    """
    Relevance check for news articles only.
    Returns True if article has a specific actionable financial development.
    Returns False if it is general commentary, opinion, or fluff.
    """
    prompt = f"""You are a financial news filter. Read the following news article and determine if it contains a specific, actionable financial development related to {company_name} or the US stock market that is worth alerting investors about.

Answer YES if the article contains any of:
- Specific price movements, earnings results, revenue figures
- Company announcements, deals, leadership changes
- Analyst upgrades, downgrades, or price target changes
- Federal Reserve decisions or major economic data releases
- Market-moving geopolitical or macroeconomic events affecting US markets
- IPO filings, mergers, acquisitions involving US companies
- Any specific named event with financial implications for US investors

Answer NO if the article is:
- General market commentary or opinion with no specific development
- A recap of events already widely known
- Personal finance advice or lifestyle content
- Purely educational or explanatory content with no current news
- Vague analysis with no specific figures or events
- News primarily about non-US markets, companies, or economies with no direct US market impact

Article:
{raw_text[:2000]}

Respond with only a JSON object: {{"relevant": true}} or {{"relevant": false}}"""

    response = call_llm(prompt, max_tokens=50)
    result = parse_json_response(response)
    return result.get("relevant", True)

def summarise_announcement(company_name, raw_text):
    """For SEC EDGAR 8-K and S-1 filings."""
    prompt = f"""Your task is to summarize the provided document specifically focusing on the company: {company_name}.

PURPOSE: The summary must help investors understand significant developments related to {company_name}.

CONTENT RULES:
1. Include ONLY details directly relevant to {company_name} and its investors.
2. Exclude any information unrelated to {company_name}.
3. Ensure the summary contains only factual information explicitly mentioned in the original document.
4. Do not add interpretations, opinions, or recommendations.
5. After writing, verify accuracy against the original document.

FORMAT:
- Write as a single paragraph, no line breaks.
- Strictly 50-70 words.
- Do not mention word count.
- Do not include contact information, salutations, or address anyone.
- Neutral, objective, professional tone.
- Do not start with the company name as the first word.
- The final character MUST be a full stop.

COMPLETENESS RULES:
- Every sentence must be 100% complete. Never cut off mid-word, mid-number, or mid-phrase.
- Never truncate numbers. Write every number exactly as it appears in the source.
- The final character of your response MUST be a full stop.

WHAT TO INCLUDE BY TYPE:
- Earnings: revenue, net income, EPS, year-over-year change, guidance.
- Leadership change: who left, their role, replacement name, effective date.
- Acquisition: both companies, deal value, strategic reason, expected close.
- Government grant: exact dollar amount, awarding body, purpose, company benefit.
- Clinical trial: drug name, phase, result, patient count, commercial significance.
- Exploration update: project name, location, milestone, timeline, investor impact.
- Regulatory/legal: what changed, who it affects, financial exposure or benefit.

INVESTOR SIGNIFICANCE: The last sentence must explain why this matters to investors or what it signals for the stock.

Document to summarize:
{raw_text[:8000]}

Return ONLY the summary paragraph. Nothing else."""
    return call_llm(prompt, max_tokens=1500)

def summarise_form4(company_name, raw_text):
    """Dedicated prompt for Form 4 insider transaction filings."""
    prompt = f"""Your task is to summarize an SEC Form 4 insider transaction filing for the company: {company_name}.

Form 4 is filed when a company executive, director, or major shareholder buys or sells company stock. Your summary must clearly tell investors exactly what transaction occurred.

CONTENT RULES:
1. Include ONLY the transaction details from the filing.
2. Ensure all figures are exactly as stated in the filing — never round or approximate.
3. Do not add interpretations or opinions.
4. Verify all names, titles, share counts, and prices against the source before returning.

FORMAT:
- Write as a single paragraph, no line breaks.
- Strictly 50-70 words.
- Do not mention word count.
- Neutral, objective, professional tone.
- The final character MUST be a full stop.

REQUIRED INFORMATION — extract all that are present:
- Insider's full name
- Insider's exact title or role (CEO, CFO, Director, VP, etc.)
- Transaction type: purchased or sold
- Exact number of shares
- Price per share (if disclosed)
- Total dollar value of the transaction
- Date of the transaction
- Total shares owned after the transaction
- Type of security (Common Stock, Options, etc.)

INVESTOR SIGNIFICANCE: The last sentence must state what this transaction signals — insider buying generally signals confidence in the company, insider selling may reflect personal financial planning or profit-taking.

EXAMPLE OF A GOOD FORM 4 SUMMARY:
"John Smith, Chief Financial Officer, purchased 5,000 shares of Common Stock at $42.30 per share on June 5, 2026, for a total investment of $211,500. Following this transaction, Smith now directly owns 47,320 shares. Insider buying at this level typically signals management confidence in the company's near-term financial outlook."

Form 4 filing data:
{raw_text[:8000]}

Return ONLY the summary paragraph. Nothing else."""
    return call_llm(prompt, max_tokens=1500)

def summarise_news(company_name, raw_text):
    """For news articles — CNBC, Bloomberg, MarketWatch, Reuters."""
    prompt = f"""Your task is to summarize the provided news article specifically focusing on: {company_name}.

CONTENT SCOPE:
1. Include ONLY details from the article directly relevant to {company_name}.
2. Completely exclude any information unrelated to {company_name}.
3. If an analyst, firm, or person's name is mentioned in connection with {company_name}, include it.
4. Ensure the summary contains only factual information explicitly stated in the article.
5. Do not add interpretations, opinions, or recommendations.
6. After writing, verify accuracy against the original article.

FORMAT:
- Write as a single paragraph, no line breaks.
- Strictly 50-70 words.
- Do not mention word count.
- Do not include contact information, salutations, or address anyone.
- Neutral, objective, professional tone.
- Do not start with the subject name as the first word.
- The final character MUST be a full stop.

COMPLETENESS RULES:
- Every sentence must be 100% complete. Never cut off mid-word, mid-number, or mid-phrase.
- Never truncate numbers. Write every number completely as it appears in the source.
- The final character of your response MUST be a full stop.

WHAT TO INCLUDE:
- Specific figures: price targets, revenue numbers, percentages, share prices, deal values, index levels.
- Named people: analysts, executives, officials — include their names and organisations.
- Market context: how does this news affect stock prices or investor sentiment?
- The last sentence must explain why this matters to investors or what it signals for the market.

News article to summarize:
{raw_text[:8000]}

Return ONLY the summary paragraph. Nothing else."""
    return call_llm(prompt, max_tokens=1500)

def summarise(company_name, raw_text, filing_type=""):
    """Route to the correct summarisation function based on filing type."""
    if filing_type == "NEWS":
        return summarise_news(company_name, raw_text)
    elif filing_type == "4":
        return summarise_form4(company_name, raw_text)
    else:
        return summarise_announcement(company_name, raw_text)

def expand_summary(summary, company_name, raw_text):
    prompt = f"""You are a financial editor. The summary below about {company_name} is INCOMPLETE — it ends mid-sentence, mid-number, or without a full stop.

YOUR JOB: Fix it so it becomes a complete, informative single-paragraph summary of 50-70 words.

STRICT RULES:
1. Keep every word and number from the original exactly as written — do not change any existing content.
2. Complete any sentence that was cut off using details from the source text below.
3. If the summary is under 40 words after fixing, add more complete sentences with specific details.
4. The final character of your response MUST be a full stop.
5. Never truncate a number — write it completely.
6. The summary must answer: what happened, key details, and why it matters to investors.
7. Do not say "this filing" or "this article".
8. Return only the completed summary — no preamble, no explanation.

Current summary (incomplete):
{summary}

Source text:
{raw_text[:4000]}

Return ONLY the fixed summary. Last character MUST be a full stop."""
    result = call_llm(prompt, max_tokens=2000)
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

    # ── Step 3: Summarisation — routed by filing type ─────────
    # For market-wide news use descriptive name not raw ticker
    if filing_type == "NEWS" and company_name in ("MARKET", "SPY", "QQQ", "DIA", "UNKNOWN"):
        summarise_name = "the US stock market and major indices"
    else:
        summarise_name = company_name

    summary = summarise(summarise_name, raw_text, filing_type)
    if not summary or len(summary.strip()) < 10:
        print(f"[DISCARDED] Summarisation failed — {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    word_count = len(summary.split())
    print(f"[SUMMARY] {word_count} words — {summary[:100]}...")

    # ── Step 4: Completeness check ───────────────────────────
    if not is_complete_summary(summary):
        print(f"[INCOMPLETE] {word_count} words — expanding...")
        expanded = expand_summary(summary, summarise_name, raw_text)
        if expanded and expanded.strip().endswith((".", "!", "?")):
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