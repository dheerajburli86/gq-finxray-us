"""
mf_company_data.py
GQ FinXray US — company data for what mutual funds hold.

The India platform carries a company master behind its fund data — company
code, full and short name, ISIN, exchange codes, category, market-cap class,
sector and industry — so a portfolio alert can say WHAT a fund bought, not just
the line of text the filing used. This is the US equivalent of that master.

Where each field comes from
---------------------------
  from SEC Form N-PORT itself (already in mf_holdings, written by mf_poller):
      issuer name, title of the issue, ticker, CUSIP, ISIN, LEI, asset category
  from the platform's own stocks table (SEC + FMP, already loaded):
      sector, exchange, company name
  from FMP's profile endpoint, for names the stocks table does not carry:
      sector, industry, market cap, exchange, country, currency, CIK, ISIN

US equivalents of the India columns:
      Company code    -> SEC CIK          (the US registrant number)
      BSE / NSE code  -> exchange + ticker (NYSE, NASDAQ, …)
      BSE group       -> no US equivalent; US exchanges have no A/B/T groups
      ISIN            -> ISIN, as reported in the fund's own N-PORT filing
      market-cap class-> LARGE_CAP / MID_CAP / SMALL_CAP / MICRO_CAP, by the
                         usual US thresholds ($10bn / $2bn / $300m)

This module does NOT send alerts of its own. It is the reference layer the
mutual-fund portfolio alerts read, so an addition can be described as "Nvidia
(NVDA), large-cap Technology" rather than a bare name out of a filing.

Scope: positions held by mutual funds, filled newest-held first, with a budget
per run — the long tail of a fund's bond book is never worth an API call.

Job (scheduled separately in main.py)
-------------------------------------
  run_mf_company_data()   daily

Table: mf_security_master.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import mf_common as mc

POLLER = "mf_company_data"
TAG = "MF_COMPANY"

FMP_BUDGET = int(os.getenv("MF_COMPANY_FMP_BUDGET", "250"))      # profile calls per run
REFRESH_DAYS = int(os.getenv("MF_COMPANY_REFRESH_DAYS", "30"))   # re-check a known name
MIN_WEIGHT = float(os.getenv("MF_COMPANY_MIN_WEIGHT", "0.25"))   # % of a fund, to be worth filling

# US market-cap bands, the ones every US screen uses.
CAP_BANDS = ((200e9, "MEGA_CAP"), (10e9, "LARGE_CAP"), (2e9, "MID_CAP"),
             (300e6, "SMALL_CAP"), (50e6, "MICRO_CAP"))

# N-PORT asset categories. EC is an equity position in a common share; the rest
# are debt, derivatives and cash-like lines that have no company profile to look up.
EQUITY_CATEGORIES = {"EC", "EP", "RE"}


def log(msg):
    mc.log(TAG, msg)


def _err(job, e, ctx=None):
    mc.log_poller_error(POLLER, job, e, ctx)


def cap_class(market_cap):
    v = mc.num(market_cap)
    if v is None or v <= 0:
        return None
    for floor, name in CAP_BANDS:
        if v >= floor:
            return name
    return "NANO_CAP"


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

def known_securities() -> dict:
    """{security_key: row} already in the master."""
    try:
        rows = mc.paged(lambda: mc.sb().table("mf_security_master")
                        .select("security_key,ticker,sector,industry,market_cap,"
                                "cap_class,updated_at,source").order("security_key"))
        return {r["security_key"]: r for r in rows if r.get("security_key")}
    except Exception as e:
        _err("known_securities", e)
        return {}


def stock_reference(tickers) -> dict:
    """{TICKER: row} from the platform's own stocks table — free, no API call."""
    out, tickers = {}, sorted({t for t in tickers if t})
    for i in range(0, len(tickers), 150):
        chunk = tickers[i:i + 150]
        try:
            rows = (mc.sb().table("stocks").select("*").in_("ticker", chunk)
                    .execute().data or [])
            for r in rows:
                t = (r.get("ticker") or "").upper()
                if t:
                    out[t] = r
        except Exception as e:
            _err("stock_reference", e, {"n": len(chunk)})
    return out


