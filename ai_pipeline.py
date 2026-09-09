import os
import json
import re
import threading
import time
import requests
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# The budget day is the market/delivery day, matching delivery.py's daily cap.
ET = ZoneInfo("America/New_York")

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
from Prompt_S1F_Form4Insider import get_prompt as s1f_prompt
from feature_map import resolve_feature, TOTAL_FEATURES


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
STARTING_TARGET = 150  # Increased from 75 to allow comprehensive coverage
TARGET_STEP = 10       # Increased from 5 for larger jumps during retries
MAX_TARGET = 250       # Increased from 100 to support detailed summaries of complex stories

# Summary class selection: News uses S.1.N, Announcements/filings use S.1.A,
# Earnings call transcripts (Feature 11) use S.1.T. Transcripts run several
# times longer than a filing excerpt, so they get a bigger raw-text slice —
# still capped, just a higher cap, so the model sees more of the call.
TRANSCRIPT_CHAR_LIMIT = 12000
FILING_CHAR_LIMIT = 8000
NEWS_CHAR_LIMIT = 6000


# ── Token usage tracking (per filing currently being processed) ──────────────
# Reset at the start of each filing, read back once at the end to get the TOTAL
# tokens spent across every DeepInfra call that filing needed (gibberish check,
# relevance check, every S.1/S.3 summarization attempt, V.1 validation, impact
# classification, similarity checks) -- not just the final successful
# summarization call, since that's the true per-alert cost.
#
# THREAD-LOCAL, not a plain module global: run_pipeline() now processes several
# filings concurrently, so a shared accumulator would interleave their counts
# and attribute one filing's tokens to whichever filing happened to finish
# next. Each worker thread keeps its own bucket, so per-alert cost stays exact.
_token_usage_local = threading.local()


def _usage_bucket():
    bucket = getattr(_token_usage_local, "bucket", None)
    if bucket is None:
        bucket = {"input": 0, "output": 0, "calls": 0}
        _token_usage_local.bucket = bucket
    return bucket


def _reset_token_usage():
    _token_usage_local.bucket = {"input": 0, "output": 0, "calls": 0}


def _record_token_usage(usage):
    if not usage:
        return
    bucket = _usage_bucket()
    bucket["input"] += usage.get("prompt_tokens", 0) or 0
    bucket["output"] += usage.get("completion_tokens", 0) or 0
    bucket["calls"] += 1


def get_token_usage():
    """Snapshot of accumulated tokens since the last _reset_token_usage()."""
    return dict(_usage_bucket())


# ── DeepInfra concurrency + rate limiting ────────────────────────────────────
# Two different limits, enforced by two different mechanisms. Conflating them
# is what left the old fixed 2-second gap unable to prevent 429s:
#
#   1. CONCURRENCY (the semaphore). Filings are now summarized in parallel, and
#      the scheduler's own worker pool can start an AI-backed job at any time,
#      so N threads can reach this function at once. The semaphore caps how
#      many DeepInfra requests are ever in flight together — the ceiling the
#      old single-threaded design got for free and then lost.
#
#   2. THROUGHPUT (the sliding window). Gemini's quota is requests-per-minute,
#      not requests-in-flight. Four concurrent workers each pacing themselves
#      2 seconds apart still issue 120 requests/minute between them. The window
#      below counts the calls actually issued in the last 60 seconds across all
#      threads and blocks the next one until issuing it stays under LLM_RPM,
#      which is the limit the API actually enforces.
#
# Plus a shared cooldown: when any thread does get rate limited, every other
# thread waits out that same window instead of piling straight back into the
# exhausted quota and turning one 429 into N.
# Raised 4 -> 8. Filings spend nearly all their wall-clock time blocked on a
# DeepInfra round trip, so the pool is I/O-bound and more slots means more of a
# batch in flight at once.
#
# BE HONEST ABOUT THE CEILING: this is not the binding constraint at typical
# Gemini Flash latency. Eight workers at a ~3s round trip want ~160 calls/min,
# and LLM_RPM below caps issuance at 100, so the sliding window — not this
# number — decides throughput once the queue is deep. What the extra slots
# actually buy is headroom when calls run slow (a retry ladder, a 429 cooldown,
# a long transcript), where 4 workers left RPM budget unspent. If the pipeline
# still lags with this at 8, GQ_LLM_RPM is the knob to turn, not this one.
LLM_CONCURRENCY = int(os.getenv("GQ_LLM_CONCURRENCY", "8"))

# RPM is a RATE rail, not a spend control -- the same 12 alerts cost the same
# number of tokens at 55 RPM or 200, they just clear slower at 55. So size it
# to the burst we actually want absorbed, and control money with the daily
# budget below instead.
#
# Sizing: one filing costs ~5-8 DeepInfra calls (gibberish, relevance, S.1,
# V.1, impact, plus the prefiltered dedup comparisons; a filing that needs the
# full S.3 retry ladder costs up to 4 more). A 12-alert burst is therefore
# ~60-100 calls. At 100 RPM that burst clears in about a minute instead of
# being drip-fed over two, and the ceiling still stops a runaway loop from
# free-running.
LLM_RPM = int(os.getenv("GQ_LLM_RPM", "100"))

