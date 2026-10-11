"""
ETF signal engine (etf_signals_poller.py) + the v1 ETF flow fixes, end to end.
In-memory Supabase, synthetic Massive bars/snapshots, fake Telegram, frozen
clocks -- no network. Run: python tests/test_etf_signals.py

Covers: baseline maths, every signal and its suppression rules, ex-dividend
adjustment, thin-tape and split guards, the session review, weekend/holiday/
pre-open gating, opt-in gating (incl. the column not existing yet), dedup across
runs, fail-closed ledger, delivery routing for v1 vs v2 users, rendering, and
the v1 poller no longer re-firing Friday's bar over the weekend.
"""
import asyncio
import datetime as _dt
import os
import random
import re
import sys
import types
import uuid
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
os.environ.update(SUPABASE_URL="https://fake.supabase.co", SUPABASE_KEY="k",
                  TELEGRAM_TOKEN="t", MASSIVE_API_KEY="m", FMP_API_KEY="f")
ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ── Fake Supabase ─────────────────────────────────────────────────────────────
DB = {}


def _parse_ts(v):
    try:
        d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


def _get(row, col):
    if "->>" in col:
        a, b = col.split("->>")
        v = (row.get(a) or {}).get(b)
        return None if v is None else str(v)
    return row.get(col)


def _cmp(a, b):
    ta, tb = _parse_ts(a), _parse_ts(b)
    if ta and tb and isinstance(a, str) and isinstance(b, str) and "T" in a:
        return (ta > tb) - (ta < tb)
    return (a > b) - (a < b)


class Q:
    def __init__(self, table):
        self.t, self.op, self.filters, self.payload = table, "select", [], None
        self._range = self._limit = self._order = None
        self.on_conflict = None
        self.ignore_dup = False

    def select(self, *_a, **_k): self.op = "select"; return self
    def insert(self, rows): self.op, self.payload = "insert", rows; return self
    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self.op, self.payload, self.on_conflict, self.ignore_dup = "upsert", rows, on_conflict, ignore_duplicates
        return self
    def update(self, vals): self.op, self.payload = "update", vals; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, c, v): self.filters.append(("eq", c, v)); return self
    def in_(self, c, v): self.filters.append(("in", c, list(v))); return self
    def gte(self, c, v): self.filters.append(("gte", c, v)); return self
    def lt(self, c, v): self.filters.append(("lt", c, v)); return self
    def ilike(self, c, v): self.filters.append(("ilike", c, v)); return self
    def order(self, c, desc=False): self._order = (c, desc); return self
    def range(self, a, b): self._range = (a, b); return self
    def limit(self, n): self._limit = n; return self

    def _match(self, row):
        for op, c, v in self.filters:
            x = _get(row, c)
            if op == "eq" and str(x) != str(v):
                return False
            if op == "in" and x not in v and str(x) not in [str(i) for i in v]:
                return False
            if op == "gte" and (x is None or _cmp(x, v) < 0):
                return False
            if op == "lt" and (x is None or _cmp(x, v) >= 0):
                return False
        return True

    def execute(self):
        rows = DB.setdefault(self.t, [])
        if self.t in FAIL_TABLES:
            raise RuntimeError(f"fake failure reading {self.t}")
        if self.op == "insert" and self.t in FAIL_INSERT:
            raise RuntimeError(f"fake insert failure on {self.t}")
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for r in items:
                r = dict(r)
                r.setdefault("id", str(uuid.uuid4()))
                r.setdefault("created_at", (Frozen.at or _dt.datetime.now(UTC)).astimezone(UTC).isoformat())
                rows.append(r)
                out.append(r)
            return types.SimpleNamespace(data=out)
        if self.op == "upsert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            keys = (self.on_conflict or "id").split(",")
            for r in items:
                hit = next((x for x in rows if all(x.get(k) == r.get(k) for k in keys)), None)
                if hit and not self.ignore_dup:
                    hit.update(r)
                elif not hit:
                    r = dict(r); r.setdefault("created_at", _dt.datetime.now(UTC).isoformat()); rows.append(r)
            return types.SimpleNamespace(data=items)
        matched = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in matched:
                r.update(self.payload)
            return types.SimpleNamespace(data=matched)
        if self.op == "delete":
            DB[self.t] = [r for r in rows if r not in matched]
            return types.SimpleNamespace(data=matched)
        if self._order:
            c, desc = self._order
            matched.sort(key=lambda r: str(_get(r, c) or ""), reverse=desc)
        if self._range:
            a, b = self._range
            matched = matched[a:b + 1]
        if self._limit:
            matched = matched[:self._limit]
        return types.SimpleNamespace(data=[dict(r) for r in matched])


class FakeClient:
    def table(self, name):
        return Q(name)


FAIL_TABLES = set()
FAIL_INSERT = set()
import supabase  # noqa: E402
supabase.create_client = lambda *a, **k: FakeClient()

