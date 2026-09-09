"""
tests/test_runtime_contracts.py

Three production failures from the 2026-09-09 boot log, and the contracts that
would have caught them before deploy. All three were silent: the process stayed
up, the summary lines looked normal, and the alerts simply never came.

  1. FEATURE 11 CALLED METHODS THAT DID NOT EXIST.
     analyst_ratings_poller called fmp_client.get_grades_consensus() and
     .get_price_target_consensus(). Neither was ever defined on fmp_client.
     _fetch_snapshot guards each call individually, so every ticker logged
     "module 'fmp_client' has no attribute ..." at WARNING and the run finished
     with "no-coverage=25, alerts=0" — which reads exactly like a watchlist no
     analyst covers. Feature 11 had never emitted an alert.

     The check below is deliberately general: it verifies EVERY attribute any
     production module reaches for on a client module actually exists there.
     This bug class is invisible at import time and only fires on the code path.

  2. AN EMPTY CIK MAP DISABLED EVERY WATCHLIST-SCOPED SEC FEATURE.
     SEC answered 429 to company_tickers.json three times at boot; the loader
     logged one line and returned. CIK_MAP stayed empty for the life of the
     process, so ticker_from_cik() returned "UNKNOWN" for every filing, the
     watchlist filter dropped all of them, and each poll printed the cheerful
     "No watchlisted 8-K filings." Features 1, 3 and 10 produced nothing.

  3. EVERY CALLER BACKED OFF FROM 429 PRIVATELY.
     SEC counts requests per requester, but each poller retried on its own
     schedule, so the retries kept the throttle alive instead of letting it
     clear. Six feeds and the CIK map all 429'd inside fifteen seconds.

Run:  python tests/test_runtime_contracts.py
"""
import ast
import asyncio
import glob
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for m in ["supabase", "telegram", "telegram.constants", "telegram.error", "dotenv",
          "aiohttp", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
          "pandas", "numpy", "feedparser", "bs4"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "x")
os.environ.setdefault("TELEGRAM_TOKEN", "x")

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


def _local_modules():
    return {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(ROOT, "*.py"))}


def _imports_of(path, local):
    try:
        tree = ast.parse(open(path).read())
    except Exception:
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module.split(".")[0])
    return {m for m in out if m in local}


def production_modules():
    """Everything reachable from main.py. Scratch scripts are not deployed."""
    local = _local_modules()
    seen, stack = set(), ["main"]
    while stack:
        m = stack.pop()
        if m in seen or m not in local:
            continue
        seen.add(m)
        stack += list(_imports_of(os.path.join(ROOT, m + ".py"), local))
    return sorted(seen)


# ── 1. Client-call contracts ──────────────────────────────────────────────────
print("=== 1. EVERY CLIENT METHOD A POLLER CALLS ACTUALLY EXISTS ===")

CLIENTS = ["fmp_client", "massive_client", "sec_client", "edgar_link",
           "sec_financials", "feature_map", "heatmap_style"]

prod = production_modules()
print(f"  ({len(prod)} production modules reachable from main.py)\n")

# module -> {attribute names it reaches for}
wanted = {c: {} for c in CLIENTS}
for mod in prod:
    path = os.path.join(ROOT, mod + ".py")
    try:
        tree = ast.parse(open(path).read())
    except Exception:
        continue
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in wanted):
            wanted[node.value.id].setdefault(node.attr, set()).add(mod)

checked = 0
for client_name, attrs in wanted.items():
    if not attrs:
        continue
    try:
        client = __import__(client_name)
    except Exception as e:
        check(f"{client_name} imports", False, str(e))
        continue
    for attr, callers in sorted(attrs.items()):
        checked += 1
        check(f"{client_name}.{attr}  (called by {', '.join(sorted(callers))})",
              hasattr(client, attr),
              "attribute does not exist — the caller's try/except turns this "
              "into a WARNING and the feature silently produces nothing")

check(f"at least a few client calls were actually inspected", checked >= 20,
      f"only {checked} — the AST scan probably stopped matching")


