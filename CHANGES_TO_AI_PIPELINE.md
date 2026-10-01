# Changes Required in ai_pipeline.py

## Line 52-54: Update Word Count Targets

### BEFORE:
```python
STARTING_TARGET = 75
TARGET_STEP = 5
MAX_TARGET = 100
```

### AFTER:
```python
STARTING_TARGET = 150  # Increased from 75 to allow comprehensive coverage
TARGET_STEP = 10       # Increased from 5 for larger jumps during retries
MAX_TARGET = 250       # Increased from 100 to support detailed summaries of complex stories
```

## Line 440-458: Update Prompts Calls (No changes needed to code structure)

These calls already exist and will automatically use the new word count targets:

```python
# Line 451-452: NEWS prompt
prompt = s1n_prompt(company_name, sub_summary, raw_text[:NEWS_CHAR_LIMIT],
                    target_word_count=STARTING_TARGET, min_word_count=min_words)

# Line 454-455: EARNINGS_TRANSCRIPT prompt
prompt = s1t_prompt(company_name, sub_summary, raw_text[:TRANSCRIPT_CHAR_LIMIT],
                     target_word_count=STARTING_TARGET, min_word_count=min_words)

# Line 457-458: SEC filing prompt
prompt = s1a_prompt(company_name, sub_summary, raw_text[:FILING_CHAR_LIMIT],
                     target_word_count=STARTING_TARGET, min_word_count=min_words)
```

The STARTING_TARGET variable will automatically propagate to all three prompts.

## Line 462-482: Update S.3 Resummarize Prompt

Replace the entire `generate_s3()` function with the new version that includes:
- Professional/formal tone guidance
- Comprehensive coverage requirements
- Better structured instructions

See `Prompt_S3_Resummarize.py` for the complete updated function.

---

## Summary of Impact

| Setting | Old | New | Impact |
|---------|-----|-----|--------|
| STARTING_TARGET | 75 | 150 | 2x more room for comprehensive summaries |
| TARGET_STEP | 5 | 10 | Faster escalation, fewer retry attempts |
| MAX_TARGET | 100 | 250 | Can handle complex multi-part stories |
| Default Min Words | 100 | 120 | Higher quality floor |

**Result**: News and SEC summaries will now be:
- ✅ 150+ words (was 75-100)
- ✅ Professional, institutional tone
- ✅ Comprehensive (all major points covered)
- ✅ Fact-based (no clickbait language)
- ✅ Specific numbers and metrics included
