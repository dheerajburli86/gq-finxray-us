"""Prompt_S1A_AnnouncementSummarization.py — Summarize SEC filings"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=75, min_word_count=70):
    return f"""Summarize this SEC filing from {company_name} in exactly {target_word_count} words.
Focus on what happened, why it matters, and key numbers. No boilerplate.
Minimum {min_word_count} words, maximum {target_word_count} words.

{sub_summary or ""}

Filing:
{raw_text}

Return only the summary."""