# ── Frozen clock ──────────────────────────────────────────────────────────────
class Frozen(_dt.datetime):
    at = None

    @classmethod
    def now(cls, tz=None):
        base = cls.at or _dt.datetime.now(UTC)
        return base.astimezone(tz) if tz else base.astimezone(ET).replace(tzinfo=None)


def freeze(*args):
    Frozen.at = _dt.datetime(*args, tzinfo=ET)


# ── Import the code under test ────────────────────────────────────────────────
import massive_client  # noqa: E402
import heatmap_generator as hg  # noqa: E402
import feature_map  # noqa: E402
import etf_signals_poller as esp  # noqa: E402
import etf_flow_poller as efp  # noqa: E402
import delivery  # noqa: E402
import alert_formatter  # noqa: E402

esp.datetime = Frozen
efp.datetime = Frozen

CALLS = {"snapshot": 0, "aggs": 0, "divs": 0, "single": 0}

# ── Synthetic market ──────────────────────────────────────────────────────────
FRI = _dt.date(2026, 10, 9)
MON = _dt.date(2026, 10, 12)       # Columbus Day: NYSE OPEN


def sessions_back(end, n):
    out, d = [], end
    while len(out) < n:
        if hg.is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def bar_t(d):
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


rng = random.Random(7)
DAYS = sessions_back(FRI, 300)
spy_r = [rng.gauss(0.0004, 0.009) for _ in DAYS]
spy_r[-6] = -0.010                       # down day, then five up days
for k in range(-5, 0):
    spy_r[k] = 0.004


def series(p0, sigma, adv, beta=None, resid=None):
    closes, vols, p = [], [], p0
    for i, _ in enumerate(DAYS):
        r = (beta * spy_r[i] + rng.gauss(0, resid)) if beta is not None else rng.gauss(0.0003, sigma)
        p *= (1 + r) if i else 1
        closes.append(p)
        vols.append(adv * rng.lognormvariate(0, 0.25))
    return closes, vols


MKT = {}
MKT["SPY"] = (lambda c: (c, [70e6 * rng.lognormvariate(0, 0.2) for _ in DAYS]))(
    [500 * __import__("math").prod(1 + x for x in spy_r[1:i + 1]) for i in range(len(DAYS))])
MKT["VOO"] = series(450, 0.008, 5e6, beta=1.0, resid=0.0004)
MKT["AGG"] = series(98, 0.003, 7e6)
MKT["XLE"] = series(90, 0.012, 15e6, beta=0.9, resid=0.010)
MKT["PSP"] = series(55, 0.012, 20e3)
MKT["TQQQ"] = series(60, 0.035, 60e6)
MKT["AAPL"] = series(230, 0.015, 50e6)

# VOO tracks SPY, whose last six returns were set above: exactly 5 up closes.
# XLE: no streak and nowhere near a drawdown line on Friday
c = MKT["XLE"][0]
c[-3] = max(c[-255:-3]) * 0.995
c[-2] = c[-3] * 1.001
c[-1] = c[-2] * 0.999
# TQQQ: cross -10% from its 52-week closing high on Friday
c = MKT["TQQQ"][0]
peak = max(c[-252:-2])
c[-2] = peak * 0.91
c[-1] = peak * 0.885
for t in ("AGG", "PSP", "AAPL"):          # no accidental streaks for these
    MKT[t][0][-1] = MKT[t][0][-2] * 1.0005
    MKT[t][0][-2] = MKT[t][0][-3] * 0.9995


def fake_aggs(ticker, mult, span, frm, to, limit=5000, _count=True):
    if _count:
        CALLS["aggs"] += 1
    closes, vols = MKT[ticker]
    to_d = _dt.date.fromisoformat(to)
    bars = [{"t": bar_t(d), "o": c_, "h": c_ * 1.004, "l": c_ * 0.996, "c": c_, "v": v}
            for d, c_, v in zip(DAYS, closes, vols) if d <= to_d]
    # a partial bar for "today" that must never reach a baseline
    bars.append({"t": bar_t(to_d), "o": 1, "h": 1, "l": 1, "c": 1.0, "v": 1})
    return sorted(bars, key=lambda b: -b["t"])


SNAP = {}


def fake_full_snapshot(tickers=None):
    CALLS["snapshot"] += 1
    return [SNAP[t] for t in (tickers or SNAP) if t in SNAP]


DIVHIST = {}            # ticker -> [{"ex_date", "cash"}]; a ticker in DIV_FAIL returns None
DIV_FAIL = set()


def fake_divhist(ticker, since):
    CALLS["divs"] += 1
    if ticker in DIV_FAIL:
        return None
    return [d for d in DIVHIST.get(ticker, []) if d["ex_date"] >= since]


massive_client.get_aggregates = fake_aggs
massive_client.get_full_market_snapshot = fake_full_snapshot
massive_client.get_dividend_history = fake_divhist
alert_formatter.fmp_client.get_quote = lambda t: {"price": 101.25, "changePercentage": 1.1}

# ── DB fixtures ───────────────────────────────────────────────────────────────
ME = "a1146826-39ea-48c5-9236-45405ff166f2"
OTHER = "d8e8a79f-20ef-425b-a157-d58695c1d855"


