"""
sec_fee_tables.py
GQ FinXray US — the prospectus FEE TABLE, read out of inline XBRL.

A pure parser: HTML in, numbers out. No product logic, no database, no alerts,
no network. ETFs and mutual funds both file the same fee table under the SEC's
risk/return taxonomy, so the parsing is shared while the two product families
keep entirely separate pollers around it.

Since 2011 the fee table rows are XBRL-tagged (`rr:` then `oef:`), and since
2023 the 485BPOS / 497K documents are inline XBRL, so every number can be read
straight out of the HTML with no PDF or table-layout guesswork:

    management fee + 12b-1 fee + other expenses + acquired fund fees
      = total (gross) expenses
    total + fee waiver = net expenses        (waiver stored negative)

Percent of assets throughout: 0.55 means 0.55%.
"""

from __future__ import annotations

import re

FEE_TAGS = {
    "ManagementFeesOverAssets": "management_fee",
    "DistributionAndService12b1FeesOverAssets": "distribution_12b1",
    "OtherExpensesOverAssets": "other_expenses",
    "AcquiredFundFeesAndExpensesOverAssets": "acquired_fund_fees",
    "ExpensesOverAssets": "gross_expense_ratio",
    "FeeWaiverOrReimbursementOverAssets": "fee_waiver",
    "NetExpensesOverAssets": "net_expense_ratio",
}
FIELDS = tuple(FEE_TAGS.values())

_RE_CONTEXT = re.compile(
    r'<(?:[\w-]+:)?context\b[^>]*?\bid\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</(?:[\w-]+:)?context\s*>',
    re.S | re.I)
_RE_MEMBER = re.compile(
    r'<(?:[\w-]+:)?explicitMember\b[^>]*?\bdimension\s*=\s*["\']([^"\']+)["\'][^>]*>\s*([^<]+?)\s*<',
    re.S | re.I)
_RE_NONFRACTION = re.compile(r'<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction\s*>', re.S | re.I)
_RE_NONNUMERIC = re.compile(r'<ix:nonNumeric\b([^>]*)>(.*?)</ix:nonNumeric\s*>', re.S | re.I)
_RE_ATTR = re.compile(r'([\w:.-]+)\s*=\s*["\']([^"\']*)["\']')
_RE_TAG = re.compile(r'<[^>]+>')
_RE_SERIES = re.compile(r'S\d{9}')
_RE_CLASS = re.compile(r'C\d{9}')
_DASHES = {"", "-", "—", "–", "none", "n/a", "na", "nil"}


def _attrs(s):
    return {k.lower(): v for k, v in _RE_ATTR.findall(s)}


def _local(name):
    return (name or "").split(":")[-1]


def _text(inner):
    t = _RE_TAG.sub("", inner or "")
    return re.sub(r"&nbsp;|&#160;|\s+", " ", t).strip()


def parse_number(text, attrs):
    """Displayed text + ix attributes -> percent of assets (0.03 = 0.03%), or None."""
    fmt = (attrs.get("format") or "").lower()
    t = text.strip().lower()
    if "zero" in fmt or t in _DASHES:
        val = 0.0
    else:
        t = t.replace(",", "").replace("$", "").replace("%", "").strip()
        neg = t.startswith("(") and t.endswith(")")
        t = t.strip("()").strip()
        try:
            val = float(t)
        except ValueError:
            return None
        if neg:
            val = -val
    try:
        scale = int(attrs.get("scale") or 0)
    except ValueError:
        scale = 0
    val *= 10 ** scale
    if attrs.get("sign") == "-":
        val = -val
    pct = val * 100.0                        # XBRL "pure" fraction -> percent
    if not -10.0 <= pct <= 25.0:             # a fee table number, or a misparse
        return None
    return round(pct, 4)


