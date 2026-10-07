"""
etf_fmp_client.py
GQ FinXray US — FMP (stable API) access for the ETF features.

Reuses fmp_client._get, so retries and the persistent-429 FMPError behave
exactly like every other FMP caller in the codebase. Adds the one thing the
shared client does not have: a process-wide request-rate ceiling. The ETF
universe refresh walks ~4,150 funds, and without a ceiling it would burst
straight through the plan's per-minute allowance and starve the news,
analyst and earnings pollers that share the same key.

Endpoints (confirmed live against SPY on 2026-10-02):
  holdings            -> /stable/etf/holdings            (asset, name, isin, securityCusip,
                                                          sharesNumber, weightPercentage,
                                                          marketValue, updatedAt)
  info                -> /stable/etf/info                (expenseRatio, assetsUnderManagement,
                                                          nav, etfCompany, assetClass, ...)
  sector weights      -> /stable/etf/sector-weightings   (weightPercentage: number)
  country weights     -> /stable/etf/country-weightings  (weightPercentage: "97.79%" string)
  profile             -> /stable/profile                 (isin, cusip, beta, industry)
  returns             -> /stable/stock-price-change      (1D 5D 1M 3M 6M ytd 1Y 3Y 5Y 10Y max)

Units: every percentage FMP returns here is already in percent
(weightPercentage 8.36 = 8.36%, expenseRatio 0.09 = 0.09%). Nothing in this
module divides by 100.

Run `python etf_fmp_client.py SPY` to probe every endpoint.
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import date

from fmp_client import _get, FMPError  # noqa: F401  (FMPError re-exported for the poller)

# FMPError is re-exported: etf_data_poller catches fmp.FMPError to stop a run on
# a persistent 429 instead of grinding through thousands of doomed calls.
__all__ = ["FMPError"]

# ── Rate ceiling ──────────────────────────────────────────────────────────────
ETF_FMP_RPS = float(os.getenv("ETF_FMP_RPS", "6"))
_rate_lock = threading.Lock()
_next_slot = [0.0]


def _throttle():
    gap = 1.0 / ETF_FMP_RPS if ETF_FMP_RPS > 0 else 0.0
    if not gap:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = _next_slot[0] - now
        _next_slot[0] = max(now, _next_slot[0]) + gap
    if wait > 0:
        time.sleep(wait)


def _fmp(path, params):
    _throttle()
    return _get(path, params)


# ── Small parsers ─────────────────────────────────────────────────────────────
def num(v):
    """Float or None. Accepts '97.79%' and '1,234' as FMP sometimes sends."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").rstrip("%").strip()
    if s in ("", "None", "null", "NaN", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _first(data):
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def _clip(v, n):
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] if s else None


def _date_or_none(v):
    s = (str(v).strip()[:10] if v else "")
    return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None


# ── Raw calls ─────────────────────────────────────────────────────────────────
def raw_holdings(ticker):
    data = _fmp("etf/holdings", {"symbol": ticker})
    return data if isinstance(data, list) else []


def raw_info(ticker):
    return _first(_fmp("etf/info", {"symbol": ticker}))


def raw_sector_weights(ticker):
    data = _fmp("etf/sector-weightings", {"symbol": ticker})
    return data if isinstance(data, list) else []


def raw_country_weights(ticker):
    data = _fmp("etf/country-weightings", {"symbol": ticker})
    return data if isinstance(data, list) else []


def raw_profile(ticker):
    return _first(_fmp("profile", {"symbol": ticker}))


def raw_price_change(ticker):
    return _first(_fmp("stock-price-change", {"symbol": ticker}))


# ── Holdings ──────────────────────────────────────────────────────────────────
_BLANK = {"", "-", "N/A", "NA", "NULL", "NONE"}


_UNSAFE = re.compile(r'["\\]')


