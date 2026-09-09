"""Prompt_S1N_NewsSummarization.py — Summarize news articles"""

def get_prompt(company_name, sub_summary, raw_text, target_word_count=150, min_word_count=120):
    return f"""You are a professional financial news analyst. Summarize this news article about {company_name} in a formal, institutional tone suitable for investment professionals.

Target: exactly {target_word_count} words
Minimum: {min_word_count} words (never shorter)
Maximum: {target_word_count} words

CONTENT REQUIREMENTS:
- Cover all major developments: what happened, quantified impact, why investors should care
- Include every material fact: specific numbers, percentages, valuations, timeframes, guidance
- Explain implications: business impact, competitive positioning, financial consequences
- Name key parties, products, markets, and financial metrics mentioned
- Ensure the summary is comprehensive — assume the reader will not see the original article

STYLE & TONE:
- Professional, institutional, formal language (as if written for a financial analyst or portfolio manager)
- Neutral, factual, objective — no emotional language, no clickbait phrases
- Never speculate, opine, or suggest interpretation beyond what's explicitly stated
- Avoid headlines like "X is concerned" or "X signals" — state facts, not implications
- Cannot end with questions, exclamations, or speculation about future outcomes
- Never start with filler: "This article", "The following", "Summary:", "Note:", "According to"
- Every word must convey real information — no padding, no repetition, no filler
- If word count cannot be exact, come as close as possible while staying accurate and comprehensive

{sub_summary or ""}

ARTICLE:
{raw_text}

Return only the summary. No preamble, no explanation, no meta-commentary."""
