"""Prompt_V2_SimilarityCheck.py — Semantic deduplication"""

def get_prompt(old_summary, new_summary):
    return f"""Are these two summaries semantically similar (saying the same thing)?
Respond with JSON only:
{{"is_similar": true/false}}

Old summary:
{old_summary}

New summary:
{new_summary}"""
