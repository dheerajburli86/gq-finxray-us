#!/bin/bash

# GQ FinXray US — Watchlist Cleanup
# Remove dead tickers and update renamed ones in Supabase
# Usage: bash UPDATE_WATCHLIST.sh

set -e

source .env

SUPABASE_URL="${SUPABASE_URL}"
SUPABASE_KEY="${SUPABASE_KEY}"

echo "=== GQ FinXray US — Watchlist Cleanup ==="
echo ""

# 1. Remove SPCX (delisted)
echo "[1/2] Removing SPCX (delisted ETF)..."
curl -s -X DELETE \
  "${SUPABASE_URL}/rest/v1/watchlists?ticker=eq.SPCX" \
  -H "apikey: ${SUPABASE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_KEY}" \
  -H "Content-Type: application/json" \
  | python3 -m json.tool 2>/dev/null || echo "Done"

# 2. Update SQ → XYZ (Block renamed ticker)
echo "[2/2] Updating SQ → XYZ (Block renamed ticker Jan 2025)..."
curl -s -X PATCH \
  "${SUPABASE_URL}/rest/v1/watchlists?ticker=eq.SQ" \
  -H "apikey: ${SUPABASE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"ticker":"XYZ"}' \
  | python3 -m json.tool 2>/dev/null || echo "Done"

echo ""
echo "✅ Watchlist cleanup complete!"
echo ""
echo "To push the code fix:"
echo "  git add watchlist_util.py UPDATE_WATCHLIST.sh"
echo "  git commit -m 'Remove SPCX, update SQ→XYZ in watchlist audit'"
echo "  git push origin main"
echo ""