# The actual money guard. A rate limit alone cannot stop a bad day from
# spending: 100 RPM sustained for 24h is 144,000 calls. This caps the total
# for a calendar day (ET, matching the delivery day) and is checked BEFORE a
# batch is picked up, so hitting it leaves filings PENDING for tomorrow rather
# than failing them mid-flight and flagging them as undeliverable.
LLM_DAILY_CALL_BUDGET = int(os.getenv("GQ_LLM_DAILY_CALL_BUDGET", "6000"))

MAX_RATE_LIMIT_RETRIES = 5

_llm_semaphore = threading.BoundedSemaphore(LLM_CONCURRENCY)
_rate_lock = threading.Lock()
_call_window = deque()      # monotonic timestamps of recently issued calls
_cooldown_until = [0.0]     # global "everybody wait" deadline, set on a 429
_calls_today = [0]          # calls issued since _budget_day
_budget_day = [None]        # ET date the counter belongs to


def _budget_state():
    """(calls_used, budget). Rolls the counter over at ET midnight."""
    today = datetime.now(ET).date()
    if _budget_day[0] != today:
        _budget_day[0] = today
        _calls_today[0] = 0
    return _calls_today[0], LLM_DAILY_CALL_BUDGET


def budget_exhausted():
    """True once today's call budget is spent. Checked between batches."""
    with _rate_lock:
        used, budget = _budget_state()
        return used >= budget


def _set_cooldown(seconds):
    """Make every thread wait out a rate-limit window one thread just hit."""
    with _rate_lock:
        _cooldown_until[0] = max(_cooldown_until[0], time.monotonic() + seconds)


def _acquire_call_slot():
    """
    Block until issuing one more call keeps us under LLM_RPM for the trailing
    60 seconds, and until any global cooldown has expired.
    """
    while True:
        with _rate_lock:
            now = time.monotonic()

            cooldown_left = _cooldown_until[0] - now
            if cooldown_left > 0:
                wait = cooldown_left
            else:
                while _call_window and (now - _call_window[0]) >= 60.0:
                    _call_window.popleft()
                if len(_call_window) < LLM_RPM:
                    _call_window.append(now)
                    _budget_state()          # roll the day over if needed
                    _calls_today[0] += 1
                    return
                # Oldest call in the window falls out at +60s; that is the
                # earliest moment another call can be issued.
                wait = 60.0 - (now - _call_window[0]) + 0.05

        time.sleep(min(max(wait, 0.05), 5.0))


def call_deepinfra(prompt, retries=3, max_tokens=1000):
    """Call DeepInfra API with Gemini 2.5 Flash.

    Every call passes through the semaphore (at most LLM_CONCURRENCY in
    flight) and the sliding window (at most LLM_RPM issued per rolling
    minute), so parallel filing processing cannot outrun the quota.

    A 429 is treated separately from every other failure. It means "you're
    inside a rate-limit window right now," which is recoverable by waiting
    long enough for that window to roll over -- the quick 1-2-4 second
    backoff below is meant for transient network blips, not a per-minute
    quota, so it's nowhere near long enough to let a 429 clear. A 429 gets
    its own longer, capped wait (honoring a Retry-After header if DeepInfra
    sends one), its own retry budget (MAX_RATE_LIMIT_RETRIES), and it parks
    every other thread behind the same cooldown.
    """
    with _llm_semaphore:
        return _call_deepinfra_locked(prompt, retries, max_tokens)


def _call_deepinfra_locked(prompt, retries, max_tokens):
    """The request loop itself. Only ever runs with a semaphore slot held."""
    headers = {
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPINFRA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }

    normal_attempt = 0
    rate_limit_attempt = 0

    while True:
        _acquire_call_slot()
        try:
            r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=30)
        except Exception as e:
            normal_attempt += 1
            print(f"[DEEPINFRA] Attempt {normal_attempt} error: {e}")
            if normal_attempt >= retries:
                return None
            time.sleep(2 ** normal_attempt)
            continue

        if r.status_code == 200:
            resp = r.json()
            text = (resp["choices"][0]["message"]["content"] or "").strip()
            # Strip <think>...</think> reasoning tokens
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            _record_token_usage(resp.get("usage"))
            return text

        # DeepInfra proxies google/gemini-2.5-flash straight through to Google.
        # When Google's own quota is exhausted, DeepInfra does NOT surface it as
        # an HTTP 429 -- it wraps Google's "code": 429 / "Resource exhausted"
        # error inside its OWN HTTP 500 response body. The `r.status_code == 429`
        # branch below therefore never fired for the single most common failure
        # mode in production: every one of these was falling into the generic
        # "normal_attempt" path, which gives up after 3 tries with a 2-4 second
        # backoff -- nowhere near long enough for a quota window to clear -- and
        # returns None. summarise() then read that as a content failure ("empty"/
        # "too_short"), burned its entire word-target retry ladder re-calling
        # this same rate-limited API, and flagged the alert as undeliverable
        # instead of just waiting out the quota and sending it. Detecting the
        # wrapped error here routes it through the real rate-limit backoff.
        is_wrapped_429 = (
            r.status_code in (429, 500, 503)
            and ('"code": 429' in r.text or '"code":429' in r.text
                 or "resource exhausted" in r.text.lower()
                 or "rate limit" in r.text.lower())
        )

        if is_wrapped_429:
            rate_limit_attempt += 1
            if rate_limit_attempt > MAX_RATE_LIMIT_RETRIES:
                print(f"[DEEPINFRA] Rate limited (429) persisted after {MAX_RATE_LIMIT_RETRIES} extended waits -- giving up")
                return None
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else min(15 * rate_limit_attempt, 60)
            print(f"[DEEPINFRA] Rate limited (429) -- waiting {wait:.0f}s before retry {rate_limit_attempt}/{MAX_RATE_LIMIT_RETRIES}")
            # Park every other worker behind the same window. Without this the
            # other threads keep firing into an already-exhausted quota while
            # this one backs off, so one 429 immediately becomes N.
            _set_cooldown(wait)
            time.sleep(wait)
            continue

        normal_attempt += 1
        print(f"[DEEPINFRA] Attempt {normal_attempt} failed: {r.status_code} {r.text[:100]}")
        if normal_attempt >= retries:
            return None
        time.sleep(2 ** normal_attempt)