def reset_db(beta=True, column=True):
    DB.clear()
    DB["users"] = [
        {"id": ME, "telegram_chat_id": 111, "telegram_username": "dj_12312", "display_name": "D", "is_active": True},
        {"id": OTHER, "telegram_chat_id": 222, "telegram_username": "Breakout100", "display_name": "B", "is_active": True},
    ]
    me = {"user_id": ME, "min_impact": "MEDIUM", "muted_features": [1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13],
          "max_alerts_per_day": 200, "receive_market_wide": False}
    other = {"user_id": OTHER, "min_impact": "MEDIUM", "muted_features": [],
             "max_alerts_per_day": 200, "receive_market_wide": True}
    if column:
        me["etf_signals_beta"] = beta
        other["etf_signals_beta"] = False
    DB["user_preferences"] = [me, other]
    DB["watchlists"] = ([{"user_id": ME, "ticker": t} for t in ("VOO", "AGG", "XLE", "PSP", "TQQQ", "MSTY")] +
                        [{"user_id": OTHER, "ticker": t} for t in ("AAPL", "XLE")])
    DB["etf_master"] = [{"ticker": t, "name": n} for t, n in (
        ("VOO", "Vanguard S&P 500 ETF"), ("AGG", "iShares Core US Aggregate Bond ETF"),
        ("XLE", "Energy Select Sector SPDR Fund"), ("PSP", "Invesco Global Listed Private Equity ETF"),
        ("TQQQ", "ProShares UltraPro QQQ"), ("SPY", "SPDR S&P 500 ETF Trust"))]
    DB["alerts"] = []
    DB["alert_deliveries"] = []


def ns(dt):
    return int(dt.timestamp() * 1e9)


def set_snapshot(now, moves, vol_mult, live=True, spy_move=0.002):
    """moves: ticker -> return today; vol_mult: ticker -> x of 20d ADV pace."""
    SNAP.clear()
    from technical_poller import session_volume_fraction
    el = session_volume_fraction(now)
    stamp = ns(now if live else now - timedelta(days=3))
    for t in ("SPY", "VOO", "AGG", "XLE", "PSP", "TQQQ"):
        closes, vols = MKT[t]
        prev = closes[-1]
        adv = sum(vols[-20:]) / 20
        r = spy_move if t == "SPY" else moves.get(t, 0.001)
        v = adv * el * vol_mult.get(t, 1.0)
        SNAP[t] = {"ticker": t, "updated": stamp,
                   "day": {"o": prev, "h": prev * (1 + abs(r)), "l": prev * (1 - abs(r)), "c": prev * (1 + r), "v": v},
                   "prevDay": {"c": prev, "v": vols[-1]}, "lastTrade": {"p": prev * (1 + r), "t": stamp}}


# ── Tests ─────────────────────────────────────────────────────────────────────
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def kinds(rows):
    return sorted(f"{a['ticker']}:{a['filing_type']}" for a in rows)


def sessions_of(t, before=MON):
    ses = esp.bars_to_sessions(fake_aggs(t, 1, "day", "x", before.isoformat(), _count=False), before)
    return esp.apply_distributions(ses, DIVHIST.get(t, []))


def base_of(t, before=MON):
    return esp.compute_baseline(sessions_of(t, before), sessions_of("SPY", before))


print("== pure functions ==")
ses = esp.bars_to_sessions(fake_aggs("VOO", 1, "day", "x", MON.isoformat()), MON)
check("partial bar for today dropped", ses[-1]["date"] == FRI and all(s["c"] > 1 for s in ses))
check("sessions ascending", all(ses[i]["date"] < ses[i + 1]["date"] for i in range(len(ses) - 1)))
b = base_of("VOO")
import statistics  # noqa: E402
rets = [ses[i]["c"] / ses[i - 1]["c"] - 1 for i in range(1, len(ses))]
check("sigma = stdev(last 60 returns)", abs(b["sigma"] - statistics.stdev(rets[-60:])) < 1e-12)
check("adv = mean(last 20 volumes)", abs(b["adv"] - sum(s["v"] for s in ses[-20:]) / 20) < 1e-6)
bx = base_of("XLE")
check("XLE beta ~0.9 and mid R^2", 0.5 < bx["beta"] < 1.4 and 0.3 <= bx["r2"] < 0.95,
      f"beta={bx['beta']:.2f} r2={bx['r2']:.2f}")
check("VOO is a clone (R^2 >= 0.95)", base_of("VOO")["r2"] >= 0.95, f"r2={base_of('VOO')['r2']:.3f}")

S = MON.isoformat()


def ev(t, r, ratio, elapsed=0.5, sent=None, bench_r=0.002, div=0.0, vol=None):
    bb = base_of(t)
    prev = bb["last_close"]
    v = vol if vol is not None else bb["adv"] * elapsed * ratio
    return esp.evaluate_intraday(t, t, bb, {"price": prev * (1 + r) - div, "prev_close": prev, "volume": v},
                                 elapsed, S, set() if sent is None else sent, bench={"r": bench_r}, div=div)


