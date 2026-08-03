"""
ai_pipeline.py
GQ FinXray US — LLM summarisation pipeline. v2.

WHY v1 PRODUCED ZERO ALERTS FOR THREE DAYS
------------------------------------------
Measured against the live database, not guessed:

1. THE WORD LADDER COULD NOT PASS. Attempt 1 demanded 70-75 words -- a
   six-word window. Gemini naturally writes 80-90. Query every summary that
   ever succeeded: NOT ONE passed on attempt 1. The ones that got through
   took 2-5 attempts and landed at 70, 72, 77, 79, 80, 82, 83, 85, 86, 90, 91
   words. So every filing paid 3-6 summarisation calls before it could
   possibly succeed, and 2,667 SEC filings failed all six and were flagged.

2. FORCING A WORD COUNT MANUFACTURED THE PADDING. The summaries that DID
   pass ended like "...is truly remarkable.", "...trade disputes!", and
   "What steps should be taken?" -- that is a model being ordered to stretch
   an 8-K with 60 words of substance up to a demanded 90.

3. TWENTY LLM CALLS PER FILING CAUSED THE 429. gibberish(1) + relevance(1) +
   summarise(1-6) + validation(1) + impact(1) + dedup(UP TO 10, one LLM call
   per recent summary, sequentially). At a 2s minimum gap that is a 40s floor
   per filing, and each 429 could add 5 x 60s of blocking backoff.

WHAT v2 DOES
------------
- Accept-first validation: 45-120 words, judged on whether the sentence
  actually finishes, not on hitting a number. No ladder.
- Prompts ask for "3-4 complete sentences, stop when the substance runs out,
  never pad" instead of an exact count.
- Gibberish + relevance merged into ONE call.
- Impact returned by the summarisation call as a second JSON field, not a
  separate round trip.
- Dedup does local text similarity first and only escalates to the LLM in the
  genuinely ambiguous band -- typically zero LLM calls.
- max_tokens raised 600 -> 2000. Gemini 2.5 Flash is a reasoning model and
  its thinking tokens come out of the same budget; 600 risked truncating the
  visible answer mid-sentence, which is exactly the "no full stop, cut off
  halfway" symptom seen before the ladder was introduced.
- Booleans normalised. v1 compared LLM output against the STRING "True", so
  when the model returned JSON `true` the V.1 validation and the dedup check
  silently never fired at all.
- 429 responses now log their body, so "rate limited" and "out of credit" are
  distinguishable.

Typical cost per filing: 2 calls. Worst case 4. Down from 5-20.
"""

import os
import json
import re
import time
import difflib
import requests
from datetime import datetime, timezone

from supabase import create_client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from feature_map import resolve_feature

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = os.getenv("DEEPINFRA_MODEL", "google/gemini-2.5-flash")

# ── Summary acceptance band ───────────────────────────────────────────────────
# Wide on purpose. The job of this gate is to reject summaries that are empty,
# truncated mid-sentence, or obvious boilerplate -- NOT to enforce a word
# count. An 8-K announcing a dividend genuinely needs 50 words; an earnings
# transcript needs 110. Demanding both hit the same narrow band is what broke
# v1, and what made the model pad.
MIN_WORDS = 45
MAX_WORDS = 120

# ── Adaptive output length ───────────────────────────────────────────────────
# THE root cause of padded, filler-stuffed alerts. Measured across 3,700 live
# filings, median INPUT length by type:
#
#     8-K            623 words
#     10-Q           337 words
#     S-1            916 words
#     transcript   8,748 words
#     Form 4          45 words   <-- shorter than the summary we demanded
#     NEWS            37 words   <-- shorter than the summary we demanded
#     INSIDER_FMP     18 words   <-- shorter than the summary we demanded
#
# ~2,000 of those 3,700 had LESS source material than the 45-120 word summary
# they were asked to produce. That is not summarisation, it is expansion: the
# model had no choice but to invent connective filler. "...is truly
# remarkable.", "What steps should be taken?" and the exclamation marks all
# come from here, not from the retry ladder (which merely amplified it by
# pushing the target to 90 then 100).
#
# So the target is now derived from what the source actually contains.
SHORT_INPUT_WORDS = 60      # below this, the source IS the summary -- no LLM call
SUMMARY_MAX_RATIO = 0.5     # never ask for more than half the input length
SUMMARY_MIN_RATIO = 0.55    # floor as a fraction of the computed ceiling