# ── Summary quality helpers ───────────────────────────────────────────────────
BAD_START_KEYWORDS = [
    "this content", "the following", "this document", "this filing",
    "this report", "this article", "this press release", "this announcement",
    "this form", "this exhibit", "this call", "this transcript", "note:", "summary:", "overview:",
    "the company has filed", "pursuant to", "in accordance with"
]

# Headline slang for price/stock movement. "Meta stock pops after unveiling..."
# reads like a tabloid ticker crawl, not an institutional summary -- the old
# prompt only said "no clickbait phrases" in the abstract, which the model
# reliably ignored the moment the source article's own headline used one of
# these verbs (news headlines are written to be punchy; that's the entire
# reason this list exists). This is the deterministic backstop: even if a
# retry re-introduces one of these, the gate below catches it and forces
# another attempt rather than letting a tabloid verb reach a subscriber.
CLICKBAIT_MOVEMENT_WORDS = [
    "pops", "pop after", "soars", "soaring", "skyrockets", "rockets",
    "surges", "surging", "spikes", "spiking", "explodes", "erupts",
    "tanks", "craters", "cratering", "plummets", "plunges", "plunging",
    "tumbles", "tumbling", "nosedives", "dives", "slides", "sliding",
    "goes wild", "goes crazy", "skyrocketing", "blows past", "smashes",
    "obliterates", "crushes it", "shatters",
]

def contains_clickbait_language(text):
    """True if the summary uses a tabloid-style movement verb instead of a
    neutral, quantified one (e.g. 'rose 2.4%', 'declined 1.1%')."""
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in CLICKBAIT_MOVEMENT_WORDS)

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

def ends_with_question_or_exclamation(text):
    """
    A summary must state facts, not pose a rhetorical question or exclaim.
    last_sentence_incomplete() treats "?" and "!" as valid complete endings
    (they are grammatically complete), which let alerts like "...Is this the
    future of ride-sharing?" through untouched. This is the deterministic
    gate that actually blocks that ending, independent of whether the LLM
    followed the prompt's "neutral, no rhetorical question" instruction.
    """
    if not text:
        return False
    return text.strip().endswith(("?", "!"))

def strip_rhetorical_ending(text):
    """
    Drop trailing rhetorical question / exclamation sentences.

    The model reliably pads to a word target by tacking a closer onto the end
    ("What does this mean for investors?"). The gate then rejects the whole
    summary and the ladder re-rolls it from scratch -- several LLM calls to
    fix one removable sentence, and when the ladder runs out the alert is
    flagged and never sent, losing a summary whose actual reporting was fine.
    Removing the offending sentence is deterministic, costs nothing, and is
    exactly the edit we would ask for.
    """
    if not text:
        return text
    stripped = text.strip()
    if not stripped.endswith(("?", "!")):
        return stripped
    parts = re.split(r"(?<=[.!?])\s+", stripped)
    while parts and parts[-1].strip().endswith(("?", "!")):
        parts.pop()
    return " ".join(parts).strip()


def effective_min_words(raw_text):
    """
    The word floor this source can honestly support.

    MIN_WORDS (70) assumes a filing or a full article. A lot of what the news
    pollers surface is a two-line institutional-holding item -- there is no
    honest way to write 70 words about "HB Wealth Management reduced its AMD
    stake by 5.6%". The prompts forbid padding, the gate demands 70, and the
    result was a deadlock that always ended in FLAGGED, NOT SENT: a permanent
    loss of a real alert because the source was short, not because anything
    was wrong. Scale the floor to the material instead.
    """
    chars = len(raw_text or "")
    if chars < 600:
        return 35
    if chars < 1500:
        return 50
    return MIN_WORDS


# Filing types whose source material is a handful of structured facts -- an
# insider's name, a trade direction, a share count, a price, a date. There is no
# honest 150-word summary of that, so they get their own short ladder.
SHORT_FORM_TYPES = {"4", "INSIDER_FMP"}


