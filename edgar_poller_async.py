"""
edgar_poller_async.py — GQ FinXray US
Features 1 (SEC filings), 5 (insider Form 4), and the S-1 trigger for Feature 8.

Replaces edgar_poller.py. Two changes that matter:

1. WATCHLIST-FIRST. The old poller called fetch_filing_text() for every
   filing in the EDGAR feed and only later cared who the issuer was. That
   is the pattern that produced the surprise bills. Here the order is:
   parse RSS -> resolve CIK from the in-memory map -> check watchlist ->
   only then fetch document bodies. Nothing leaves the process for a
   ticker nobody is watching.

   S-1 is the deliberate exception: pre-IPO filers cannot be on anyone's
   watchlist, so poll_sec_s1 runs with watchlist_only=False. Those rows are
   stored under source=SEC_IPO (Feature 8) rather than SEC_EDGAR, because
   delivery.py treats SEC_EDGAR as company-scoped and an IPO registrant has
   no ticker for a watchlist to match — see poll_sec_s1_async.

2. ASYNC. Document bodies for the surviving filings are fetched
   concurrently through sec_client's rate-limited session instead of
   serially with time.sleep(0.3) between each one.

Public API is unchanged from edgar_poller.py, so main.py imports keep
working: poll_sec_8k, poll_sec_form4, poll_sec_10q, poll_sec_10k,
poll_sec_s1, load_cik_map.
"""

import asyncio
import logging
import os
import re
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime

from dotenv import load_dotenv
from supabase import create_client

import sec_client
import sec_financials
from typing import Dict, Any

load_dotenv()
logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

EDGAR_BASE = "https://www.sec.gov/cgi-bin/browse-edgar"
EDGAR_FEED = (
    EDGAR_BASE
    + "?action=getcurrent&type={form}&dateb=&owner=include&count={count}"
      "&search_text=&output=atom"
)
EDGAR_CIK_URL = "https://www.sec.gov/files/company_tickers.json"

FEED_COUNT = int(os.getenv("EDGAR_FEED_COUNT", "100"))
DOC_TEXT_LIMIT = 6000

# Feature 8's EDGAR half. Deliberately NOT "SEC_EDGAR": delivery.py lists that
# in COMPANY_ONLY_SOURCES so a filing for an unwatched company can never be
# broadcast, which is right for 8-K/10-Q/Form 4 and fatal for an S-1 — the
# registrant is pre-IPO and cannot be on anyone's watchlist by definition.
IPO_SOURCE = "SEC_IPO"

CIK_MAP: dict[str, str] = {}

# Watchlist cache — one Supabase read per WATCHLIST_TTL seconds, not per filing.
WATCHLIST_TTL = int(os.getenv("WATCHLIST_TTL", "300"))
_watchlist: set[str] = set()
_watchlist_at = 0.0


# ── Logging ───────────────────────────────────────────────────────────────────
def log_poller_error(job_name, error, context=None):
    print(f"[ERROR] {job_name}: {error}")
    try:
        supabase.table("poller_error_log").insert({
            "poller_name": "edgar_poller_async",
            "job_name": job_name,
            "error_message": str(error)[:2000],
            "error_traceback": traceback.format_exc()[:8000],
            "context": context or {},
        }).execute()
    except Exception as log_err:
        print(f"[ERROR] Failed to write to poller_error_log: {log_err}")


# ── CIK map ───────────────────────────────────────────────────────────────────
async def load_cik_map_async():
    print("[SETUP] Loading SEC CIK-to-ticker mapping...")
    data = await sec_client.get_json(EDGAR_CIK_URL)
    if not data:
        log_poller_error("load_cik_map", "empty response from company_tickers.json")
        return
    try:
        for val in data.values():
            cik = str(val["cik_str"]).zfill(10)
            CIK_MAP[cik] = val["ticker"].upper()
        print(f"[SETUP] Loaded {len(CIK_MAP):,} ticker mappings")
    except Exception as e:
        log_poller_error("load_cik_map", e)


