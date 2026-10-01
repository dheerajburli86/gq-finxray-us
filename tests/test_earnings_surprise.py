"""
tests/test_earnings_surprise.py

Feature 4's second half: the EPS surprise itself, not just the heads-up that
earnings are due.

TWO FAILURES STACKED, so this produced nothing since it was written:

  1. NEVER SCHEDULED. feature_map listed EARNINGS_MISS and EARNINGS_BEAT as
     Feature 4 filing types, with a comment crediting earnings_alerts.py as
     their emitter — and main.py never imported or called that module. The map
     documented a promise the scheduler did not keep.

  2. WOULD NOT HAVE WORKED IF IT HAD BEEN. A 2026-08-19 bugfix renamed
     `announcement_date` to `event_date_str` at the read site and left four uses
     of the old name in the insert. That is an undefined local, so every insert
     raised NameError into a broad `except Exception` that only special-cases
     "duplicate"/"unique" and logged the rest as a warning. The module's own
     comment already said "This feature has never written a row" about an
     earlier column bug; this is why that was still true.

Run:  python tests/test_earnings_surprise.py
"""
import ast
import builtins
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for m in ["supabase", "dotenv", "requests"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "x")

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


import earnings_alerts as ea
import feature_map


# ── 1. No undefined locals anywhere in the poller ─────────────────────────────
# A NameError here is invisible in production: it is swallowed by the insert's
# own except-branch and logged as a warning, so the poller reports success while
# writing nothing. Static check rather than a runtime one, because only the
# branch that actually inserts would trip it.
print("=== 1. THE POLLER HAS NO UNDEFINED NAMES ===")
_tree = ast.parse(open(os.path.join(os.path.dirname(__file__), "..",
                                    "earnings_alerts.py")).read())
_module_globals = set(dir(builtins))
_module_globals |= {n.name for n in ast.walk(_tree)
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
_module_globals |= {a.asname or a.name.split(".")[0] for n in ast.walk(_tree)
                    if isinstance(n, ast.Import) for a in n.names}
_module_globals |= {a.asname or a.name for n in ast.walk(_tree)
                    if isinstance(n, ast.ImportFrom) for a in n.names}
_module_globals |= {t.id for n in _tree.body if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}

for fn in ast.walk(_tree):
    if not isinstance(fn, ast.FunctionDef):
        continue
    assigned = {t.id for t in ast.walk(fn)
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)}
    assigned |= {h.name for h in ast.walk(fn)
                 if isinstance(h, ast.ExceptHandler) and h.name}
    assigned |= {a.arg for a in ast.walk(fn) if isinstance(a, ast.arg)}
    used = {t.id for t in ast.walk(fn)
            if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Load)}
    undefined = sorted(used - assigned - _module_globals)
    check(f"{fn.name}() has no undefined names", not undefined,
          f"{undefined} — would raise NameError into the swallowing except")


# ── 2. It actually writes rows ────────────────────────────────────────────────
print("\n=== 2. A SURPRISE BECOMES A PENDING ROW ===")

inserted = []


class _Table:
    def insert(self, row):
        inserted.append(row)
        return self

    def execute(self):
        class R:
            data = []
        return R()


ea.supabase = MagicMock(table=lambda name: _Table())
ea.get_watched_tickers = lambda: {"AAPL", "MSFT"}

TODAY = datetime.now(timezone.utc).date().isoformat()
ea.fmp_client = MagicMock()
ea.fmp_client.get_earnings_calendar = lambda a, b: [
    {"symbol": "AAPL", "date": TODAY, "epsActual": 1.64, "epsEstimated": 1.50},
    {"symbol": "MSFT", "date": TODAY, "epsActual": 2.10, "epsEstimated": 2.35},
    {"symbol": "NVDA", "date": TODAY, "epsActual": 5.00, "epsEstimated": 4.00},
    {"symbol": "AAPL", "date": TODAY, "epsActual": None, "epsEstimated": 1.50},
]

ea.poll_earnings_for_tickers()

check("rows are written at all", len(inserted) == 2,
      f"got {len(inserted)} — this was 0 for the life of the module")
types = {r["filing_type"] for r in inserted}
check("a beat is reported, not just a miss", "EARNINGS_BEAT" in types, str(types))
check("a miss is reported", "EARNINGS_MISS" in types, str(types))
check("an unwatched ticker is skipped",
      not any(r["ticker"] == "NVDA" for r in inserted),
      "NVDA is not on the watchlist")
check("an unreported quarter is skipped (epsActual=None)", len(inserted) == 2,
      "a null actual is 'not announced yet', not a 100% miss")

for r in inserted:
    check(f"{r['ticker']} lands PENDING so the AI pipeline summarises it",
          r["status"] == "PENDING", r["status"])
    check(f"{r['ticker']} carries a stable content_hash for dedup",
          r["content_hash"] == f"{r['ticker']}-{TODAY}-{r['filing_type']}",
          r["content_hash"])
    check(f"{r['ticker']} filed_at is a real timestamp",
          isinstance(r["filed_at"], str) and r["filed_at"].startswith(TODAY[:4]),
          str(r["filed_at"]))
    check(f"{r['ticker']} is tagged Feature 4",
          (r["extra"] or {}).get("feature_id") == 4,
          str((r["extra"] or {}).get("feature_id")))


# ── 3. The map and the emission agree ─────────────────────────────────────────
print("\n=== 3. THE EMISSION RESOLVES TO A REAL FEATURE ===")
for ft in ("EARNINGS_MISS", "EARNINGS_BEAT"):
    fid, name = feature_map.resolve_feature("FMP", ft)
    check(f"FMP/{ft} -> Feature {fid} ({name})", fid == 4,
          "an Unmapped alert cannot be muted or monitored per feature")

import ai_pipeline
for ft in ("EARNINGS_MISS", "EARNINGS_BEAT"):
    check(f"{ft} jumps the news backlog", ft in ai_pipeline.PRIORITY_FILING_TYPES,
          "source is the shared 'FMP', so only the filing_type axis can prioritise it")

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL EARNINGS SURPRISE TESTS PASS")