def security_key(h):
    """Stable identity for one holding line: symbol, else ISIN, else CUSIP, else name.

    Double quotes and backslashes are stripped: postgrest-py quotes an in_()
    value containing , : ( ) but does not escape an embedded quote, so a key
    like NAME:FOO "A" SHARES would break the stale-holding delete."""
    key = _security_key_raw(h)
    return _UNSAFE.sub("", key).strip() or None if key else None


def _security_key_raw(h):
    sym = (h.get("asset") or h.get("symbol_held") or "").strip().upper()
    if sym and sym not in _BLANK:
        return sym[:64]
    isin = (h.get("isin") or "").strip().upper()
    if isin and isin not in _BLANK:
        return "ISIN:" + isin
    cusip = (h.get("securityCusip") or h.get("cusip") or "").strip().upper()
    if cusip and cusip not in _BLANK:
        return "CUSIP:" + cusip
    name = re.sub(r"\s+", " ", (h.get("name") or "").strip().upper())
    return ("NAME:" + name[:120]) if name else None


def normalize_holdings(raw):
    """
    -> (rows, version)

    rows    {security_key: {security_key, security_symbol, security_name, isin,
                            cusip, portfolio_pct, market_value, shares}}
    version string identifying THIS snapshot: FMP's own updatedAt plus a hash
            of the security set. Unchanged version == nothing to diff.

    A security listed on several lines (share classes, lots) is merged and its
    weights summed, so it cannot show up as added and removed at once.
    """
    rows = {}
    stamps = []
    for h in raw or []:
        if not isinstance(h, dict):
            continue
        key = security_key(h)
        if not key:
            continue
        w = num(h.get("weightPercentage"))
        mv = num(h.get("marketValue"))
        sh = num(h.get("sharesNumber"))
        if h.get("updatedAt"):
            stamps.append(str(h["updatedAt"]))
        if key in rows:
            r = rows[key]
            r["portfolio_pct"] = (r["portfolio_pct"] or 0) + (w or 0)
            r["market_value"] = (r["market_value"] or 0) + (mv or 0) if (mv is not None or r["market_value"] is not None) else None
            r["shares"] = (r["shares"] or 0) + (sh or 0) if (sh is not None or r["shares"] is not None) else None
            continue
        sym = (h.get("asset") or "").strip().upper()
        rows[key] = {
            "security_key": key,
            "security_symbol": (sym if sym and sym not in _BLANK else None),
            "security_name": (h.get("name") or "").strip() or None,
            "isin": (h.get("isin") or "").strip() or None,
            "cusip": (h.get("securityCusip") or "").strip() or None,
            "portfolio_pct": w,
            "market_value": mv,
            "shares": sh,
        }
    if not rows:
        return {}, None
    keyhash = hashlib.sha1("|".join(sorted(rows)).encode()).hexdigest()[:12]
    version = f"{max(stamps) if stamps else 'nostamp'}#{keyhash}"
    return rows, version


def snapshot_as_of(version):
    """The FMP timestamp part of a version string, or None."""
    if not version:
        return None
    stamp = version.split("#", 1)[0]
    return None if stamp == "nostamp" else stamp


# ── Row builders (shape = Supabase columns) ───────────────────────────────────
def expense_ratio(info):
    """Expense ratio in percent (0.09 = 0.09%), or None."""
    if not info:
        return None
    er = num(info.get("expenseRatio"))
    # FMP sends 0 for "not populated yet". Treating it as a real ratio made it
    # the baseline, and the real value arriving later alerted as a +35bp hike.
    return er if er is not None and er > 0 else None


