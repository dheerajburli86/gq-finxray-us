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
from Prompt_S1N_NewsSummarization import get_prompt as s1n_prompt
from Prompt_S1A_AnnouncementSummarization import get_prompt as s1a_prompt
from Prompt_S1T_TranscriptSummarization import get_prompt as s1t_prompt
from feature_map import resolve_feature


# ── Word-count escalation ladder ──────────────────────────────────────────────
# Attempt 1 uses the real S.1.N / S.1.A / S.1.T prompt, asking for EXACTLY 75
# words (floor 70) — not "under 75", which let short summaries through too
# easily. Every retry after that goes through S.3 (resummarize) asking for
# exactly {target} words, with the ceiling raised by 5 each time (80, 85, 90,
# 95, 100), so the model gets more room to *finish its thought* instead of
# truncating awkwardly — but the floor stays fixed at MIN_WORDS the whole
# time, so a summary can never pass by being short. All three prompt files
# explicitly forbid padding with filler just to hit the count, so a summary
# that's still short after 6 honest attempts means the source content
# genuinely can't support 70+ real words — at that point it stops (no
# infinite loop burning tokens) and gets flagged for manual review instead
# of silently discarded or sent out as a low-quality alert.
MIN_WORDS = 70
STARTING_TARGET = 75
TARGET_STEP = 5
MAX_TARGET = 100

# Summary class selection: News uses S.1.N, Announcements/filings use S.1.A,
# Earnings call transcripts (Feature 11) use S.1.T. Transcripts run several
# times longer than a filing excerpt, so they get a bigger raw-text slice —
# still capped, just a higher cap, so the model sees more of the call.
TRANSCRIPT_CHAR_LIMIT = 12000
FILING_CHAR_LIMIT = 8000
NEWS_CHAR_LIMIT = 6000


# ── Token usage tracking (per filing currently being processed) ──────────────
# process_filing() runs one filing at a time on a single thread (run_pipeline's
# for-loop), so a simple module-level accumulator is safe -- reset it at the
# start of each filing, read it back once at the end to get the TOTAL tokens
# spent across every DeepInfra call that filing needed (gibberish check,
# relevance check, every S.1/S.3 summarization attempt, V.1 validation,
# impact classification, similarity checks) -- not just the final successful
# summarization call, since that's the true per-alert cost.
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
                resp = r.json()
                text = (resp["choices"][0]["message"]["content"] or "").strip()
                # Strip <think>...</think> reasoning tokens
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                _record_token_usage(resp.get("usage"))
                return text
            else:
                print(f"[DEEPINFRA] Attempt {attempt+1} failed: {r.status_code} {r.text[:100]}")
        except Exception as e:
            print(f"[DEEPINFRA] Attempt {attempt+1} error: {e}")
        time.sleep(2 ** attempt)
    return None