def load_cik_map():
    """
    Sync entry point, kept for main.py.

    Goes through the same session cleanup as every other poll. Calling
    asyncio.run() directly here left the HTTP session this download opened
    bound to a loop that was then closed, so it was never closed itself --
    aiohttp reports that as an unclosed-connector warning at startup.
    """
    asyncio.run(_with_session_cleanup(load_cik_map_async()))


def ticker_from_cik(cik: str) -> str:
    """
    In-memory only. Deliberately does NOT fall back to a network call:
    a CIK missing from company_tickers.json has no listed ticker, so it
    cannot be on a watchlist, and hitting data.sec.gov to confirm that
    for every unknown filer is exactly the cost we removed.
    """
    if not cik:
        return "UNKNOWN"
    return CIK_MAP.get(str(cik).zfill(10), "UNKNOWN")


# ── Watchlist ─────────────────────────────────────────────────────────────────
def get_watchlist(force: bool = False) -> set[str]:
    global _watchlist, _watchlist_at
    if not force and _watchlist and (time.time() - _watchlist_at) < WATCHLIST_TTL:
        return _watchlist
    try:
        rows = supabase.table("watchlists").select("ticker").execute().data or []
        _watchlist = {r["ticker"].upper() for r in rows if r.get("ticker")}
        _watchlist_at = time.time()
        print(f"[WATCHLIST] {len(_watchlist)} tickers loaded")
    except Exception as e:
        log_poller_error("get_watchlist", e)
        # Fail closed: an empty watchlist means zero alerts, never a
        # market-wide fetch. Keep the last good copy if we have one.
    return _watchlist


