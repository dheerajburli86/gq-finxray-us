"""Prompt_V1_SummaryValidation.py — Validate summary quality"""

def get_prompt(summary):
    return f"""Review this summary for quality issues. Flag issues_detected=true if ANY of these are present:
- Ends with a question mark or exclamation point instead of a period
- Ends with a rhetorical question, speculation, or a sentence asking what will happen next
- States an opinion, prediction, or interpretation instead of only reported facts
- Takes a stance, editorializes, or is not neutral

If issues_detected is true, provide corrected_summary: the same facts, rewritten to be strictly
neutral and factual, ending in a period with no rhetorical question or speculation.

Respond with JSON only:
{{"issues_detected": true/false, "corrected_summary": "improved version or null"}}

Summary to review:
{summary}"""