sv = base_of("VOO")["sigma"]
check("VOO 1.9x move on 2x volume -> MOMENTUM_UP", kinds(ev("VOO", 1.9 * sv, 2.0)) == ["VOO:ETF_MOMENTUM_UP"])
check("VOO 1.9x move on 1.2x volume -> nothing", ev("VOO", 1.9 * sv, 1.2) == [])
check("VOO 2.8x move -> BIG_MOVE_UP only (momentum superseded)",
      kinds(ev("VOO", 2.8 * sv, 2.0)) == ["VOO:ETF_BIG_MOVE_UP"])
sent = {esp.dedup_key("VOO", "ETF_MOMENTUM_UP", S)}
check("momentum earlier, big move later -> escalates", kinds(ev("VOO", 2.8 * sv, 2.0, sent=sent)) == ["VOO:ETF_BIG_MOVE_UP"])
sent = {esp.dedup_key("VOO", "ETF_BIG_MOVE_UP", S)}
check("big move earlier -> no momentum after", ev("VOO", 1.9 * sv, 2.0, sent=sent) == [])
st = base_of("TQQQ")["sigma"]
check("TQQQ +3% (under 1x its typical move) -> nothing", ev("TQQQ", 0.03, 2.0) == [], f"sigma={st:.4f}")
sa = base_of("AGG")["sigma"]
r_agg = -max(2.8 * sa, 0.0080)
check("AGG -2.8x move -> BIG_MOVE_DOWN", kinds(ev("AGG", r_agg, 1.0)) == ["AGG:ETF_BIG_MOVE_DOWN"],
      f"sigma={sa:.4f} r={r_agg:.4f}")
prev = base_of("AGG")["last_close"]
drop = prev * 0.0085
bb = base_of("AGG")
got = esp.evaluate_intraday("AGG", "AGG", bb, {"price": prev - drop, "prev_close": prev, "volume": bb["adv"] * 0.5},
                            0.5, S, set(), bench={"r": 0.0}, div=drop * 0.9)
check("ex-dividend drop is added back -> no alert", got == [])
got = esp.evaluate_intraday("AGG", "AGG", bb, {"price": prev - drop, "prev_close": prev, "volume": bb["adv"] * 0.5},
                            0.5, S, set(), bench={"r": 0.0}, div=0.0)
check("same drop without a distribution -> BIG_MOVE_DOWN", kinds(got) == ["AGG:ETF_BIG_MOVE_DOWN"])
rs = bx["resid_sigma"]
r_xle = bx["beta"] * 0.002 + 2.4 * rs
expect_rel = abs(r_xle) / bx["sigma"] < esp.BIG_Z
got = ev("XLE", r_xle, 1.0, bench_r=0.002)
check("XLE beta-adjusted gap 2.4x -> OUTPERFORM",
      kinds(got) == ["XLE:ETF_OUTPERFORM"] if expect_rel else True, f"{kinds(got)} total_z={r_xle / bx['sigma']:.2f}")
check("relative not evaluated for an S&P clone", ev("VOO", 0.6 * sv, 1.0, bench_r=-0.02) == [])
sp = base_of("PSP")["sigma"]
check("thin tape: PSP 4x move on 300 shares -> nothing", ev("PSP", 4 * sp, 1.0, vol=300) == [])
check("PSP 4x move on a live tape -> BIG_MOVE_UP", kinds(ev("PSP", 4 * sp, 1.0)) == ["PSP:ETF_BIG_MOVE_UP"])
check("momentum needs 10K shares", ev("PSP", 1.9 * sp, 3.0, vol=9000, elapsed=1.0) == [])
txt = ev("VOO", 1.9 * sv, 2.0)[0]["summary"]
check("momentum text has price and volume context", "$" in txt and "expected" in txt and "20-day average" in txt, txt)
txt = ev("VOO", 1.9 * sv, 2.0, elapsed=1.0)[0]["summary"]
check("closing momentum text uses 20-day average wording", "20-day average volume" in txt, txt)

print("== session review ==")
vs = esp.bars_to_sessions(fake_aggs("VOO", 1, "day", "x", MON.isoformat()), MON)
got = esp.evaluate_session("VOO", "VOO", vs, set())
check("VOO 5 straight up closes -> STREAK_UP", kinds(got) == ["VOO:ETF_STREAK_UP"], kinds(got))
check("streak summary names the run", "5 straight sessions" in got[0]["summary"] and "Fri Oct 9" in got[0]["summary"],
      got[0]["summary"])
vs6 = vs + [dict(vs[-1], date=MON, c=vs[-1]["c"] * 1.002)]
check("6th up close -> no repeat", [a for a in esp.evaluate_session("VOO", "VOO", vs6, set())
                                     if "STREAK" in a["filing_type"]] == [])
