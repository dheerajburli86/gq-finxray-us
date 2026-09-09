"""
Prompt_S3_Resummarize.py — S.3 Resummarization Prompt

This is the retry prompt used when initial summarization fails the gates.
Used for all filing types (NEWS, EARNINGS_TRANSCRIPT, and SEC filings).

Located in ai_pipeline.py as generate_s3() function (lines 462-482)
Included here for reference and archival.
"""

def generate_s3_prompt(company_name, raw_text, target_words, min_words, char_limit):
    """
    Generate S.3 resummarization prompt with escalated word targets.

    Args:
        company_name: Company name for context
        raw_text: Full text to summarize (will be truncated to char_limit)
        target_words: Escalated target word count (e.g., 80, 85, 90, 95, 100, ...)
        min_words: Minimum word count floor
        char_limit: Character limit for source text (NEWS_CHAR_LIMIT or TRANSCRIPT_CHAR_LIMIT)

    Returns:
        Prompt string ready for LLM call
    """
    return f"""You are a professional financial analyst. Write a comprehensive, formal summary of the following content in exactly {target_words} words.

CONTENT REQUIREMENTS:
- Cover all major developments and material facts from the content
- Include quantified impacts: specific numbers, percentages, amounts, timeframes
- Explain strategic significance and why investors should care
- Ensure comprehensive coverage that stands alone without reference to the original

STYLE & TONE:
- Professional, institutional tone suitable for investment professionals
- Neutral, objective, factual — no editorializing, speculation, or emotional language
- Precise: name parties, specific products, markets, financial metrics
- Never speculate about future outcomes

WRITING RULES:
- Write exactly {target_words} words. If exact {target_words} is impossible while staying strictly accurate, come as close as possible, but never fewer than {min_words} and never more than {target_words}.
- Do not pad with filler phrases, restated facts, or generic commentary — every word must carry real information.
- Must end with a complete factual sentence ending in a period. Never end with a question mark or exclamation point.
- Never end with a rhetorical question, speculation, or a sentence asking what happens next.
- Do not start with "This", "The following", "Summary:", "Note:" or similar
- Plain English only, neutral and factual, no first person

Company: {company_name}

Content:
{raw_text[:char_limit]}

Return only the summary. Nothing else."""


# USAGE IN ai_pipeline.py:
# prompt = generate_s3(company_name, raw_text, target_words, filing_type, min_words)
# call_deepinfra(prompt, max_tokens=700)

# ESCALATION LADDER (in summarise() function):
# Starts at STARTING_TARGET (150), escalates by TARGET_STEP (10) each retry
# Sequence: 150, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250
# Stops at MAX_TARGET (250) or after all gates pass
