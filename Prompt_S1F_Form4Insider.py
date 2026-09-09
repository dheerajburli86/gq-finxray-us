"""Prompt_S1F_Form4Insider.py — Summarize SEC Form 4 (insider trading)"""

def get_prompt(company_name, raw_text, target_word_count=30, min_word_count=15):
    """
    Generate Form 4 (insider trading) summary prompt.

    Form 4 filings are inherently brief: insider name, trade type, share count, price, date.
    No need for comprehensive coverage or institutional tone — just facts.

    Args:
        company_name: Company ticker or name
        raw_text: Full Form 4 XML/text (will be truncated)
        target_word_count: 30 words (not 150 — Form 4s are short)
        min_word_count: 15 words minimum

    Returns:
        Prompt string ready for LLM call
    """
    return f"""Extract the insider trading information from this Form 4 filing for {company_name}.

Output EXACTLY {target_word_count} words or fewer (minimum {min_word_count} words).

Extract and report:
- Insider name and title
- Transaction type (sale or purchase)
- Number of shares
- Price per share (if available)
- Transaction date
- Resulting ownership percentage (if available)

Format: Plain English, one sentence per transaction. Be concise — no padding.

FILING TEXT:
{raw_text[:4000]}

Return only the extracted trading information. No preamble."""
