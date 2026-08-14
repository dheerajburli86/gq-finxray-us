"""
Prompt_C1_ImpactClassification.py
Classifies alert impact as HIGH / MEDIUM / LOW.

WHY THIS WAS REWRITTEN
----------------------
71% of every alert in the system (389 of 544 over 48h) was classified HIGH, so
the field carried no information and the user's MEDIUM impact floor filtered
almost nothing. Two causes, both addressed here:

* **The old prompt was written for news, not for financial statements.** Its
  HIGH examples were all catastrophes -- bankruptcy, breach, FTC lawsuit,
  revenue down >20%. It offered no category at all for a routine quarterly
  result, so when Feature 3 handed it "record revenue, up 26%, $28.24 billion"
  the only reachable conclusion was "major financial news, therefore HIGH".
  Every single quarterly result graded HIGH.

* **Scale was being confused with surprise.** A large, healthy, entirely
  expected quarter from a mega-cap is not a high-impact event; it is the single
  most predictable thing a public company does. HIGH now requires a *surprise*
  or a genuine threat -- something that should change a holder's thinking --
  not merely a big number.

FINANCE-ONLY BAR
----------------
Anything without direct financial materiality to the company -- product
launches, marketing, sponsorships, conference appearances, survey mentions,
non-executive personnel -- is LOW. With the default MEDIUM floor in delivery.py
that means it is classified, stored and never sent, rather than being dropped
silently upstream where it could not be audited.
"""

def get_prompt(company_name, summary, current_date):
    """
    Return a prompt that classifies impact as HIGH/MEDIUM/LOW.

    Args:
        company_name: Name of the company
        summary: The summarized alert text
        current_date: Today's date in YYYY-MM-DD format

    Returns:
        A string prompt to pass to the LLM
    """

    return f"""You are a sell-side analyst triaging alerts for a portfolio manager who already holds {company_name}. They see hundreds of alerts a day. Your job is to protect their attention.

Ask one question: does this change what a holder should think or do?

If the answer is "no, this is roughly what anyone would have expected", it is not HIGH -- no matter how large the numbers are.

IMPACT LEVELS

HIGH -- a genuine surprise, or a threat to the business. Reserved for things that move a stock or change a thesis:
- Earnings or revenue that clearly missed or beat consensus by a wide margin
- Revenue, EPS or margin down more than 15% year over year
- A loss where the market expected a profit
- Guidance cut, withdrawn, or raised materially
- Dividend cut or suspended; buyback halted
- Bankruptcy, restructuring, going-concern doubt, debt default or covenant breach
- Merger, acquisition, spin-off or major divestiture involving this company
- Regulatory or legal action with material financial exposure
- Critical security breach or data incident with business consequences
- CEO or CFO departing abruptly or under adverse circumstances

MEDIUM -- financially material, but expected or routine in nature:
- A quarterly or annual result broadly in line with expectations, INCLUDING one showing solid growth. This is the default for a normal earnings report.
- Planned CEO/CFO succession, or other C-suite change
- Analyst rating change or price-target revision
- Dividend initiated or raised; new buyback authorised
- Regulatory inquiry or investigation opened, without quantified exposure
- Large insider transaction, or a block trade above $200M
- Debt raise, refinancing or credit-rating change

LOW -- not financially material to this company:
- Product launches, feature releases, partnerships, integrations
- Marketing, sponsorship, branding, awards, conference appearances
- Personnel below C-suite level
- Industry commentary, survey mentions, opinion, or listicles
- Passing mentions where {company_name} is not the subject
- Routine scheduling notices, such as a reminder that earnings are upcoming

DECISION RULES

1. Scale is not surprise. "$28 billion in revenue, up 26%" from a large company is MEDIUM unless the summary itself says it beat or missed expectations, or shows a decline over 15%.
2. If the summary does not compare against an expectation, consensus, or a prior period, you cannot call it a surprise. Default to MEDIUM.
3. If the content is not financially material to {company_name}, choose LOW. Do not inflate it because the company is well known.
4. Growth alone is not HIGH. Deterioration alone is not HIGH unless it exceeds the thresholds above.
5. When genuinely torn between two levels, choose the lower one.

Summary to classify:
{summary}

Today's date: {current_date}

Respond with JSON only:
{{
    "impact": "HIGH" or "MEDIUM" or "LOW",
    "reason": "Brief explanation (1 sentence), naming the specific fact that set the level"
}}"""


if __name__ == "__main__":
    print(get_prompt(
        "Tesla, Inc.",
        "Tesla, Inc. announced strong Q2 FY2026 financial results, achieving "
        "$28.24 billion in revenue, a 26.1% increase year over year.",
        "2026-08-14",
    ))