ts = esp.bars_to_sessions(fake_aggs("TQQQ", 1, "day", "x", MON.isoformat()), MON)
got = esp.evaluate_session("TQQQ", "TQQQ", ts, set())
check("TQQQ crosses -10% -> DRAWDOWN_10 only", kinds(got) == ["TQQQ:ETF_DRAWDOWN_10"], kinds(got))
check("drawdown cooldown via dedup key", esp.evaluate_session("TQQQ", "TQQQ", ts,
                                                             {esp.dedup_key("TQQQ", "ETF_DRAWDOWN_10")}) == [])
for t in ("AGG", "XLE", "PSP"):
    s_ = esp.bars_to_sessions(fake_aggs(t, 1, "day", "x", MON.isoformat()), MON)
    check(f"{t}: no session signal", esp.evaluate_session(t, t, s_, set()) == [],
          kinds(esp.evaluate_session(t, t, s_, set())))
check("feature ids: momentum=7, others=17",
      feature_map.resolve_feature("ETF_SIGNAL", "ETF_MOMENTUM_UP")[0] == 7 and
      all(feature_map.resolve_feature("ETF_SIGNAL", f)[0] == 17 for f in feature_map.FEATURES[17]["filing_types"]))
check("legacy ETF_FLOW still Feature 7", feature_map.resolve_feature("ETF_FLOW", "BULLISH_MOMENTUM")[0] == 7)

print("== total return: distributions are added back ==")
# INC pays 0.8% a month; its price drifts down by exactly the payouts, so its
# total return is flat. Unadjusted it looks like a fund sliding into a drawdown.
inc_c, inc_v, divs, p = [], [], [], 50.0
for i, d in enumerate(DAYS):
    if i and d.day <= 7 and DAYS[i - 1].month != d.month:      # first session of each month
        cash = round(p * 0.008, 4)
        divs.append({"ex_date": d.isoformat(), "cash": cash})
        p -= cash
    p *= 1 + rng.gauss(0, 0.0015)
    inc_c.append(p)
    inc_v.append(2e6 * rng.lognormvariate(0, 0.2))
MKT["INC"] = (inc_c, inc_v)
DIVHIST["INC"] = divs
raw_ses = esp.bars_to_sessions(fake_aggs("INC", 1, "day", "x", MON.isoformat(), _count=False), MON)
unadj = esp.apply_distributions([dict(x) for x in raw_ses], None)
adj = esp.apply_distributions([dict(x) for x in raw_ses], divs)
check("last session's total-return close equals its real close", abs(adj[-1]["ac"] - adj[-1]["c"]) < 1e-9)
check("distributions land on their ex-date sessions", sum(1 for x in adj if x["div"]) == len(
      [d for d in divs if raw_ses[0]["date"] < _dt.date.fromisoformat(d["ex_date"]) <= raw_ses[-1]["date"]]))
dd_raw = esp.drawdown_at(unadj, len(unadj) - 1)[0]
dd_adj = esp.drawdown_at(adj, len(adj) - 1)[0]
check("unadjusted, the income fund looks >5% under water", dd_raw < -0.05, f"{dd_raw:.3f}")
check("on total return it is not", dd_adj > -0.05, f"{dd_adj:.3f}")
ex_days = [i for i, x in enumerate(adj) if x["div"]]
r_raw = [unadj[i]["c"] / unadj[i - 1]["c"] - 1 for i in ex_days]
r_adj = [adj[i]["ac"] / adj[i - 1]["ac"] - 1 for i in ex_days]
check("ex-date returns: raw shows the payout as a drop, total return does not",
      max(r_raw) < -0.003 and min(abs(x) for x in r_adj) < 0.006 and
      sum(abs(x) for x in r_adj) < sum(abs(x) for x in r_raw) / 2, (r_raw[:3], r_adj[:3]))
alerts_unadj = [a for i in range(260, len(unadj)) for a in esp.evaluate_session("INC", "INC", unadj, set(), idx=i)]
alerts_adj = [a for i in range(260, len(adj)) for a in esp.evaluate_session("INC", "INC", adj, set(), idx=i)]
check("no false drawdown alerts on total return",
      not [a for a in alerts_adj if "DRAWDOWN" in a["filing_type"]],
      kinds(alerts_adj))
print(f"   (unadjusted would have raised: {kinds(alerts_unadj) or 'nothing'})")

print("== scheduled run: gating ==")
reset_db()
for k in CALLS:
    CALLS[k] = 0
freeze(2026, 10, 10, 11, 0)           # Saturday
esp.run_etf_signals()
check("Saturday: no API calls, no rows", sum(CALLS.values()) == 0 and DB["alerts"] == [])
freeze(2026, 10, 12, 7, 30)            # Monday before review time
esp._review.update(date=None, done=set())
esp.run_etf_signals()
check("Mon 07:30: nothing yet", sum(CALLS.values()) == 0 and DB["alerts"] == [])
reset_db(column=False)
freeze(2026, 10, 12, 11, 0)
esp.run_etf_signals()
check("no etf_signals_beta column: engine idle", sum(CALLS.values()) == 0 and DB["alerts"] == [])
reset_db(beta=False)
esp.run_etf_signals()
check("column present, nobody opted in: idle", sum(CALLS.values()) == 0 and DB["alerts"] == [])

