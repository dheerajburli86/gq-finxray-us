def get_prompt(company_name, summary, filing_type=""):
    """
    H.1 — Headline generation.

    Every alert in the India system leads with a scannable title
    ("Bank Addresses Lawsuits, Governance, and Post-Merger Outlook") above the
    paragraph. The US alerts had no equivalent and opened straight into the
    summary, which reads as a wall of text on a phone. This generates that
    title from the already-validated summary, so the headline can never assert
    anything the summary does not.
    """

    prompt = f"""<INST>

    You are a financial news editor. Write a single headline for the summary below, about {company_name}.

    Rules:
    1. Between 4 and 10 words. Title Case.
    2. State the specific development. "Q3 Revenue Beats On Cloud Growth" — not "Company Reports Results".
    3. Use ONLY facts that appear in the summary. Do not add, infer, or extrapolate anything.
    4. Do NOT include the company name or ticker — the alert already displays them above the headline.
    5. No ending punctuation. No quotation marks. No colons.
    6. Neutral and factual. Never hype ("Soars", "Plunges", "Shocking", "Massive"), never advice
       ("Buy", "Sell", "Time To Act"), never a question.
    7. If the summary describes a routine administrative event with no substance, say so plainly
       rather than inflating it.

    Filing type: {filing_type}

    Summary:
    {summary}

    Output JSON with a single key 'headline'. Example: {{"headline": "Q3 Revenue Beats On Cloud Growth"}}

    Return only the JSON. Nothing else.

    </INST>"""

    return prompt
