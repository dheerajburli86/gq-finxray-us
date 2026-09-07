# GQ FinXray US — Data Collection Sources and Processing Summary (FMP + Massive)

Status: current as of 2026-07-27. Replaces EODHD and TwelveData everywhere in this codebase with FMP (Financial Modeling Prep) and Massive (formerly Polygon.io). SEC EDGAR, CNBC/Reuters/MarketWatch RSS, and DeepInfra (Gemini 2.5 Flash) are unchanged — they never depended on EODHD/TwelveData.

**Read `claude/us-market-data-licensing-risk.md` before scaling subscriber count.** Both FMP and Massive personal tiers prohibit redistribution to a multi-user product; this rewrite is a mechanical vendor swap, not a resolution of that licensing gap.

## The 11 features

Every alert this system sends is tagged with exactly one of these (`extra.feature_id` / `extra.feature_name` in Supabase, and a `🏷 Feature N/11` footer line on every Telegram message) so alert performance can be monitored feature-by-feature:

1. **SEC EDGAR Filings** — 8-K, 10-Q, 10-K, S-1, Form 4. `edgar_poller.py`. Unchanged — always free/direct from SEC, no vendor dependency.
2. **Company & Sector News** — ticker news via FMP (`/stable/stock-news`), broad market sweep via FMP (`/stable/general-news`), plus the existing CNBC/Reuters/MarketWatch/Nasdaq/IBD RSS aggregation in `news_poller.py` (unchanged). `fmp_poller.py` replaces `eodhd_poller.py`'s news half.
3. **Result Snapshot** — structured quarterly/annual financials triggered off 10-Q/10-K, now from FMP's flat `/stable/income-statement` list instead of EODHD's nested fundamentals payload. `result_snapshot.py`.
4. **Earnings Calendar Heads-Up** — 24h-ahead earnings date/time + EPS estimate for watchlisted tickers, via FMP `/stable/earnings-calendar`. Part of `fmp_poller.py`.
5. **Insider Transactions & Large Deals** — FMP `/stable/insider-trading/search` replaces EODHD's insider-transactions endpoint; the $1M+ bulk/block deal flag rides the same feed. Part of `fmp_poller.py`.
6. **Technical Alerts** — RSI overbought/oversold, 52-week high/low, volume spike, 200-SMA crossover. `technical_poller.py` replaces `eodhd_technical_poller.py`. Volume-spike detection now runs off Massive's whole-market snapshot (one call, ~10,000+ tickers) instead of EODHD's custom Screener API, which the old code's own comments documented as silently broken for 14+ days. RSI/SMA come from Massive's purpose-built indicator endpoints; 52-week high/low comes from FMP's quote endpoint (`yearHigh`/`yearLow` fields).
7. **ETF Flow Alerts** — institutional inflow/outflow signal from Massive's single-ticker snapshot, which returns today's volume AND prior session's volume in one call (EODHD needed two calls: real-time quote + a separate 20-day EOD pull). `etf_flow_poller.py`.
8. **IPO Deep Dive** — upcoming US IPO alerts from FMP `/stable/ipos-calendar`. `ipo_poller.py` replaces `eodhd_ipo_poller.py`.
9. **Sector Heatmap** — daily/weekly/monthly S&P 500 GICS sector ETF heatmap image, unchanged rendering (958px, 5 columns, 180px cells), data now from FMP quote (daily) and FMP historical-price-eod/full (weekly/monthly). `heatmap_generator.py`.
10. **News Roundup & ETF Xray** — morning/evening AI digest (DeepInfra, unchanged) + structured ETF fundamentals snapshot, now via FMP quote/etf-info instead of EODHD real-time/fundamentals. `news_roundup.py`.
11. **Earnings Call Transcripts — NEW.** EODHD never offered this at any tier (confirmed in the project's own Finnhub reference PDFs). FMP's Ultimate plan covers 8,000+ US companies, 10+ years of transcript history, via `/stable/earning-call-transcript`. `earnings_transcript_poller.py` watches for 10-Q/10-K filings on watchlisted tickers (same trigger `result_snapshot.py` uses) and queues the matching transcript into `raw_filings` with `filing_type=EARNINGS_TRANSCRIPT`, so it flows through the *exact same* AI pipeline as news and filings — no separate summarization path was built.

## The summarization flowchart (implemented, applies to every text alert)

This already existed in `ai_pipeline.py` before this rewrite and now also covers transcripts. End to end, for News (S.1.N), Announcements/filings (S.1.A), and Transcripts (S.1.T — new):

1. **Gibberish check** (`Prompt_P2_GibberishChecker`) — discard if the source text itself is garbage.
2. **Relevance check** (`Prompt_V3_RelevanceCheck`) — discard if not actually about the company.
3. **S.1 summarization** — real prompt for the content's class (S.1.N / S.1.A / S.1.T), target **75 words, floor 70**.
4. **Regex cleanup** — strip a leading "Summary:", collapse extra whitespace, strip parenthetical word-count mentions, capitalize the first letter.
5. **Number standardization** — `$ 150` → `$150`, `24 %` → `24%`, bare 4+ digit numbers get thousands separators (years 1900–2100 excluded).
6. **Quality gate** — word count in band, doesn't start with a banned phrase ("This content...", "The following...", etc.), doesn't end mid-sentence.
7. **If it fails:** retry via **S.3** (resummarize) with the ceiling raised by **+5 words** — 80, then 85, 90, 95, 100 — floor pinned at 70 the whole time. This is a real retry loop, not a single re-ask: verified in this session with a mock that always returns a too-short summary, and it correctly stepped through 75→80→85→90→95→100 before giving up.
8. **If it still fails at 100 words:** stop (no infinite loop burning tokens). Write the final attempt, full attempt log, ticker, company, failure reason, and now also `feature_id`/`feature_name`/`source`/`filing_type` to the Supabase `flagged_summaries` table (columns added in this session via migration `add_feature_tagging_to_flagged_summaries`). Nothing gets sent. This is where you'd go look and tell me it needs a code check, per your instructions.
9. **If it passes:** runs through **V.1 validation** (`Prompt_V1_SummaryValidation`) — catches incomplete sentences, dummy placeholder values, explicit word-count mentions, second-person instructions, first-person narration. If issues are found, the corrected summary is captured and re-checked against the same quality gate; if the correction still fails, it's flagged for review too rather than trusted blindly.
10. Then impact classification (`Prompt_C1_ImpactClassification`, HIGH/MEDIUM/LOW) and semantic deduplication against the ticker's last 10 summaries (`Prompt_V2_SimilarityCheck`) before an alert is finally stored.

## New workflow, end to end

```
SEC EDGAR (8-K/10-Q/10-K/S-1/Form 4)  ──┐
FMP news (ticker + market sweep)        │
CNBC/Reuters/MarketWatch RSS            ├──► raw_filings (status=PENDING)
FMP insider trading / bulk deals        │
FMP earnings call transcripts (NEW)   ──┘         │
                                                    ▼
                                          ai_pipeline.py (every 60s)
                                  gibberish → relevance → S.1/S.3 retry ladder
                                  → cleanup/standardize → V.1 validation
                                  → impact classification → dedup
                                                    │
                              PASS ──► alerts (feature-tagged) ──► Telegram (every 30s)
                              FAIL (100 words, still bad) ──► flagged_summaries (you review)

Technical alerts (Massive RSI/SMA + FMP 52w quote)     ──► alerts directly (templated, no LLM)
ETF flow (Massive single-ticker snapshot)               ──► alerts directly
IPO calendar (FMP)                                      ──► alerts directly
Result snapshot (FMP income statement)                  ──► alerts directly
Sector heatmap (FMP quote + historical)                 ──► Telegram image, daily/weekly/monthly
News roundup + ETF Xray (FMP quote/etf-info + DeepInfra) ──► Telegram, morning/evening/9am
Market open/close/premarket/midday/afterhours reports    ──► Telegram, FMP quotes for SPY/QQQ/DIA + commodities
```

Templated alerts (technical, ETF flow, IPO, result snapshot, heatmap) skip the LLM summarization pipeline entirely — they're structured data formatted directly into Markdown, same as before. Only free-text content (news, SEC filings/8-Ks, Form 4, S-1, and now earnings call transcripts) goes through the S.1/S.3/V.1 retry pipeline described above.

## Files replaced or added in this rewrite

| Old (EODHD/TwelveData) | New (FMP/Massive) |
|---|---|
| `eodhd_poller.py` | `fmp_poller.py` |
| `eodhd_technical_poller.py` | `technical_poller.py` |
| `eodhd_ipo_poller.py` | `ipo_poller.py` |
| `eodhd_fundamentals.py` | `fmp_fundamentals.py` |
| `eodhd_scraper.py` | `fmp_scraper.py` |
| `test_eodhd*.py` | `test_fmp.py` |
| `test_td_crypto*.py`, `twelvedata_crypto_test.txt` | `test_massive.py` |
| (n/a) | `fmp_client.py`, `massive_client.py` (shared API wrappers) |
| (n/a) | `feature_map.py` (11-feature tagging) |
| (n/a) | `earnings_transcript_poller.py`, `Prompt_S1T_TranscriptSummarization.py` (Feature 11) |

Modified in place (same filename, EODHD/TwelveData calls swapped for FMP/Massive): `etf_flow_poller.py`, `result_snapshot.py`, `news_roundup.py`, `heatmap_generator.py`, `scraper_common.py`, `ai_pipeline.py`, `main.py`.

## Things to do before this scales

1. **Licensing.** Both FMP and Massive personal tiers explicitly prohibit redistribution — see `claude/us-market-data-licensing-risk.md`, unresolved as of 2026-07-24. Get commercial quotes before growing the subscriber base.
2. **Row Level Security.** Supabase flagged 11 tables (`alerts`, `raw_filings`, `watchlists`, `users`, etc.) with RLS disabled — anyone with the anon key can read/write every row. Not something to auto-fix (enabling RLS without policies would break the app), but worth a deliberate pass. SQL is in the advisory Supabase's own tooling surfaced.
3. **Endpoint verification.** Every FMP/Massive endpoint path in `fmp_client.py`/`massive_client.py` was checked against each vendor's current developer docs on 2026-07-27 (quote, news, screener, IPO calendar, insider trading, transcripts, income statement, RSI/SMA, snapshots). The one exception is `fmp_client.get_etf_info()` (ETF expense ratio/AUM) — flagged in its own docstring as unverified, and every caller degrades gracefully if it 404s.
4. **API keys.** `.env` has `FMP_API_KEY` and `MASSIVE_API_KEY` placeholders — drop your real keys in before running anything. Transcripts (Feature 11) need FMP's Ultimate plan specifically.
