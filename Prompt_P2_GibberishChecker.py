"""Prompt_P2_GibberishChecker.py — Reject gibberish/lorem ipsum output"""

def get_prompt(text):
    return f"""Is this text gibberish, lorem ipsum, or nonsensical? Respond with JSON only:
{{"is_gibberish": true/false}}

Text:
{text[:2000]}"""
