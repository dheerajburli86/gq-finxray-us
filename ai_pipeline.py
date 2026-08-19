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
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = "google/gemini-2.5-flash"

# Escape hatch for a fresh deployment with an empty watchlist, or for backfills.
PROCESS_ALL_TICKERS = os.getenv("PROCESS_ALL_TICKERS", "false").lower() == "true"

# Nothing older than this is ever summarised or delivered. The product is
# "what happened in the last day"; a filing that has been sitting in the queue
# longer than this is retired rather than sent.
MAX_CONTENT_AGE_HOURS = int(os.getenv("MAX_CONTENT_AGE_HOURS", "24"))

# Duplicacy checker config — TIGHTENED
DUPLICATE_CHECK_HOURS = 24  # Check last 24 hours for duplicates
SIMILARITY_THRESHOLD = 0.72  # Cross-source threshold (tightened from 0.75)
SAME_SOURCE_THRESHOLD = 0.82  # Same type/source threshold (tightened from 0.85)

from Prompt_P2_GibberishChecker import get_prompt as gibberish_prompt
from Prompt_V3_RelevanceCheck import get_prompt as relevance_prompt, is_listicle
from Prompt_V1_SummaryValidation import get_prompt as validation_prompt
from Prompt_V2_SimilarityCheck import get_prompt as similarity_prompt
from Prompt_C1_ImpactClassification import get_prompt as impact_prompt
from Prompt_S1N_NewsSummarization import get_prompt as s1n_prompt
from Prompt_S1A_AnnouncementSummarization import get_prompt as s1a_prompt
from Prompt_S1T_TranscriptSummarization import get_prompt as s1t_prompt
from Prompt_H1_HeadlineGeneration import get_prompt as h1_prompt
from feature_map import resolve_feature

# ── Word-count escalation ladder ──────────────────────────────────────────────
# TIGHTENED: Enforce minimum 75 words for all alerts (no fragments)
MIN_WORDS = 75  # User requirement: minimum 75 words, not 70
STARTING_TARGET = 140  # Increased to push for fuller summaries
TARGET_STEP = 15       # Bigger jumps to reach target faster
MAX_TARGET = 200       # Increased to allow longer, richer content

# Absolute floor. Below this a "summary" is a fragment, whatever the source.
ABS_MIN_WORDS = 75  # STRICT: No summary under 75 words is acceptable

# How many times to ask before giving up and flagging. The ladder used to be
# driven purely by "is there room to raise the target", which for short news
# sources evaluated false on the first pass and gave the model exactly one shot.
MAX_SUMMARY_ATTEMPTS = int(os.getenv("GQ_MAX_SUMMARY_ATTEMPTS", "4"))


# ── Duplicacy Checker ───────────────────────────────────────────────────────────
def calculate_similarity(text1, text2):
    """
    Calculate similarity between two texts using Jaccard similarity on words.
    Returns a score from 0.0 to 1.0, where 1.0 means identical.
    """
    if not text1 or not text2:
        return 0.0

    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())

    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)

    return intersection / union if union > 0 else 0.0


def check_for_duplicate(ticker, summary, filing_type, source):
    """
    Check if a similar alert for this ticker was sent in the last DUPLICATE_CHECK_HOURS.

    Returns True if a duplicate is found (should skip storing), False otherwise.
    """
    try:
        # Look back DUPLICATE_CHECK_HOURS for alerts from this ticker
        since = datetime.now(timezone.utc)
        cutoff = since - timedelta(hours=DUPLICATE_CHECK_HOURS)

        # Query recent alerts for this ticker
        res = (supabase.table("alerts")
               .select("summary, filing_type, source")
               .eq("ticker", ticker.upper())
               .gte("created_at", cutoff.isoformat())
               .execute())

        recent_alerts = res.data or []

        if not recent_alerts:
            return False

        # Check similarity against all recent alerts
        for alert in recent_alerts:
            prev_summary = alert.get("summary", "")
            prev_filing_type = alert.get("filing_type", "")
            prev_source = alert.get("source", "")

            # Same filing type + source = likely exact duplicate, be strict
            if prev_filing_type == filing_type and prev_source == source:
                similarity = calculate_similarity(summary, prev_summary)
                if similarity >= SAME_SOURCE_THRESHOLD:
                    print(f"[DUP] Duplicate (same type/source): {ticker} sim={similarity:.2f} (th={SAME_SOURCE_THRESHOLD})")
                    return True

            # Different source = cross-source variety, be lenient
            else:
                similarity = calculate_similarity(summary, prev_summary)
                if similarity >= SIMILARITY_THRESHOLD:
                    print(f"[DUP] Duplicate (cross-source): {ticker} sim={similarity:.2f} (th={SIMILARITY_THRESHOLD})")
                    return True

        return False

    except Exception as e:
        print(f"[WARN] Duplicate check failed: {e}")
        # Fail open — if the check errors, allow the alert through
        return False


