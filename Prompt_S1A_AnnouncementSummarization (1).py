"""Prompt_S1A_AnnouncementSummarization.py — Summarize SEC filings"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=120, min_word_count=100):
    return f"""You are a financial analyst. Summarize this SEC filing from {company_name}.

Target: exactly {target_word_count} words
Minimum: {min_word_count} words (never shorter)
Maximum: {target_word_count} words

KEY PRIORITIES:
- Lead with what happened: the material event, transaction, or announcement
- Include financial impact: dollar amounts, percentages, affected units
- Explain why it matters: strategic implications, market impact, risk factors
- Specific dates, parties, terms if material to the news
- No SEC boilerplate, no legal disclaimers, no repeat of submission format

MUST FOLLOW:
- Exactly {target_word_count} words if possible; never below {min_word_count}
- End with complete sentence (period, exclamation, or question mark)
- Never start with: "This filing", "The following", "Summary:", "Note:", "This document"
- Plain English, neutral factual tone, no personal opinion
- Every word must convey real information; no filler or padding
- If exact word count impossible, come as close as possible while staying accurate

{sub_summary or ""}

FILING:
{raw_text}

Return only the summary. No preamble, no explanation."""
