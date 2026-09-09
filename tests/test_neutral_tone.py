"""
tests/test_neutral_tone.py

Locks in the "we send news, we don't ask questions or take sides" rule at
every layer it can leak from:

  1. Every summarization prompt forbids a question/exclamation ending.
  2. No prompt anywhere still PERMITS one (the S.3 retry prompt used to).
  3. The deterministic gate rejects such a summary regardless of the LLM.
  4. Clickbait-question headlines from third-party news sources are stripped
     before they render as the alert's bold first line.

Run:  python tests/test_neutral_tone.py
"""
import sys, os, glob, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 1 & 2. No prompt may permit a "?" or "!" ending ─────────────────────────
# The S.3 retry prompt in ai_pipeline.py literally instructed the model to end
# in ". ! or ?", so every retry rung actively invited the ending the gate then
# rejected -- burning a rung, a call, and the latency of both.
PERMISSIVE = re.compile(r"ending in\s*\.\s*!\s*or\s*\?|\.\s*!\s*or\s*\?")

targets = sorted(glob.glob(os.path.join(ROOT, "Prompt_*.py")))
targets.append(os.path.join(ROOT, "ai_pipeline.py"))

for path in targets:
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    hit = PERMISSIVE.search(body)
    assert not hit, f"{os.path.basename(path)} still permits a ?/! ending: {hit.group(0)!r}"
print(f"no ?/! ending permitted in any of {len(targets)} prompt sources ✓")

for name in ("Prompt_S1N_NewsSummarization",
             "Prompt_S1A_AnnouncementSummarization",
             "Prompt_S1T_TranscriptSummarization"):
    with open(os.path.join(ROOT, f"{name}.py"), encoding="utf-8") as fh:
        body = fh.read().lower()
    assert "never end with a question mark" in body, f"{name} lost its no-question rule"
print("all three summarization prompts still forbid it explicitly ✓")

# ── 3. Deterministic gate, independent of prompt compliance ────────────────
import ai_pipeline as ap

assert ap.ends_with_question_or_exclamation("Is this the future of delivery?")
assert ap.ends_with_question_or_exclamation("Revenue soared!")
assert not ap.ends_with_question_or_exclamation("Revenue rose 6% to $94.9 billion.")

long_q = " ".join(["word"] * 80) + " what happens next?"
assert ap.classify_failure(long_q, 100) == "rhetorical_or_exclamatory_ending", \
    "a well-formed but rhetorical summary must still be rejected"
print("deterministic gate rejects rhetorical/exclamatory summaries ✓")

# ── 4. Third-party clickbait headlines never reach the alert ───────────────
import fmp_client
fmp_client.get_quote = lambda t: None
from alert_formatter import build_message, _clean_headline

assert _clean_headline("Is Apple Stock A Buy After Earnings?") == ""
assert _clean_headline("Should You Sell NVDA Now?") == ""
assert _clean_headline("Apple Crushes Earnings!") == "Apple Crushes Earnings"
assert _clean_headline("Apple reports Q4 revenue of $94.9B") == \
    "Apple reports Q4 revenue of $94.9B"
assert _clean_headline(None) == ""
print("headline sanitizer drops questions, de-exclaims, keeps factual titles ✓")

msg = build_message({
    "id": "x1", "ticker": "AAPL", "impact": "HIGH", "source": "FMP_NEWS",
    "filing_type": "NEWS",
    "summary": "Apple reported fourth-quarter revenue of $94.9 billion.",
    "extra": {"title": "Is Apple Stock A Buy After Earnings?",
              "company_name": "Apple Inc."},
}, reason="AAPL is on your watchlist.")

assert "Is Apple Stock A Buy" not in msg, \
    "clickbait question headline leaked into the delivered message"
assert "$94.9 billion" in msg, "sanitizing the headline must not drop the summary"
print("delivered message carries the facts, not the question ✓")

print("\n✅ NEUTRAL TONE TEST PASS (prompts, gate, and headlines)")