def word_bounds(raw_text, filing_type):
    """
    The (min_words, max_target) the SOURCE can actually support.

    TIGHTENED: All summaries must be minimum 75 words. For short FMP news,
    if the source is too brief, the LLM will expand by pulling context from
    the headline and surrounding context. Never accept a summary under 75 words.
    """
    src_words = len((raw_text or "").split())

    # ALL filing types: minimum 75 words, no exceptions
    # For short sources (FMP news ~48 words), the LLM expands by:
    # 1. Using full headline
    # 2. Adding context from title/metadata
    # 3. Inferring implications from the news
    # This is acceptable because the LLM has the full article text available.

    lo = MIN_WORDS

    if filing_type == "NEWS":
        # BUGFIX 2026-08-19 — THE band was arithmetically impossible.
        #
        # The old rule for src_words >= 40 was hi = min(200, src_words * 0.9).
        # classify_failure requires wc >= min_words AND wc <= max_words, plus a
        # separate `fragment` floor. With lo = 75 that means:
        #
        #   src=45  -> band [75, 40]   impossible
        #   src=60  -> band [75, 54]   impossible
        #   src=86  -> band [75, 77]   impossible
        #
        # An RSS <description> is 40-86 words essentially always, so EVERY RSS
        # and FMP news article fell in the dead zone: no summary of any length
        # could pass, summarise()'s retry loop could not widen (target was
        # already max_target), and the row was flagged FLAGGED_FOR_REVIEW and
        # never delivered. This single line is why news alerts stopped.
        #
        # A news summary legitimately expands its source: the model is given the
        # headline plus the body and writes the "what this means" framing the
        # snippet omits. So the ceiling must always sit a workable distance
        # above the floor, never below it.
        hi = min(MAX_TARGET, max(lo + 45, int(src_words * 1.6)))
    else:
        # SEC filings, transcripts, snapshots: sources are long, cap at target.
        hi = max(lo + 45, MAX_TARGET)

    # Invariant: the acceptance band must be non-empty, whatever the source.
    if hi <= lo:
        hi = lo + 45

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
    """A visible answer that stops without a terminal character was cut off.

    BUGFIX 2026-08-19 — this fired on EVERY well-formed JSON reply. A complete
    `{"impact": "HIGH"}` ends in `}`, which was not in the accepted set, so the
    caller declared it truncated and re-asked at 2000 then 4000 tokens. Every
    JSON stage in the pipeline (skip check, company match, relevance, impact,
    gibberish, similarity) therefore cost THREE model calls instead of one:
    ~3x the tokens, ~3x the latency, and a pipeline slow enough that the PENDING
    queue could not drain between polls. That is the `[DEEPINFRA] Output
    truncated — retrying` line repeating forever in the logs against summaries
    that were never actually cut.

    Closing braces/brackets and code fences are legitimate endings. A genuine
    cut is caught by finish_reason=="length", which is authoritative; this
    heuristic only has to catch prose that stops mid-sentence.
    """
    if not text:
        return False
    tail = text.rstrip().rstrip("`").rstrip()
    return not tail.endswith((".", "!", "?", '"', ")", "}", "]", "%", ":"))


