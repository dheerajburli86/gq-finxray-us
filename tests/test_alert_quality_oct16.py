"""
Defects found by reading the 44 alerts actually delivered on 2026-10-15/16.
Every case below is taken from real alert text, not invented.

Run: python tests/test_alert_quality_oct16.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x",
             "TELEGRAM_TOKEN": "1:x", "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x",
             "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


import technical_poller as TP      # noqa: E402
import etf_data_poller as EDP      # noqa: E402

print("1. 200-SMA crossover needs a real margin, not a graze")

# Live on Oct 15/16: SCHA crossed "above" at +0.1% then "below" at -0.1% the
# next morning; VB +0.3% then -0.4%; VGK +0.2%. Six HIGH-impact trend
# reversals in two days from prices sitting on their own 200-day average.
check("the deadband is at least 0.25%",
      TP.SMA200_MIN_DISTANCE_PCT >= 0.25, TP.SMA200_MIN_DISTANCE_PCT)


def _run_sma(price, prev_close, sma200, sent=None, recent=None):
    """One ticker through check_indicators with RSI and SMA stubbed."""
    index = {"TEST": {"price": price, "prev_close": prev_close, "change_pct": 1.0,
                      "day_high": price, "day_low": price,
                      "day_volume": 1e6, "prev_volume": 1e6}}
    real_rsi, real_sma = TP.massive_client.get_rsi, TP.massive_client.get_sma
    TP.massive_client.get_rsi = lambda t, window=14, **kw: [{"value": 50}]
    TP.massive_client.get_sma = lambda t, window=200: [{"value": sma200}]
    try:
        return TP.check_indicators(["TEST"], index, sent if sent is not None else set(),
                                   recent_crossovers=set() if recent is None else recent)
    finally:
        TP.massive_client.get_rsi, TP.massive_client.get_sma = real_rsi, real_sma


# SCHA's real numbers: MA 32.42, price 32.45 -> +0.09% above.
_graze_up = _run_sma(price=32.45, prev_close=32.30, sma200=32.42)
check("a +0.09% graze above the MA does not alert (SCHA, Oct 15)",
      not [a for a in _graze_up if a["filing_type"].startswith("SMA200")],
      [a["filing_type"] for a in _graze_up])

# SCHA the next morning: MA 32.44, price 32.41 -> -0.09% below.
_graze_dn = _run_sma(price=32.41, prev_close=32.50, sma200=32.44)
check("a -0.09% graze below the MA does not alert (SCHA, Oct 16)",
      not [a for a in _graze_dn if a["filing_type"].startswith("SMA200")],
      [a["filing_type"] for a in _graze_dn])

# VB's real numbers: MA 285.23, price 284.05 -> -0.41% below. Still chop.
_vb = _run_sma(price=284.05, prev_close=288.00, sma200=285.23)
check("VB's -0.41% cross does not alert either (Oct 16)",
      not [a for a in _vb if a["filing_type"].startswith("SMA200")],
      [a["filing_type"] for a in _vb])

# A genuine cross still fires: price clears the average by 1.5%.
_real = _run_sma(price=101.5, prev_close=99.0, sma200=100.0)
check("a decisive +1.5% cross still alerts",
      [a["filing_type"] for a in _real] == ["SMA200_CROSSOVER_UP"],
      [a["filing_type"] for a in _real])
_real_dn = _run_sma(price=98.5, prev_close=101.0, sma200=100.0)
check("a decisive -1.5% cross still alerts",
      [a["filing_type"] for a in _real_dn] == ["SMA200_CROSSOVER_DOWN"],
      [a["filing_type"] for a in _real_dn])

print("\n2. The same crossover direction does not repeat days later")

# EEM alerted SMA200_CROSSOVER_DOWN on Oct 15 AND Oct 16. It closed back above
# the average in between, so the per-day dedup let the same death cross through
# twice. A trend reversal is not a daily event.
_again = _run_sma(price=98.5, prev_close=101.0, sma200=100.0,
                  recent={("TEST", "SMA200_CROSSOVER_DOWN")})
check("a down-cross already alerted in the window is suppressed (EEM)",
      not [a for a in _again if a["filing_type"].startswith("SMA200")],
      [a["filing_type"] for a in _again])
check("but the opposite direction is still allowed through",
      [a["filing_type"] for a in _run_sma(
          price=101.5, prev_close=99.0, sma200=100.0,
          recent={("TEST", "SMA200_CROSSOVER_DOWN")})] == ["SMA200_CROSSOVER_UP"])
check("the repeat window is at least a week",
      TP.SMA200_REPEAT_DAYS >= 7, TP.SMA200_REPEAT_DAYS)

# Fails closed: an unreadable ledger must not re-send, but RSI still works.
_closed = _run_sma(price=101.5, prev_close=99.0, sma200=100.0, recent=None)
_closed = TP.check_indicators(
    ["TEST"],
    {"TEST": {"price": 101.5, "prev_close": 99.0, "change_pct": 1.0,
              "day_high": 101.5, "day_low": 101.5,
              "day_volume": 1e6, "prev_volume": 1e6}},
    set(), recent_crossovers=None)
check("an unreadable crossover ledger suppresses crossovers, not the whole run",
      not [a for a in _closed if a["filing_type"].startswith("SMA200")],
      [a["filing_type"] for a in _closed])

print("\n3. A 52-week breakout must clear the old high, not equal it")

# XLE, Oct 16: "hit a new 52-week high: $67.90 (up 0.0% from previous high of
# $67.90)". FMP folds the live session into yearHigh, so >= was always true for
# a ticker sitting at its high and the alert printed its own premise back.
TP._year_range_cache["date"] = None


def _run_year(price, day_high, day_low, year_high, year_low):
    from datetime import datetime
    TP._year_range_cache["date"] = datetime.now(TP.ET).date()
    TP._year_range_cache["levels"] = {"TEST": (year_high, year_low)}
    index = {"TEST": {"price": price, "prev_close": price, "change_pct": 1.0,
                      "day_high": day_high, "day_low": day_low,
                      "day_volume": 1e6, "prev_volume": 1e6}}
    return TP.check_52_week(index, set())


_tauto = _run_year(67.86, 67.90, 67.50, 67.90, 50.0)
check("session high EQUAL to the 52-week high does not alert (XLE, Oct 16)",
      not [a for a in _tauto if a["filing_type"] == "52W_HIGH"],
      [a["filing_type"] for a in _tauto])
_break = _run_year(70.00, 70.10, 69.0, 67.90, 50.0)
check("a genuine breakout above the old high still alerts",
      [a["filing_type"] for a in _break] == ["52W_HIGH"],
      [a["filing_type"] for a in _break])
check("and it never prints a 0.0% breakout",
      all("up 0.0%" not in a["summary"] for a in _break),
      [a["summary"][:70] for a in _break])
_tauto_low = _run_year(50.0, 55.0, 50.0, 90.0, 50.0)
check("session low EQUAL to the 52-week low does not alert either",
      not [a for a in _tauto_low if a["filing_type"] == "52W_LOW"],
      [a["filing_type"] for a in _tauto_low])
_break_low = _run_year(48.0, 52.0, 47.9, 90.0, 50.0)
check("a genuine breakdown below the old low still alerts",
      [a["filing_type"] for a in _break_low] == ["52W_LOW"],
      [a["filing_type"] for a in _break_low])

print("\n4. A ticker change is not a buy and a sale")

# PSP, Oct 15: "Added: Blue Owl Capital Corp (ORCC) at 1.00%. Removed: Blue Owl
# Capital Corp (OBDC), previously 0.98%." One issuer, one position, a renamed
# symbol — reported to the holder as a portfolio decision.
_add = [{"security_name": "Blue Owl Capital Corp", "security_symbol": "ORCC",
         "action": "ADDED", "weight_new": 1.00, "weight_prev": None,
         "security_key": "ORCC"}]
_rem = [{"security_name": "Blue Owl Capital Corp", "security_symbol": "OBDC",
         "action": "REMOVED", "weight_prev": 0.98, "weight_new": None,
         "security_key": "OBDC"}]
a2, r2, rel = EDP.pair_relabels(list(_add), list(_rem))
check("the PSP pair is recognised as one relabel", len(rel) == 1, len(rel))
check("and leaves nothing reported as added", a2 == [], a2)
check("and nothing reported as removed", r2 == [], r2)

# Corporate suffix and share-class noise must not defeat the match.
for nm_a, nm_r in (("Alphabet Inc Class A", "Alphabet Inc. Cl A"),
                   ("Meta Platforms, Inc.", "Meta Platforms Inc"),
                   ("Blue Owl Capital Corporation", "Blue Owl Capital Corp."),
                   ("JPMorgan Chase & Co", "JPMorgan Chase and Co.")):
    _, _, r = EDP.pair_relabels(
        [{"security_name": nm_a, "security_symbol": "X", "weight_new": 1.0}],
        [{"security_name": nm_r, "security_symbol": "Y", "weight_prev": 1.0}])
    check(f"relabel matched across naming noise: {nm_a!r} / {nm_r!r}", len(r) == 1)

# DELIBERATELY not paired: an ADR and the ordinary line are different
# instruments (different custody, fees and withholding), so a fund moving
# between them is a real change. Pairing is deliberately conservative here —
# a false relabel SUPPRESSES a genuine alert, which is the worse failure.
_, _, _adr = EDP.pair_relabels(
    [{"security_name": "Shell PLC ADR", "security_symbol": "SHEL", "weight_new": 1.0}],
    [{"security_name": "Shell plc", "security_symbol": "SHEL-LN", "weight_prev": 1.0}])
check("an ADR against the ordinary line is NOT paired away", len(_adr) == 0, len(_adr))

# A REAL trade between two different issuers must survive untouched.
a3, r3, rel3 = EDP.pair_relabels(
    [{"security_name": "Nvidia Corp", "security_symbol": "NVDA", "weight_new": 2.0}],
    [{"security_name": "Intel Corp", "security_symbol": "INTC", "weight_prev": 1.9}])
check("a genuine swap between two issuers is NOT treated as a relabel",
      len(rel3) == 0 and len(a3) == 1 and len(r3) == 1,
      f"rel={len(rel3)} add={len(a3)} rem={len(r3)}")

# Same issuer but the weight moved a long way: that is a real decision.
a4, r4, rel4 = EDP.pair_relabels(
    [{"security_name": "Blue Owl Capital Corp", "security_symbol": "ORCC", "weight_new": 4.0}],
    [{"security_name": "Blue Owl Capital Corp", "security_symbol": "OBDC", "weight_prev": 0.5}])
check("same issuer but an 8x weight jump is not a relabel",
      len(rel4) == 0, f"rel={len(rel4)}")

# One side only can never be a relabel.
check("an add with no matching removal is untouched",
      EDP.pair_relabels(list(_add), [])[2] == [])
check("a removal with no matching add is untouched",
      EDP.pair_relabels([], list(_rem))[2] == [])

print("\n5. The relabel is stated plainly when there IS other real activity")
_summary, _impact, _ftype = EDP.holdings_alert(
    "Invesco Global Listed Private Equity ETF", "PSP",
    [{"security_name": "Ares Capital Corp", "security_symbol": "ARCC",
      "weight_new": 1.5, "security_key": "ARCC"}],
    [], 0, 75, "2026-10-15T00:00:00Z",
    relabelled=[(_add[0], _rem[0])])
print(f"\n  {_summary}\n")
check("the summary names the symbol change", "ORCC" in _summary and "OBDC" in _summary)
check("and says plainly that it was not a trade",
      "not a trade" in _summary.lower(), _summary[:120])
check("while still reporting the genuine addition", "Ares Capital" in _summary)
check("no placeholder or None leaked",
      "None" not in _summary and "{" not in _summary)

print()
if failures:
    print(f"FAILURES ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("ALL PASS")