# ── Text helpers ──────────────────────────────────────────────────────────────
def clean_html_text(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&reg;', '®')
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'This page uses Javascript.*?Javascript enabled browser\.', '',
                  text, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip()


def _accession_and_cik(index_url: str):
    acc = re.search(r'(\d{10}-\d{2}-\d{6})', index_url)
    cik = re.search(r'/data/(\d+)/', index_url)
    if not acc or not cik:
        return None, None, None
    return acc.group(1), acc.group(1).replace("-", ""), cik.group(1)


async def fetch_filing_text(index_url: str, form_type: str) -> str:
    accession, acc_nodash, cik = _accession_and_cik(index_url)
    if not accession:
        return ""

    index_json_url = (
        f"https://data.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"
        f"{accession}-index.json"
    )
    payload = await sec_client.get_json(index_json_url)

    primary_doc = None
    if payload:
        files = payload.get("directory", {}).get("item", [])
        for f in files:
            name = (f.get("name") or "").lower()
            if "index" in name:
                continue
            if name.endswith((".htm", ".html")) and f.get("type") == form_type:
                primary_doc = f.get("name")
                break
        if not primary_doc:
            for f in files:
                name = (f.get("name") or "").lower()
                if name.endswith((".htm", ".html")) and "index" not in name:
                    primary_doc = f.get("name")
                    break
        if not primary_doc:
            for f in files:
                name = (f.get("name") or "").lower()
                if any(x in name for x in ("ex99", "ex-99", "exhibit99", "exhibit-99")):
                    primary_doc = f.get("name")
                    break

    if primary_doc:
        doc_url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{acc_nodash}/{primary_doc}")
        text = clean_html_text(await sec_client.get_text(doc_url))
        if len(text) > 300:
            return text[:DOC_TEXT_LIMIT]

    txt_url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
               f"{acc_nodash}/{accession}.txt")
    raw = await sec_client.get_text(txt_url)
    if raw:
        pos = raw.find("<TEXT>")
        if pos != -1:
            raw = raw[pos + 6:]
            end = raw.find("</TEXT>")
            if end != -1:
                raw = raw[:end]
        elif "</SEC-HEADER>" in raw:
            raw = raw[raw.find("</SEC-HEADER>") + 13:]
        text = clean_html_text(raw)
        text = re.sub(r'</?[A-Z][A-Z\-]*>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 300:
            return text[:DOC_TEXT_LIMIT]

    return ""



def _fmt_usd(v: float) -> str:
    """Money for `it` table rows; "-" when the filer disclosed no price."""
    if not v:
        return "-"
    from gquants_format_converter import _format_currency
    return _format_currency(v)


async def fetch_form4_text(index_url: str, company_name: str,
                           insider_name: str) -> tuple[str, dict]:
    """
    Returns (summary_text, structured_payload).

    The XML parse already produces every field the GQuants `it` view needs
    (security, action, shares, price, date, holdings after). It used to be
    flattened to prose and thrown away, so the payload had to be rebuilt
    from scratch or -- as shipped -- never built at all. The second element
    is {} whenever parsing failed and the prose fallback is in use.
    """
    fallback = (f"{company_name} insider {insider_name} filed Form 4 "
                f"disclosing changes in ownership.")
    accession, acc_nodash, cik = _accession_and_cik(index_url)
    if not accession:
        return fallback, {}

    txt_url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
               f"{acc_nodash}/{accession}.txt")
    content = await sec_client.get_text(txt_url)
    if not content:
        return fallback, {}

    start = content.find("<ownershipDocument>")
    end = content.find("</ownershipDocument>")
    if start == -1 or end == -1:
        return fallback, {}

    try:
        root = ET.fromstring(content[start:end + len("</ownershipDocument>")])
    except ET.ParseError as e:
        logger.debug("[FORM4] XML parse error: %s", e)
        return fallback, {}

    issuer_name, issuer_ticker = company_name, ""
    issuer_elem = root.find(".//issuer")
    if issuer_elem is not None:
        n = issuer_elem.find("issuerName")
        t = issuer_elem.find("issuerTradingSymbol")
        if n is not None and n.text:
            issuer_name = n.text.strip()
        if t is not None and t.text:
            issuer_ticker = t.text.strip()

    reporter_name, reporter_title = insider_name, ""
    owner = root.find(".//reportingOwner")
    if owner is not None:
        rp = owner.find(".//rptOwnerName")
        if rp is not None and rp.text:
            reporter_name = rp.text.strip()
        title = owner.find(".//officerTitle")
        if title is not None and title.text:
            reporter_title = title.text.strip()
        if not reporter_title:
            d = owner.find(".//isDirector")
            p = owner.find(".//isTenPercentOwner")
            if d is not None and d.text == "1":
                reporter_title = "Director"
            elif p is not None and p.text == "1":
                reporter_title = "10% Owner"

    def _num(elem):
        if elem is None or not elem.text:
            return 0.0
        try:
            return float(elem.text.replace(",", ""))
        except ValueError:
            return 0.0

    transactions = []
    for tag, verbs in (("nonDerivativeTransaction", ("purchased", "sold", "transacted")),
                       ("derivativeTransaction", ("acquired", "disposed", "exercised"))):
        for trans in root.findall(f".//{tag}"):
            shares_el = trans.find(".//transactionShares/value")
            if shares_el is None:
                continue
            ad = trans.find(".//transactionAcquiredDisposedCode/value")
            direction = ad.text.strip() if ad is not None and ad.text else ""
            action = verbs[0] if direction == "A" else verbs[1] if direction == "D" else verbs[2]

            shares = _num(shares_el)
            price = _num(trans.find(".//transactionPricePerShare/value"))
            owned = _num(trans.find(".//sharesOwnedFollowingTransaction/value"))
            date_el = trans.find(".//transactionDate/value")
            sec_el = trans.find(".//securityTitle/value")
            transactions.append({
                "action": action,
                "shares": shares,
                "price": price,
                "total": shares * price if price > 0 else 0,
                "owned_after": owned,
                "date": date_el.text.strip() if date_el is not None and date_el.text else "",
                "security": (sec_el.text.strip() if sec_el is not None and sec_el.text
                             else "Common Stock"),
            })

    if not transactions:
        return (f"{issuer_name} insider {reporter_name} ({reporter_title}) filed "
                f"Form 4 disclosing changes in ownership of {issuer_name} shares."), {}

    lines = [
        f"Company: {issuer_name} ({issuer_ticker})",
        f"Insider: {reporter_name}, {reporter_title or 'Insider'}",
        "Filing: Form 4 – Insider Transaction Report",
        "",
    ]
    for t in transactions:
        price_str = (f"at ${t['price']:.2f} per share" if t["price"] > 0
                     else "at undisclosed price")
        total_str = f"(total value ${t['total']:,.0f})" if t["total"] > 0 else ""
        date_str = f"on {t['date']}" if t["date"] else ""
        lines.append(
            f"{reporter_name} {t['action']} {t['shares']:,.0f} shares of "
            f"{t['security']} {price_str} {total_str} {date_str}.".replace("  ", " ").strip()
        )
        if t["owned_after"] > 0:
            lines.append(f"Total shares owned after transaction: {t['owned_after']:,.0f}")
    rows = [{
        "security": t["security"],
        "action": t["action"],
        "shares": f"{t['shares']:,.0f}",
        "price": _fmt_usd(t["price"]),
        "value": _fmt_usd(t["total"]),
        "date": t["date"] or "-",
        "held_after": f"{t['owned_after']:,.0f}" if t["owned_after"] else "-",
        "security_type": ("Derivative" if t["action"] in ("acquired", "disposed", "exercised")
                          else "Non-Derivative"),
    } for t in transactions]

    payload = build_form4_structured_payload(
        ticker=issuer_ticker or "",
        company_name=issuer_name,
        insider_name=reporter_name,
        insider_title=reporter_title or "Insider",
        transactions=rows,
        filing_date=(transactions[0].get("date") or ""),
        cik=cik,
    )
    return "\n".join(lines), payload