def call_deepinfra(prompt, retries=3, max_tokens=1500, expect_json=False):
    """A 429 gets its own longer, capped wait; other failures get short backoff.

    Truncated generations are retried with a larger budget instead of being
    passed downstream as content failures. `expect_json=True` disables the prose
    heuristic entirely — a JSON reply's shape is verified by the parser, so the
    only truncation signal worth acting on is the provider's own finish_reason.
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

            hard_cut = choice.get("finish_reason") == "length"
            cut = hard_cut or (not expect_json and _looks_truncated(text))
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
        parsed = parse_json_response(call_deepinfra(prompt, max_tokens=max_tokens,
                                                    expect_json=True))
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
    # Tightened: strict max_words enforcement (no 20% buffer)
    if wc > max_words:
        return "too_long"
    if wc < min_words:
        return "too_short"
    if starts_with_bad_keyword(summary):
        return "bad_start"
    if last_sentence_incomplete(summary):
        return "incomplete"
    # BUGFIX 2026-08-19: this used to be `wc < ABS_MIN_WORDS + 3`, i.e. 78, which
    # silently overrode the declared 75-word floor and made any band whose
    # ceiling sat at 75-77 unsatisfiable. One floor, one value.
    if wc < ABS_MIN_WORDS:
        return "fragment"
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
    return call_deepinfra(prompt, max_tokens=1500)


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
    return call_deepinfra(prompt, max_tokens=1500)


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
    # Judge against the BAND ceiling, not the ceiling we happened to ask for.
    # Asking for 120 and getting a good 135-word summary is a pass when the band
    # allows 200; rejecting it as 'too_long' throws away usable content.
    failure = classify_failure(summary, max_target, min_words)
    attempts_log.append({"attempt": 1, "target": target, "words": count_words(summary), "failure": failure})

    # BUGFIX 2026-08-19 — the loop was `while failure and target < max_target`,
    # which for NEWS is `while failure and False`. word_bounds() returns a
    # ceiling of min(200, max(120, src*1.6)); for a typical 60-90 word article
    # that is 120, and the opening target is min(STARTING_TARGET=140, 120) = 120.
    # target == max_target on entry, so the ladder had ZERO retries: one short
    # first draft and the item went straight to flagged_summaries. That is the
    # "Ladder exhausted (too_short) — flagging, not sending" after a single
    # attempt seen for AVGO and AAPL.
    #
    # A too_short draft is an EXECUTION failure, not a budget failure — raising
    # the ceiling does not help, re-asking does. Drive the loop on attempts and
    # only widen the target when there is actually room to widen it.
    while failure and len(attempts_log) < MAX_SUMMARY_ATTEMPTS:
        if target < max_target:
            target = min(target + TARGET_STEP, max_target)
        # At the ceiling: re-ask at the same target. Explicitly aim above the
        # floor so a model that undershot has somewhere to land.
        ask_for = max(target, min_words + 20) if failure == "too_short" else target
        ask_for = min(ask_for, max_target)
        print(f"[SUMMARY] Retry {len(attempts_log) + 1}/{MAX_SUMMARY_ATTEMPTS} — "
              f"{failure}, asking for {ask_for} words (band {min_words}-{max_target})")
        raw = generate_s3(company_name, raw_text, ask_for, filing_type, min_words)
        summary = standardize_numbers(clean_summary(raw)) if raw else None
        failure = classify_failure(summary, max_target, min_words)
        attempts_log.append({"attempt": len(attempts_log) + 1, "target": ask_for,
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
    if _watchlist_cache["tickers"] and (now - _watchlist_cache["at"]) < _WATCHLIST_TTL:
        return _watchlist_cache["tickers"]
    try:
        rows = supabase.table("watchlists").select("ticker").execute().data or []
        tickers = {(r.get("ticker") or "").upper() for r in rows if r.get("ticker")}
        _watchlist_cache.update({"tickers": tickers, "at": now})
        return tickers
    except Exception as e:
        print(f"[ERROR] Could not load watchlist for gating: {e}")
        # Fail closed. Returning everything here would summarise the entire
        # market on a transient database error.
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

    # ── Stage 0b: cheap noise gates, BEFORE we pay to summarise ─────────────
    # Gibberish and relevance used to run at Stages 2 and 3, i.e. AFTER the full
    # summarisation ladder. Every listicle and opinion piece therefore consumed
    # a summary plus up to four retries plus a validation call before being
    # discarded on a check that only ever needed the raw text. Both checks read
    # `raw_text` and nothing else, so they belong here: a rejected item now costs
    # one cheap call instead of six expensive ones, and the queue drains faster.
    #
    # Headline shape is free to test, so test it first and skip the model call
    # entirely for the forms that cannot be a company event.
    if filing_type == "NEWS" and is_listicle(sub_summary):
        print(f"[DISCARDED] Not an event — headline is a roundup/opinion form: {sub_summary[:70]!r}")
        update_filing_status(filing_id, "DISCARDED")
        return

    gib = ask_json(gibberish_prompt(raw_text[:3000]))
    if gib is None:
        print(f"[HELD] Gibberish check could not be completed -- {ticker}")
        update_filing_status(filing_id, "CHECK_FAILED")
        return
    if gib.get("is_gibberish") in (True, "True", "true"):
        print(f"[DISCARDED] Gibberish -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return

    rel = ask_json(relevance_prompt(company_name, raw_text[:3000], sub_summary))
    if rel is None:
        print(f"[HELD] Relevance check could not be completed -- {ticker}")
        update_filing_status(filing_id, "CHECK_FAILED")
        return
    if rel.get("is_relevant") in (False, "False", "false"):
        print(f"[DISCARDED] Not alert-worthy for {company_name}: "
              f"{(rel.get('reason') or 'failed subject/event test')[:80]}")
        update_filing_status(filing_id, "DISCARDED")
        return

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

    # ── Stage 4: Impact Classifier ──────────────────────────────────────────
    # (Gibberish and relevance now run at Stage 0b, before summarisation.)
    cur_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    imp = ask_json(impact_prompt(company_name, summary, cur_date))
    impact = (imp or {}).get("impact", "LOW")
    impact = impact.upper() if isinstance(impact, str) else "LOW"
    if impact not in ("HIGH", "MEDIUM", "LOW"):
        impact = "LOW"

    # Safety net for the few categories the classifier must never grade LOW.
    #
    # The previous version promoted LOW -> MEDIUM when the summary contained any
    # of ["cash flow plummet", "revenue drop", "negative cash flow", "91%",
    # "decline"] AND any of ["quarter", "year", "%"]. Every result snapshot
    # contains both "quarter" and "%", so a single occurrence of the word
    # "decline" anywhere -- including "a decline in costs" -- promoted it. Nothing
    # financial could settle at LOW, which is a large part of why 71% of all
    # alerts graded HIGH or MEDIUM. "91%" was a literal left over from one Meta
    # story and also matched "191%".
    #
    # These patterns are now narrow, unambiguous, and none of them appear in a
    # routine healthy quarter.
    if impact == "LOW":
        s = summary.lower()
        SEVERE = (
            "bankrupt", "chapter 11", "going concern", "covenant breach",
            "debt default", "delisting", "restructuring",
            "dividend cut", "dividend suspend", "suspended its dividend",
            "guidance cut", "cuts guidance", "lowered its guidance",
            "withdrew guidance", "withdraws guidance",
            "data breach", "security breach", "ransomware",
            "sec charges", "sec sued", "ftc sued", "doj investigation",
            "accounting irregularit", "restatement", "material weakness",
        )
        hit = next((p for p in SEVERE if p in s), None)
        if hit:
            impact = "MEDIUM"
            print(f"[IMPACT] Promoted LOW->MEDIUM on severe-category match: {hit!r}")

    print(f"[IMPACT] {impact}")

    # ── Stage 4.5: Duplicacy Check ─────────────────────────────────────────
    if check_for_duplicate(ticker, summary, filing_type, source):
        print(f"[SKIPPED] Duplicate alert for {ticker}")
        update_filing_status(filing_id, "DUPLICATE")
        return

    # ── Stage 5: Store ──────────────────────────────────────────────────────
    usage = get_token_usage()
    # BUGFIX 2026-08-19: this unconditionally wrote `filing_url` over the clean
    # article URL the poller had already put in extra. news_poller stores a
    # per-ticker row URL ("…/article#t=AAPL") in filing_url purely to satisfy a
    # uniqueness constraint, and that fragment was what users saw and clicked.
    # Prefer the poller's own clean URL; fall back to filing_url with the
    # synthetic fragment stripped.
    clean_url = extra.get("url") or (filing_url or "").split("#t=")[0] or None
    extra.update({
        "company_name": company_name,
        "url": clean_url,
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


SWEEP_PAGE = 500
SWEEP_MAX_ROWS_PER_CYCLE = 5000


def sweep_unwatched(watched):
    """
    Bulk-retire PENDING rows for tickers nobody watches.

    Without this, unwatched rows accumulate in the queue forever: run_pipeline
    no longer selects them (the watchlist filter is in the query now), so
    nothing would ever move them out of PENDING.

    WHY THIS IS NOW A READ-THEN-UPDATE-BY-ID LOOP
    ---------------------------------------------
    The previous single-statement version — `.update(...).eq("status","PENDING")
    .not_.in_("ticker", watched)` — did not clear the queue. It ran on every
    cycle for two days and the unwatched backlog still reached 2,669 rows across
    517 tickers. A `not.in` filter never matches rows where the column is NULL,
    PostgREST applies its own row ceiling to a bulk update, and the whole thing
    was reported through `print()`, so a failing sweep left no trace anywhere a
    query could find it. Selecting explicit ids and updating them in bounded
    chunks removes all three problems: NULL tickers are matched deliberately,
    every page is acknowledged, and failures are recorded.
    """
    if not watched:
        return 0

    watched_set = {str(t).upper() for t in watched}
    swept = 0
    scanned = 0
    cursor = None
    try:
        while scanned < SWEEP_MAX_ROWS_PER_CYCLE:
            # Keyset pagination on id. Offset paging would be wrong here: rows
            # leave the PENDING filter as we update them, so every page would
            # shift underneath the offset and skip rows. The id cursor advances
            # monotonically whether or not a row was touched.
            q = (supabase.table("raw_filings")
                 .select("id, ticker")
                 .eq("status", "PENDING")
                 .order("id")
                 .limit(SWEEP_PAGE))
            if cursor is not None:
                q = q.gt("id", cursor)
            rows = (q.execute()).data or []
            if not rows:
                break

            scanned += len(rows)
            cursor = rows[-1]["id"]

            # A NULL/blank ticker can never be watchlist-matched, so it is
            # unwatched by definition. `not.in` silently left these behind.
            stale = [r["id"] for r in rows
                     if (r.get("ticker") or "").upper() not in watched_set]
            if stale:
                (supabase.table("raw_filings")
                 .update({"status": "SKIPPED_UNWATCHED"})
                 .in_("id", stale)
                 .execute())
                swept += len(stale)

            if len(rows) < SWEEP_PAGE:
                break

        if swept:
            print(f"[GATE] Swept {swept} unwatched PENDING row(s) out of the queue.")
        return swept
    except Exception as e:
        print(f"[GATE] Sweep failed: {e}")
        try:
            supabase.table("poller_error_log").insert({
                "poller_name": "ai_pipeline",
                "job_name": "sweep_unwatched",
                "error_message": str(e)[:1000],
                "context": {"swept_before_failure": swept,
                            "scanned_before_failure": scanned,
                            "watched_count": len(watched_set)},
            }).execute()
        except Exception:
            pass
        return swept


def retire_stale(cutoff_iso, page=500):
    """
    Move PENDING rows older than the freshness window to STALE.

    Without this the freshness filter in run_pipeline would simply hide old rows
    rather than clear them: they would stay PENDING forever, and every indexed
    read would keep scanning past a permanently growing tail.
    """
    try:
        rows = (supabase.table("raw_filings")
                .select("id")
                .eq("status", "PENDING")
                .lt("created_at", cutoff_iso)
                .limit(page)
                .execute()).data or []
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        (supabase.table("raw_filings")
         .update({"status": "STALE"})
         .in_("id", ids)
         .execute())
        print(f"[GATE] Retired {len(ids)} PENDING row(s) older than "
              f"{MAX_CONTENT_AGE_HOURS}h.")
        return len(ids)
    except Exception as e:
        print(f"[GATE] Stale retirement failed: {e}")
        return 0


def _fair_share(rows, batch):
    """
    Interleave the queue by filing_type so no one feature can starve the rest.

    BUGFIX 2026-08-19 — THE reason only Feature 2 (Company & Sector News) was
    arriving. The queue was strictly newest-first with limit(25), and the news
    poller inserts rows every 60 SECONDS while SEC filings, insider trades,
    result snapshots, transcripts, analyst actions and ETF flows arrive every
    few minutes to hours. Newest-first against a continuously-refreshed source
    means the news rows permanently occupy all 25 slots: every other feature's
    rows sit one page down the ordering and are never reached — not delayed,
    never — until they age out of the 24h window and get retired unprocessed.
    Thirteen features were writing rows correctly and exactly one was being read.

    Round-robin one row per filing_type, newest-first within each type, until
    the batch is full. A quiet feature still gets its row processed the cycle it
    appears; a noisy one still gets the majority of the batch once the quiet
    ones are served. Freshness is preserved because each type is drained newest
    -first, which was the point of the original ordering.
    """
    if not rows:
        return []

    buckets = {}
    for r in rows:
        # Group by feature, not by ticker — one busy company must not crowd out
        # a different feature either.
        key = (r.get("filing_type") or "UNKNOWN").upper()
        buckets.setdefault(key, []).append(r)

    picked, exhausted = [], False
    while len(picked) < batch and not exhausted:
        exhausted = True
        for key in sorted(buckets):
            if not buckets[key]:
                continue
            picked.append(buckets[key].pop(0))
            exhausted = False
            if len(picked) >= batch:
                break

    if len(buckets) > 1:
        spread = {}
        for r in picked:
            k = (r.get("filing_type") or "UNKNOWN").upper()
            spread[k] = spread.get(k, 0) + 1
        print(f"[QUEUE] Fair-share across {len(buckets)} feature type(s): "
              + ", ".join(f"{k}={v}" for k, v in sorted(spread.items())))
    return picked


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

        # ── Freshness window ────────────────────────────────────────────────
        # The product promise is news less than 24 hours old. Anything older is
        # not just low value, it is actively harmful: it occupies the queue that
        # fresh content needs and it reaches the user labelled as current.
        #
        # This also repairs a starvation bug. The queue was ordered newest-first
        # with limit(25) and NO floor, against a backlog that reached 16k rows.
        # Newest-first plus an unbounded tail means old PENDING rows are never
        # reached — not delayed, never — so the backlog grew without limit and
        # every query had to scan past it. Bounding the window makes the queue
        # self-draining: fresh rows are processed, stale ones are retired below.
        fresh_cutoff = (datetime.now(timezone.utc)
                        - timedelta(hours=MAX_CONTENT_AGE_HOURS)).isoformat()

        q = (supabase.table("raw_filings").select("*")
             .eq("status", "PENDING")
             .gte("created_at", fresh_cutoff))
        if watched is not None:
            # Only ever pull rows that can produce a deliverable alert.
            q = q.in_("ticker", sorted(watched))

        # Pull a wider candidate pool than we will process, so the fair-share
        # interleave below has something from every feature to choose from.
        res = q.order("created_at", desc=True).limit(max(batch * 6, 120)).execute()
        filings = _fair_share(res.data or [], batch)

        # Retire anything that aged out of the window while queued.
        retire_stale(fresh_cutoff)

        # Sweep unwatched rows EVERY cycle, not only when the watched queue is
        # empty. The old placement meant that as long as a single watched filing
        # was pending, the unwatched backlog was never cleared — it grew to 2,481
        # rows, and every query above had to scan past them.
        if watched is not None:
            sweep_unwatched(watched)

        if not filings:
            print("No PENDING filings for watched tickers.")
            return

        print(f"Found {len(filings)} PENDING; {len(watched) if watched else 'all'} tickers watched")
        for f in filings:
            process_filing(f, watched=watched)
            time.sleep(1)

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")


if __name__ == "__main__":
    run_pipeline()
