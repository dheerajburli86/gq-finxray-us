"""
tests/test_summary_gates.py

The production log showed ~6 alerts flagged and 0 sent in one window, all from
three gate bugs. This pins each one to the behaviour that was actually broken.

Run:  python tests/test_summary_gates.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import ai_pipeline as ap

# ── 1. S.1.N must be told the SAME word budget the gate enforces ───────────
# It was called without the two word arguments, so it asked for 120 words
# (floor 100) while classify_failure rejected anything over 75 -- every news
# item failed "too_long" on arrival, before the ladder even started.
seen = {}
ap.s1n_prompt = lambda company, sub, raw, target_word_count=120, min_word_count=100: (
    seen.update(target=target_word_count, floor=min_word_count) or "PROMPT")
ap.call_deepinfra = lambda prompt, max_tokens=600: "x"

ap.generate_s1("Apple Inc.", "some article text", "NEWS", "", min_words=70)
print(f"S.1.N asked for target={seen['target']} floor={seen['floor']}")
assert seen["target"] == ap.STARTING_TARGET, "S.1.N still asks for a length the gate rejects"
assert seen["floor"] == 70
assert seen["target"] <= ap.STARTING_TARGET

# ── 2. A trailing rhetorical sentence is trimmed, not re-rolled ────────────
body = " ".join(["Revenue"] * 72) + " grew sharply."
assert ap.strip_rhetorical_ending(body + " What comes next?") == body
assert ap.strip_rhetorical_ending(body + " Huge quarter!") == body
assert ap.strip_rhetorical_ending(body) == body          # untouched when clean
# Multiple trailing rhetorical sentences all go.
assert ap.strip_rhetorical_ending(body + " Really? Wow!") == body
print("rhetorical trim keeps the reporting, drops the closer ✓")

# The realistic shape: the padding closer is what pushes it OVER the ceiling,
# so classify_failure reports "too_long" and the rhetorical check is never
# reached. The repair must still fire, or this alert is lost to the ladder.
calls = {"n": 0}
def counting_s1(*a, **k):
    calls["n"] += 1
    return " ".join(["Revenue"] * 71) + " grew. What does this mean for investors?"
ap.generate_s1 = counting_s1
ap.generate_s3 = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("ladder was burned on a trimmable rhetorical ending"))

summary, attempts = ap.summarise("Apple Inc.", "x" * 3000, filing_type="NEWS",
                                 filing_id="f1", ticker="AAPL")
assert summary is not None, "a trimmable summary must be sent, not flagged"
assert not summary.endswith("?")
assert attempts == 1, f"expected 1 attempt, got {attempts}"
assert calls["n"] == 1
print(f"trimmed and accepted in {attempts} attempt, 0 extra LLM calls ✓")

# ── 3. A thin source gets a floor it can honestly meet ─────────────────────
# "HB Wealth Management reduced its AMD stake by 5.6%" cannot become 70 honest
# words. Demanding it deadlocked: prompts forbid padding, gate demands 70,
# result was FLAGGED/NOT SENT forever.
assert ap.effective_min_words("x" * 300) == 35
assert ap.effective_min_words("x" * 1000) == 50
assert ap.effective_min_words("x" * 5000) == ap.MIN_WORDS == 70
print("word floor scales to source length ✓")

short_summary = " ".join(["Stake"] * 40) + " was reduced."
assert ap.classify_failure(short_summary, 75, 70) == "too_short"   # old behaviour
assert ap.classify_failure(short_summary, 75, 35) is None          # thin source passes
print("a 40-word summary of a 2-line source now passes instead of being lost ✓")

ap.generate_s1 = lambda *a, **k: " ".join(["Stake"] * 40) + " was reduced."
ap.generate_s3 = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("ladder burned on a source that cannot support the floor"))
summary, attempts = ap.summarise("AMD", "short news blurb.", filing_type="NEWS",
                                 filing_id="f2", ticker="AMD")
assert summary is not None, "thin-source alert was flagged instead of sent"
print(f"thin-source alert delivered in {attempts} attempt ✓")

# ── 4. Genuinely bad summaries are still rejected ──────────────────────────
assert ap.classify_failure("This article discusses " + " ".join(["x"] * 80), 100, 35) == "bad_start"
assert ap.classify_failure(" ".join(["x"] * 200), 100, 35) == "too_long"
assert ap.classify_failure("", 100, 35) == "empty"
# A summary that is ONLY a rhetorical question trims to nothing -> stays a failure.
assert ap.strip_rhetorical_ending("Is this the future?") == ""
print("real quality failures still rejected ✓")

# ── 5. Tabloid headline verbs are rejected, not just "clickbait" in the abstract ──
# Production sent "Meta stock pops after unveiling AI agent Muse" -- "pops" slipped
# past every existing gate because none of them checked for movement slang.
clickbait_summary = " ".join(["Meta"] * 30) + " stock pops after unveiling AI agent Muse."
assert ap.classify_failure(clickbait_summary, 100, 35) == "clickbait_language"
neutral_summary = " ".join(["Meta"] * 30) + " shares rose 2.4% after unveiling AI agent Muse."
assert ap.classify_failure(neutral_summary, 100, 35) is None
for word in ("soars", "tanks", "plunges", "skyrockets", "surges", "craters"):
    assert ap.contains_clickbait_language(f"The stock {word} today.") is True, word
print("tabloid movement verbs (pops/soars/tanks/...) are rejected ✓")

print("\n✅ SUMMARY GATE TEST PASS")