# ── Supabase ──────────────────────────────────────────────────────────────────
def known_filing_urls(urls: list[str]) -> set[str]:
    """One batched existence check instead of one query per filing."""
    if not urls:
        return set()
    try:
        rows = (supabase.table("raw_filings")
                .select("filing_url")
                .in_("filing_url", urls)
                .execute().data or [])
        return {r["filing_url"] for r in rows}
    except Exception as e:
        log_poller_error("known_filing_urls", e, {"count": len(urls)})
        # Fail closed: treat everything as seen rather than risk a duplicate storm.
        return set(urls)


def known_registrant_ciks(ciks: list[str], source: str) -> set[str]:
    """
    CIKs that already have a stored row from `source`. One batched query.

    Used to suppress S-1 AMENDMENT spam. The EDGAR feed pattern deliberately
    matches "S-1/A" as well as "S-1" (an amendment is still the filing), and a
    live IPO files a long string of them — every one a fresh URL, so the
    filing_url dedup above lets all of them through. The initial registration is
    the news ("X has filed to go public"); the amendments are procedural and
    would each become an identical broadcast alert.
    """
    ciks = [c for c in ciks if c]
    if not ciks:
        return set()
    try:
        rows = (supabase.table("raw_filings")
                .select("extra")
                .eq("source", source)
                .in_("extra->>cik", ciks)
                .execute().data or [])
        return {(r.get("extra") or {}).get("cik") for r in rows
                if (r.get("extra") or {}).get("cik")}
    except Exception as e:
        log_poller_error("known_registrant_ciks", e, {"count": len(ciks)})
        # Fail closed: a lookup outage must not turn into a broadcast storm.
        return set(ciks)


def store_filing(filing_type, company_name, ticker, raw_text, filing_url,
                 extra=None, status="PENDING", source="SEC_EDGAR"):
    """
    Insert one raw_filings row.

    `source` is a parameter and not a constant because delivery.py routes on it:
    SEC_EDGAR is in COMPANY_ONLY_SOURCES, which is correct for 8-K/10-Q/Form 4
    (a filing for an unwatched company must never broadcast) and wrong for S-1,
    whose whole point is a company nobody can have watchlisted yet. S-1 is stored
    under SEC_IPO so it routes market-wide; everything else keeps SEC_EDGAR.
    `source_priority` stays SEC_EDGAR either way — it records which FEED the row
    came from, which is what the pipeline's precedence rules read.
    """
    try:
        # Payloads are attached by the caller that actually has the parsed data
        # (Form 4 here, financial results in result_snapshot). No "needs_payload"
        # flag is written: nothing consumes one, and a flag that looks like
        # wiring but isn't is how the payload feature shipped broken.
        stored_extra = dict(extra or {})
        stored_extra["source_priority"] = "SEC_EDGAR"

        supabase.table("raw_filings").insert({
            "source": source,
            "filing_type": filing_type,
            "company_name": company_name,
            "ticker": ticker,
            "raw_text": raw_text,
            "filing_url": filing_url,
            "filed_at": datetime.now().isoformat(),
            "status": status,
            "extra": stored_extra,
        }).execute()
        print(f"[STORED] {filing_type} – {company_name} ({ticker}) → {status}")
    except Exception as e:
        log_poller_error("store_filing", e, {
            "filing_type": filing_type, "ticker": ticker, "filing_url": filing_url,
        })


