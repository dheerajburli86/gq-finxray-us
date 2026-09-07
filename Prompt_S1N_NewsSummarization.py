"""Prompt_S1N_NewsSummarization.py — Summarize news articles"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=120, min_word_count=100):
    return f"""You are a financial news analyst. Summarize this news article about {company_name}.

Target: exactly {target_word_count} words
Minimum: {min_word_count} words (never shorter)
Maximum: {target_word_count} words

CRITICAL RULES:
- Include the most important facts: what happened, why it matters, financial impact if relevant
- Include specific numbers, percentages, dates if present in the article
- Must end with complete sentence (period, exclamation, or question mark)
- Never start with filler: "This article", "The following", "Summary:", "Note:", "According to"
- Plain English, neutral and factual, no personal opinion
- Every word must carry real information — no padding or repetition
- If word count cannot be exact, come as close as possible while staying accurate

{sub_summary or ""}

ARTICLE:
{raw_text}

Return only the summary. No preamble, no explanation, no meta-commentary."""
