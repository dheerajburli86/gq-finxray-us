"""Prompt_S1T_TranscriptSummarization.py — Summarize earnings call transcripts"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=150, min_word_count=120):
    return f"""You are a financial analyst. Summarize this earnings call transcript from {company_name}.

Target: exactly {target_word_count} words
Minimum: {min_word_count} words (never shorter)
Maximum: {target_word_count} words

KEY TAKEAWAYS TO COVER:
- Earnings results: revenue, earnings per share, guidance vs expectations
- Business segment performance: growth areas, headwinds, margin trends
- Forward guidance: what management says about the next quarter and year
- Key strategic moves: acquisitions, divestitures, partnerships, product launches
- Management tone: optimistic, cautious, or under pressure
- Analyst surprises: what was unexpected or concerning

STRUCTURE:
- Lead with headline earnings and guidance
- Include specific numbers: revenue, EPS, growth %, margins
- Key risks or tailwinds management mentioned
- One key quote if exceptionally important

MUST FOLLOW:
- Exactly {target_word_count} words if possible; never below {min_word_count}
- End with complete sentence (period, exclamation, or question mark)
- Never start with: "This transcript", "The following", "Summary:", "Management noted"
- Plain English, neutral factual tone, no personal commentary
- Every word conveys real information; no filler or repetition
- If exact word count impossible, come as close as possible while staying accurate

{sub_summary or ""}

TRANSCRIPT:
{raw_text}

Return only the summary. No preamble, no explanation."""