def target_band(input_words):
    """(min_words, max_words) appropriate to this source, or None to skip.

    Returning None means the input is too short to summarise meaningfully and
    should be passed through verbatim -- a 37-word news snippet is already a
    summary, and running it through an LLM can only add noise.
    """
    if input_words < SHORT_INPUT_WORDS:
        return None
    hi = min(MAX_WORDS, max(35, int(input_words * SUMMARY_MAX_RATIO)))
    lo = max(20, int(hi * SUMMARY_MIN_RATIO))
    return lo, hi

# Content slice sent to the model.
TRANSCRIPT_CHAR_LIMIT = 12000
FILING_CHAR_LIMIT = 8000
NEWS_CHAR_LIMIT = 6000

# Untickered general news ("ticker": "MARKET") was being fed to a prompt that
# literally says "summarize focusing on the company: MARKET". 621 of the
# flagged filings are exactly this. It cannot produce a usable company alert,
# so it is skipped before any LLM call is made. Set False to let them through
# to a market-wide summary instead.
SKIP_UNTICKERED_NEWS = True
UNTICKERED = {"MARKET", "UNKNOWN", "", None}

# Local dedup thresholds. Above HIGH we call it a duplicate outright with no
# LLM involvement; below LOW it is clearly distinct. Only the band between
# them is worth spending a call on.
DEDUP_LOCAL_HIGH = 0.82
DEDUP_LOCAL_LOW = 0.55


# ── Token accounting (per filing) ────────────────────────────────────────────
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
# With v2 making 2-4 calls per filing instead of up to 20, the pacing gap can
# come down substantially without going anywhere near the rate limit.
MIN_CALL_GAP_SECONDS = 0.5
MAX_RATE_LIMIT_RETRIES = 4
_last_call_at = [0.0]


def _pace_before_call():
    elapsed = time.monotonic() - _last_call_at[0]
    if elapsed < MIN_CALL_GAP_SECONDS:
        time.sleep(MIN_CALL_GAP_SECONDS - elapsed)