def ladder_for(filing_type, raw_text):
    """
    (starting_target, step, max_target, min_words) for this filing type.

    THE BUG THIS FIXES. Form 4 ran a 30 -> 60 word ladder while min_words came
    from effective_min_words(), which returns 35 / 50 / 70 purely by source
    length. Those two were never reconciled, so:

        raw_text  300 chars -> floor 35 vs ceiling 30 on attempt 1  -> always fails
        raw_text 2000 chars -> floor 70 vs ceiling 60 at EVERY rung -> impossible

    classify_failure() demands `min_words <= wc <= max_words`, so a floor above
    the ceiling cannot be satisfied by any output. Every Form 4 burned the full
    six-call ladder and then landed in flagged_summaries -- which is why SEC
    Form 4, the fastest insider source in the stack, produced no alerts at all
    while FMP's 90-minute-old copy did.

    The floor is now clamped strictly below the starting ceiling, so rung 1 is
    always satisfiable and the ladder means what it says.
    """
    if filing_type in SHORT_FORM_TYPES:
        start, step, max_target = 30, 10, 70
    else:
        start, step, max_target = STARTING_TARGET, TARGET_STEP, MAX_TARGET

    min_words = min(effective_min_words(raw_text), start - 10)
    return start, step, max_target, max(min_words, 12)


def classify_failure(summary, max_words, min_words=None):
    """Returns the failure reason for this attempt, or None if it passes."""
    min_words = MIN_WORDS if min_words is None else min_words
    if not summary:
        return "empty"
    wc = count_words(summary)
    if wc > max_words:
        return "too_long"
    if wc < min_words:
        return "too_short"
    if starts_with_bad_keyword(summary):
        return "bad_start"
    if contains_clickbait_language(summary):
        return "clickbait_language"
    if ends_with_question_or_exclamation(summary):
        return "rhetorical_or_exclamatory_ending"
    if last_sentence_incomplete(summary):
        return "incomplete"
    return None


# ── S.1 — Primary summarisation (real S.1.N / S.1.A / S.1.T / S.1.F prompts) ───
def generate_s1(company_name, raw_text, filing_type="", sub_summary="",
                min_words=None, target=None):
    min_words = MIN_WORDS if min_words is None else min_words
    target = STARTING_TARGET if target is None else target
    if filing_type == "NEWS":
        # These two arguments were omitted, so S.1.N fell back to its own
        # defaults -- target 120 words, floor 100 -- while classify_failure
        # rejects anything over STARTING_TARGET (75). Every news item was
        # therefore instructed to write ~120 words and then failed as
        # "too_long" on arrival, guaranteed, before the ladder even started;
        # the ladder then climbed 80/85/90/95/100, never reaching the length
        # the prompt had asked for, and usually ended flagged and unsent.
        # NEWS is the highest-volume feature, so this alone accounted for most
        # of the "FLAGGED, NOT SENT" traffic. S.1.A and S.1.T always passed
        # these correctly; only this branch did not.
        prompt = s1n_prompt(company_name, sub_summary, raw_text[:NEWS_CHAR_LIMIT],
                            target_word_count=target, min_word_count=min_words)
    elif filing_type == "EARNINGS_TRANSCRIPT":
        prompt = s1t_prompt(company_name, sub_summary, raw_text[:TRANSCRIPT_CHAR_LIMIT],
                             target_word_count=target, min_word_count=min_words)
    elif filing_type in SHORT_FORM_TYPES:
        # Form 4 (SEC) and INSIDER_FMP (vendor mirror of the same event) are both
        # inherently brief: insider name, trade type, share count, price, date.
        # Asking for 150 words produces padding or an empty response. S.1.F asks
        # for a tight factual extraction instead, and is now given the SAME
        # target/floor the quality gate will actually judge it against -- the
        # prompt used to hardcode 30/15 while the gate demanded up to 70, so the
        # model was being told to write something guaranteed to be rejected.
        prompt = s1f_prompt(company_name, raw_text[:FILING_CHAR_LIMIT],
                            target_word_count=target, min_word_count=min_words)
    else:
        prompt = s1a_prompt(company_name, sub_summary, raw_text[:FILING_CHAR_LIMIT],
                             target_word_count=target, min_word_count=min_words)
    return call_deepinfra(prompt, max_tokens=600)


