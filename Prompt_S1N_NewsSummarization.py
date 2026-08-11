"""Prompt_S1N_NewsSummarization.py — Summarize news articles"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=75, min_word_count=70):
    return f"""Summarize this news article about {company_name} in exactly {target_word_count} words.
Rules: Must end with complete sentence. No padding. No "This article", "The following", "Summary:".
Minimum {min_word_count} words, maximum {target_word_count} words.

{sub_summary or ""}

Article:
{raw_text}

Return only the summary."""