# ── Feed parsing ──────────────────────────────────────────────────────────────
NS = {"atom": "http://www.w3.org/2005/Atom"}


def _parse_feed(xml_text: str):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("[EDGAR] Feed parse error: %s", e)
        return []
    out = []
    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        link_el = entry.find("atom:link", NS)
        summ_el = entry.find("atom:summary", NS)
        if title_el is None or link_el is None:
            continue
        url = link_el.attrib.get("href", "")
        if not url:
            continue
        out.append({
            "title": title_el.text or "",
            "url": url,
            "summary": summ_el.text if summ_el is not None else "",
        })
    return out


# ── 8-K item classification ───────────────────────────────────────────────────
# An 8-K is "a material event happened" -- the item number says WHICH, and that
# is the whole meaning of the filing. 2.02 is the quarterly earnings release,
# 1.01 a material agreement, 5.02 an executive departure; they are entirely
# different alerts. alert_formatter already renders extra["item_types"], but
# nothing ever populated it, so every 8-K reached the reader as an unlabelled
# "material event" and the earnings releases -- the most valuable filing SEC
# publishes, out hours before any vendor re-reports them -- were
# indistinguishable from routine ones.
EIGHT_K_ITEMS = {
    "1.01": "Entry into a Material Agreement",
    "1.02": "Termination of a Material Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition or Disposition",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Material Direct Financial Obligation",
    "2.04": "Triggering Events Accelerating a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sale of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Election of Directors or Officers",
    "5.03": "Amendments to Articles or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

# "Item 2.02" / "ITEM 2.02." / "Item&nbsp;2.02"
_ITEM_RE = re.compile(r"item\s*(\d\.\d{2})", re.IGNORECASE)

# Item 9.01 is on nearly every 8-K (it just lists the exhibits) and 7.01 is a
# disclosure wrapper, so neither identifies what the filing is ABOUT. They are
# kept out of the headline label so the meaningful item leads.
_LOW_SIGNAL_ITEMS = {"9.01", "7.01"}


def extract_8k_items(text):
    """
    The 8-K item numbers present in the filing, most meaningful first.

    Returns ["2.02: Results of Operations and Financial Condition", ...].
    """
    if not text:
        return []
    found = []
    for code in dict.fromkeys(_ITEM_RE.findall(text[:20000])):
        code = code.strip()
        if code in EIGHT_K_ITEMS:
            found.append(code)
    if not found:
        return []
    found.sort(key=lambda c: (c in _LOW_SIGNAL_ITEMS, c))
    return [f"{c}: {EIGHT_K_ITEMS[c]}" for c in found]


def is_earnings_8k(item_codes):
    """True when this 8-K carries the quarterly results (Item 2.02)."""
    return any(str(i).startswith("2.02") for i in (item_codes or []))


# ── Generic poller ────────────────────────────────────────────────────────────
async def poll_edgar_generic_async(form_type, label, watchlist_only=True,
                                   status="PENDING", source="SEC_EDGAR",
                                   dedupe_by_cik=False):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{stamp}] Polling SEC EDGAR for {label}"
          f"{'' if watchlist_only else ' (market-wide)'}...")
    try:
        feed = await sec_client.get_text(
            EDGAR_FEED.format(form=form_type, count=FEED_COUNT)
        )
        if not feed:
            log_poller_error(f"poll_edgar_generic:{label}", "empty EDGAR feed",
                             {"form_type": form_type})
            return 0

        entries = _parse_feed(feed)
        if not entries:
            print(f"[{stamp}] No entries in {label} feed.")
            return 0

        watchlist = get_watchlist() if watchlist_only else None
        # (?:/A)? handles amended filings (8-K/A, 10-Q/A, 10-K/A, S-1/A), which
        # SEC EDGAR files constantly and which otherwise never match this
        # pattern -- title is "8-K/A - Company (0001234567)", not "8-K - ...",
        # so the plain form_type pattern stops right after "8-K" and fails.
        # A fallback pattern (same shape as edgar_poller.py's) catches any
        # other title layout SEC uses. Without either, m is None, cik is "",
        # ticker_from_cik("") returns "UNKNOWN", and the filing is silently
        # dropped -- even when it is for a watchlisted company.
        pattern = re.compile(rf'{re.escape(form_type)}(?:/A)?\s*-\s*(.+?)\s*\((\d+)\)')
        fallback_pattern = re.compile(r'[^-]+-\s*(.+?)\s*\((\d+)\)')

        # ---- FILTER BEFORE FETCH ----
        candidates = []
        for e in entries:
            m = pattern.match(e["title"]) or fallback_pattern.match(e["title"])
            company = m.group(1).strip() if m else e["title"]
            cik = m.group(2) if m else ""
            ticker = ticker_from_cik(cik)

            if watchlist_only:
                if ticker == "UNKNOWN" or ticker not in watchlist:
                    continue

            candidates.append({**e, "company": company, "cik": cik, "ticker": ticker})

        if not candidates:
            print(f"[{stamp}] No watchlisted {label} filings.")
            return 0

        seen = known_filing_urls([c["url"] for c in candidates])
        candidates = [c for c in candidates if c["url"] not in seen]
        if not candidates:
            print(f"[{stamp}] No new {label} filings.")
            return 0

        # One registration per company, not one per amendment. See
        # known_registrant_ciks(). Applied AFTER the URL check so the batched
        # CIK query only runs for filings that are genuinely new.
        if dedupe_by_cik:
            already = known_registrant_ciks([c["cik"] for c in candidates], source)
            fresh, suppressed = [], 0
            for c in candidates:
                # Guard within this batch too: the same CIK can appear twice in
                # one feed page (S-1 and S-1/A filed minutes apart).
                if c["cik"] and c["cik"] in already:
                    suppressed += 1
                    continue
                already.add(c["cik"])
                fresh.append(c)
            if suppressed:
                print(f"[{stamp}] Suppressed {suppressed} {label} amendment(s) "
                      f"for companies already captured.")
            candidates = fresh
            if not candidates:
                return 0

        print(f"[{stamp}] {len(candidates)} new {label} filing(s) to fetch.")

        # ---- FETCH (concurrent, rate-limited) ----
        texts = await sec_client.gather_limited(
            fetch_filing_text(c["url"], form_type) for c in candidates
        )

        stored = 0
        for c, text in zip(candidates, texts):
            if isinstance(text, Exception) or not text or len(text) < 100:
                text = f"{c['company']}\n\n{c['title']}\n\n{c['summary']}"
            extra = {"cik": c["cik"], "form_type": form_type,
                     "watchlist_only": watchlist_only}
            if form_type in ("10-Q", "10-K"):
                extra["needs_result_snapshot"] = True
            # SEC's own machine-readable endpoints for this filing/company.
            # Carrying them means an alert can point at the structured source
            # data itself -- companyfacts XBRL, and the filing index that
            # lists the EX-99.1 earnings release -- with no vendor involved.
            sec_json = sec_financials.build_sec_json_links(c["cik"], c["url"])
            if sec_json:
                extra["sec_json"] = sec_json

            if form_type.startswith("8-K"):
                items = extract_8k_items(text)
                if items:
                    extra["item_types"] = items
                    # An earnings 8-K is the quarterly result, reported by the
                    # company itself the moment it announces -- hours ahead of
                    # any vendor, and typically WEEKS ahead of the 10-Q that
                    # currently triggers the Result Snapshot. Flagging it lets
                    # downstream treat it as earnings rather than as a generic
                    # material event.
                    if is_earnings_8k(items):
                        extra["is_earnings_release"] = True
                        print(f"[8-K] {c['ticker']}: Item 2.02 earnings release")
            store_filing(form_type, c["company"], c["ticker"], text, c["url"],
                         extra=extra, status=status, source=source)
            stored += 1

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Stored {stored} {label} filings.")
        return stored

    except Exception as e:
        log_poller_error(f"poll_edgar_generic:{label}", e, {"form_type": form_type})
        return 0