# ── Summary quality helpers ───────────────────────────────────────────────────
BAD_START_KEYWORDS = [
    "this content", "the following", "this document", "this filing",
    "this report", "this article", "this press release", "this announcement",
    "this form", "this exhibit", "this call", "this transcript", "note:", "summary:", "overview:",
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
    """Normalize number formatting so alerts read consistently."""
    if not text:
        return text
    # No space between $ and the number ($ 150 -> $150)
    text = re.sub(r"\$\s+(\d)", r"$\1", text)
    # No space before a percent sign (24 % -> 24%)
    text = re.sub(r"(\d)\s+%", r"\1%", text)
    # Add thousands separators to bare 4+ digit numbers, but leave things that
    # look like years (1900-2100) or are already part of a decimal untouched.
    def add_commas(m):
        num = m.group(0)
        if len(num) == 4 and 1900 <= int(num) <= 2100:
            return num
        return f"{int(num):,}"
    text = re.sub(r"(?<!\d)(?<!\.)\d{4,}(?!\.\d)", add_commas, text)
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

def classify_failure(summary, max_words):
    """Returns the failure reason for this attempt, or None if it passes."""
    if not summary:
        return "empty"
    wc = count_words(summary)
    if wc > max_words:
        return "too_long"
    if wc < MIN_WORDS:
        return "too_short"
    if starts_with_bad_keyword(summary):
        return "bad_start"
    if last_sentence_incomplete(summary):
        return "incomplete"
    return None


# ── S.1 — Primary summarisation (real S.1.N / S.1.A / S.1.T prompts) ─────────
def generate_s1(company_name, raw_text, filing_type="", sub_summary=""):
    if filing_type == "NEWS":
        prompt = s1n_prompt(company_name, sub_summary, raw_text[:NEWS_CHAR_LIMIT])
    elif filing_type == "EARNINGS_TRANSCRIPT":
        prompt = s1t_prompt(company_name, sub_summary, raw_text[:TRANSCRIPT_CHAR_LIMIT],
                             target_word_count=STARTING_TARGET, min_word_count=MIN_WORDS)
    else:
        prompt = s1a_prompt(company_name, sub_summary, raw_text[:FILING_CHAR_LIMIT],
                             target_word_count=STARTING_TARGET, min_word_count=MIN_WORDS)
    return call_deepinfra(prompt, max_tokens=600)


# ── S.3 — Resummarize at an escalated word target ─────────────────────────────
def generate_s3(company_name, raw_text, target_words, filing_type=""):
    char_limit = TRANSCRIPT_CHAR_LIMIT if filing_type == "EARNINGS_TRANSCRIPT" else NEWS_CHAR_LIMIT
    prompt = f"""You are a financial analyst. Write a summary of the following content using exactly {target_words} words.

Rules:
- Write exactly {target_words} words. If exactly {target_words} cannot be achieved while staying strictly accurate, come as close as possible, but never fewer than {MIN_WORDS} words and never more than {target_words} words.
- Do not pad the summary with filler phrases, restated facts, or generic commentary just to reach the word count -- every added word must carry real information from the content below.
- Must end with a complete sentence ending in . ! or ?
- Do not start with "This", "The following", "Summary:", "Note:" or similar
- Plain English only, neutral and factual, no first person, no word count mentions

Company: {company_name}

Content:
{raw_text[:char_limit]}

Return only the summary. Nothing else."""
    return call_deepinfra(prompt, max_tokens=700)


# ── Flagged-for-review sink (replaces "best available" fallback) ─────────────
def store_flagged_summary(filing_id, ticker, company_name, final_summary, failure_reason, attempts,
                           source="SEC_EDGAR", filing_type=""):
    try:
        fid, fname = resolve_feature(source, filing_type)
        supabase.table("flagged_summaries").insert({
            "filing_id": filing_id,
            "ticker": ticker,
            "company_name": company_name,
            "final_summary": final_summary,
            "final_word_count": count_words(final_summary) if final_summary else 0,
            "max_target_reached": MAX_TARGET,
            "failure_reason": failure_reason,
            "attempts": attempts,
            "feature_id": fid,
            "feature_name": fname,
            "source": source,
            "filing_type": filing_type
        }).execute()
        print(f"[FLAGGED] {ticker} sent to review queue after exhausting retries ({failure_reason}) — Feature {fid}/11 {fname}")
    except Exception as e:
        print(f"[ERROR] Failed to store flagged summary: {e}")


# ── Master summarise — retry-until-valid, escalating word budget ─────────────
def summarise(company_name, raw_text, filing_type="", sub_summary="", filing_id=None, ticker=None, source="SEC_EDGAR"):
    """
    Attempt 1: S.1.N / S.1.A / S.1.T prompt, target 75 words.
    Each failure retries via S.3 with the ceiling raised by 5 words
    (80, 85, 90, 95, 100), floor fixed at MIN_WORDS the whole time.
    If still failing at MAX_TARGET, stop and flag for manual review —
    never discard silently, never send a summary that failed validation.
    """
    attempts_log = []
    target = STARTING_TARGET

    raw = generate_s1(company_name, raw_text, filing_type, sub_summary)
    summary = standardize_numbers(clean_summary(raw)) if raw else None
    failure = classify_failure(summary, target)
    attempts_log.append({"attempt": 1, "target": target, "words": count_words(summary), "failure": failure})

    while failure and target < MAX_TARGET:
        target += TARGET_STEP
        print(f"[SUMMARY] Retry — previous failure: {failure}, new target: {target} words")
        raw = generate_s3(company_name, raw_text, target, filing_type)
        summary = standardize_numbers(clean_summary(raw)) if raw else None
        failure = classify_failure(summary, target)
        attempts_log.append({"attempt": len(attempts_log) + 1, "target": target, "words": count_words(summary), "failure": failure})

    if not failure:
        print(f"[SUMMARY] Passed at target={target} ({count_words(summary)} words, {len(attempts_log)} attempt(s))")
        return summary, len(attempts_log)

    print(f"[SUMMARY] Exhausted ladder at {target} words, still failing ({failure}) — flagging for review, not sending")
    store_flagged_summary(
        filing_id=filing_id,
        ticker=ticker,
        company_name=company_name,
        final_summary=summary,
        failure_reason=failure,
        attempts=attempts_log,
        source=source,
        filing_type=filing_type
    )
    return None, len(attempts_log)


# ── Other pipeline helpers ────────────────────────────────────────────────────
def parse_json_response(text):
    if not text:
        return {}
    try:
        clean = text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        # NOTE: every caller treats {} the same as "check didn't fire" (a
        # missing key reads as None, which fails every downstream equality
        # check). That means malformed JSON from the LLM silently fails
        # OPEN -- gibberish passes, irrelevant content passes, V.1 validation
        # is skipped, dedup doesn't dedupe -- with no signal that the check
        # never actually ran. Log loudly so this shows up in monitoring
        # instead of looking like a clean pass.
        print(f"[WARN] parse_json_response: malformed LLM JSON, check fails OPEN -- {e} | raw={text[:200]!r}")
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
    except Exception:
        return []

def store_summary(filing_id, ticker, summary, impact, event_type):
    try:
        result = supabase.table("ai_summaries").insert({
            "filing_id": filing_id,
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "event_type": event_type
        }).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        print(f"[ERROR] Failed to store summary: {e}")
        return None

def store_alert(ticker, summary, impact, source, filing_type="", extra=None, summary_id=None):
    try:
        fid, fname = resolve_feature(source, filing_type)
        merged_extra = dict(extra or {})
        merged_extra["feature_id"] = fid
        merged_extra["feature_name"] = fname
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": source,
            "filing_type": filing_type,
            "extra": merged_extra,
            "delivered": False,
            "summary_id": summary_id
        }).execute()
        print(f"[ALERT READY] {impact} -- {ticker}: {summary[:80]}... (Feature {fid}/11 {fname})")
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

    # Reset the per-filing token accumulator so get_token_usage() at the end
    # of this function reflects only what THIS filing's checks/summarization
    # cost, not a running total across every filing the pipeline has ever
    # processed since the process started.
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

    # Step 3: Summarisation — retry-until-valid, escalating word budget.
    # Returns None only if it exhausted the ladder up to MAX_TARGET; in that
    # case it has already been written to flagged_summaries for review.
    summary, summarization_attempts = summarise(company_name, raw_text, filing_type, sub_summary,
                                                 filing_id=filing_id, ticker=ticker, source=source)
    if not summary:
        print(f"[FLAGGED, NOT SENT] {ticker} -- see flagged_summaries for review")
        update_filing_status(filing_id, "FLAGGED_FOR_REVIEW")
        return
    print(f"[SUMMARY] {summary[:100]}... ({count_words(summary)} words)")

    # Step 4: Summary validation (V.1) — a corrected summary must still pass
    # the same word-count/quality checks; if it doesn't, flag it too rather
    # than blindly trusting the correction.
    validation_result = parse_json_response(call_deepinfra(validation_prompt(summary)))
    if validation_result.get("issues_detected") == "True":
        corrected = validation_result.get("corrected_summary", "").strip()
        if not corrected:
            # V.1 flagged real problems with a summary that had already passed
            # the full S.1/S.3 quality gate, but didn't give us a usable fix.
            # Previously this just discarded the summary with a console print
            # and no database record -- a summary that made it through the
            # entire retry ladder would vanish with zero trace. Flag it for
            # review instead, same as every other "couldn't produce something
            # trustworthy" path in this pipeline.
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

    # Record what it cost to produce this alert -- how many summarization
    # attempts the retry ladder needed, and total input/output tokens across
    # EVERY DeepInfra call this filing triggered (gibberish/relevance/S.1-S.3
    # attempts/validation/impact/similarity), not just the final summary
    # call. main.py's delivery loop reads these back out of `extra` when it
    # writes the alert_run_log row at the moment the alert actually goes out.
    usage = get_token_usage()
    extra = dict(extra or {})
    extra["summarization_attempts"] = summarization_attempts
    extra["input_tokens"] = usage["input"]
    extra["output_tokens"] = usage["output"]
    extra["total_tokens"] = usage["input"] + usage["output"]
    extra["llm_calls"] = usage["calls"]

    summary_id = store_summary(filing_id=filing_id, ticker=ticker, summary=summary,
                                impact=impact, event_type=filing_type)
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
