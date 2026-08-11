"""Prompt_H1_HeadlineGeneration.py — Generate scannable headlines"""

def get_prompt(company_name, summary, filing_type=""):
    return f"""Generate a short, scannable headline (2-14 words) for this alert.
Company: {company_name}
Type: {filing_type or "news"}

Summary:
{summary}

Respond with JSON only:
{{"headline": "Your headline here"}}"""