# ── S.3 — Resummarize at an escalated word target ─────────────────────────────
def generate_s3(company_name, raw_text, target_words, filing_type="", min_words=None):
    min_words = MIN_WORDS if min_words is None else min_words
    if filing_type == "EARNINGS_TRANSCRIPT":
        char_limit = TRANSCRIPT_CHAR_LIMIT
    elif filing_type in ("NEWS", ""):
        char_limit = NEWS_CHAR_LIMIT
    elif filing_type in SHORT_FORM_TYPES:
        char_limit = FILING_CHAR_LIMIT
    else:
        # Was falling through to NEWS_CHAR_LIMIT (6000) for every SEC filing type
        # and Form 4 -- filings can legitimately need more source text than a
        # news article, so retries were seeing less content than the S.1 attempt
        # that already failed on the fuller text.
        char_limit = FILING_CHAR_LIMIT
    prompt = f"""You are a professional financial analyst. Write a comprehensive, formal summary of the following content in exactly {target_words} words.

CONTENT REQUIREMENTS:
- Cover all major developments and material facts from the content
- Include quantified impacts: specific numbers, percentages, amounts, timeframes
- Explain strategic significance and why investors should care
- Ensure comprehensive coverage that stands alone without reference to the original

STYLE & TONE:
- Professional, institutional tone suitable for investment professionals
- Neutral, objective, factual — no editorializing, speculation, or emotional language
- Precise: name parties, specific products, markets, financial metrics
- NEVER use tabloid/headline movement verbs, even if the source material itself uses
  them: pops, soars, skyrockets, rockets, surges, spikes, explodes, tanks, craters,
  plummets, plunges, tumbles, nosedives, dives, slides, goes wild, blows past, smashes,
  crushes it, shatters. Use a neutral, quantified verb paired with the actual number
  instead: "rose 2.4%", "declined 1.1%", "increased", "fell".
- Never speculate about future outcomes

WRITING RULES:
- Write exactly {target_words} words. If exact {target_words} is impossible while staying strictly accurate, come as close as possible, but never fewer than {min_words} and never more than {target_words}.
- Do not pad with filler phrases, restated facts, or generic commentary — every word must carry real information.
- Must end with a complete factual sentence ending in a period. Never end with a question mark or exclamation point.
- Never end with a rhetorical question, speculation, or a sentence asking what happens next.
- Do not start with "This", "The following", "Summary:", "Note:" or similar
- Plain English only, neutral and factual, no first person

Company: {company_name}

Content:
{raw_text[:char_limit]}

Return only the summary. Nothing else."""
    return call_deepinfra(prompt, max_tokens=700)


# ── Flagged-for-review sink (replaces "best available" fallback) ─────────────
def store_flagged_summary(filing_id, ticker, company_name, final_summary, failure_reason, attempts,
                           source="SEC_EDGAR", filing_type="", max_target_reached=None):
    try:
        fid, fname = resolve_feature(source, filing_type)
        if max_target_reached is None:
            max_target_reached = ladder_for(filing_type, "")[2]
        supabase.table("flagged_summaries").insert({
            "filing_id": filing_id,
            "ticker": ticker,
            "company_name": company_name,
            "final_summary": final_summary,
            "final_word_count": count_words(final_summary) if final_summary else 0,
            "max_target_reached": max_target_reached,
            "failure_reason": failure_reason,
            "attempts": attempts,
            "feature_id": fid,
            "feature_name": fname,
            "source": source,
            "filing_type": filing_type
        }).execute()
        print(f"[FLAGGED] {ticker} sent to review queue after exhausting retries ({failure_reason}) — Feature {fid}/{TOTAL_FEATURES} {fname}")
    except Exception as e:
        print(f"[ERROR] Failed to store flagged summary: {e}")


# ── Master summarise — retry-until-valid, escalating word budget ─────────────
def summarise(company_name, raw_text, filing_type="", sub_summary="", filing_id=None, ticker=None, source="SEC_EDGAR"):
    """
    Attempt 1: S.1.N / S.1.A / S.1.T / S.1.F prompt, target varies by type.
    For most filings: target 150 words, escalate by 10 up to 250.
    For Form 4 (insider trading): target 30 words, escalate by 5 up to 60.

    Each failure retries via S.3 with the ceiling raised, floor fixed at min_words.
    If still failing at MAX_TARGET, stop and flag for manual review —
    never discard silently, never send a summary that failed validation.
    """
    attempts_log = []

    starting_target, target_step, max_target, min_words = ladder_for(filing_type, raw_text)
    target = starting_target

    def _evaluate(raw_response):
        """
        Clean, repair what is deterministically repairable, then judge.

        Returns (summary, failure). A trailing rhetorical sentence is removed
        here rather than being bounced back to the model: it is the single
        most common failure and the only one we can fix without another call.
        """
        if raw_response is None:
            return None, "api_unavailable"
        text = standardize_numbers(clean_summary(raw_response))
        verdict = classify_failure(text, target, min_words)
        # Attempt the repair whenever the text ENDS rhetorically, not only
        # when that is the reported verdict. classify_failure checks length
        # before the rhetorical ending, and the padding closer is frequently
        # the very thing that pushed the summary over the ceiling -- so the
        # reported failure reads "too_long" while the actual defect is one
        # removable sentence. Only checking for the rhetorical verdict would
        # miss exactly the case that occurs most.
        if verdict and text and text.strip().endswith(("?", "!")):
            trimmed = strip_rhetorical_ending(text)
            if trimmed and classify_failure(trimmed, target, min_words) is None:
                print(f"[SUMMARY] Trimmed a trailing rhetorical sentence, "
                      f"resolving '{verdict}' without a retry")
                return trimmed, None
        return text, verdict

    raw = generate_s1(company_name, raw_text, filing_type, sub_summary,
                      min_words=min_words, target=target)
    summary, failure = _evaluate(raw)
    attempts_log.append({"attempt": 1, "target": target, "words": count_words(summary), "failure": failure})

    # "api_unavailable" (call_deepinfra returned None — DeepInfra/Gemini gave up
    # after its own rate-limit backoff) is an infra failure, not a content
    # failure. Escalating the word-target ladder and calling generate_s3 again
    # immediately just re-hits the same exhausted quota one more time per rung,
    # each paying that same backoff again, for a summary the content is not at
    # fault for. Stop the ladder on the first "api_unavailable" and flag it —
    # classify_failure's real length/quality checks still get their full ladder
    # for actual content problems.
    while failure and failure != "api_unavailable" and target < max_target:
        target += target_step
        print(f"[SUMMARY] Retry — previous failure: {failure}, new target: {target} words")
        raw = generate_s3(company_name, raw_text, target, filing_type,
                          min_words=min_words)
        summary, failure = _evaluate(raw)
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
        filing_type=filing_type,
        max_target_reached=max_target
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

