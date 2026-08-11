"""Prompt_S1T_TranscriptSummarization.py — Summarize earnings call transcripts"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=75, min_word_count=70):
    return f"""Summarize this earnings call transcript from {company_name} in exactly {target_word_count} words.
Highlight key takeaways, guidance, surprises, and management outlook.
Minimum {min_word_count} words, maximum {target_word_count} words.

{sub_summary or ""}

Transcript:
{raw_text}

Return only the summary."""
