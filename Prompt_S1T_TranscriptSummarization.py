"""Prompt_S1T_TranscriptSummarization.py — Summarize earnings call transcripts"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=150, min_word_count=120):
    return f"""You are a professional financial analyst. Summarize this earnings call transcript from {company_name} in a formal, institutional tone suitable for investment professionals.

Target: exactly {target_word_count} words
Minimum: {min_word_count} words (never shorter)
Maximum: {target_word_count} words

CONTENT REQUIREMENTS:
- Lead with headline results: revenue, earnings per share, guidance vs. expectations
- Include complete financial details: specific numbers, growth percentages, margin trends
- Cover each business segment: performance, headwinds, tailwinds, strategic moves
- Address forward guidance: management projections, confidence level, key drivers
- Flag risks or catalysts management emphasized
- Ensure comprehensive coverage — the reader should understand management's outlook without hearing the call

STYLE & TONE:
- Professional, institutional language suitable for portfolio managers and securities analysts
- Neutral, factual, objective — report what was said, not personal interpretations
- Precise: name segments, cite specific percentages, margins, guidance metrics
- Never speculate or editorialize; distinguish management's projections from historical facts

WRITING RULES:
- Write exactly {target_word_count} words if possible; never below {min_word_count}
- Do not pad with filler or repeat facts just to reach the word count — every word must convey real information
- Must end with a complete factual sentence, ending in a period. Never end with a question mark or exclamation point
- Never end with a rhetorical question, speculation, or a sentence asking what happens next
- Never start with: "This transcript", "The following", "Summary:", "Management noted"
- Plain English, neutral factual tone, no personal commentary, no stance-taking
- If exact word count impossible, come as close as possible while staying accurate and comprehensive

{sub_summary or ""}

TRANSCRIPT:
{raw_text}

Return only the summary. No preamble, no explanation."""