# Words carried by almost every financial summary — they signal nothing about
# whether two summaries describe the same event, so they are excluded from the
# overlap score that decides which pairs are worth an LLM call.
_DEDUP_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "it", "its", "this", "that", "these", "those",
    "will", "would", "may", "also", "which", "than", "then", "company",
    "inc", "corp", "corporation", "said", "reported", "quarter", "year",
    "million", "billion", "percent", "shares", "stock", "usd",
}

# Below this word-overlap ratio two summaries share almost no substance, so an
# LLM comparison can only come back "not similar". Deliberately permissive --
# it is a prefilter for obvious non-matches, not the duplicate decision.
DEDUP_MIN_OVERLAP = float(os.getenv("GQ_DEDUP_MIN_OVERLAP", "0.25"))
# Real duplicates are near-simultaneous reprints, so they sort to the top of
# the overlap ranking. Comparing more than a handful buys nothing.
DEDUP_MAX_LLM_COMPARISONS = int(os.getenv("GQ_DEDUP_MAX_COMPARISONS", "3"))


def _content_words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _DEDUP_STOPWORDS}


def rank_dedup_candidates(summary, recent_summaries,
                          min_overlap=None, max_candidates=None):
    """
    The subset of `recent_summaries` worth an LLM duplicate check, most
    similar first.

    Scored by Jaccard overlap of content words. Anything below min_overlap is
    dropped without a call; the rest are capped at max_candidates.
    """
    min_overlap = DEDUP_MIN_OVERLAP if min_overlap is None else min_overlap
    max_candidates = (DEDUP_MAX_LLM_COMPARISONS if max_candidates is None
                      else max_candidates)

    new_words = _content_words(summary)
    if not new_words:
        return list(recent_summaries or [])[:max_candidates]

    scored = []
    for old in (recent_summaries or []):
        old_words = _content_words(old)
        if not old_words:
            continue
        union = new_words | old_words
        if not union:
            continue
        overlap = len(new_words & old_words) / len(union)
        if overlap >= min_overlap:
            scored.append((overlap, old))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [old for _, old in scored[:max_candidates]]


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

def is_real_url(value):
    """raw_filings.filing_url is overloaded: for EDGAR/news rows it holds a
    genuine http(s) URL harvested from the provider, but for the FMP insider
    and transcript pollers it holds a SYNTHETIC DEDUP KEY
    ("fmp_insider_AAPL_2026-08-20_John_Smith", "fmp_transcript_AAPL_2026_Q3")
    that was never a URL. Rendering those as "View source" is exactly what
    produces the 404s on Features 5 and 11 -- so only pass through values
    that are actually addressable.
    """
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def store_alert(ticker, summary, impact, source, filing_type="", extra=None, summary_id=None, link=None):
    try:
        fid, fname = resolve_feature(source, filing_type)
        merged_extra = dict(extra or {})
        merged_extra["feature_id"] = fid
        merged_extra["feature_name"] = fname
        alert_dict = {
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": source,
            "filing_type": filing_type,
            "extra": merged_extra,
            "delivered": False,
            "summary_id": summary_id
        }
        if is_real_url(link):
            alert_dict["link"] = link
        supabase.table("alerts").insert(alert_dict).execute()
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
    if validation_result.get("issues_detected") in (True, "True"):
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
        # Judge the correction by the same floor the summary was written to.
        # Using the bare MIN_WORDS here re-imposed 70 words on a thin source
        # that summarise() had already, correctly, accepted at a lower floor --
        # so V.1 "correcting" a valid short summary flagged it as
        # v1_correction_too_short and the alert was lost after passing every
        # earlier gate. The rhetorical trim applies here too.
        _, _, v1_ceiling, min_words = ladder_for(filing_type, raw_text)
        failure = classify_failure(corrected, v1_ceiling, min_words)
        if failure and corrected.strip().endswith(("?", "!")):
            trimmed = strip_rhetorical_ending(corrected)
            if trimmed and classify_failure(trimmed, v1_ceiling, min_words) is None:
                corrected, failure = trimmed, None
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

    # Step 6: Semantic deduplication.
    #
    # This used to send EVERY one of the last 10 summaries for the ticker to
    # the LLM, one call each -- so a heavily-followed ticker cost up to 10
    # extra calls per filing purely to ask "is this a duplicate?", dwarfing
    # the 5 calls that actually produce the alert. Most of those comparisons
    # are between summaries with almost no words in common, which no model
    # needs to read to reject.
    #
    # A local lexical prefilter drops those for free and only spends a call on
    # candidates close enough to plausibly be the same story. The LLM still
    # makes every actual duplicate/not-duplicate decision -- it just stops
    # being asked about obviously unrelated pairs.
    candidates = rank_dedup_candidates(summary, get_recent_summaries(ticker))
    for old_summary in candidates:
        sim_result = parse_json_response(call_deepinfra(similarity_prompt(old_summary, summary)))
        if sim_result.get("is_similar") == "True":
            print(f"[DISCARDED] Duplicate -- {ticker}")
            update_filing_status(filing_id, "DISCARDED")
            return
    print(f"[PASS] Deduplication check ({len(candidates)} LLM comparison(s))")

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
                filing_type=filing_type, extra=extra, summary_id=summary_id, 
                link=filing.get("filing_url"))
    update_filing_status(filing_id, "PROCESSED")
    print(f"[DONE] {ticker} -- {impact} alert stored ({summarization_attempts} attempt(s), "
          f"{usage['input']}+{usage['output']} tokens in+out)")


