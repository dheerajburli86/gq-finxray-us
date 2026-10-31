"""
Defects found reading the 50 alerts delivered on 2026-10-30 (Friday).
Run: python tests/test_alert_quality_oct31.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

import fund_manager_extractor as fx     # noqa: E402
import mf_common as mc                  # noqa: E402
import mf_manager_poller as mmp         # noqa: E402
import technical_poller as TP           # noqa: E402
import etf_signals_poller as ESP        # noqa: E402

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


class _Q:
    """Chainable fake; honours .eq() like PostgREST, ignores everything else."""
    def __init__(self, rows): self.rows = rows
    def eq(self, col, val): return _Q([r for r in self.rows if r.get(col, val) == val])
    def __getattr__(self, _): return lambda *a, **k: self
    def execute(self): return type("R", (), {"data": self.rows})()
class _SB:
    def __init__(self, rows): self.rows = rows
    def table(self, _): return _Q(self.rows)


print("1. AGTHX: titles glued to names are not part of the name")
check("'Co-President' is not a surname", not fx.is_person_name("Christopher D. Buchbinder Co-President"))
check("person inside is found", fx._best_person("Alan J. Wilson Co-President") == "Alan J. Wilson")
check("stored glued name == clean name",
      fx.canonical_key("Anne-Marie Peterson Co-President") == fx.canonical_key("Anne-Marie Peterson"))
check("hyphenated first name still a person", fx.is_person_name("Anne-Marie Peterson"))
old = [{"name": n, "manager_name": n} for n in
       ("Christopher D. Buchbinder Co-President", "Alan J. Wilson Co-President", "Mark L. Casey")]
new = [{"name": n} for n in ("Christopher D. Buchbinder", "Alan J. Wilson", "Mark L. Casey")]
d = fx.diff_rosters(old, new)
check("old glued roster vs clean roster: no change", not d["added"] and not d["removed"], d)

print("2. AGTHX: board members are not portfolio managers")
row = fx._parse_table_row("Christopher D. Buchbinder Co-President | 30 years | Partner - Capital Research Global Investors", 2026)
check("PM row parses to the clean name", row and row["name"] == "Christopher D. Buchbinder", row)
check("director row with birth year rejected",
      fx._parse_table_row("Vanessa C. L. Chang, 1952 | Director | 2015 | Retired", 2026) is None)
check("board role cell rejected",
      fx._parse_table_row("Yvonne L. Greenstreet | Independent Director | 2023 | Chief Executive Officer", 2026) is None)
check("plain 'Director' PM title kept (iShares)",
      fx._parse_table_row("Marcus Tom | 2021 | Director", 2026) is not None)
check("'Managing Director' PM title still kept",
      fx._parse_table_row("Jane Q. Smith | 2019 | Managing Director", 2026) is not None)
text = "\n".join([
    "Portfolio managers",
    "Name | Portfolio manager experience in this fund | Primary title",
    "Christopher D. Buchbinder Co-President | 30 years | Partner - Capital Research Global Investors",
    "Mark L. Casey | 15 years | Partner - Capital International Investors",
    "Independent directors",
    "Vanessa C. L. Chang | 2014 | Retired",
    "Yvonne L. Greenstreet | 2023 | Chief Executive Officer, Alnylam",
])
secs = fx.extract_roster_sections(text, 2026)
names = sorted(m["name"] for s in secs for m in s["managers"])
check("PM section stops at the board table", names == ["Christopher D. Buchbinder", "Mark L. Casey"], names)

print("3. Fund named once when the registrant has the fund's own name")
s, _, _ = mmp.build_alert("GROWTH FUND OF AMERICA", "GROWTH FUND OF AMERICA", [],
                          [{"manager": "A B", "role": None}], ["C D"], "2026-10-30",
                          {"form": "485BPOS", "filing_date": "2026-10-30"})
check("no 'a mutual fund at GROWTH FUND OF AMERICA'", "a mutual fund at" not in s, s[:120])
s2, _, _ = mmp.build_alert("AB Select US Long/Short Portfolio", "AB CAP FUND, INC.", [],
                           [{"manager": "A B", "role": None}], ["C D"], "2026-10-30",
                           {"form": "497K", "filing_date": "2026-10-30"})
check("different registrant still named", "a mutual fund at AB CAP FUND, INC." in s2, s2[:120])
check("same_name", mc.same_name("Growth Fund of America", "GROWTH FUND OF AMERICA")
      and not mc.same_name("X Fund", "Y Trust"))

print("4. Technical: one message per ticker per run (XLV 10:27 on Oct 30)")
def ta(t, ft, impact, head):
    return {"ticker": t, "summary": head + ".", "impact": impact, "source": "TECHNICAL",
            "filing_type": ft, "extra": {"headline": head}}
out = TP.merge_alerts_by_ticker([ta("XLV", "VOLUME_SPIKE", "MEDIUM", "Trading Volume Far Above Normal Pace"),
                                 ta("XLV", "52W_HIGH", "HIGH", "Breaks Out To New 52-Week High"),
                                 ta("USO", "52W_HIGH", "HIGH", "Breaks Out To New 52-Week High")])
check("three alerts, two tickers -> two messages", len(out) == 2, [o["ticker"] for o in out])
x = next(o for o in out if o["ticker"] == "XLV")
check("led by the HIGH 52-week high", x["filing_type"] == "52W_HIGH" and x["impact"] == "HIGH", x)
check("merged type recorded", x["extra"]["merged_filing_types"] == ["VOLUME_SPIKE"], x["extra"])
check("both bodies present", "52-Week" in x["summary"] and "Volume" in x["summary"])

TP.supabase = _SB([{"ticker": "XLV", "filing_type": "52W_HIGH", "source": "TECHNICAL",
                    "extra": {"merged_filing_types": ["VOLUME_SPIKE"]}},
                   {"ticker": "QQQ", "filing_type": "52W_HIGH", "source": "TECHNICAL",
                    "extra": {"merged_filing_types": ["SMA200_CROSSOVER_DOWN"]}}])
sent = TP.load_sent_today()
check("merged spike counts as sent today", ("XLV", "VOLUME_SPIKE") in sent, sent)
check("merged crossover remembered", ("QQQ", "SMA200_CROSSOVER_DOWN") in TP.load_recent_crossovers(), "")
ESP.sb = lambda: _SB([{"ticker": "XLV", "filing_type": "52W_HIGH",
                       "extra": {"merged_filing_types": ["VOLUME_SPIKE"]}},
                      {"ticker": "VTV", "filing_type": "52W_HIGH", "extra": {}}])
import datetime as _dt
cov = ESP.technical_volume_tickers(_dt.date(2026, 10, 30), {"XLV", "VTV"})
check("ETF engine sees a merged spike, not a plain 52W high", cov == {"XLV"}, cov)

print("5. A repeat move alert must be clearly bigger than what was sent (QQQ, Oct 30)")
import datetime as _d2
_day = _d2.date(2026, 10, 30)
def _ev(z, sent, levels, vol_ratio=2.2):
    sigma = 0.0103
    base = {"sigma": sigma, "adv": 3.67e7, "beta": None, "rets": [0.0] * 300,
            "sessions": [{"date": _day}] * 301, "last_close": 715.0}
    m = {"price": 715.0 * (1 - z * sigma), "prev_close": 715.0, "volume": 3.67e7 * 0.11 * vol_ratio}
    return ESP.evaluate_intraday("QQQ", "QQQ", base, m, 0.11, _day.isoformat(), sent, levels=levels)
kinds = lambda out: [a["filing_type"] for a in out]
sent, lv = set(), {}
check("first: momentum at 2.48x", kinds(_ev(2.48, sent, lv)) == ["ETF_MOMENTUM_DOWN"], lv)
check("2.6x 15 min later: not sent (no bigger by 0.5)", _ev(2.60, sent, lv) == [])
out = _ev(3.0, sent, lv)
check("3.0x: sent as a bigger drop", kinds(out) == ["ETF_BIG_MOVE_DOWN"], kinds(out))
check("says it is bigger", out and "bigger than the 2.5x move" in out[0]["summary"],
      out and out[0]["summary"])
check("3.2x after 3.0x: not sent", _ev(3.2, sent, lv) == [])
out = _ev(3.55, sent, lv)
check("3.55x after 3.0x: sent again, new key", kinds(out) == ["ETF_BIG_MOVE_DOWN"]
      and out[0]["extra"]["dedup_key"].endswith(":x3.55"), out and out[0]["extra"]["dedup_key"])
check("2.0x never re-alerts", _ev(2.0, sent, lv) == [])
sent2, lv2 = set(), {}
check("no prior alert: 2.6x big move goes out", kinds(_ev(2.6, sent2, lv2)) == ["ETF_BIG_MOVE_DOWN"])
sent3 = {ESP.dedup_key("QQQ", "ETF_MOMENTUM_DOWN", _day.isoformat())}
check("unknown earlier size blocks the repeat", _ev(3.5, sent3, {}) == [])
check("legacy callers (levels=None) unchanged", kinds(_ev(3.5, set(sent3), None)) == ["ETF_BIG_MOVE_DOWN"])
rows = [{"ticker": "QQQ", "filing_type": "ETF_BIG_MOVE_DOWN", "extra": {"move_multiple": 2.6,
         "merged_filing_types": ["ETF_MOMENTUM_DOWN"], "merged_signals": [{"move_multiple": 2.48}]}},
        {"ticker": "XLE", "filing_type": "ETF_MOMENTUM_UP", "extra": {"move_multiple": 1.51}}]
check("levels read back from stored rows", ESP.levels_from_rows(rows) == {("QQQ", "DOWN"): 2.6, ("XLE", "UP"): 1.51},
      ESP.levels_from_rows(rows))

if failures:
    print("\nFAILURES:", failures); sys.exit(1)
print("\nALL PASS")
