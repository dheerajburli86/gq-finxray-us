"""
Quarterly income statements from SEC XBRL companyfacts.

result_snapshot.py calls exactly one name:
    sec_financials.get_income_statement_sync(cik, limit=8)

It must return a list of dicts shaped like fmp_client.get_income_statement(),
because build_result_snapshot() reads both through the same g() helper:
    date, period, fiscalYear, revenue, grossProfit, operatingIncome,
    ebitda, netIncome, epsDiluted
plus "_source", which the snapshot reads to label where the numbers came from.

This is the SEC-first half of the SEC-over-FMP priority: companyfacts carries
the same facts FMP resells, without the ingestion lag or per-call cost.
"""

import json
import logging
import os
import time
import urllib.request
from collections import defaultdict

logger = logging.getLogger(__name__)

USER_AGENT = os.getenv("SEC_USER_AGENT", "GQuants FinXray admin@gquants.com")
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
TIMEOUT = int(os.getenv("SEC_TIMEOUT", "30"))

# us-gaap tags, most specific first. Filers tag the same economic concept
# differently — a retailer's revenue may be RevenueFromContractWith...,
# an older filing's may be SalesRevenueNet — so each field tries several.
TAG_MAP = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "grossProfit": ["GrossProfit"],
    "operatingIncome": [
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ],
    "netIncome": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "epsDiluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
    "_depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
}

# Cache companyfacts per CIK — one filing season means many snapshots for the
# same company, and the payload is several MB.
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = int(os.getenv("SEC_FACTS_TTL", "3600"))


def _fetch_companyfacts(cik: str) -> dict | None:
    cik = str(cik).strip().lstrip("CIK").zfill(10)

    hit = _cache.get(cik)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]

    url = COMPANYFACTS_URL.format(cik=cik)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("[SEC-XBRL] HTTP %s for CIK %s", resp.status, cik)
                return None
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            data = json.loads(raw)
    except Exception as e:
        logger.warning("[SEC-XBRL] fetch failed for CIK %s: %s", cik, e)
        return None

    _cache[cik] = (time.time(), data)
    return data


def _quarterly_points(facts: dict, tags: list[str]) -> dict:
    """
    Pull quarterly values for the first tag that has usable data.

    Keyed by period end date. Only Q-duration facts are kept (roughly 80-100
    days) so annual and half-year filings don't get mixed into a quarterly
    series and silently overstate a quarter. Instant facts (EPS has none, but
    balance-type tags do) carry no start date and are skipped.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    for tag in tags:
        entry = us_gaap.get(tag)
        if not entry:
            continue
        by_date = {}
        for unit_key, points in entry.get("units", {}).items():
            if unit_key not in ("USD", "USD/shares"):
                continue
            for p in points:
                start, end, val = p.get("start"), p.get("end"), p.get("val")
                if not end or val is None:
                    continue
                if not start:
                    continue
                try:
                    from datetime import date
                    s = date.fromisoformat(start)
                    e = date.fromisoformat(end)
                except ValueError:
                    continue
                days = (e - s).days
                if not (60 <= days <= 115):  # quarter-ish only
                    continue
                # Later-filed values supersede earlier ones (restatements).
                prev = by_date.get(end)
                if prev is None or (p.get("filed", "") >= prev[1]):
                    by_date[end] = (val, p.get("filed", ""), p.get("fy"), p.get("fp"))
        if by_date:
            return by_date
    return {}


def get_income_statement_sync(cik, limit: int = 8) -> list:
    """
    Return up to `limit` most recent quarters, newest first.

    Returns [] on any failure or when fewer than 2 quarters are recoverable —
    result_snapshot treats an empty or too-short list as "fall back to FMP",
    which is the intended behaviour for filers whose XBRL is sparse or absent.
    """
    if not cik:
        return []

    facts = _fetch_companyfacts(cik)
    if not facts:
        return []

    series = {field: _quarterly_points(facts, tags)
              for field, tags in TAG_MAP.items()}

    if not series.get("revenue") and not series.get("netIncome"):
        logger.info("[SEC-XBRL] CIK %s: no quarterly revenue or net income", cik)
        return []

    # Union of every period end date we found any fact for.
    all_dates = set()
    for pts in series.values():
        all_dates.update(pts.keys())

    rows = []
    for end_date in sorted(all_dates, reverse=True)[:limit]:
        row = {"date": end_date, "_source": "SEC XBRL"}

        for field in ("revenue", "grossProfit", "operatingIncome",
                      "netIncome", "epsDiluted"):
            point = series.get(field, {}).get(end_date)
            row[field] = point[0] if point else None

        # EBITDA isn't an XBRL concept — US GAAP doesn't define it. Derive it
        # the standard way (operating income + D&A) only when both parts are
        # present for this exact quarter; otherwise leave it None so the
        # snapshot prints "N/A" rather than a number built on a guess.
        op = row.get("operatingIncome")
        dep_point = series.get("_depreciation", {}).get(end_date)
        if op is not None and dep_point:
            try:
                row["ebitda"] = float(op) + float(dep_point[0])
            except (TypeError, ValueError):
                row["ebitda"] = None
        else:
            row["ebitda"] = None

        # Fiscal labels ride along on whichever fact we matched.
        meta = None
        for field in ("revenue", "netIncome", "operatingIncome"):
            meta = series.get(field, {}).get(end_date)
            if meta:
                break
        if meta:
            row["fiscalYear"] = meta[2]
            row["period"] = meta[3]
        else:
            row["fiscalYear"] = None
            row["period"] = None

        rows.append(row)

    # A row with no revenue AND no net income is noise, not a quarter.
    rows = [r for r in rows
            if r.get("revenue") is not None or r.get("netIncome") is not None]

    logger.info("[SEC-XBRL] CIK %s: %d quarters", cik, len(rows))
    return rows


# Convenience alias — some call sites use the async-free name directly.
get_income_statement = get_income_statement_sync
