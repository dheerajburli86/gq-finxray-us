# Updated Summarization Prompts — 2026-09-09

## Changes Summary

### Word Count Targets (in ai_pipeline.py)
```python
# OLD VALUES:
STARTING_TARGET = 75
TARGET_STEP = 5
MAX_TARGET = 100

# NEW VALUES:
STARTING_TARGET = 150      # Increased from 75 (room for comprehensive coverage)
TARGET_STEP = 10           # Increased from 5 (larger jumps during retries)
MAX_TARGET = 250           # Increased from 100 (supports detailed summaries)
```

### Three Prompt Files Updated

1. **Prompt_S1N_NewsSummarization.py** (News articles)
   - Default targets: 150 words (was 120)
   - Minimum: 120 words (was 100)
   - Added: Professional tone, comprehensive coverage requirements
   - Added: Explicit guidance against clickbait language

2. **Prompt_S1A_AnnouncementSummarization.py** (SEC filings)
   - Default targets: 150 words (was 120)
   - Minimum: 120 words (was 100)
   - Added: Professional tone, strategic significance requirements
   - Added: Complete financial impact requirements

3. **Prompt_S1T_TranscriptSummarization.py** (Earnings calls)
   - Default targets: 150 words (was 120)
   - Minimum: 120 words (was 100)
   - Added: Professional tone, segment-by-segment breakdown requirements
   - Added: Distinction between facts and management projections

### S.3 Prompt (Resummarize)
Located in ai_pipeline.py, lines 462-482, also updated with:
- Professional tone guidance
- Comprehensive coverage requirements
- Explicit instructions against padding/filler
- Better structure for longer summaries

## Key Improvements

✅ **Word Count**: 75→150 starting (2x more room for detail)
✅ **Tone**: Formal/institutional (no clickbait)
✅ **Comprehensiveness**: Explicit requirement to cover ALL major points
✅ **Accuracy**: Better guidance on facts vs. speculation
✅ **Financial Details**: Specific numbers, percentages, timeframes required
✅ **Professionalism**: Language suitable for portfolio managers

## Implementation

These files are ready to deploy:
1. Replace the three Prompt_S1*.py files in your repo
2. Update ai_pipeline.py with new STARTING_TARGET/MAX_TARGET values
3. Redeploy to production

All changes are backward compatible — existing code will work with new prompts.