def call_deepinfra(prompt, retries=2, max_tokens=4000, temperature=0.2):
    """Call DeepInfra. Returns text or None.

    max_tokens defaults to 2000, not 600. Gemini 2.5 Flash reasons before
    answering and those tokens are drawn from the same budget -- a tight cap
    can consume the allowance during thinking and return a visible answer
    that stops mid-sentence. You only pay for tokens actually generated, so
    the headroom is nearly free.
    """
    if not DEEPINFRA_API_KEY:
        print("[DEEPINFRA] No API key set -- cannot summarise.")
        return None

    headers = {"Authorization": f"Bearer {DEEPINFRA_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "model": DEEPINFRA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    normal_attempt = 0
    rate_limit_attempt = 0

    while True:
        _pace_before_call()
        try:
            r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=90)
        except Exception as e:
            _last_call_at[0] = time.monotonic()
            normal_attempt += 1
            print(f"[DEEPINFRA] Attempt {normal_attempt} network error: {e}")
            if normal_attempt > retries:
                return None
            time.sleep(2 ** normal_attempt)
            continue

        _last_call_at[0] = time.monotonic()

        if r.status_code == 200:
            resp = r.json()
            choice = (resp.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content") or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            _record_token_usage(resp.get("usage"))
            if choice.get("finish_reason") == "length":
                # The model was cut off by max_tokens. Surfacing this matters:
                # it is the difference between "the model wrote a bad summary"
                # and "we didn't give it room to finish".
                print(f"[DEEPINFRA] WARNING: hit max_tokens ({max_tokens}) -- output truncated")
            return text

        if r.status_code == 429:
            rate_limit_attempt += 1
            # v1 printed r.text for every status EXCEPT 429, so the one error
            # worth diagnosing was the one we never saw. On DeepInfra a 429
            # can mean per-minute throttling OR an exhausted balance, and the
            # body is the only way to tell them apart.
            print(f"[DEEPINFRA] 429 body: {r.text[:300]}")
            if rate_limit_attempt > MAX_RATE_LIMIT_RETRIES:
                print(f"[DEEPINFRA] Rate limited after {MAX_RATE_LIMIT_RETRIES} waits -- giving up")
                return None
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() \
                else min(10 * rate_limit_attempt, 45)
            print(f"[DEEPINFRA] Waiting {wait:.0f}s (retry {rate_limit_attempt}/{MAX_RATE_LIMIT_RETRIES})")
            time.sleep(wait)
            continue

        normal_attempt += 1
        print(f"[DEEPINFRA] Attempt {normal_attempt} failed: {r.status_code} {r.text[:200]}")
        if normal_attempt > retries:
            return None
        time.sleep(2 ** normal_attempt)


# ── JSON parsing ──────────────────────────────────────────────────────────────
def parse_json_response(text):
    """Parse a JSON object out of an LLM response, tolerating fences and prose."""
    if not text:
        return {}
    clean = text.strip()
    clean = re.sub(r"^```(?:json)?", "", clean).strip()
    clean = re.sub(r"```$", "", clean).strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    # Fall back to the outermost {...} block -- models like to add a sentence
    # of commentary either side of the JSON.
    m = re.search(r"\{.*\}", clean, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    print(f"[WARN] Could not parse JSON from LLM response: {clean[:200]!r}")
    return {}


def as_bool(value, default=False):
    """Normalise the many shapes an LLM returns a boolean in.

    v1 compared against the string "True" in some places and the bool True in
    others. When the model returned JSON `true`, `issues_detected == "True"`
    was False -- so V.1 validation and semantic dedup never ran at all, in
    production, for the entire life of that code.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "yes", "y", "1")


# ── Summary hygiene ───────────────────────────────────────────────────────────
BAD_START_KEYWORDS = [
    "this content", "the following", "this document", "this filing",
    "this report", "this article", "this press release", "this announcement",
    "this form", "this exhibit", "this call", "this transcript",
    "note:", "summary:", "overview:", "here is", "here's",
]

SENTENCE_END = (".", "!", "?", '."', ".'", '.”', ".)", "!\"", "?\"", "…")


def strip_invisibles(text):
    """Remove zero-width and other invisible characters.

    RSS descriptions arrive with zero-width spaces embedded mid-sentence
    ("the reigning champions and​winner of..."). They survive HTML tag
    stripping, are invisible in logs, and break word counts and Telegram
    formatting in ways that are near-impossible to spot by eye.
    """
    if not text:
        return text
    return re.sub(r"[​‌‍⁠﻿­]", "", text)


def clean_summary(text):
    if not text:
        return text
    text = strip_invisibles(text)
    text = re.sub(r"^summary[\s\-:]+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\(\d+\s*words?\)", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"word\s*count[\s:]+\d+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = text.strip('"').strip()
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


def _oneline(text, limit=100):
    """Flatten a summary onto a single line for logging.

    Passthrough alerts (Form 4, news) legitimately contain newlines, and
    printing them raw shredded log entries across multiple interleaved lines
    -- the XYF Form 4 entry came out with its '[DONE]' line separated from its
    '[ALERT READY]' line by three unrelated HTTP logs.
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def starts_with_bad_keyword(text):
    if not text:
        return False
    low = text.lower().strip()
    return any(low.startswith(kw) for kw in BAD_START_KEYWORDS)


def ends_incomplete(text):
    """True when the summary doesn't finish a sentence.

    This is the check that actually matters -- an abrupt cut-off is what made
    the old alerts unreadable. Handles closing quotes and brackets, which the
    v1 version flagged as incomplete.
    """
    if not text:
        return True
    return not text.strip().endswith(SENTENCE_END)


def trim_to_limit(summary, max_words):
    """Drop whole trailing sentences until the summary fits.

    A summary that runs 122 words against a 120 ceiling is a GOOD summary two
    words too long -- rejecting it and burning another LLM call (which then
    came back empty and got the filing flagged) is absurd. Trimming whole
    sentences keeps the text well-formed and costs nothing.

    Returns the trimmed text, or the original if trimming can't help.
    """
    if not summary or count_words(summary) <= max_words:
        return summary
    sentences = re.findall(r"[^.!?]+[.!?]+(?:[\"'”’)]*)?", summary)
    if not sentences:
        return summary
    out = ""
    for s in sentences:
        candidate = (out + s).strip()
        if count_words(candidate) > max_words:
            break
        out = candidate
    return out if out else summary


# Phrases the model produces when the source had nothing in it to summarise --
# most often a 10-Q whose XBRL extract came through as bare taxonomy tags with
# no values attached. Retrying cannot help: the input is empty of facts. These
# are dropped silently rather than retried and flagged.
NO_CONTENT_MARKERS = (
    "no specific financial values",
    "only taxonomy tags",
    "cannot be generated",
    "does not contain any",
    "contains no ",
    "no quantitative information",
    "insufficient information",
    "unable to summarize",
    "unable to summarise",
    "no information about",
)


def says_no_content(summary):
    if not summary:
        return False
    low = summary.lower()
    return any(m in low for m in NO_CONTENT_MARKERS)


def classify_failure(summary, band=None):
    """Reason this summary should be rejected, or None if it's good.

    `band` is the (min, max) computed from the SOURCE length by target_band().

    THE FLOOR IS NOT THE BAND'S LOWER BOUND. It is MIN_WORDS, the absolute
    readability floor. The band's `lo` is a TARGET handed to the prompt, not a
    rejection threshold -- and conflating the two was still costing real
    alerts. CNA Financial's 8-K summarised to 61 clean words against a 66-word
    target: five words short, entirely readable, and it was thrown away. The
    retry then returned empty, so the filing was flagged and no alert went out
    at all. Loews lost the same way at 51 words. A concise summary is not a
    defective one; only a stub is. The ceiling stays enforced because an
    over-long summary is genuinely harder to read on a phone -- and trimming
    makes that free to fix.
    """
    lo, hi = band if band else (MIN_WORDS, MAX_WORDS)

    # The floor can never exceed the ceiling. A 74-word news item gets a band
    # of 20-37 words; pairing that ceiling with a flat 45-word floor demands a
    # summary that is simultaneously under 37 and over 45 words, which nothing
    # can satisfy -- every attempt is rejected as too_short, retried, rejected
    # again, and the alert is flagged. Seen live on AMZN. Taking min() keeps
    # the CNA fix intact for long filings (band 66-120 still floors at 45)
    # while letting genuinely short sources through.
    floor = min(MIN_WORDS, lo)

    if not summary:
        return "empty"
    if says_no_content(summary):
        return "no_content"
    wc = count_words(summary)
    if wc < floor:
        return "too_short"
    if wc > hi:
        return "too_long"
    if starts_with_bad_keyword(summary):
        return "bad_start"
    if ends_incomplete(summary):
        return "incomplete"
    return None


# ── Prompts ───────────────────────────────────────────────────────────────────
def screen_prompt(company_name, raw_text):
    """ONE call replacing v1's separate gibberish and relevance round trips."""
    return f"""You are screening source material before it is summarised for investors.

Company of interest: {company_name}

Answer two questions about the content below.
1. Is the text unusable? Unusable means OCR garbage, broken encoding, navigation
   boilerplate, a paywall notice, or almost no readable prose.
2. Is it genuinely about {company_name}? A passing mention in a list of tickers
   does not count. It must carry real information about this company.

Respond with ONLY this JSON, no other text:
{{"is_gibberish": true or false, "is_relevant": true or false, "reason": "<8 words max>"}}

Content:
{raw_text[:3000]}"""


def _style_rules(lo=MIN_WORDS, hi=MAX_WORDS):
    return f"""Rules:
- Write {lo}-{hi} words. Stop as soon as the substance runs out.
  A shorter accurate summary beats a padded one. If the source does not
  contain {lo} words of real information, write less rather than inventing.
- NEVER pad with filler, restated facts, or generic commentary to reach a length.
  Do not add phrases like "this is significant for investors" or "remains to be seen".
- The final sentence MUST end with a full stop, question mark or exclamation mark.
  Never stop mid-sentence.
- Only facts explicitly present in the source. No speculation, no advice, no
  recommendations, no opinions.
- Neutral professional tone. Plain English. No first person. No greeting, no sign-off.
- Do not begin with "This", "The following", "Summary:", "Note:", or "Here is".
- Do not mention the word count."""


def summarise_prompt(company_name, raw_text, filing_type, sub_summary="", band=None):
    """S.1.N / S.1.A / S.1.T + impact, in a single call.

    v1 asked for "exactly 75 words" and then made a SEPARATE call to classify
    impact. The word count produced padding and the extra call produced
    latency and rate-limit pressure for a one-word answer.
    """
    if filing_type == "NEWS":
        kind, limit = "news article", NEWS_CHAR_LIMIT
    elif filing_type == "EARNINGS_TRANSCRIPT":
        kind, limit = "earnings call transcript", TRANSCRIPT_CHAR_LIMIT
    else:
        kind, limit = "regulatory filing", FILING_CHAR_LIMIT

    context = f"\nHeadline/context: {sub_summary}\n" if sub_summary else ""
    lo, hi = band if band else (MIN_WORDS, MAX_WORDS)

    return f"""You are a financial analyst writing an alert for investors who follow {company_name}.

Summarise the {kind} below, covering only what matters to someone holding or
considering this stock.

{_style_rules(lo, hi)}

Also judge market impact:
- HIGH: materially moves the stock (earnings surprise, M&A, guidance change,
  regulatory action, executive departure, major contract)
- MEDIUM: relevant but not decisive
- LOW: routine, administrative, or already widely known

Respond with ONLY this JSON, no other text:
{{"summary": "<your summary>", "impact": "HIGH" or "MEDIUM" or "LOW"}}

Company: {company_name}{context}
{kind.title()}:
{raw_text[:limit]}"""


def retry_prompt(company_name, raw_text, filing_type, previous, failure, band=None):
    """Single corrective retry that tells the model exactly what went wrong.

    v1 retried by demanding five more words each time, which made truncation
    and padding worse rather than better. Naming the actual defect works far
    better than moving a numeric target.
    """
    lo, hi = band if band else (MIN_WORDS, MAX_WORDS)
    diagnosis = {
        "empty": "You returned nothing. Produce a real summary.",
        "too_short": f"Your summary was too short. It needs at least {lo} words "
                     f"of real content from the source -- add substance, not filler.",
        "too_long": f"Your summary exceeded {hi} words. Tighten it by cutting "
                    f"the least important detail, not by truncating a sentence.",
        "bad_start": "Your summary opened with a banned phrase. Start directly with the fact.",
        "incomplete": "Your summary stopped mid-sentence. Every sentence must finish, "
                      "and the last one must end with proper punctuation.",
    }.get(failure, "Your summary did not meet the quality bar.")

    limit = TRANSCRIPT_CHAR_LIMIT if filing_type == "EARNINGS_TRANSCRIPT" else NEWS_CHAR_LIMIT
    prev = (previous or "")[:600]

    return f"""Your previous summary was rejected. {diagnosis}

Previous attempt:
{prev}

Rewrite it correctly for {company_name}.

{_style_rules(lo, hi)}

Respond with ONLY this JSON, no other text:
{{"summary": "<corrected summary>", "impact": "HIGH" or "MEDIUM" or "LOW"}}

Source:
{raw_text[:limit]}"""


# ── Storage ───────────────────────────────────────────────────────────────────
def store_flagged_summary(filing_id, ticker, company_name, final_summary,
                          failure_reason, attempts, source="SEC_EDGAR", filing_type=""):
    try:
        fid, fname = resolve_feature(source, filing_type)
        supabase.table("flagged_summaries").insert({
            "filing_id": filing_id,
            "ticker": ticker,
            "company_name": company_name,
            "final_summary": final_summary,
            "final_word_count": count_words(final_summary) if final_summary else 0,
            "max_target_reached": MAX_WORDS,
            "failure_reason": failure_reason,
            "attempts": attempts,
            "feature_id": fid,
            "feature_name": fname,
            "source": source,
            "filing_type": filing_type,
        }).execute()
        print(f"[FLAGGED] {ticker} -> review queue ({failure_reason})")
    except Exception as e:
        # If this is failing with a permissions error, SUPABASE_KEY is still
        # the anon key. flagged_summaries has RLS enabled with an INSERT-only
        # policy and no SELECT policy, so supabase-py's default
        # return-the-row behaviour gets denied and the insert rolls back.
        # Use the service_role key on the backend.
        print(f"[ERROR] Failed to store flagged summary: {e}")


def get_recent_summaries(ticker, limit=10):
    try:
        result = supabase.table("ai_summaries").select("summary") \
            .eq("ticker", ticker).order("created_at", desc=True).limit(limit).execute()
        return [r["summary"] for r in result.data if r.get("summary")]
    except Exception:
        return []


def store_summary(filing_id, ticker, summary, impact, event_type):
    try:
        result = supabase.table("ai_summaries").insert({
            "filing_id": filing_id, "ticker": ticker, "summary": summary,
            "impact": impact, "event_type": event_type,
        }).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        print(f"[ERROR] Failed to store summary: {e}")
        return None


def store_alert(ticker, summary, impact, source, filing_type="", extra=None, summary_id=None):
    try:
        fid, fname = resolve_feature(source, filing_type)
        merged = dict(extra or {})
        merged["feature_id"] = fid
        merged["feature_name"] = fname
        supabase.table("alerts").insert({
            "ticker": ticker, "summary": summary, "impact": impact,
            "source": source, "filing_type": filing_type, "extra": merged,
            "delivered": False, "summary_id": summary_id,
        }).execute()
        print(f"[ALERT READY] {impact} -- {ticker}: {_oneline(summary, 80)} (F{fid}/11 {fname})")
    except Exception as e:
        print(f"[ERROR] Failed to store alert: {e}")


def update_filing_status(filing_id, status):
    try:
        supabase.table("raw_filings").update({"status": status}).eq("id", filing_id).execute()
    except Exception as e:
        print(f"[ERROR] Failed to update status: {e}")


# ── Deduplication ─────────────────────────────────────────────────────────────
def _similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def is_duplicate(ticker, summary):
    """Local-first dedup.

    v1 made ONE LLM CALL PER RECENT SUMMARY -- up to ten sequential calls per
    filing, purely to ask "are these the same story?" -- and then compared the
    answer against the string "True", so with a JSON boolean it never matched
    and nothing was ever actually deduplicated. Ten calls, zero effect.

    Text similarity settles the clear cases for free. Only the ambiguous
    middle band costs a call, and it's a single call covering all candidates.
    """
    recent = get_recent_summaries(ticker)
    if not recent:
        return False

    scored = sorted(((_similarity(summary, old), old) for old in recent),
                    key=lambda x: x[0], reverse=True)
    best_score, best_match = scored[0]

    if best_score >= DEDUP_LOCAL_HIGH:
        print(f"[DEDUP] Local match {best_score:.2f} -- duplicate, no LLM call needed")
        return True
    if best_score < DEDUP_LOCAL_LOW:
        return False

    candidates = [old for score, old in scored if score >= DEDUP_LOCAL_LOW][:3]
    listed = "\n".join(f"{i + 1}. {c[:300]}" for i, c in enumerate(candidates))
    prompt = f"""Does the NEW summary describe the same underlying event as any EARLIER one?

Same event = same announcement or story, even if worded differently.
Different events on the same company are NOT duplicates.

NEW:
{summary}

EARLIER:
{listed}

Respond with ONLY: {{"is_duplicate": true or false}}"""

    # Same reasoning-token headroom as the screen call -- 200 was nowhere near
    # enough for the model to think and then emit even a one-field JSON object.
    result = parse_json_response(call_deepinfra(prompt, max_tokens=1200))
    dup = as_bool(result.get("is_duplicate"), default=False)
    print(f"[DEDUP] Ambiguous ({best_score:.2f}) -> LLM says duplicate={dup}")
    return dup


# ── Summarisation ─────────────────────────────────────────────────────────────
def _format_passthrough(raw_text):
    """Render a short source as an alert without an LLM.

    Pollers store news as "{title}\\n\\n{description}". Running that through
    clean_summary() collapses the blank line and welds the headline onto the
    body -- "...partners with Google on AI Paris Saint Germain (PSG) - the
    reigning..." -- which reads like a typo. Keep the two parts distinct and
    make sure the headline terminates.
    """
    text = strip_invisibles(raw_text or "").strip()
    parts = [p.strip() for p in re.split(r"\n\s*\n", text, maxsplit=1)]

    if len(parts) == 2 and parts[0] and parts[1]:
        head, body = parts
        if not head.endswith((".", "!", "?", ":", "—", "-")):
            head += "."
        # Drop a body that just restates the headline.
        if body.lower().startswith(head.rstrip(".").lower()[:40]):
            merged = body
        else:
            merged = f"{head} {body}"
    else:
        merged = parts[0] if parts else ""

    merged = standardize_numbers(clean_summary(merged))
    if merged and not merged.endswith(SENTENCE_END):
        merged += "."
    return merged


def summarise(company_name, raw_text, filing_type="", sub_summary="",
              filing_id=None, ticker=None, source="SEC_EDGAR"):
    """Returns (summary, impact, attempts). summary is None if it was flagged."""
    attempts_log = []
    input_words = count_words(raw_text)
    band = target_band(input_words)

    # ── Passthrough: the source is already shorter than any summary ──────────
    # Median news item is 37 words and median Form 4 is 45. Asking a model to
    # write 45-120 words about 37 words of input guarantees invented filler.
    # These are shipped as-is, cleaned. Zero LLM calls, and the alert reads
    # better because every word in it came from the source.
    if band is None:
        passthrough = _format_passthrough(raw_text)
        # A passthrough that formats down to nothing means the source was a
        # bare stub -- a headline row with no body. Ninety-nine of those went
        # to the LLM in one morning and came back empty twice each; every one
        # of them was a wasted pair of calls on a record that could never have
        # produced an alert. Caught here for free.
        if count_words(passthrough) < 8:
            print(f"[SUMMARY] Source is an empty stub ({input_words} words in, "
                  f"{count_words(passthrough)} usable) -- discarding, no LLM call")
            return None, "DISCARD", 0
        print(f"[SUMMARY] Source is only {input_words} words -- passing through "
              f"verbatim, no LLM call (summarising would mean inventing text)")
        return passthrough, "LOW", 0

    lo, hi = band
    print(f"[SUMMARY] {input_words} words in -> targeting {lo}-{hi} words out")

    raw = call_deepinfra(summarise_prompt(company_name, raw_text, filing_type,
                                          sub_summary, band))
    parsed = parse_json_response(raw)
    summary = standardize_numbers(clean_summary(parsed.get("summary", "")))
    impact = str(parsed.get("impact", "LOW")).upper()

    # Salvage an over-long summary by dropping trailing sentences before
    # deciding it failed. Costs nothing and avoids a wasted retry.
    if summary and count_words(summary) > hi:
        trimmed = trim_to_limit(summary, hi)
        if trimmed and count_words(trimmed) >= lo:
            print(f"[SUMMARY] Trimmed {count_words(summary)} -> "
                  f"{count_words(trimmed)} words (no extra LLM call)")
            summary = trimmed

    failure = classify_failure(summary, band)
    attempts_log.append({"attempt": 1, "words": count_words(summary),
                         "target": f"{lo}-{hi}", "input_words": input_words,
                         "failure": failure})

    if not failure:
        print(f"[SUMMARY] Passed first attempt ({count_words(summary)} words)")
        return summary, impact, 1

    # The source had no facts in it. A retry re-reads the same empty input and
    # produces the same non-answer, so skip straight to discard.
    if failure == "no_content":
        print("[SUMMARY] Source carries no reportable content "
              "(likely XBRL tags with no values) -- discarding, no retry")
        return None, "DISCARD", 1

    print(f"[SUMMARY] Attempt 1 rejected ({failure}) -- one corrective retry")
    raw2 = call_deepinfra(retry_prompt(company_name, raw_text, filing_type,
                                       summary, failure, band))
    parsed2 = parse_json_response(raw2)
    summary2 = standardize_numbers(clean_summary(parsed2.get("summary", "")))
    impact2 = str(parsed2.get("impact", impact)).upper()
    if summary2 and count_words(summary2) > hi:
        t2 = trim_to_limit(summary2, hi)
        if t2 and count_words(t2) >= lo:
            summary2 = t2

    # If the retry came back empty but attempt 1 produced something usable,
    # keep attempt 1 rather than throwing both away. An empty retry is a
    # transport/truncation failure, not a judgement that attempt 1 was bad.
    if not summary2 and summary and not classify_failure(summary, band):
        print("[SUMMARY] Retry returned nothing -- keeping the valid first attempt")
        return summary, impact, 2

    failure2 = classify_failure(summary2, band)
    attempts_log.append({"attempt": 2, "words": count_words(summary2),
                         "target": f"{lo}-{hi}", "failure": failure2})

    if not failure2:
        print(f"[SUMMARY] Passed on retry ({count_words(summary2)} words)")
        return summary2, impact2, 2

    print(f"[SUMMARY] Retry also failed ({failure2}) -- flagging for review")
    store_flagged_summary(filing_id, ticker, company_name,
                          summary2 or summary, failure2, attempts_log, source, filing_type)
    return None, None, 2


# ── Per-filing processor ──────────────────────────────────────────────────────
def process_filing(filing):
    filing_id = filing["id"]
    ticker = filing.get("ticker", "UNKNOWN")
    company_name = filing.get("company_name") or ticker
    raw_text = filing.get("raw_text", "") or ""
    filing_type = filing.get("filing_type", "")
    source = filing.get("source", "SEC_EDGAR")
    extra = filing.get("extra") or {}
    sub_summary = extra.get("title", "")

    print(f"\n[PROCESSING] {filing_type} -- {company_name} ({ticker}) [{source}]")
    _reset_token_usage()

    # Gate 0 — free. No LLM call.
    if SKIP_UNTICKERED_NEWS and ticker in UNTICKERED:
        print(f"[SKIP] No ticker matched -- not a company alert. Zero LLM cost.")
        update_filing_status(filing_id, "DISCARDED")
        return

    if len(raw_text.strip()) < 120:
        print(f"[SKIP] Source text too short to summarise ({len(raw_text)} chars)")
        update_filing_status(filing_id, "DISCARDED")
        return

    # Gate 1 — free. Decide the summarisation path BEFORE spending any call.
    #
    # If the source is short enough to be passed through verbatim, the screen
    # cannot change the outcome -- we ship the source text either way. Paying
    # an LLM call to ask "is this gibberish?" about a 41-word SEC Form 4
    # (structured data, from EDGAR, with a resolved ticker) and then emitting
    # that exact text regardless is pure waste. Over the last two weeks that
    # was ~2,000 filings (999 Form 4 + 996 NEWS) each burning one useless call.
    will_passthrough = target_band(count_words(raw_text)) is None

    if will_passthrough:
        screen = {"is_gibberish": False, "is_relevant": True, "reason": "passthrough"}
        print("[SCREEN] Skipped — source passes through verbatim, "
              "screening cannot change the output (0 LLM calls)")
    else:
        # Call 1 — gibberish + relevance together.
        # max_tokens=1500, not 300. Gemini 2.5 Flash is a REASONING model: it
        # thinks before answering and those tokens come out of the same budget.
        # At 300 the thinking consumed the whole allowance and the response was
        # cut off at '{"is_gibber' -- unparseable, so the screen silently failed
        # OPEN on every single filing. A tiny JSON answer still needs room for
        # the reasoning that precedes it.
        screen = parse_json_response(call_deepinfra(screen_prompt(company_name, raw_text),
                                                   max_tokens=1500))
    if as_bool(screen.get("is_gibberish")):
        print(f"[DISCARDED] Unusable text -- {screen.get('reason', '')}")
        update_filing_status(filing_id, "DISCARDED")
        return
    if not as_bool(screen.get("is_relevant"), default=True):
        print(f"[DISCARDED] Not about {company_name} -- {screen.get('reason', '')}")
        update_filing_status(filing_id, "DISCARDED")
        return

    if not screen:
        # Both gates above default to "allow" so a screening failure never
        # silently drops a real filing. But an empty dict means the check did
        # not actually RUN, and a clean "[PASS]" line would misrepresent that.
        print("[SCREEN] WARNING: screen returned no usable JSON -- gates failed "
              "OPEN, content was not actually checked")
    else:
        print("[PASS] Screen (gibberish + relevance, 1 call)")

    # Call 2 (+ optional 3) — summary and impact together.
    summary, impact, attempts = summarise(company_name, raw_text, filing_type,
                                          sub_summary, filing_id, ticker, source)
    if not summary:
        # DISCARD means the source had nothing in it -- that is not a quality
        # failure and does not belong in a human review queue.
        update_filing_status(filing_id,
                             "DISCARDED" if impact == "DISCARD" else "FLAGGED_FOR_REVIEW")
        return

    if impact not in ("HIGH", "MEDIUM", "LOW"):
        impact = "LOW"
    print(f"[SUMMARY] {_oneline(summary, 100)} ({count_words(summary)} words, impact={impact})")

    # Dedup — usually zero LLM calls.
    if is_duplicate(ticker, summary):
        print(f"[DISCARDED] Duplicate -- {ticker}")
        update_filing_status(filing_id, "DISCARDED")
        return
    print("[PASS] Deduplication")

    usage = get_token_usage()
    extra = dict(extra)
    extra.update({
        "summarization_attempts": attempts,
        "input_tokens": usage["input"],
        "output_tokens": usage["output"],
        "total_tokens": usage["input"] + usage["output"],
        "llm_calls": usage["calls"],
    })

    summary_id = store_summary(filing_id, ticker, summary, impact, filing_type)
    store_alert(ticker, summary, impact, source, filing_type, extra, summary_id)
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} | {usage['calls']} LLM call(s), "
          f"{usage['input'] + usage['output']} tokens")


# ── Runner ────────────────────────────────────────────────────────────────────
def run_pipeline(batch_size=10):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking PENDING filings "
          f"[{DEEPINFRA_MODEL}]")
    try:
        result = supabase.table("raw_filings").select("*") \
            .eq("status", "PENDING").order("created_at").limit(batch_size).execute()
        filings = result.data
        if not filings:
            print("No PENDING filings.")
            return

        print(f"Processing {len(filings)} filing(s)...")
        started = time.time()
        for filing in filings:
            try:
                process_filing(filing)
            except Exception as e:
                # One bad filing must never stop the batch. v1 let an
                # exception here escape to the caller, ending the run.
                print(f"[ERROR] {filing.get('ticker', '?')}: {e}")
                try:
                    update_filing_status(filing["id"], "FLAGGED_FOR_REVIEW")
                except Exception:
                    pass
        print(f"[BATCH] {len(filings)} filing(s) in {time.time() - started:.0f}s")

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")


if __name__ == "__main__":
    run_pipeline()
