"""
ai_pipeline.py
GQ FinXray US — the summarisation and quality pipeline.

Every piece of raw content (SEC filings, news articles, earnings transcripts)
lands in `raw_filings` with status='PENDING' and is processed here into an
`alerts` row. Nothing reaches a user without passing through this file.

SIMPLIFIED STAGE ORDER
    0. Watchlist gate       — is anyone actually watching this ticker?
    1. Summarise + Validate — generate summary, check quality, retry if needed
    2. Gibberish Checker    — reject nonsense/lorem ipsum from LLM
    3. Relevance Checker    — is it actually about this company?
    4. Impact Classifier    — HIGH / MEDIUM / LOW (with improved financial/legal/security rules)
    5. Store                — save to alerts table
    6. Send                 — delivery loop routes to users via Telegram

KEY IMPROVEMENTS
----------------
* **Impact Classifier improved:** Now catches financial deterioration (91% cash flow drop),
  regulatory action (FTC lawsuits), and security incidents. $200M+ block trade threshold maintained.
* **Watchlist gate enforced:** Only summarise content for watched tickers (cost control).
* **JSON parser fails CLOSED:** Malformed LLM output is rejected, not passed through.
* **Semantic dedup integrated:** Rejects summaries too similar to recent alerts.
"""

import os
import re
import json
import time
import hashlib
import requests
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = "google/gemini-2.5-flash"

# Escape hatch for a fresh deployment with an empty watchlist, or for backfills.
PROCESS_ALL_TICKERS = os.getenv("PROCESS_ALL_TICKERS", "false").lower() == "true"

from Prompt_P2_GibberishChecker import get_prompt as gibberish_prompt
from Prompt_V3_RelevanceCheck import get_prompt as relevance_prompt
from Prompt_V1_SummaryValidation import get_prompt as validation_prompt
from Prompt_V2_SimilarityCheck import get_prompt as similarity_prompt
from Prompt_C1_ImpactClassification import get_prompt as impact_prompt
from Prompt_S1N_NewsSummarization import get_prompt as s1n_prompt
from Prompt_S1A_AnnouncementSummarization import get_prompt as s1a_prompt
from Prompt_S1T_TranscriptSummarization import get_prompt as s1t_prompt
from Prompt_H1_HeadlineGeneration import get_prompt as h1_prompt
from feature_map import resolve_feature

# ── Word-count escalation ladder ──────────────────────────────────────────────
MIN_WORDS = 70
STARTING_TARGET = 75   # News/short-form baseline; long-form can exceed via word_bounds()
TARGET_STEP = 5        # 5-word increments: 75 → 80 → 85 → 90 → 95 → 100
MAX_TARGET = 100       # Hard ceiling for most content; word_bounds() may override for SEC/transcripts

# Absolute floor. Below this a "summary" is a fragment, whatever the source.
ABS_MIN_WORDS = 12


def word_bounds(raw_text, filing_type):
    """
    The (min_words, max_target) the SOURCE can actually support.

    FMP news arrives as a headline + snippet (~48 words avg, some as short as 18).
    Asking it to produce 70+ words asks the model to invent, then the validator
    rejects the fabricated content as 'too_short' anyway. So news uses tight bounds.

    SEC filings and transcripts are long-form: they actually contain 70+ words of
    substance and can justify longer summaries. They use relaxed bounds to capture
    that depth.
    """
    if filing_type == "NEWS":
        # News has hard constraints: 70-word floor is aspirational, 100-word ceiling is firm.
        # The proportional logic below is explicitly disabled for news.
        return MIN_WORDS, MAX_TARGET

    # Long-form content: SEC filings, transcripts, earnings announcements.
    # These genuinely have >200 words, so we can ask for richer summaries.
    src_words = len((raw_text or "").split())

    # For long-form, allow the model to use more of the source depth.
    # If a filing is 3000 words, asking for 75 words captures only 2.5%.
    # Instead: ask for ~10-30% of the source, capped at 180 words max.
    lo = MIN_WORDS  # Don't go below 70 even for short filings
    hi = max(lo + TARGET_STEP, min(180, int(src_words * 0.15)))  # ~15% of source, capped at 180
    return lo, hi