print("== late daily bars are retried, not skipped for the day ==")
reset_db()
esp._review.update(date=None, done=set())
esp._bars.update(session=None, data={})
LATE = {"PSP"}


def late_aggs(t, *a, **k):
    bars = fake_aggs(t, *a, **k)
    return [b for b in bars if b["t"] != bar_t(FRI)] if t in LATE else bars


massive_client.get_aggregates = late_aggs
freeze(2026, 10, 12, 8, 30)
esp.run_etf_signals()
check("fund with Friday's bar missing is not marked reviewed", "PSP" not in esp._review["done"]
      and {"VOO", "AGG", "XLE", "TQQQ"} <= esp._review["done"])
LATE.clear()
freeze(2026, 10, 12, 8, 45)
esp.run_etf_signals()
check("next run refetches and reviews it", "PSP" in esp._review["done"])
massive_client.get_aggregates = fake_aggs

print("== dividend history failures are retried, not cached ==")
esp._bars.update(session=None, data={})
DIV_FAIL.add("AGG")
e1 = esp._entry("AGG", MON)
check("failed dividend read: bars kept, marked not-ok", e1["sessions"] and not e1["div_ok"])
DIV_FAIL.clear()
DIVHIST["AGG"] = [{"ex_date": "2026-09-01", "cash": 0.25}]
e2 = esp._entry("AGG", MON)
check("next read retries and adjusts", e2["div_ok"] and any(x["div"] for x in e2["sessions"]))
del DIVHIST["AGG"]

print("== a failed write does not mark the review done ==")
reset_db()
esp._review.update(date=None, done=set())
esp._bars.update(session=None, data={})
FAIL_INSERT.add("alerts")
freeze(2026, 10, 12, 8, 30)
esp.run_etf_signals()
check("insert failed -> VOO/TQQQ not marked reviewed, nothing stored",
      not ({"VOO", "TQQQ"} & esp._review["done"]) and DB["alerts"] == [] and "AGG" in esp._review["done"])
FAIL_INSERT.clear()
freeze(2026, 10, 12, 8, 45)
esp.run_etf_signals()
check("next run writes them", kinds(DB["alerts"]) == ["TQQQ:ETF_DRAWDOWN_10", "VOO:ETF_STREAK_UP"], kinds(DB["alerts"]))

print("== scheduled run: Monday 08:30 review then 11:00 intraday ==")
reset_db()
for k in CALLS:
    CALLS[k] = 0
esp._review.update(date=None, done=set())
esp._bars.update(session=None, data={})
freeze(2026, 10, 12, 8, 30)
esp.run_etf_signals()
rows = kinds(DB["alerts"])
check("08:30 review -> VOO streak + TQQQ drawdown", rows == ["TQQQ:ETF_DRAWDOWN_10", "VOO:ETF_STREAK_UP"], rows)
check("08:30: no snapshot call before the open", CALLS["snapshot"] == 0)
esp.run_etf_signals()
check("review runs once per day (5 funds, bars fetched once)", len(DB["alerts"]) == 2 and CALLS["aggs"] == 5, CALLS["aggs"])

now = _dt.datetime(2026, 10, 12, 11, 0, tzinfo=ET)
sv, sa = base_of("VOO")["sigma"], base_of("AGG")["sigma"]
bx = base_of("XLE")
set_snapshot(now, {"VOO": 1.9 * sv, "AGG": -max(2.8 * sa, 0.008), "XLE": bx["beta"] * 0.002 + 2.4 * bx["resid_sigma"],
                   "PSP": 0.004, "TQQQ": 0.03}, {"VOO": 2.0})
freeze(2026, 10, 12, 11, 0)
esp.run_etf_signals()
rows = kinds([a for a in DB["alerts"] if "STREAK" not in a["filing_type"] and "DRAWDOWN" not in a["filing_type"]])
check("11:00 intraday -> AGG big drop, VOO momentum, XLE outperform",
      rows == ["AGG:ETF_BIG_MOVE_DOWN", "VOO:ETF_MOMENTUM_UP", "XLE:ETF_OUTPERFORM"], rows)
check("MSTY (watched, not in etf_master) never evaluated", all(a["ticker"] != "MSTY" for a in DB["alerts"]))
n = len(DB["alerts"])
freeze(2026, 10, 12, 11, 15)
esp.run_etf_signals()
check("same conditions 15 min later -> no duplicates", len(DB["alerts"]) == n)

check("one daily-bars call per fund per session (cached)", CALLS["aggs"] == 6, CALLS["aggs"])
DB["alerts"] = [a for a in DB["alerts"] if a["ticker"] != "AGG"]
esp._bars.update(session=None, data={})
DIVHIST["AGG"] = [{"ex_date": MON.isoformat(), "cash": round(base_of("AGG")["last_close"] * 0.0085, 4)}]
set_snapshot(now, {"VOO": 1.9 * sv, "AGG": -0.0085}, {"VOO": 2.0})
freeze(2026, 10, 12, 11, 30)
esp.run_etf_signals()
check("AGG dropping exactly its distribution on its ex-date -> no alert",
      not [a for a in DB["alerts"] if a["ticker"] == "AGG"])
