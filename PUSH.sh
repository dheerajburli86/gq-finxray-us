#!/usr/bin/env bash
set -euo pipefail

command -v git >/dev/null && git rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git repo"; exit 1; }

echo "== 1. verify before anything else =="
python -m py_compile $(ls *.py | grep -v '^edgar_poller\.py$')
echo "   compile OK"
./tests/run_all.sh

echo
echo "== 2. env: the deep link is OFF until you set this =="
if ! grep -q '^GQUANTS_ALERT_BASE_URL=' .env 2>/dev/null; then
  cat >> .env <<'ENVEOF'

GQUANTS_ALERT_BASE_URL=
ENVEOF
  echo "   added GQUANTS_ALERT_BASE_URL (blank)"
else
  echo "   already present"
fi

echo
echo "== 3. commit =="
git add gquants_format_converter.py main.py result_snapshot.py \
        edgar_poller_async.py earnings_transcript_poller.py tests/
git commit -m "fix: make structured payloads actually reach alerts

The payload feature was a no-op. build_result_snapshot() never returned
quarters/cik, so xbrl_to_financial_results() got an empty list and returned
{}, which is falsy, so no payload and no link ever shipped.

- result_snapshot: return quarters + cik so the fr payload can be built
- edgar_poller_async: fetch_form4_text now returns (text, payload); the
  parsed transactions are reused instead of discarded, and the payload
  rides raw_filings.extra into alerts.extra via ai_pipeline
- earnings_transcript_poller: attach earning_calls payload; the FMP URL is
  deliberately NOT stored (it embeds the API key)
- gquants_format_converter: negative currency now formats by magnitude
  (-\$5.20B, was \$-5,200,000,000.00); frontend base URL comes from
  GQUANTS_ALERT_BASE_URL with no default instead of a guessed domain
- main: format_alert wraps the real 10-branch body rather than replacing it
- main: Result Snapshot drain 30min -> 5min (was the largest alert delay)
- removed needs_structured_payload/payload_type flags: nothing read them
- tests/: 4 offline tests, no network or credentials required"

echo
echo "== 4. push =="
BR=$(git rev-parse --abbrev-ref HEAD)
echo "   on branch: $BR"
git push origin "$BR"
