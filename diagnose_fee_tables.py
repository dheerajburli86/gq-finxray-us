"""
diagnose_fee_tables.py
Read-only diagnostic: WHY does a prospectus come back NO_FEE_TABLE?

mf_expense_poller reads one document per filing — the `primaryDocument` named
by SEC's submissions API — and looks for inline-XBRL fee tags in it. On
2026-10-17 that returned NO_FEE_TABLE for 222 of 301 documents read, so the
assumption behind "one document per filing" is wrong somewhere. There are two
candidate causes and they need opposite fixes:

  A. the fee table IS in the filing, but in a DIFFERENT document than the
     primary one  ->  fix is to pick the right document
  B. the filing has no inline XBRL at all, and the risk/return data sits in a
     separate .xml exhibit  ->  fix is to parse that exhibit instead

This script tells you which, by listing every document in a filing and
counting fee tags in each. It writes nothing and sends no alerts.

Usage (from the repo root, with your .env in place):

    python diagnose_fee_tables.py                 # 6 watched funds, newest filing each
    python diagnose_fee_tables.py FCNTX FBALX     # specific tickers
    python diagnose_fee_tables.py --filings 2     # look at 2 filings per fund
"""

from __future__ import annotations

import sys

import mf_common as mc
import sec_fee_tables as ft

PROSPECTUS_FORMS = ("485BPOS", "497K", "497")
LOOKBACK_DAYS = 460
MAX_DOCS_PER_FILING = 14          # keep the SEC call count modest
DEFAULT_TICKERS = 6


def fee_tag_count(html):
    """How many inline-XBRL FEE facts are in this document."""
    if not html:
        return 0, 0
    total = fee = 0
    for attrs_s, _inner in ft._RE_NONFRACTION.findall(html):
        total += 1
        if ft.FEE_TAGS.get(ft._local(ft._attrs(attrs_s).get("name"))):
            fee += 1
    return fee, total


def filing_documents(cik, adsh):
    """Every file in a filing, from its directory index."""
    url = (f"https://www.sec.gov/Archives/edgar/data/"
           f"{int(mc.cik_plain(cik))}/{str(adsh).replace('-', '')}/index.json")
    data = mc.sec.json(url)
    items = (((data or {}).get("directory") or {}).get("item")) or []
    return [i.get("name") for i in items if i.get("name")]


def classify(names):
    """Split a filing's files into documents, XBRL instances and XBRL plumbing."""
    html = [n for n in names if n.lower().endswith((".htm", ".html"))]
    xml = [n for n in names if n.lower().endswith(".xml")]
    plumbing = tuple("_cal.xml _lab.xml _pre.xml _def.xml".split())
    instances = [n for n in xml if not n.lower().endswith(plumbing)
                 and not n.lower().startswith("r")]
    return html, instances, [n for n in xml if n not in instances]


