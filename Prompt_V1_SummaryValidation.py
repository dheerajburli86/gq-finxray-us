"""Prompt_V1_SummaryValidation.py — Validate summary quality"""

def get_prompt(summary):
    return f"""Review this summary for quality issues. Respond with JSON only:
{{"issues_detected": true/false, "corrected_summary": "improved version or null"}}

Summary to review:
{summary}"""