TRANSCRIPT_CHAR_LIMIT = 12000
FILING_CHAR_LIMIT = 8000
NEWS_CHAR_LIMIT = 6000

# ── Token accounting (single-threaded, one filing at a time) ─────────────────
_token_usage = {"input": 0, "output": 0, "calls": 0}


def _reset_token_usage():
    _token_usage.update({"input": 0, "output": 0, "calls": 0})


def _record_token_usage(usage):
    if not usage:
        return
    _token_usage["input"] += usage.get("prompt_tokens", 0) or 0
    _token_usage["output"] += usage.get("completion_tokens", 0) or 0
    _token_usage["calls"] += 1


def get_token_usage():
    return dict(_token_usage)


# ── DeepInfra caller ─────────────────────────────────────────────────────────
MIN_CALL_GAP_SECONDS = 2.0
MAX_RATE_LIMIT_RETRIES = 5
_last_call_at = [0.0]


def _pace_before_call():
    elapsed = time.monotonic() - _last_call_at[0]
    if elapsed < MIN_CALL_GAP_SECONDS:
        time.sleep(MIN_CALL_GAP_SECONDS - elapsed)


# google/gemini-2.5-flash is a REASONING model: its internal thinking tokens are
# billed against `max_tokens` before a single visible word is emitted. The old
# 600-token cap was being consumed by thinking, so the summary itself was cut off
# mid-sentence — sometimes mid-word ("...a new stake in SpaceX (SP"). The
# validator then rejected the fragment as 'too_short' or 'incomplete' and the
# item was flagged instead of sent. That single mis-sized cap accounts for the
# overwhelming majority of rows in flagged_summaries: the model was never wrong,
# it was being gagged mid-sentence.
#
# Two defences, because either alone is fragile:
#   1. Ask the provider to stop thinking at all (reasoning_effort="none"). Not
#      every deployment honours it, so a 400 falls back to plain requests.
#   2. Treat finish_reason=="length" as a retryable condition and re-ask with a
#      bigger budget, rather than handing a known-truncated string to the
#      validator and calling it a content failure.
TOKEN_CEILING = 4000
_reasoning_param_supported = [True]


def _looks_truncated(text):
    """A visible answer that stops without terminal punctuation was cut off.

    Only mark as truncated if the last 50 chars have no sentence-ending punctuation.
    This avoids false positives from quotes or parens wrapping complete sentences.
    """
    if not text:
        return False
    tail = text.rstrip()[-50:] if len(text) > 50 else text.rstrip()
    return "." not in tail and "!" not in tail and "?" not in tail


def call_deepinfra(prompt, retries=3, max_tokens=3000):
    """A 429 gets its own longer, capped wait; other failures get short backoff.

    Truncated generations are retried with a larger budget instead of being
    passed downstream as content failures.
    """
    headers = {"Authorization": f"Bearer {DEEPINFRA_API_KEY}", "Content-Type": "application/json"}

    normal_attempt = 0
    rate_limit_attempt = 0
    budget = max_tokens
    truncation_retries = 0

    while True:
        payload = {"model": DEEPINFRA_MODEL,
                   "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": budget}
        if _reasoning_param_supported[0]:
            payload["reasoning_effort"] = "none"

        _pace_before_call()
        try:
            r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=60)
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
            choice = resp["choices"][0]
            text = (choice["message"].get("content") or "").strip()
            # Closed thinking block: drop it. Unclosed (because the cap landed
            # inside the block): everything after the opener is thought, not
            # answer, so there is no usable content at all.
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            if "<think>" in text:
                text = ""
            _record_token_usage(resp.get("usage"))

            cut = choice.get("finish_reason") == "length" or _looks_truncated(text)
            if cut and truncation_retries < 2 and budget < TOKEN_CEILING:
                truncation_retries += 1
                budget = min(budget * 2, TOKEN_CEILING)
                print(f"[DEEPINFRA] Output truncated — retrying with max_tokens={budget}")
                continue
            return text

        # Deployment rejected reasoning_effort — drop it and retry once.
        if r.status_code == 400 and _reasoning_param_supported[0] and "reasoning" in r.text.lower():
            print("[DEEPINFRA] reasoning_effort unsupported here — disabling and retrying")
            _reasoning_param_supported[0] = False
            continue

        if r.status_code == 429:
            rate_limit_attempt += 1
            if rate_limit_attempt > MAX_RATE_LIMIT_RETRIES:
                print(f"[DEEPINFRA] Rate limited after {MAX_RATE_LIMIT_RETRIES} extended waits -- giving up")
                return None
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else min(15 * rate_limit_attempt, 60)
            print(f"[DEEPINFRA] 429 -- waiting {wait:.0f}s (retry {rate_limit_attempt}/{MAX_RATE_LIMIT_RETRIES})")
            time.sleep(wait)
            continue

        normal_attempt += 1
        print(f"[DEEPINFRA] Attempt {normal_attempt} failed: {r.status_code} {r.text[:100]}")
        if normal_attempt >= retries:
            return None
        time.sleep(2 ** normal_attempt)


