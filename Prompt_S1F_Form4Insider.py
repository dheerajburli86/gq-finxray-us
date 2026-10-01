"""Prompt_S1F_Form4Insider.py — Summarize insider transactions (SEC Form 4 / FMP)"""


def get_prompt(company_name, raw_text, target_word_count=30, min_word_count=20):
    """
    Insider-transaction summary prompt.

    An insider filing is a handful of structured facts — who, which direction,
    how many shares, at what price, on what date, holding what afterwards. There
    is no honest long-form summary of that, so this asks for a tight factual
    extraction rather than the 150-word institutional treatment the other S.1
    prompts request.

    THE TARGETS ARE PASSED IN, NOT HARDCODED. This used to fix the request at
    "30 words or fewer, minimum 15" while ai_pipeline's quality gate judged the
    result against a floor derived separately from the source length — up to 70
    words. The two never agreed, so the model was being instructed to write
    something the gate was guaranteed to reject, and every insider alert burned
    the full retry ladder before being flagged unsent. Caller and prompt now
    share one number.

    Args:
        company_name: issuer name or ticker
        raw_text: Form 4 XML / parsed transaction block (truncated below)
        target_word_count: hard ceiling, supplied by ai_pipeline.ladder_for()
        min_word_count: hard floor, supplied by the same call
    """
    return f"""Extract the insider transaction disclosed in this filing for {company_name}.

Write between {min_word_count} and {target_word_count} words. Plain prose, one
sentence per transaction.

REPORT EVERY ONE OF THESE THAT THE FILING CONTAINS:
- Insider name and their role or title
- Direction: purchase, sale, option exercise, award, or disposal
- Number of shares
- Price per share
- Transaction date
- Shares held after the transaction

CARRY EVERY NUMBER. Share counts, prices, dates and post-transaction holdings
must appear with their units intact — those figures are the entire content of an
insider alert, and a summary that drops them has said nothing. If the filing
records several transactions, report each one; do not summarise them as "several
trades". If a field genuinely is not disclosed, omit it silently rather than
writing "unknown" or "N/A".

TONE:
- Neutral and factual. State the transaction, never characterise it.
- No judgement words: significant, notable, large, aggressive, bullish, bearish,
  confident, worrying. A share count is the magnitude; do not editorialise it.
- Never infer motive or predict consequences. "The CFO sold 12,000 shares" is the
  alert; "signalling a loss of confidence" is not.
- No tabloid verbs (dumped, offloaded, snapped up, unloaded, piled into).
- Do not open with "This filing", "The following", "Summary:" or similar.
- End with a complete sentence and a period. Never a question or exclamation.

FILING TEXT:
{raw_text[:4000]}

Return only the transaction summary. No preamble."""