# ── Freshness ─────────────────────────────────────────────────────────────────
# An alert is only worth sending while it is still news. Past this window the
# reader has seen it elsewhere, and delivering it makes the product look slow
# rather than thorough. Filings are given a longer life than news because a
# 10-K matters for longer than a headline does.
MAX_CONTENT_AGE_MINUTES = int(os.getenv("GQ_MAX_CONTENT_AGE_MINUTES", "90"))
MAX_FILING_AGE_MINUTES = int(os.getenv("GQ_MAX_FILING_AGE_MINUTES", "360"))

NEWS_LIKE = ("NEWS",)


def _cutoff_iso(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


_last_expiry_at = [0.0]
EXPIRY_INTERVAL_SECONDS = float(os.getenv("GQ_EXPIRY_INTERVAL_SECONDS", "60"))


def expire_stale_filings(force=False):
    """
    Retire PENDING rows that are too old to be news.

    Two windows, because the two kinds of content age differently: a news
    article is worthless within the hour, an SEC filing is not. Runs as two
    bulk UPDATEs, so a backlog of any size clears in constant time instead of
    being summarized one expensive filing at a time.

    Throttled to once a minute. run_pipeline() ticks every few seconds when
    the queue is empty, and firing two UPDATEs on every one of those ticks
    would add ~40 pointless writes a minute against Supabase to enforce a
    window measured in hours.
    """
    now = time.monotonic()
    if not force and (now - _last_expiry_at[0]) < EXPIRY_INTERVAL_SECONDS:
        return 0
    _last_expiry_at[0] = now

    total = 0
    try:
        news = (supabase.table("raw_filings")
                .update({"status": "EXPIRED"})
                .eq("status", "PENDING")
                .in_("filing_type", list(NEWS_LIKE))
                .lt("created_at", _cutoff_iso(MAX_CONTENT_AGE_MINUTES))
                .execute()).data or []
        total += len(news)

        filings = (supabase.table("raw_filings")
                   .update({"status": "EXPIRED"})
                   .eq("status", "PENDING")
                   .not_.in_("filing_type", list(NEWS_LIKE))
                   .lt("created_at", _cutoff_iso(MAX_FILING_AGE_MINUTES))
                   .execute()).data or []
        total += len(filings)
    except Exception as e:
        print(f"[EXPIRE] Could not expire stale filings: {e}")
        return 0

    if total:
        print(f"[EXPIRE] Retired {total} stale PENDING filing(s) "
              f"(news >{MAX_CONTENT_AGE_MINUTES}m, filings >{MAX_FILING_AGE_MINUTES}m)")
    return total


# ── Queue priority ────────────────────────────────────────────────────────────
# THE LATENCY BUG THIS FIXES. main.py gives SEC EDGAR a dedicated thread pool and
# a 15-second cadence so a filing is captured seconds after publication -- and
# then the pipeline threw that away, because it read the queue ordered ONLY by
# created_at. News is by far the highest-volume source (10 articles x 25 tickers
# every 15 minutes from FMP, plus 19 RSS feeds every 60s), so an 8-K routinely
# queued behind dozens of news rows inserted moments after it and waited several
# batches -- minutes -- for a summary. Prioritising in the poller and then
# ignoring it here meant the SEC lane bought nothing end to end.
#
# Filings from the primary source now jump the queue. News still drains, it just
# no longer sits in front of a material event the company filed itself.
PRIORITY_SOURCES = ["SEC_EDGAR", "FMP_TRANSCRIPT"]
PRIORITY_FILING_TYPES = ["8-K", "10-Q", "10-K", "4", "EARNINGS_TRANSCRIPT", "INSIDER_FMP"]
PIPELINE_BATCH_SIZE = int(os.getenv("GQ_PIPELINE_BATCH_SIZE", "12"))


def _pg_in(values):
    """PostgREST `in.(...)` list. Quoted because "8-K"/"10-Q" contain a hyphen."""
    return ",".join('"{}"'.format(str(v).replace('"', '')) for v in values)


# Matched on EITHER axis. Source alone was not enough: FMP writes insider
# transactions to raw_filings as source="FMP_NEWS", filing_type="INSIDER_FMP"
# (fmp_poller.py), which is the same time-critical content as an SEC Form 4 but
# arrives under the same source label as ordinary news.
_PRIORITY_OR_FILTER = (f"source.in.({_pg_in(PRIORITY_SOURCES)}),"
                       f"filing_type.in.({_pg_in(PRIORITY_FILING_TYPES)})")


def _fetch_prioritised_batch(limit):
    """
    One batch, primary-source filings first, then everything else newest-first.

    Two queries rather than one so the ordering is explicit and cheap: PostgREST
    cannot express "sort by an allowlist" in a single indexed order clause
    without a computed column.

    THE DEAD-CONSTANT BUG THIS FIXES. PRIORITY_FILING_TYPES was defined and then
    never referenced — the query filtered on source alone. Every type in it that
    does not arrive under a priority SOURCE was therefore never prioritised at
    all, which in practice meant INSIDER_FMP: FMP insider trades queued behind
    the news backlog they were listed specifically to jump.
    """
    def _q(builder):
        try:
            return builder.execute().data or []
        except Exception as e:
            print(f"[PIPELINE] Queue read failed: {e}")
            return []

    priority = _q(supabase.table("raw_filings")
                  .select("*")
                  .eq("status", "PENDING")
                  .or_(_PRIORITY_OR_FILTER)
                  .order("created_at", desc=True)
                  .limit(limit))

    if len(priority) >= limit:
        return priority[:limit]

    seen = {r["id"] for r in priority}
    # NEWEST FIRST for the remainder. Strict FIFO put fresh content BEHIND stale
    # content, so a backlog larger than one batch meant the newest item could not
    # be reached until the whole backlog cleared. The stale tail is expired by
    # expire_stale_filings() above rather than being allowed to block the head.
    #
    # Deliberately NOT the complement filter. `not.in` on a nullable column is
    # NULL, not true, for a NULL filing_type — such a row would be excluded from
    # this query AND from the priority query above, and would never be processed
    # by anything. Over-fetching a full page and de-duplicating on id in Python
    # cannot drop a row that way, and costs one page of rows we already index.
    rest = _q(supabase.table("raw_filings")
              .select("*")
              .eq("status", "PENDING")
              .order("created_at", desc=True)
              .limit(limit))

    return (priority + [r for r in rest if r["id"] not in seen])[:limit]


# ── Main pipeline runner ──────────────────────────────────────────────────────
def run_pipeline():
    mode = f"AI (DeepInfra - {DEEPINFRA_MODEL})"
    # Only announce a cycle that has work. The loop ticks every few seconds, so
    # logging every idle poll printed ~40 lines a minute and buried the events
    # that matter (a filing arriving, a summary failing) in scrollback.
    verbose_idle = os.getenv("GQ_VERBOSE_IDLE", "").strip().lower() in ("1", "true", "yes")
    if verbose_idle:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking for PENDING filings... [{mode} MODE]")

    # Checked here, between batches, rather than inside call_deepinfra: a call
    # refused mid-filing would surface as api_unavailable and get the filing
    # flagged as undeliverable. Stopping before we pick anything up leaves the
    # queue PENDING, so it resumes on its own when the budget rolls over.
    if budget_exhausted():
        used, budget = _budget_state()
        print(f"[BUDGET] Daily DeepInfra call budget spent ({used}/{budget}). "
              f"Filings stay PENDING until ET midnight. "
              f"Raise GQ_LLM_DAILY_CALL_BUDGET to lift this.")
        return 0

    try:
        # Retire anything too old to be news before picking work up. Without
        # this the queue is append-only under load: a backlog never drains,
        # and every LLM call spent on a 12-hour-old article is money spent on
        # something nobody should receive.
        expire_stale_filings()

        # NEWEST FIRST. This was .order("created_at") -- strict FIFO -- so a
        # backlog put fresh filings BEHIND stale ones, and since each cycle
        # takes only 10, a backlog larger than one cycle meant the newest
        # filing could not be reached until the entire backlog cleared. That
        # is how a Feature 2 alert generated 12 hours ago went out ahead of
        # news that broke seconds ago. Freshness is the product, so the queue
        # is now LIFO and the stale tail is expired above rather than
        # blocking the head.
        filings = _fetch_prioritised_batch(PIPELINE_BATCH_SIZE)
        if not filings:
            if verbose_idle:
                print("No PENDING filings found.")
            return 0

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
              f"{len(filings)} PENDING filing(s) [{mode} MODE]")

        print(f"Found {len(filings)} PENDING filings -- processing "
              f"({LLM_CONCURRENCY} at a time)...")

        # Filings are independent of each other, and each one spends nearly all
        # its wall-clock time waiting on DeepInfra. Processing them one after
        # another (plus a 1s sleep between each) meant a batch of 10 took the
        # sum of all ten round-trips before ANY of their alerts reached the
        # delivery queue -- the single largest remaining source of alert
        # latency. They now overlap, bounded by the same semaphore and RPM
        # window that protect the API, so throughput goes up without the call
        # rate going up.
        def _run_one(filing):
            try:
                process_filing(filing)
            except Exception as e:
                # One bad filing must not sink the rest of the batch.
                print(f"[ERROR] Filing {filing.get('id')} "
                      f"({filing.get('ticker', 'UNKNOWN')}) failed: {e}")

        with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY,
                                thread_name_prefix="pipeline") as pool:
            list(pool.map(_run_one, filings))

        # Returned so the caller can drain a backlog back-to-back instead of
        # sleeping between batches. A full page means there is very likely more
        # waiting behind it; the caller only idles when this comes back 0.
        return len(filings)

    except Exception as e:
        print(f"[ERROR] Pipeline failed: {e}")
        return 0


if __name__ == "__main__":
    run_pipeline()