del DIVHIST["AGG"]
esp._bars.update(session=None, data={})
x = next(a for a in DB["alerts"] if a["filing_type"] == "ETF_MOMENTUM_UP")
check("row tagged Feature 7 + engine + dedup key", x["extra"]["feature_id"] == 7 and
      x["extra"]["engine"] == "etf_signals_v2" and x["extra"]["dedup_key"].endswith(":2026-10-12"))
import json  # noqa: E402
check("every row JSON-serialisable", all(json.dumps(a) for a in DB["alerts"]))

set_snapshot(now, {"VOO": 0.03}, {}, live=False)
before = len(DB["alerts"])
freeze(2026, 10, 12, 11, 30)
esp._review.update(date=MON, done={"VOO", "AGG", "XLE", "PSP", "TQQQ"})
DB["alerts"] = [a for a in DB["alerts"] if "VOO" not in a["ticker"]]
esp.run_etf_signals()
check("stale snapshot (last update Friday) -> nothing for any fund",
      len([a for a in DB["alerts"] if a["ticker"] == "VOO"]) == 0)
FAIL_TABLES.add("alerts")
set_snapshot(now, {"VOO": 0.03}, {})
esp.run_etf_signals()
FAIL_TABLES.discard("alerts")
check("dedup ledger unreadable -> fail closed", len([a for a in DB["alerts"] if a["ticker"] == "VOO"]) == 0)

print("== delivery routing ==")
reset_db()
users = delivery._fetch_active_users()
check("prefs read with *, beta flag parsed", users[ME]["etf_signals_beta"] and not users[OTHER]["etf_signals_beta"])
reset_db(column=False)
users_nc = delivery._fetch_active_users()
check("prefs without the column still load (no delivery outage)", len(users_nc) == 2 and
      not users_nc[ME]["etf_signals_beta"])
reset_db()
users = delivery._fetch_active_users()
watch, _ = delivery._fetch_watchers(["XLE", "VOO", "PSP", "AAPL", "SPY"])


def aud(source, ft, ticker, extra=None, impact="MEDIUM"):
    a = {"source": source, "filing_type": ft, "ticker": ticker, "impact": impact, "extra": extra or {}}
    return sorted(u["user_id"] for u, _ in delivery.resolve_audience(a, users, watch))


check("v2 XLE signal -> only the opted-in watcher", aud("ETF_SIGNAL", "ETF_OUTPERFORM", "XLE") == [ME])
check("v2 momentum -> opted-in watcher", aud("ETF_SIGNAL", "ETF_MOMENTUM_UP", "VOO") == [ME])
check("v1 XLE seed broadcast -> other user only (both legs), not the beta user",
      aud("ETF_FLOW", "BULLISH_MOMENTUM", "XLE") == [OTHER])
check("v1 SPY seed broadcast -> unchanged for non-beta (market-wide leg)",
      aud("ETF_FLOW", "BULLISH_MOMENTUM", "SPY") == [OTHER])
check("v1 PSP watchlist_only -> nobody (beta user moved to v2)",
      aud("ETF_FLOW", "BULLISH_MOMENTUM", "PSP", {"watchlist_only": True}) == [])
check("technical VOO -> unchanged", aud("TECHNICAL", "VOLUME_SPIKE", "VOO") == [ME])
check("technical AAPL -> unchanged", aud("TECHNICAL", "VOLUME_SPIKE", "AAPL") == [OTHER])
users[ME]["muted_features"].add(17)
check("mute 17 silences price signals but not v2 momentum",
      aud("ETF_SIGNAL", "ETF_BIG_MOVE_UP", "VOO") == [] and aud("ETF_SIGNAL", "ETF_MOMENTUM_UP", "VOO") == [ME])
users[ME]["muted_features"].discard(17)

print("== end to end: engine -> delivery -> Telegram ==")
reset_db()
esp._review.update(date=None, done=set())
freeze(2026, 10, 12, 11, 0)
set_snapshot(now, {"VOO": 1.9 * sv, "AGG": -max(2.8 * sa, 0.008), "XLE": bx["beta"] * 0.002 + 2.4 * bx["resid_sigma"]},
             {"VOO": 2.0})
esp.run_etf_signals()
SENT = []


class FakeBot:
    def __init__(self, token=None):
        pass

    async def send_message(self, chat_id, text, **k):
        SENT.append((str(chat_id), text))


delivery.Bot = FakeBot
delivery.PER_CHAT_GAP_SECONDS = 0
delivery.SEND_GAP_SECONDS = 0
asyncio.run(delivery.deliver_pending_alerts())
to_me = [t for c, t in SENT if c == "111"]
to_other = [t for c, t in SENT if c == "222"]
expected_rows = len(DB["alerts"])
check(f"all {expected_rows} signals delivered to the opted-in user", len(to_me) == expected_rows == 5,
      (len(to_me), expected_rows, kinds(DB["alerts"])))