def parse_contexts(html):
    """{context id: {"series": S..., "class": C..., "members": (dims...)}}"""
    out = {}
    for cid, body in _RE_CONTEXT.findall(html):
        series = klass = None
        members = []
        for dim, member in _RE_MEMBER.findall(body):
            d = _local(dim).lower()
            members.append((d, member.strip()))
            if "legalentity" in d or "series" in d:
                m = _RE_SERIES.search(member)
                series = m.group(0) if m else series
            elif "class" in d:
                m = _RE_CLASS.search(member)
                klass = m.group(0) if m else klass
        out[cid] = {"series": series, "class": klass, "members": tuple(sorted(members))}
    return out


def extract_fee_tables(html, by_class=None, by_series=None, filing_series=None):
    """
    html           prospectus document (inline XBRL)
    by_class       {class_id: ticker}
    by_series      {series_id: [tickers]}
    filing_series  [series_id] named in this filing's SEC header
    -> {ticker: {field: percent, "series_id", "class_id"}}

    A context is tied to a ticker by, in order: a dei:TradingSymbol fact with
    the same dimensions, its ClassAxis id, a series with exactly one ticker, or
    — for a filing about a single one-ticker series — a context with no
    dimensions at all. A mutual fund prospectus usually covers several share
    classes with different fees, so the class dimension is what separates them.
    """
    by_class, by_series = by_class or {}, by_series or {}
    ctx = parse_contexts(html)
    if not ctx:
        return {}

    sym_by_members = {}
    for attrs_s, inner in _RE_NONNUMERIC.findall(html):
        a = _attrs(attrs_s)
        if _local(a.get("name")) == "TradingSymbol" and a.get("contextref") in ctx:
            sym = _text(inner).upper()
            if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
                sym_by_members[ctx[a["contextref"]]["members"]] = sym

    def ticker_for(c):
        if c["members"] in sym_by_members:
            return sym_by_members[c["members"]]
        if c["class"] and c["class"] in by_class:
            return by_class[c["class"]]
        if c["series"] and len(by_series.get(c["series"]) or []) == 1:
            return by_series[c["series"]][0]
        if not c["members"] and filing_series and len(filing_series) == 1:
            ts = by_series.get(filing_series[0]) or []
            if len(ts) == 1:
                return ts[0]
        return None

    out = {}
    for attrs_s, inner in _RE_NONFRACTION.findall(html):
        a = _attrs(attrs_s)
        field = FEE_TAGS.get(_local(a.get("name")))
        c = ctx.get(a.get("contextref"))
        if not field or not c:
            continue
        t = ticker_for(c)
        if not t:
            continue
        v = parse_number(_text(inner), a)
        if v is None:
            continue
        rec = out.setdefault(t, {"series_id": c["series"], "class_id": c["class"]})
        rec.setdefault(field, v)             # first value wins (fee table precedes the examples)
    for rec in out.values():
        # Waivers print as negatives in the table; store them that way always.
        if rec.get("fee_waiver") is not None:
            rec["fee_waiver"] = -abs(rec["fee_waiver"])
        if rec.get("net_expense_ratio") is None and rec.get("gross_expense_ratio") is not None:
            rec["net_expense_ratio"] = round(rec["gross_expense_ratio"] + (rec.get("fee_waiver") or 0.0), 4)
    return {t: r for t, r in out.items() if any(r.get(f) is not None for f in FIELDS)}


def breakdown_text(b):
    """One sentence for an alert: 'management fee 0.55%, other expenses 0.12%,
    total 0.67%, fee waiver -0.07%, net 0.60%'."""
    if not b:
        return None
    parts = []
    for field, lbl in (("management_fee", "management fee"), ("distribution_12b1", "12b-1 fee"),
                       ("other_expenses", "other expenses"), ("acquired_fund_fees", "acquired fund fees"),
                       ("gross_expense_ratio", "total"), ("fee_waiver", "fee waiver"),
                       ("net_expense_ratio", "net")):
        v = b.get(field)
        if v is None or (field in ("distribution_12b1", "acquired_fund_fees", "fee_waiver") and v == 0):
            continue
        parts.append(f"{lbl} {v:.2f}%")
    return ", ".join(parts) or None
