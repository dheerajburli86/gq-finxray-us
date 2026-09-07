# GQ FinXray US — Structured Payload Fixes

## Files to download and copy to your repo root

- `result_snapshot.py`
- `edgar_poller_async.py`
- `gquants_format_converter.py`
- `main.py`
- `earnings_transcript_poller.py`
- `tests/` (entire folder with 4 test files)

## Steps

```bash
cd /path/to/your/repo

# 1. Copy files from this bundle into your repo root
# (result_snapshot.py, edgar_poller_async.py, etc. at root level)
# (tests/ folder at repo root)

# 2. Run the push script
chmod +x PUSH.sh
./PUSH.sh
```

The script will:
- Verify code compiles
- Run all 4 offline tests
- Add `GQUANTS_ALERT_BASE_URL=` to .env (blank)
- Create commit and push

## After push

1. Rotate API keys (FMP and Massive)
2. Message frontend team for the real alert base URL
3. Run one test on a watchlist ticker before deploying

## What was fixed

- `result_snapshot.py`: Now returns `quarters` and `cik` so payloads actually build
- `edgar_poller_async.py`: Form 4 parser returns structured data instead of discarding it
- `earnings_transcript_poller.py`: Attaches earning_calls payload (no FMP API key in payload)
- `gquants_format_converter.py`: Negative currency now formats correctly; URL comes from env
- `main.py`: format_alert wrapped to include deep links; snapshot latency 30min → 5min
- `tests/`: 4 offline payload tests that run without network or credentials
