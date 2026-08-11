"""Prompt_V3_RelevanceCheck.py — Filter non-actionable content"""

def get_prompt(company_name, text):
    return f"""Is this text relevant to {company_name}? Or is {company_name} just mentioned in passing?
Relevant = about company's operations, financials, strategy, products, executives, events.
Not relevant = {company_name} mentioned in survey, market commentary, or as a comparison.

Respond with JSON only:
{{"is_relevant": true/false}}

Text:
{text[:2000]}"""