def diagnose(ticker, universe, n_filings=1):
    row = universe["by_ticker"].get(ticker.upper())
    if not row:
        print(f"\n=== {ticker}: not in mf_master as an ordinary mutual fund — skipped")
        return
    cik = row["cik"]
    print(f"\n=== {ticker}  {row.get('series_name') or ''}  (CIK {cik})")

    filings = mc.recent_filings(cik, PROSPECTUS_FORMS, LOOKBACK_DAYS)
    if not filings:
        print(f"    NO PROSPECTUS in the last {LOOKBACK_DAYS} days — this fund cannot be")
        print("    baselined from the current form list, whatever the parser does.")
        return
    filings.sort(key=lambda f: f.get("filing_date") or "", reverse=True)

    # The newest of EACH form, not just the newest overall. 497/497K stickers
    # are filed many times a year and a 485BPOS once, so a plain newest-first
    # list is all stickers — which is exactly what the poller was reading. The
    # question this answers is whether the annual 485BPOS carries the XBRL the
    # stickers lack.
    by_form, counts = {}, {}
    for f in filings:
        counts[f["form"]] = counts.get(f["form"], 0) + 1
        by_form.setdefault(f["form"], []).append(f)
    print("    filings in window by form: "
          + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())))
    picked = []
    for form in ("485BPOS", "497K", "497"):
        picked.extend((by_form.get(form) or [])[:n_filings])
    if not picked:
        picked = filings[:n_filings]

    for f in picked:
        adsh, form, fdate = f["accession"], f["form"], f["filing_date"]
        primary = (f.get("primary_doc") or "").rsplit("/", 1)[-1]
        print(f"  {form} filed {fdate}  {adsh}")
        print(f"    primaryDocument: {primary or '(none)'}")

        names = filing_documents(cik, adsh)
        if not names:
            print("    could not read the filing index (throttled?) — rerun")
            continue
        html, instances, plumbing = classify(names)
        print(f"    {len(names)} files: {len(html)} html, {len(instances)} xbrl instance, "
              f"{len(plumbing)} xbrl plumbing")

        # The document the poller actually reads today.
        if primary:
            fee, total = fee_tag_count(mc.sec.text(mc.archive(cik, adsh, primary)))
            verdict = ("FEE TABLE PRESENT" if fee else
                       "no fee facts" + (" (has inline XBRL, but not fee tags)"
                                         if total else " (NO inline XBRL at all)"))
            print(f"    -> primary doc: {fee} fee facts / {total} inline facts  [{verdict}]")

        # Every other html document, to see whether the table lives elsewhere.
        others = [n for n in html if n != primary][:MAX_DOCS_PER_FILING]
        found_elsewhere = []
        for n in others:
            fee, total = fee_tag_count(mc.sec.text(mc.archive(cik, adsh, n)))
            if fee:
                found_elsewhere.append((n, fee, total))
        if found_elsewhere:
            print("    -> FEE TABLE IS IN ANOTHER DOCUMENT (cause A):")
            for n, fee, total in found_elsewhere[:5]:
                print(f"         {n}: {fee} fee facts / {total} inline facts")
        elif others:
            print(f"    -> checked {len(others)} other html docs, none carry fee facts")
        if instances:
            print(f"    -> separate XBRL instance present (cause B candidate): "
                  f"{', '.join(instances[:3])}")


def main(argv):
    n_filings = 1
    args = []
    i = 0
    while i < len(argv):
        if argv[i] == "--filings" and i + 1 < len(argv):
            n_filings = int(argv[i + 1])
            i += 2
            continue
        args.append(argv[i])
        i += 1

    u = mc.load_mf_universe()
    if not u["by_ticker"]:
        print("mf_master is empty — nothing to diagnose")
        return 1

    if args:
        tickers = [a.upper() for a in args]
    else:
        # Watched funds with no fee table yet: exactly the ones that failed.
        try:
            have = {r["ticker"] for r in mc.paged(
                lambda: mc.sb().table("mf_expense_breakdown").select("ticker").order("ticker"))}
        except Exception as e:
            print(f"could not read mf_expense_breakdown ({e}); pass tickers explicitly")
            return 1
        watched = mc.watched_tickers()
        tickers = sorted(t for t in watched if t in u["by_ticker"] and t not in have)
        if not tickers:
            print("every watched mutual fund already has a fee table — nothing to diagnose")
            return 0
        print(f"{len(tickers)} watched funds still without a fee table; "
              f"sampling the first {DEFAULT_TICKERS}")
        tickers = tickers[:DEFAULT_TICKERS]

    for t in tickers:
        try:
            diagnose(t, u, n_filings)
        except Exception as e:
            print(f"    ERROR on {t}: {e}")

    print("\nWhat the output means")
    print("  The question is whether 485BPOS carries fee facts where 497/497K does not.")
    print("  485BPOS has fee facts, 497/497K has none -> the poller is reading stickers")
    print("     instead of the annual prospectus. Fix is to rank 485BPOS first.")
    print("  485BPOS also has none -> the fee data is not inline XBRL for these filers")
    print("     at all, and the parser needs a different source entirely.")
    print("  'NO PROSPECTUS in the last 460 days' -> the fund files on a longer cycle")
    print("     than the lookback, or under a form the poller does not collect.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
