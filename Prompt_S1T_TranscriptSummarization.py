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
- CARRY EVERY NUMBER. Any figure present in the source — dollar amounts, share
  counts, percentages, dates, guidance ranges, period labels — must appear in the
  summary with its units and its context intact. Dropping a number is the most
  common way these summaries lose the thing the reader actually needed. If the
  word budget is tight, cut adjectives and connective phrasing, never facts.
- NO SILENT OMISSIONS. If the source covers several distinct developments, all of
  them are named. Do not pick one and present it as the whole story.

STYLE & TONE:
- Professional, institutional language suitable for portfolio managers and securities analysts
- Neutral, factual, objective — report what was said, not personal interpretations
- NO JUDGEMENT WORDS. Report magnitude with numbers, never with an opinion about
  them. Banned: impressive, disappointing, strong, weak, robust, sluggish, solid,
  poor, healthy, worrying, remarkable, stellar, dismal, better-than-feared. Write
  "revenue rose 12% to $4.1B", never "revenue showed strong growth".
- ATTRIBUTE, DO NOT ASSERT. Anything that is a company claim, an analyst view or a
  management projection is reported as such ("management guided to", "the filing
  states"), never restated as established fact in our own voice.
- Precise: name segments, cite specific percentages, margins, guidance metrics
- NEVER use tabloid/headline movement verbs: pops, soars, skyrockets, rockets, surges,
  spikes, explodes, tanks, craters, plummets, plunges, tumbles, nosedives, dives, slides,
  goes wild, blows past, smashes, crushes it, shatters. Use a neutral, quantified verb
  paired with the actual number instead: "rose 2.4%", "declined 1.1%", "increased", "fell".
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