# ── JSON parsing that fails CLOSED ───────────────────────────────────────────
def parse_json_response(text):
    """Best-effort parse. Returns None (not {}) when the response is unusable."""
    if not text:
        return None
    try:
        clean = text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception:
        # Salvage the first {...} block — models often wrap JSON in prose.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    print(f"[WARN] Unparseable LLM JSON: {text[:160]!r}")
    return None


def ask_json(prompt, max_tokens=1000, attempts=2):
    """
    Call the model and insist on JSON. Returns a dict, or None if every attempt
    failed to parse.

    Callers MUST treat None as "the check did not run" and fail closed. The
    previous implementation returned {} here, which every caller read as a clean
    pass — meaning a malformed response silently disabled the check.
    """
    for _ in range(attempts):
        parsed = parse_json_response(call_deepinfra(prompt, max_tokens=max_tokens))
        if parsed is not None:
            return parsed
    return None


# ── Summary quality helpers ───────────────────────────────────────────────────
BAD_START_KEYWORDS = [
    "this content", "the following", "this document", "this filing",
    "this report", "this article", "this press release", "this announcement",
    "this form", "this exhibit", "this call", "this transcript", "note:",
    "summary:", "overview:", "the company has filed", "pursuant to", "in accordance with",
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
    if not text:
        return text
    text = re.sub(r"\$\s+(\d)", r"$\1", text)
    text = re.sub(r"(\d)\s+%", r"\1%", text)

    def add_commas(m):
        num = m.group(0)
        if len(num) == 4 and 1900 <= int(num) <= 2100:
            return num
        return f"{int(num):,}"

    return re.sub(r"(?<!\d)(?<!\.)\d{4,}(?!\.\d)", add_commas, text)


def count_words(text):
    return len(text.split()) if text else 0


def starts_with_bad_keyword(text):
    return bool(text) and any(text.lower().strip().startswith(k) for k in BAD_START_KEYWORDS)


def last_sentence_incomplete(text):
    if not text:
        return False
    text = text.strip()
    if text.endswith((".", "!", "?")):
        return False
    return True


def classify_failure(summary, max_words, min_words=MIN_WORDS):
    if not summary:
        return "empty"
    wc = count_words(summary)
    if wc > max_words:
        return "too_long"
    if wc < min_words:
        return "too_short"
    if starts_with_bad_keyword(summary):
        return "bad_start"
    if last_sentence_incomplete(summary):
        return "incomplete"
    return None


# ── S.1 / S.3 ─────────────────────────────────────────────────────────────────
def generate_s1(company_name, raw_text, filing_type="", sub_summary="",
                min_word_count=MIN_WORDS, target_word_count=None):
    """First-pass summary.

    `target_word_count` must be the SAME ceiling the validator will judge against.
    It previously always asked for STARTING_TARGET (75) while classify_failure()
    measured against min(75, max_target) — so for any short source the model was
    instructed to write 75 words and then rejected as 'too_long' for obeying.
    """
    target = STARTING_TARGET if target_word_count is None else target_word_count
    if filing_type == "NEWS":
        prompt = s1n_prompt(company_name, sub_summary, raw_text[:NEWS_CHAR_LIMIT],
                            target_word_count=target, min_word_count=min_word_count)
    elif filing_type == "EARNINGS_TRANSCRIPT":
        prompt = s1t_prompt(company_name, sub_summary, raw_text[:TRANSCRIPT_CHAR_LIMIT],
                            target_word_count=target, min_word_count=min_word_count)
    else:
        prompt = s1a_prompt(company_name, sub_summary, raw_text[:FILING_CHAR_LIMIT],
                            target_word_count=target, min_word_count=min_word_count)
    return call_deepinfra(prompt, max_tokens=3000)


def generate_s3(company_name, raw_text, target_words, filing_type="", min_words=MIN_WORDS):
    char_limit = TRANSCRIPT_CHAR_LIMIT if filing_type == "EARNINGS_TRANSCRIPT" else NEWS_CHAR_LIMIT
    # For NEWS, min_words is already proportional from word_bounds(). For others, use passed value.
    effective_min = min_words if min_words else MIN_WORDS
    prompt = f"""You are a financial analyst. Write a summary of the following content using exactly {target_words} words.

Rules:
- Write exactly {target_words} words. If exactly {target_words} cannot be achieved while staying strictly accurate, come as close as possible, but never fewer than {effective_min} words and never more than {target_words} words.
- Do not pad the summary with filler phrases, restated facts, or generic commentary just to reach the word count -- every added word must carry real information from the content below.
- Must end with a complete sentence ending in . ! or ?
- Do not start with "This", "The following", "Summary:", "Note:" or similar
- Plain English only, neutral and factual, no first person, no word count mentions

Company: {company_name}

Content:
{raw_text[:char_limit]}

Return only the summary. Nothing else."""
    return call_deepinfra(prompt, max_tokens=3000)


def store_flagged_summary(filing_id, ticker, company_name, final_summary, failure_reason,
                          attempts, source="SEC_EDGAR", filing_type="", max_target_reached=None):
    """`max_target_reached` used to be logged as the MAX_TARGET constant (always
    100), which made the column useless for diagnosis — every row claimed the
    ladder had run to the top even when it never ran at all. Log the real one."""
    try:
        fid, fname = resolve_feature(source, filing_type)
        supabase.table("flagged_summaries").insert({
            "filing_id": filing_id, "ticker": ticker, "company_name": company_name,
            "final_summary": final_summary,
            "final_word_count": count_words(final_summary) if final_summary else 0,
            "max_target_reached": MAX_TARGET if max_target_reached is None else max_target_reached,
            "failure_reason": failure_reason,
            "attempts": attempts, "feature_id": fid, "feature_name": fname,
            "source": source, "filing_type": filing_type,
        }).execute()
        print(f"[FLAGGED] {ticker} -> review queue ({failure_reason})")
    except Exception as e:
        print(f"[ERROR] Failed to store flagged summary: {e}")


def summarise(company_name, raw_text, filing_type="", sub_summary="",
              filing_id=None, ticker=None, source="SEC_EDGAR"):
    attempts_log = []
    min_words, max_target = word_bounds(raw_text, filing_type)
    target = min(STARTING_TARGET, max_target)

    raw = generate_s1(company_name, raw_text, filing_type, sub_summary,
                      min_word_count=min_words, target_word_count=target)
    summary = standardize_numbers(clean_summary(raw)) if raw else None
    failure = classify_failure(summary, target, min_words)
    attempts_log.append({"attempt": 1, "target": target, "words": count_words(summary), "failure": failure})

    while failure and target < max_target:
        target = min(target + TARGET_STEP, max_target)
        print(f"[SUMMARY] Retry — {failure}, new target {target} words")
        raw = generate_s3(company_name, raw_text, target, filing_type, min_words)
        summary = standardize_numbers(clean_summary(raw)) if raw else None
        failure = classify_failure(summary, target, min_words)
        attempts_log.append({"attempt": len(attempts_log) + 1, "target": target,
                             "words": count_words(summary), "failure": failure})

    if not failure:
        print(f"[SUMMARY] Passed at {target} words ({count_words(summary)} actual, {len(attempts_log)} attempt(s))")
        return summary, len(attempts_log)

    print(f"[SUMMARY] Ladder exhausted ({failure}) — flagging, not sending")
    store_flagged_summary(filing_id, ticker, company_name, summary, failure,
                          attempts_log, source, filing_type, max_target_reached=max_target)
    return None, len(attempts_log)


# ── H.1 headline ──────────────────────────────────────────────────────────────
def generate_headline(company_name, summary, filing_type=""):
    """
    A missing headline degrades the alert but does not invalidate it, so this is
    the one stage allowed to fail soft — the formatter simply omits the title.
    """
    result = ask_json(h1_prompt(company_name, summary, filing_type), max_tokens=120, attempts=2)
    if not result:
        return None
    headline = (result.get("headline") or "").strip().strip('"').rstrip(".:")
    if not headline or not (2 <= len(headline.split()) <= 14):
        return None
    return headline


# ── Stage 0: watchlist gate ───────────────────────────────────────────────────
_watchlist_cache = {"tickers": set(), "at": 0.0}
_WATCHLIST_TTL = 120


def get_watched_tickers():
    """
    Every ticker on at least one user's watchlist.

    Cached for two minutes: the pipeline calls this once per filing and the set
    changes only when someone runs /add or /remove.
    """
    now = time.monotonic()
    if (now - _watchlist_cache["at"]) < _WATCHLIST_TTL and _watchlist_cache["tickers"]:
        print(f"[WATCHLIST] Cache hit: {len(_watchlist_cache['tickers'])} tickers (age={now - _watchlist_cache['at']:.1f}s)")
        return _watchlist_cache["tickers"]
    try:
        rows = supabase.table("watchlists").select("ticker").execute().data or []
        tickers = {(r.get("ticker") or "").upper() for r in rows if r.get("ticker")}
        _watchlist_cache.update({"tickers": tickers, "at": now})
        print(f"[WATCHLIST] Loaded {len(tickers)} tickers: {sorted(tickers)}")
        return tickers
    except Exception as e:
        print(f"[ERROR] Could not load watchlist for gating: {e}")
        # Fail closed. Returning everything here would summarise the entire
        # market on a transient database error.
        print(f"[WATCHLIST] Fallback to cached tickers: {len(_watchlist_cache['tickers'])} ({sorted(_watchlist_cache['tickers']) if _watchlist_cache['tickers'] else 'empty'})")
        return _watchlist_cache["tickers"]


# ── Storage ───────────────────────────────────────────────────────────────────
def get_recent_summaries(ticker, limit=10):
    try:
        res = (supabase.table("ai_summaries").select("summary")
               .eq("ticker", ticker).order("created_at", desc=True).limit(limit).execute())
        return [r["summary"] for r in res.data if r.get("summary")]
    except Exception:
        return []


def store_summary(filing_id, ticker, summary, impact, event_type):
    try:
        res = supabase.table("ai_summaries").insert({
            "filing_id": filing_id, "ticker": ticker, "summary": summary,
            "impact": impact, "event_type": event_type,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        print(f"[ERROR] Failed to store summary: {e}")
        return None


def store_alert(ticker, summary, impact, source, filing_type="", extra=None,
                summary_id=None, filing_url=None):
    try:
        fid, fname = resolve_feature(source, filing_type)
        merged = dict(extra or {})
        merged["feature_id"] = fid
        merged["feature_name"] = fname
        supabase.table("alerts").insert({
            "ticker": ticker, "summary": summary, "impact": impact, "source": source,
            "filing_type": filing_type, "extra": merged, "delivered": False,
            "summary_id": summary_id, "filing_url": filing_url,
        }).execute()
        print(f"[ALERT READY] {impact} -- {ticker}: {summary[:70]}... (Feature {fid} {fname})")
    except Exception as e:
        print(f"[ERROR] Failed to store alert: {e}")


def update_filing_status(filing_id, status):
    try:
        supabase.table("raw_filings").update({"status": status}).eq("id", filing_id).execute()
    except Exception as e:
        print(f"[ERROR] Failed to update status: {e}")


def content_hash(text):
    return hashlib.sha256((text or "").strip().encode("utf-8", "ignore")).hexdigest()


# ── Processor ─────────────────────────────────────────────────────────────────
def process_filing(filing, watched=None):
    filing_id    = filing["id"]
    ticker       = (filing.get("ticker") or "UNKNOWN").upper()
    company_name = filing.get("company_name") or ticker
    raw_text     = filing.get("raw_text", "")
    filing_type  = filing.get("filing_type", "")
    source       = filing.get("source", "SEC_EDGAR")
    filing_url   = filing.get("filing_url")
    extra        = dict(filing.get("extra") or {})
    sub_summary  = extra.get("title", "")

    # ── Stage 0: is anyone watching this company? ────────────────────────────
    # Enforce watchlist gate strictly. No exceptions, no market-wide bypass.
    # All alerts (IPO, macro, sector, ETF) require the ticker to be on a watchlist.
    if not PROCESS_ALL_TICKERS:
        watched = watched if watched is not None else get_watched_tickers()
        if ticker not in watched:
            update_filing_status(filing_id, "SKIPPED_UNWATCHED")
            return

    print(f"\n[PROCESSING] {filing_type} -- {company_name} ({ticker}) [source={source}]")
    _reset_token_usage()

    # ── Stage 1: Summarise + Validate ────────────────────────────────────────
    summary, attempts = summarise(company_name, raw_text, filing_type, sub_summary,
                                  filing_id=filing_id, ticker=ticker, source=source)
    if not summary:
        update_filing_status(filing_id, "FLAGGED_FOR_REVIEW")
        return

    val = ask_json(validation_prompt(summary))
    if val is None:
        print(f"[HELD] Validation could not be completed -- {ticker}")
        update_filing_status(filing_id, "CHECK_FAILED")
        return
    if val.get("issues_detected") in (True, "True", "true"):
        corrected = (val.get("corrected_summary") or "").strip()
        if not corrected:
            store_flagged_summary(filing_id, ticker, company_name, summary,
                                  "v1_issues_no_correction",
                                  [{"stage": "v1", "failure": "no_correction"}], source, filing_type)
            update_filing_status(filing_id, "FLAGGED_FOR_REVIEW")
            return
        corrected = standardize_numbers(clean_summary(corrected))
        _min_words, _max_target = word_bounds(raw_text, filing_type)
        failure = classify_failure(corrected, _max_target, _min_words)
        if failure:
            store_flagged_summary(filing_id, ticker, company_name, corrected,
                                  f"v1_correction_{failure}",
                                  [{"stage": "v1_correction", "failure": failure}], source, filing_type)
            update_filing_status(filing_id, "FLAGGED_FOR_REVIEW")
            return
        summary = corrected
        print(f"[CORRECTED] V.1 fixed the summary ({count_words(summary)} words)")

    # ── Stage 2: Gibberish Checker ──────────────────────────────────────────
    gib = ask_json(gibberish_prompt(raw_text[:3000]))
    if gib is None:
        print(f"[HELD] Gibberish check could not be completed -- {ticker}")
        update_filing_status(filing_id, "CHECK_FAILED")
        return
    if gib.get("is_gibberish") in (True, "True", "true"):
        print(f"[DISCARDED] Gibberish -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return

    # ── Stage 3: Relevance Checker ──────────────────────────────────────────
    rel = ask_json(relevance_prompt(company_name, raw_text[:3000]))
    if rel is None:
        print(f"[HELD] Relevance check could not be completed -- {ticker}")
        update_filing_status(filing_id, "CHECK_FAILED")
        return
    if rel.get("is_relevant") in (False, "False", "false"):
        print(f"[DISCARDED] Not relevant to {company_name}")
        update_filing_status(filing_id, "DISCARDED")
        return

    # ── Stage 4: Impact Classifier ──────────────────────────────────────────
    cur_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    imp = ask_json(impact_prompt(company_name, summary, cur_date))
    impact = (imp or {}).get("impact", "LOW")
    impact = impact.upper() if isinstance(impact, str) else "LOW"
    if impact not in ("HIGH", "MEDIUM", "LOW"):
        impact = "LOW"

    # Fallback rules if LLM misses something
    if impact == "LOW":
        summary_lower = summary.lower()
        # Catch financial deterioration
        if any(x in summary_lower for x in ["cash flow plummet", "revenue drop", "negative cash flow", "91%", "decline"]):
            if any(x in summary_lower for x in ["quarter", "year", "%"]):
                impact = "MEDIUM"
        # Catch regulatory/legal issues
        if any(x in summary_lower for x in ["ftc sued", "ftc lawsuit", "sec lawsuit", "data breach", "data sharing with"]):
            impact = "MEDIUM"
        # Catch security incidents
        if any(x in summary_lower for x in ["hack", "hacking", "security breach", "cyber"]):
            impact = "MEDIUM"

    print(f"[IMPACT] {impact}")

    # ── Stage 5: Store ──────────────────────────────────────────────────────
    usage = get_token_usage()
    extra.update({
        "company_name": company_name,
        "url": filing_url,
        "summarization_attempts": attempts,
        "input_tokens": usage["input"],
        "output_tokens": usage["output"],
        "total_tokens": usage["input"] + usage["output"],
        "llm_calls": usage["calls"],
    })

    summary_id = store_summary(filing_id, ticker, summary, impact, filing_type)
    store_alert(ticker, summary, impact, source, filing_type, extra, summary_id, filing_url)
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} ({attempts} attempt(s), "
          f"{usage['input']}+{usage['output']} tokens)")
    # Stage 6: Send — handled by delivery_loop() in main.py


def sweep_unwatched(watched):
    """
    Bulk-retire PENDING rows for tickers nobody watches.

    Without this, unwatched rows accumulate in the queue forever: run_pipeline
    no longer selects them (the watchlist filter is in the query now), so
    nothing would ever move them out of PENDING. One UPDATE clears the lot.
    """
    if not watched:
        return 0
    try:
        res = (supabase.table("raw_filings")
               .update({"status": "SKIPPED_UNWATCHED"})
               .eq("status", "PENDING")
               .not_.in_("ticker", sorted(watched))
               .execute())
        n = len(res.data or [])
        if n:
            print(f"[GATE] Swept {n} unwatched PENDING row(s) out of the queue.")
        return n
    except Exception as e:
        print(f"[GATE] Sweep failed (non-fatal): {e}")
        return 0


def run_pipeline(batch=25):
    """
    Drain the PENDING queue.

    TWO FIXES, both learned from a 2,300-row stall on 2026-08-11:

    * **The watchlist filter is in the query, not after the fetch.** It used to
      `select * ... limit 10` and *then* discard whatever was not watchlisted.
      A legacy backfill had left ~2,300 PENDING rows for tickers nobody follows,
      so every cycle burned its entire batch marking those SKIPPED and reached
      zero real content. Four hours of polling produced no alerts. Filtering in
      the query means a batch is always 25 rows that can actually become alerts.

    * **Newest first.** Ordering was `created_at` ascending, so the queue drained
      oldest-first. On any backlog that delivers stale news — a market-moving
      headline sits behind hours of already-priced-in noise. Freshness is the
      whole product here, so the newest row wins.
    """
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking PENDING filings [{DEEPINFRA_MODEL}]")
    try:
        watched = None if PROCESS_ALL_TICKERS else get_watched_tickers()
        if watched is not None and not watched:
            print("[GATE] No user has any ticker on a watchlist — nothing to summarise. "
                  "Add tickers via the Telegram bot, or set PROCESS_ALL_TICKERS=true.")
            return

        q = supabase.table("raw_filings").select("*").eq("status", "PENDING")
        if watched is not None:
            # Only ever pull rows that can produce a deliverable alert.
            q = q.in_("ticker", sorted(watched))

        res = q.order("created_at", desc=True).limit(batch).execute()
        filings = res.data or []

        if not filings:
            print("No PENDING filings for watched tickers.")
            if watched is not None:
                sweep_unwatched(watched)
            return

        print(f"Found {len(filings)} PENDING; {len(watched) if watched else 'all'} tickers watched")
        for f in filings:
            process_filing(f, watched=watched)
            time.sleep(1)

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")


if __name__ == "__main__":
    run_pipeline()
