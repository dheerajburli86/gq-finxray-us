"""
Prompt_C1_ImpactClassification.py
Classifies alert impact as HIGH / MEDIUM / LOW.

CHANGES IN THIS REVISION
------------------------
* **Financial deterioration:** Cash flow decline, revenue drops, losses → MEDIUM+
* **Regulatory/Legal:** FTC/SEC/regulatory lawsuits, data breach actions → MEDIUM+
* **Security incidents:** Hacks, data sharing with adversaries, breaches → MEDIUM+
* **Price movements:** Stock splits, dividend announcements → MEDIUM
* **Executive changes:** CEO departures, major hire/departures → MEDIUM
* **Product/partnerships:** New products, partnerships, minor news → LOW
* **Block trades:** $200M+ threshold maintained for conviction signals
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

    return f"""You are a financial analyst. Classify the impact level of this market-moving event for {company_name}.

Impact Definitions:
- HIGH: Company-threatening, major financial deterioration, significant legal action, critical security breach
- MEDIUM: Notable developments, moderate financial changes, regulatory action, industry shifts, executive changes
- LOW: Product launches, partnerships, minor announcements, routine updates

Examples of HIGH impact:
- Cash flow plummeting 91% in a quarter
- Major lawsuit from FTC/SEC with data sharing violations
- Critical security breach or major hacking incident
- Revenue decline >20% YoY
- Bankruptcy/restructuring news

Examples of MEDIUM impact:
- CEO departure or major C-suite change
- New strategic partnership or acquisition
- Stock split or dividend announcement
- Analyst rating changes
- Product launch or new service
- FTC investigation or regulatory scrutiny
- Data practices under review
- $200M+ block trade (conviction signal)

Examples of LOW impact:
- Mentioned in survey or industry report
- Minor partnership or sponsorship
- Routine earnings date reminder
- Personnel announcements (non-executive)
- Press release about marketing campaign
- Industry commentary or analyst commentary

Summary to classify:
{summary}

Today's date: {current_date}

Respond with JSON only:
{{
    "impact": "HIGH" or "MEDIUM" or "LOW",
    "reason": "Brief explanation (1 sentence)"
}}"""


if __name__ == "__main__":
    # Test
    prompt = get_prompt(
        "Meta Platforms",
        "Meta's cash generation plummeted 91% as AI buildout costs soar.",
        "2026-08-11"
    )
    print(prompt)