check("other user (watches XLE) received none of them", to_other == [])
check("deliveries ledgered SENT", sum(1 for d in DB["alert_deliveries"] if d["status"] == "SENT") == expected_rows)
check("every message well-formed and < 4096 chars",
      all(len(t) < 4096 and t.count("<b>") == t.count("</b>") and t.count("<a ") == t.count("</a>") for t in to_me))
check("footers name Feature 7 and 17", any("Feature 7 — ETF Flow Alerts" in t for t in to_me) and
      any("Feature 17 — ETF Price Signals" in t for t in to_me))
if "-v" in sys.argv:
    print("\n--- sample messages ---")
    for t in to_me:
        print(re.sub(r"<[^>]+>", "", t))
        print("-" * 60)

print("== v1 poller fixes ==")
reset_db(beta=False)
LEG = {}


def fake_single(t):
    CALLS["single"] += 1
    return LEG.get(t)


massive_client.get_snapshot = fake_single
efp.massive_client.get_snapshot = fake_single
fri_close = _dt.datetime(2026, 10, 9, 16, 0, tzinfo=ET)
LEG["PSP"] = {"updated": ns(fri_close), "day": {"c": 56.50, "v": 26957}, "prevDay": {"c": 55.90, "v": 15800}}
DB["watchlists"].append({"user_id": ME, "ticker": "PSP"})
CALLS["single"] = 0
for when in ((2026, 10, 10, 0, 15), (2026, 10, 11, 0, 16)):
    freeze(*when)
    efp.run_etf_flow_poller()
check("Sat 00:15 / Sun 00:16 (the two PSP repeats): no run at all", CALLS["single"] == 0)
for when in ((2026, 10, 12, 0, 15), (2026, 10, 12, 9, 0)):
    freeze(*when)
    efp.run_etf_flow_poller()
check("Mon 00:15 / 09:00 still holding Friday's bar: runs, but sends nothing",
      CALLS["single"] > 0 and not [a for a in DB["alerts"] if a["source"] == "ETF_FLOW"])
freeze(2026, 10, 12, 16, 15)
efp.run_etf_flow_poller()
check("Mon 16:15 with Friday's snapshot -> rejected as stale",
      not [a for a in DB["alerts"] if a["source"] == "ETF_FLOW"])
mon = _dt.datetime(2026, 10, 12, 15, 15, tzinfo=ET)
LEG["PSP"] = {"updated": ns(mon), "day": {"c": 56.63, "v": 24064}, "prevDay": {"c": 55.90, "v": 16000}}
freeze(2026, 10, 12, 15, 15)
efp.run_etf_flow_poller()
leg = [a for a in DB["alerts"] if a["source"] == "ETF_FLOW"]
check("fresh qualifying snapshot -> one v1 alert", len(leg) == 1 and leg[0]["ticker"] == "PSP")
check("v1 text now carries the price", leg and "Price: $56.63 (+1.31%)" in leg[0]["summary"],
      leg[0]["summary"] if leg else "")
freeze(2026, 10, 12, 19, 15)
efp.run_etf_flow_poller()
check("same day later -> deduped", len([a for a in DB["alerts"] if a["source"] == "ETF_FLOW"]) == 1)
DB["alerts"] = [a for a in DB["alerts"] if a["source"] != "ETF_FLOW"]
late = _dt.datetime(2026, 10, 12, 21, 0, tzinfo=ET)
LEG["PSP"] = {"updated": ns(late), "day": {"c": 56.63, "v": 24064}, "prevDay": {"c": 55.90, "v": 16000}}
freeze(2026, 10, 12, 21, 0)
efp.run_etf_flow_poller()
check("post-market same-day run (21:00) still allowed", len([a for a in DB["alerts"] if a["source"] == "ETF_FLOW"]) == 1)
LEG["PSP"] = {"day": {"c": 56.63, "v": 24064}, "prevDay": {"c": 55.90, "v": 16000}}
DB["alerts"] = [a for a in DB["alerts"] if a["source"] != "ETF_FLOW"]
freeze(2026, 10, 13, 0, 15)
efp.run_etf_flow_poller()
check("no timestamp at all + before the open -> treated as stale",
      not [a for a in DB["alerts"] if a["source"] == "ETF_FLOW"])
check("v1 universe via paginated watchlist read", "PSP" in {e["ticker"] for e in efp.build_universe()})

print("== backtest CLI ==")
reset_db()
freeze(2026, 10, 12, 8, 0)
universe = {t: t for t in ("VOO", "AGG", "XLE", "PSP", "TQQQ")}
esp._bars.update(session=None, data={})
counts = esp.backtest(15, universe)
check("backtest runs and finds Friday's VOO streak + TQQQ drawdown",
      counts.get("ETF_STREAK_UP", 0) >= 1 and counts.get("ETF_DRAWDOWN_10", 0) >= 1, counts)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