# ── Form 4 ────────────────────────────────────────────────────────────────────
async def poll_sec_form4_async():
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{stamp}] Polling SEC EDGAR for Form 4...")
    try:
        feed = await sec_client.get_text(EDGAR_FEED.format(form="4", count=FEED_COUNT))
        if not feed:
            log_poller_error("poll_sec_form4", "empty EDGAR feed")
            return 0

        grouped: dict[str, list] = {}
        for e in _parse_feed(feed):
            m = re.search(r'(\d{10}-\d{2}-\d{6})', e["url"])
            if m:
                grouped.setdefault(m.group(1), []).append(e)

        watchlist = get_watchlist()
        # (?:/A)? for amended Form 4/A filings -- same gap as the generic
        # poller above: "4/A - Issuer (0001234567) (Issuer)" doesn't match
        # a pattern anchored on a bare "4", so cik stays "" and the filing
        # is dropped as UNKNOWN regardless of whether it's watchlisted.
        issuer_re = re.compile(r'4(?:/A)?\s*-\s*(.+?)\s*\((\d+)\)\s*\(Issuer\)')
        reporter_re = re.compile(r'4(?:/A)?\s*-\s*(.+?)\s*\(\d+\)\s*\(Reporting\)')
        fallback_issuer_re = re.compile(r'[^-]+-\s*(.+?)\s*\((\d+)\)\s*\(Issuer\)')

        candidates = []
        for entries in grouped.values():
            issuer = next((x for x in entries if "(Issuer)" in x["title"]), None)
            reporting = next((x for x in entries if "(Reporting)" in x["title"]), None)
            if not issuer:
                continue

            m = issuer_re.match(issuer["title"]) or fallback_issuer_re.match(issuer["title"])
            company = m.group(1).strip() if m else issuer["title"]
            cik = m.group(2) if m else ""
            ticker = ticker_from_cik(cik)

            # Watchlist gate before any document fetch.
            if ticker == "UNKNOWN" or ticker not in watchlist:
                continue

            insider = "Unknown Insider"
            if reporting:
                rm = reporter_re.match(reporting["title"])
                if rm:
                    insider = rm.group(1).strip()

            candidates.append({"url": issuer["url"], "company": company,
                               "cik": cik, "ticker": ticker, "insider": insider})

        if not candidates:
            print(f"[{stamp}] No watchlisted Form 4 filings.")
            return 0

        seen = known_filing_urls([c["url"] for c in candidates])
        candidates = [c for c in candidates if c["url"] not in seen]
        if not candidates:
            print(f"[{stamp}] No new Form 4 filings.")
            return 0

        texts = await sec_client.gather_limited(
            fetch_form4_text(c["url"], c["company"], c["insider"]) for c in candidates
        )

        stored = 0
        for c, result in zip(candidates, texts):
            # fetch_form4_text returns (text, payload); gather_limited yields the
            # exception itself rather than raising, so guard before unpacking.
            if isinstance(result, Exception):
                text, payload = "", {}
            else:
                text, payload = result

            if not text or len(text) < 50:
                text = (f"{c['company']} insider {c['insider']} filed Form 4 "
                        f"disclosing changes in ownership.")
                payload = {}

            extra = {"insider_name": c["insider"], "cik": c["cik"],
                     "watchlist_only": True}
            if payload:
                # ai_pipeline copies raw_filings.extra onto the alert, so the
                # payload reaches alerts.extra without any further plumbing.
                extra["structured_payload"] = payload

            store_filing("4", c["company"], c["ticker"], text, c["url"],
                         extra=extra)
            stored += 1

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Stored {stored} Form 4 filings.")
        return stored

    except Exception as e:
        log_poller_error("poll_sec_form4", e)
        return 0