def positions_needing_data(limit) -> list[dict]:
    """
    The heaviest mutual-fund positions with no company data yet.

    Heaviest first on purpose: a 6% position is what an alert will name, and a
    fund's 400th bond line is not worth an API call.
    """
    try:
        rows = mc.paged(lambda: mc.sb().table("mf_holdings")
                        .select("security_key,ticker,security_name,title,cusip,isin,"
                                "asset_category,portfolio_pct")
                        .gte("portfolio_pct", MIN_WEIGHT).order("security_key"))
    except Exception as e:
        _err("positions_needing_data", e)
        return []
    best = {}
    for r in rows:
        k = r.get("security_key")
        if not k:
            continue
        cur = best.get(k)
        if not cur or (mc.num(r.get("portfolio_pct")) or 0) > (mc.num(cur.get("portfolio_pct")) or 0):
            best[k] = r
    known = known_securities()
    stale_before = (datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)).isoformat()
    todo = []
    for k, r in best.items():
        k_row = known.get(k)
        if k_row and (k_row.get("updated_at") or "") >= stale_before:
            continue
        if k_row and k_row.get("sector") and not k_row.get("market_cap"):
            pass                              # partial row: try to finish it
        todo.append(r)
    todo.sort(key=lambda r: -(mc.num(r.get("portfolio_pct")) or 0))
    return todo[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────────

def _row_from_holding(h, stock=None, profile=None):
    cap = mc.num((profile or {}).get("marketCap") or (stock or {}).get("market_cap"))
    name = ((profile or {}).get("companyName") or (stock or {}).get("name")
            or h.get("security_name") or h.get("title"))
    row = {
        "security_key": h["security_key"],
        "ticker": (h.get("ticker") or (profile or {}).get("symbol") or "").upper() or None,
        "name": name,
        "short_name": (h.get("security_name") or name or "")[:120] or None,
        "cusip": h.get("cusip"),
        "isin": h.get("isin") or (profile or {}).get("isin"),
        "cik": (profile or {}).get("cik") or (stock or {}).get("cik"),
        "asset_category": h.get("asset_category"),
        "sector": (profile or {}).get("sector") or (stock or {}).get("sector"),
        "industry": (profile or {}).get("industry") or (stock or {}).get("industry"),
        "exchange": ((profile or {}).get("exchangeShortName") or (profile or {}).get("exchange")
                     or (stock or {}).get("exchange")),
        "country": (profile or {}).get("country") or (stock or {}).get("country"),
        "currency": (profile or {}).get("currency"),
        "market_cap": cap,
        "cap_class": cap_class(cap),
        "is_fund": bool((profile or {}).get("isEtf") or (profile or {}).get("isFund")),
        "source": "FMP" if profile else ("STOCKS" if stock else "NPORT"),
        "updated_at": mc.now_iso(),
    }
    return {k: v for k, v in row.items() if v is not None or k in ("security_key",)}


def store(rows) -> int:
    rows = [r for r in rows if r.get("security_key")]
    if not rows:
        return 0
    try:
        for i in range(0, len(rows), 300):
            (mc.sb().table("mf_security_master")
             .upsert(rows[i:i + 300], on_conflict="security_key").execute())
    except Exception as e:
        _err("store", e, {"n": len(rows)})
        return 0
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Lookup used by the portfolio alerts
# ─────────────────────────────────────────────────────────────────────────────

def describe(security_keys) -> dict:
    """
    {security_key: "large-cap Technology" | "Technology" | None}

    What mf_poller appends to a holding's name in an alert, so a line reads
    "Nvidia (NVDA), large-cap Technology" instead of a bare filing string.
    """
    keys = sorted({k for k in security_keys if k})
    out = {}
    for i in range(0, len(keys), 150):
        try:
            rows = (mc.sb().table("mf_security_master")
                    .select("security_key,sector,industry,cap_class")
                    .in_("security_key", keys[i:i + 150]).execute().data or [])
        except Exception as e:
            _err("describe", e)
            return out
        for r in rows:
            sector = r.get("sector")
            cap = (r.get("cap_class") or "").replace("_", "-").lower()
            if sector and cap:
                out[r["security_key"]] = f"{cap} {sector}"
            elif sector:
                out[r["security_key"]] = sector
            elif r.get("industry"):
                out[r["security_key"]] = r["industry"]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Job
# ─────────────────────────────────────────────────────────────────────────────

def run_mf_company_data(budget: int | None = None):
    """Daily: fill sector / industry / market cap for the biggest fund positions."""
    try:
        budget = FMP_BUDGET if budget is None else budget
        todo = positions_needing_data(budget * 4)
        if not todo:
            log("every significant mutual fund position already has company data")
            return 0

        # Pass 1 — free: the platform's own stocks table.
        ref = stock_reference([r.get("ticker") for r in todo])
        rows, needs_api = [], []
        for h in todo:
            t = (h.get("ticker") or "").upper()
            s = ref.get(t)
            cat = (h.get("asset_category") or "").upper()
            if s and s.get("sector"):
                rows.append(_row_from_holding(h, stock=s))
            elif t and (cat in EQUITY_CATEGORIES or not cat):
                needs_api.append(h)
            else:
                # A bond, a derivative or a cash line: record what N-PORT said
                # and never spend an API call on it.
                rows.append(_row_from_holding(h))

        # Pass 2 — FMP, for equities the stocks table does not carry.
        filled = 0
        try:
            import etf_fmp_client as fmp
        except Exception as e:
            _err("fmp_import", e)
            fmp = None
        if fmp is not None:
            for h in needs_api[:budget]:
                try:
                    p = fmp.raw_profile((h.get("ticker") or "").upper())
                except Exception as e:
                    _err("raw_profile", e, {"ticker": h.get("ticker")})
                    p = None
                rows.append(_row_from_holding(h, stock=ref.get((h.get("ticker") or "").upper()),
                                              profile=p or None))
                if p:
                    filled += 1
        else:
            rows.extend(_row_from_holding(h) for h in needs_api[:budget])

        n = store(rows)
        log(f"company data: {n} securities written ({len(rows) - len(needs_api[:budget])} from "
            f"stocks/N-PORT, {filled} from FMP), {max(0, len(needs_api) - budget)} still queued")
        return n
    except Exception as e:
        _err("run_mf_company_data", e)
        return 0


def coverage() -> dict:
    try:
        held = {r["security_key"] for r in mc.paged(
            lambda: mc.sb().table("mf_holdings").select("security_key")
            .gte("portfolio_pct", MIN_WEIGHT).order("security_key"))}
    except Exception:
        held = set()
    known = known_securities()
    with_sector = {k for k, r in known.items() if r.get("sector")}
    return {"significant_positions": len(held),
            "in_master": len(held & set(known)),
            "with_sector": len(held & with_sector),
            "missing": len(held - set(known))}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "coverage":
        print(coverage())
    else:
        run_mf_company_data(int(sys.argv[1]) if len(sys.argv) > 1 else None)