def master_row(ticker, name, info):
    """etf_master upsert row. aum is BIGINT in the live table: always an int."""
    info = info or {}
    aum = num(info.get("assetsUnderManagement") or info.get("aum") or info.get("netAssets"))
    hc = num(info.get("holdingsCount"))
    av = num(info.get("avgVolume"))
    row = {
        "ticker": ticker,
        "name": (name or info.get("name") or ticker)[:500],
        "fund_house": _clip(info.get("etfCompany") or info.get("fundFamily"), 255),
        "category": _clip(info.get("assetClass") or info.get("category"), 100),
        "inception_date": _date_or_none(info.get("inceptionDate")),
        "aum": int(round(aum)) if aum is not None else None,
        "nav": num(info.get("nav")),
        "nav_currency": _clip(info.get("navCurrency"), 10),
        "holdings_count": int(hc) if hc is not None else None,
        "isin": _clip(info.get("isin"), 20),
        "cusip": _clip(info.get("securityCusip"), 20),
        "domicile": _clip(info.get("domicile"), 20),
        "website": _clip(info.get("website"), 500),
        "avg_volume": av,
        "is_actively_trading": info.get("isActivelyTrading") if isinstance(info.get("isActivelyTrading"), bool) else None,
    }
    return row


def expense_row(ticker, er, as_of):
    return {
        "ticker": ticker,
        "gross_expense_ratio": er,
        "net_expense_ratio": er,
        "management_fee": None,
        "other_expenses": None,
        "date": as_of,
    }


def performance_row(ticker, pc, as_of):
    if not pc:
        return None
    return {
        "ticker": ticker,
        "return_1m": num(pc.get("1M")),
        "return_3m": num(pc.get("3M")),
        "return_6m": num(pc.get("6M")),
        "return_ytd": num(pc.get("ytd") if pc.get("ytd") is not None else pc.get("YTD")),
        "return_1yr": num(pc.get("1Y")),
        "return_3yr": num(pc.get("3Y")),
        "return_5yr": num(pc.get("5Y")),
        "return_10y": num(pc.get("10Y")),
        "return_max": num(pc.get("max")),
        "date": as_of,
    }


def company_row(ticker, info, profile, sectors, countries):
    info, profile = info or {}, profile or {}
    sw = sorted(({"sector": s.get("sector"), "weight": num(s.get("weightPercentage"))}
                 for s in (sectors or []) if s.get("sector")),
                key=lambda s: s["weight"] or 0, reverse=True)
    cw = sorted(({"country": c.get("country"), "weight": num(c.get("weightPercentage"))}
                 for c in (countries or []) if c.get("country")),
                key=lambda c: c["weight"] or 0, reverse=True)
    if not (info or profile or sw or cw):
        return None
    return {
        "ticker": ticker,
        "isin": _clip(profile.get("isin") or info.get("isin"), 20),
        "cusip": _clip(profile.get("cusip") or info.get("securityCusip"), 20),
        "sector": _clip(sw[0]["sector"] if sw else profile.get("sector"), 100),
        "industry": _clip(profile.get("industry"), 100),
        "asset_class": _clip(info.get("assetClass"), 50),
        "beta": num(profile.get("beta")),
        "top_country": _clip(cw[0]["country"] if cw else None, 100),
        "sector_weights": sw or None,
        "country_weights": cw or None,
    }


# ── Probe ─────────────────────────────────────────────────────────────────────
def _show(label, data):
    if isinstance(data, list):
        print(f"\n=== {label}: {len(data)} rows ===")
        if data:
            print(json.dumps(data[0], indent=2, default=str)[:1500])
    elif isinstance(data, dict):
        print(f"\n=== {label}: dict ===")
        print(json.dumps(data, indent=2, default=str)[:1500])
    else:
        print(f"\n=== {label}: NO DATA ({data!r}) ===")


if __name__ == "__main__":
    t = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    today = date.today().isoformat()
    print(f"Probing FMP ETF endpoints for {t}")
    info = raw_info(t)
    hold = raw_holdings(t)
    _show("etf/holdings", hold)
    _show("etf/info", info)
    rows, version = normalize_holdings(hold)
    print(f"\nnormalised holdings: {len(rows)}  version={version}")
    print("expense ratio:", expense_ratio(info))
    print("master row:", master_row(t, None, info))
    print("performance:", performance_row(t, raw_price_change(t), today))
    print("company:", company_row(t, info, raw_profile(t), raw_sector_weights(t), raw_country_weights(t)))