# ── Async entry points ────────────────────────────────────────────────────────


def build_form4_structured_payload(
    ticker: str,
    company_name: str,
    insider_name: str,
    insider_title: str,
    transactions: list,
    filing_date: str,
    cik: str
) -> Dict[str, Any]:
    """
    Convert Form 4 transaction data to GQuants `it` format.
    Called when storing insider trading alerts.
    """
    from gquants_format_converter import form4_to_insider_trading
    return form4_to_insider_trading(
        ticker, insider_name, insider_title, transactions,
        company_name, filing_date
    )


# build_s1_structured_payload() lived here: a caller-less wrapper that guessed
# form_link as a bare CIK directory URL. The Feature 8 merge it was waiting for
# now exists, and ipo_poller.process_ipo calls s1_to_ipo directly with the real
# filing URL and filing date from our own S-1 capture — strictly better inputs
# than this could produce, so the wrapper is gone rather than left as a second,
# worse path to the same payload.


async def poll_sec_8k_async():
    return await poll_edgar_generic_async("8-K", "8-K")


async def poll_sec_10q_async():
    return await poll_edgar_generic_async("10-Q", "10-Q (Quarterly Results)")


async def poll_sec_10k_async():
    return await poll_edgar_generic_async("10-K", "10-K (Annual Results)")