# ── 2. The CIK map cannot fail silently ───────────────────────────────────────
print("\n=== 2. AN EMPTY CIK MAP IS AN OUTAGE, NOT A QUIET FEED ===")

import edgar_poller_async as ep

ep.CIK_MAP.clear()
check("cik_map_ready() reports the outage", ep.cik_map_ready() is False)

# A watchlist-scoped poll must refuse to run rather than silently discard
# everything as UNKNOWN.
ep.sec_client = MagicMock()

async def _feed(url):
    return """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>8-K - Apple Inc (0000320193)</title>
      <link href="https://www.sec.gov/x/1.htm"/><summary>s</summary></entry></feed>"""

ep.sec_client.get_text = _feed
stored = []
ep.store_filing = lambda *a, **k: stored.append(a)
ep.get_watchlist = lambda force=False: {"AAPL"}

result = asyncio.run(ep.poll_edgar_generic_async("8-K", "8-K"))
check("a watchlist poll with no CIK map returns 0 and stores nothing",
      result == 0 and stored == [],
      f"result={result} stored={len(stored)}")

# The market-wide S-1 poll does NOT need the map (registrants are pre-listing),
# so it must keep working while the map is down.
ep.CIK_MAP.clear()
ep.ticker_from_cik = lambda cik: "UNKNOWN"
ep.known_filing_urls = lambda urls: set()
ep.known_registrant_ciks = lambda ciks, source: set()
ep.sec_financials = MagicMock()
ep.sec_financials.build_sec_json_links = lambda cik, url: {}

async def _s1_feed(url):
    return """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>S-1 - Northwind Robotics Inc (0001900001)</title>
      <link href="https://www.sec.gov/x/s1.htm"/><summary>s</summary></entry></feed>"""

async def _gather(coros, limit=None):
    out = []
    for c in coros:
        c.close()
        out.append("Northwind Robotics registration statement. " * 20)
    return out

ep.sec_client.get_text = _s1_feed
ep.sec_client.gather_limited = _gather
stored.clear()
ep.store_filing = lambda ft, co, tk, tx, u, extra=None, status="PENDING", \
    source="SEC_EDGAR": stored.append(source)
asyncio.run(ep.poll_sec_s1_async())
check("the market-wide S-1 poll still works without a CIK map",
      stored == ["SEC_IPO"], f"stored={stored}")

# Disk cache turns a throttled restart into one stale cycle instead of a blind
# process.
import tempfile
ep.CIK_MAP.clear()
ep.CIK_MAP["0000320193"] = "AAPL"
ep.CIK_CACHE_PATH = os.path.join(tempfile.mkdtemp(), "cik.json")
ep._save_cik_cache()
ep.CIK_MAP.clear()
check("the CIK map round-trips through the on-disk cache",
      ep._load_cik_cache() == 1 and ep.CIK_MAP.get("0000320193") == "AAPL",
      str(dict(ep.CIK_MAP)))


# ── 3. A 429 parks every caller, not just the one that saw it ─────────────────
print("\n=== 3. THROTTLING IS SHARED STATE ===")

import sec_client

check("SEC request rate leaves headroom on a shared egress IP",
      sec_client.MAX_RPS <= 6, f"MAX_RPS={sec_client.MAX_RPS}, SEC's own limit is 10/s")
check("throttling has its own retry budget, separate from transport errors",
      sec_client.MAX_THROTTLE_RETRIES > sec_client.MAX_RETRIES,
      "three flat 5s tries is how the CIK map was lost")

sec_client._cooldown_until[0] = 0.0
check("no cooldown before any 429", sec_client._cooldown_remaining() == 0.0)
sec_client._set_cooldown(30)
check("one 429 parks all subsequent SEC calls",
      25 < sec_client._cooldown_remaining() <= 30,
      f"{sec_client._cooldown_remaining()}")
sec_client._set_cooldown(5)
check("a shorter cooldown never shortens a longer one in flight",
      sec_client._cooldown_remaining() > 25,
      "a fast retry would walk straight back into the exhausted window")
sec_client._cooldown_until[0] = 0.0

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL RUNTIME CONTRACT TESTS PASS")
