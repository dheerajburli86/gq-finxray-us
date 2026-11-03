"""
Nov 3: news must not mistake a Canadian listing for the US ticker.
"SHOP.TO", "ABC.CA", "$ABC.CA" and "(TSX: ABC)" are Canadian listings (often a
different company sharing the letters); only the US mention attributes news.
Run: python tests/test_alert_quality_nov03.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

import news_poller as N     # noqa: E402

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)

W = {"SHOP", "CNQ", "ENB", "AAPL", "BRK.B", "SU"}
for text, want in [
    ("Shopify (TSX: SHOP) rallied after results.", []),
    ("$SHOP.CA jumped 5% in Toronto.", []),
    ("Canadian Natural CNQ.TO shares slid.", []),
    ("A junior miner (TSXV: CNQ) drilled new holes.", []),
    ("ABC (CSE: CNQ) listed today.", []),
    ("Riocan REI.UN.TO fell; CNQ-CA flat.", []),
    ("Enbridge Inc. (TSX: ENB) (NYSE: ENB) raised its dividend.", ["ENB"]),
    ("Shopify (NYSE: SHOP) and SHOP.TO both rose.", ["SHOP"]),
    ("$AAPL rose; BRK.B shares flat.", ["AAPL", "BRK.B"]),
    ("CNQ shares rose in New York.", ["CNQ"]),
]:
    got = N.extract_watched_tickers(text, W)
    check(f"{text!r} -> {want}", sorted(got) == sorted(want), got)

if failures:
    print("\nFAILURES:", failures); sys.exit(1)
print("\nALL PASS")