async def poll_sec_s1_async():
    """
    S-1 registrations — Feature 8's early-warning half.

    Market-wide by design: an S-1 filer is pre-IPO, so it is on nobody's
    watchlist and its CIK is not in the ticker map (these rows carry
    ticker="UNKNOWN", which is correct, not a failure).

    WHAT CHANGED. These rows were stored status="IPO_PENDING", source="SEC_EDGAR"
    and then read by nothing at all — ai_pipeline selects status="PENDING", and
    ipo_poller resolves its S-1 link live from FMP rather than from the table.
    The poll fetched document bodies market-wide, wrote them, and no alert was
    ever downstream of any of it.

    Now:
      status="PENDING"  -> the AI pipeline summarises the S-1 body with the same
                           S.1.A machinery every other filing uses
      source="SEC_IPO"  -> delivery routes it market-wide. It CANNOT be
                           SEC_EDGAR: that is in COMPANY_ONLY_SOURCES, so the
                           alert would resolve to an empty audience and be
                           marked delivered without being sent.
      dedupe_by_cik     -> the initial registration alerts, its amendments do not
    """
    return await poll_edgar_generic_async(
        "S-1", "S-1 (IPO Filing)", watchlist_only=False,
        status="PENDING", source=IPO_SOURCE, dedupe_by_cik=True,
    )


# ── Sync shims so main.py / schedule keep working unchanged ───────────────────
async def _with_session_cleanup(coro):
    """
    Run one poll, then close the HTTP session that poll opened.

    sec_client keeps one keep-alive session per event loop so the requests
    within a poll reuse connections instead of paying a TLS handshake each.
    Every poll gets a fresh loop from asyncio.run() below, so the session has
    to be closed before that loop goes away or its connector outlives it.
    """
    try:
        return await coro
    finally:
        await sec_client.close_session()


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_with_session_cleanup(coro))
    raise RuntimeError(
        "Sync shim called from inside an event loop — use the *_async variant."
    )


def poll_sec_8k():
    return _run(poll_sec_8k_async())


def poll_sec_10q():
    return _run(poll_sec_10q_async())


def poll_sec_10k():
    return _run(poll_sec_10k_async())


def poll_sec_s1():
    return _run(poll_sec_s1_async())


def poll_sec_form4():
    return _run(poll_sec_form4_async())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def _main():
        await load_cik_map_async()
        while True:
            await asyncio.gather(
                poll_sec_8k_async(),
                poll_sec_form4_async(),
                poll_sec_10q_async(),
                poll_sec_10k_async(),
                poll_sec_s1_async(),
            )
            await asyncio.sleep(30)

    try:
        asyncio.run(_main())
    finally:
                pass
