# Run from your gqfinxray folder AFTER: git pull, then extracting this zip over the folder.
# 1. Remove superseded ETF files (they target tables that no longer exist).
Remove-Item -ErrorAction SilentlyContinue etf_mf_alerts.py, etf_mf_data_poller.py, etf_mf_master_scraper.py, etf_mf_scheduler_integration.py, etf_mf_poller_full.py
# 2. Offline tests (no network). Stops before pushing if anything fails.
$env:SUPABASE_URL="https://x.supabase.co"; $env:SUPABASE_KEY="x"; $env:TELEGRAM_TOKEN="1:x"; $env:FMP_API_KEY="x"; $env:DEEPINFRA_API_KEY="x"
foreach ($t in "tests\test_etf_alerts.py","tests\test_feature_coverage.py","tests\test_poller_e2e.py","tests\test_fund_manager_extractor.py") {
  python $t | Out-Null; if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $t - not pushing"; python $t | Select-String "FAIL"; exit 1 } else { Write-Host "PASS  $t" }
}
Remove-Item Env:SUPABASE_URL, Env:SUPABASE_KEY, Env:TELEGRAM_TOKEN, Env:FMP_API_KEY, Env:DEEPINFRA_API_KEY
# 3. Commit and push -> Railway redeploys the worker.
git add -A
git commit -m "ETF alerts: features 14-16 (holdings/expense, fund manager, fund actions) + audit fixes"
git push
