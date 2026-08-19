"""
Prompt_V3_RelevanceCheck.py — the noise gate.

WHY THIS WAS REWRITTEN
----------------------
The old prompt asked "Is this text relevant to {company}? Or is {company} just
mentioned in passing?" and defined relevant as "about company's operations,
financials, strategy, products, executives, events". That bar is so low that
essentially every article naming the company passes, because almost anything
touches one of those six words. Observed survivors:

  * "3 AI Networking Stocks Quietly Dominating Their Niche in August" (AVGO)
  * "There Is So Much Alpha To Be Harvested In Micron's Options" (MU)
  * "Nvidia: Upgrade To Strong Buy On Elongation Of GPU Cycle" (NVDA)

None of these report an EVENT. They are roundups, options commentary, and one
analyst's opinion — the exact category a holder does not need pushed to their
phone at 07:46. They passed the gate, consumed a full summarisation ladder, and
arrived as alerts, which is why alerts felt like they were arriving every minute.

THE BAR THIS ENFORCES
---------------------
Two questions, both of which must be yes:

  1. Is this company the SUBJECT of the story, not an example inside it?
  2. Did something actually HAPPEN — a discrete, verifiable event with a date —
     as opposed to someone offering a view about what might happen?

Opinion, ranking, screening, technical-analysis and options-strategy content
fails (2) by construction: nothing happened, someone just published a take. An
analyst RATING CHANGE is a real event and passes; an analyst's ARGUMENT for why
a stock is cheap is not.

This runs BEFORE summarisation in ai_pipeline, so a rejected item costs one
cheap call instead of a full ladder.
"""

# Headline shapes that are never a company event. Matched case-insensitively
# against the title only — cheap, deterministic, and it spends no tokens.
# Deliberately narrow: each pattern is a form that cannot describe something
# that happened to one company on one day.
LISTICLE_PATTERNS = (
    r"^\d+\s+(best|top|reasons|things|stocks|ways|charts)\b",
    # "3 AI Networking Stocks ...", "5 Beaten-Down Chip Stocks ..." — the count
    # and the plural noun can be separated by a few words of qualifier.
    r"^\d+\s+[\w\s'’/-]{0,40}?\b(stocks|shares|companies|names|picks|etfs)\b",
    r"\b(top|best)\s+\d+\s+stocks\b",
    r"\bstocks?\s+to\s+(buy|watch|avoid|sell)\b",
    r"\bhere'?s\s+why\b",
    r"\bwhy\s+(you|i|investors?)\s+should\b",
    r"\bis\s+it\s+time\s+to\b",
    r"\bshould\s+you\s+(buy|sell|own)\b",
    r"\b(prediction|forecast)s?\s+for\s+20\d\d\b",
    r"\bbetter\s+buy\b",
    r"\bmy\s+top\s+pick\b",
    r"\balpha\s+to\s+be\s+harvested\b",
    r"\boptions?\s+(play|strategy|trade)\b",
)


def is_listicle(title):
    """True when the headline shape alone proves this is not a company event."""
    import re
    t = (title or "").strip().lower()
    if not t:
        return False
    return any(re.search(p, t) for p in LISTICLE_PATTERNS)


def get_prompt(company_name, text, title=""):
    headline = f"Headline: {title}\n\n" if title else ""
    return f"""You are the noise filter for a professional alert service. A push notification interrupts someone. Most financial articles do not deserve to.

Decide whether this item is worth alerting a holder of {company_name}.

Both tests must pass.

TEST 1 — IS {company_name} THE SUBJECT?
Pass: the story is about this company. It is what the headline is about.
Fail: the company is one example among several, appears in a roundup or screen,
      is used as a comparison or benchmark, or is named only for context in a
      story about a rival, a sector, an index, or the market as a whole.

TEST 2 — DID SOMETHING HAPPEN?
Pass: a discrete, verifiable event. Results reported, guidance given, a deal
      signed, a filing made, an executive appointed or departed, a product
      recalled, a lawsuit filed or settled, a regulator acting, a rating or
      price target formally changed, a dividend or buyback declared, an insider
      transacting, a plant or programme opened or closed.
Fail: nobody did anything and someone published a view. This includes bull and
      bear cases, valuation arguments, technical analysis, chart setups, options
      strategies, "what to expect" previews, anniversary and milestone pieces,
      rankings, screens, and general market commentary that happens to name the
      company. A writer's opinion is not an event, however confidently argued.

DECISION RULES
1. A headline framed as a question, a ranking, or advice is almost always a Fail
   on Test 2.
2. "Analyst upgrades {company_name} to Buy" is an event — pass. "Why
   {company_name} is a buy at these levels" is an argument — fail.
3. Stock price movement alone is not an event unless the item attributes it to a
   specific disclosed cause.
4. If the item would still be accurate published a week earlier or a week later,
   nothing happened. Fail it.
5. When genuinely unsure, fail it. A missed story costs less than an interruption
   that teaches the user to ignore alerts.

{headline}Text:
{text[:2000]}

Respond with JSON only:
{{"is_relevant": true or false, "reason": "one short clause naming which test failed, or the event if it passed"}}"""


if __name__ == "__main__":
    for t in ["3 AI Networking Stocks Quietly Dominating Their Niche in August",
              "There Is So Much Alpha To Be Harvested In Micron's Options",
              "Should You Buy Nvidia Before August 27?",
              "Apple overhauls EU App Store fees after regulator ruling",
              "Meta names new CFO effective October 1"]:
        print(f"{'LISTICLE' if is_listicle(t) else 'pass-to-LLM':>12}  {t}")
