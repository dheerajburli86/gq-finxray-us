"""
edgar_poller.py
GQ FinXray US — Feature 1. SEC EDGAR real-time filing ingestion.

Polls EDGAR's "current filings" Atom feeds for 8-K, Form 4, 10-Q, 10-K and S-1,
resolves each filer's CIK to a ticker, and — only for tickers somebody is
watching — downloads the filing text and stores it in `raw_filings` with
status='PENDING'. ai_pipeline.py does the rest. Nothing here writes to `alerts`
and nothing here sends a message.

WHAT CHANGED, AND WHY IT MATTERED
---------------------------------
* **SEC rate limiting is now enforced, not hoped for.** SEC's fair-access policy
  is 10 requests/second per client and requires a User-Agent that identifies the
  requester with contact details; exceeding it earns a 403 block on the whole IP
  for ten minutes. The old code issued unbounded parallel-ish bursts (one index
  fetch, one document fetch and often one submissions fetch per entry, times five
  feeds) and, on a 403, simply logged and returned — so the next cycle 30 seconds
  later walked straight back into the block. There is now a process-wide throttle
  at ~5 req/s and a shared backoff window that every SEC request respects.

* **Watchlist gating happens BEFORE the document is fetched.** This is the whole
  point of the rewrite. ai_pipeline gates at Stage 0, so an unwatched filing
  never costs an LLM call — but it was still costing two or three SEC requests
  and a megabyte of HTML per filing first. The CIK is resolved from the locally
  cached ticker map (no network), and unwatched filers are dropped there.

* **`filed_at` was `datetime.now()`** — the moment we polled, not the moment the
  company filed. Alerts and ordering were therefore all stamped with poll time.
  The Atom entry's `<updated>` element is the filing timestamp; it is parsed and
  used, timezone-aware.

* **Dedup was one SELECT per RSS entry** — roughly 200 round trips per cycle
  across the five feeds. It is one batched `.in_()` query per feed now, and it
  runs only over the handful of entries that survived the watchlist gate.

* **`filing_exists()` failed OPEN.** A transient database error returned False,
  which reads as "never seen this" — so an outage caused the whole feed to be
  re-ingested and re-summarised. Dedup now fails CLOSED: if we cannot confirm a
  filing is new, we skip it and pick it up next cycle.

* `content_hash` is computed on the extracted text, and the new
  UNIQUE(filing_url, COALESCE(ticker,'')) index is treated as "already seen"
  rather than allowed to crash the poller.
"""

import os
import re
import time
import hashlib
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

POLLER_NAME = "edgar_poller"
SOURCE = "SEC_EDGAR"

# SEC requires a User-Agent naming the requester with working contact details.
# A generic or absent UA is grounds for an immediate block.
HEADERS = {
    "User-Agent": os.getenv(
        "SEC_USER_AGENT",
        "GQ FinXray (GQuants) dheeraj.burli@gquants.com",
    ),
    "Accept-Encoding": "gzip, deflate",
}

EDGAR_8K_URL    = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_FORM4_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_10Q_URL   = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-Q&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_10K_URL   = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-K&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_S1_URL    = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-1&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_CIK_URL   = "https://www.sec.gov/files/company_tickers.json"

MAX_FILING_CHARS = 6000
CIK_MAP = {}

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


# ══════════════════════════════════════════════════════════════════════════════
# SEC request throttle
#
# The published limit is 10 requests/second. We target 5 so a burst that
# overlaps with another process on the same egress IP still lands inside the
# limit. `_blocked_until` is a shared penalty box: when any request comes back
# 403/429/503, every subsequent SEC request in the process waits it out, which
# is what stops the poller from hammering an IP that SEC has already throttled.
# ══════════════════════════════════════════════════════════════════════════════
SEC_MIN_GAP_SECONDS = 0.2
SEC_MAX_BACKOFF_SECONDS = 120.0

# ── Wall-clock ceilings ───────────────────────────────────────────────────────
# A single sec_get must never be able to monopolise the thread that called it.
# The old defaults — timeout=20, retries=3, backoff starting at 5s — could burn
# 20+5+20+10+20 = 75 seconds on one unreachable URL. poll_sec_8k and
# poll_sec_form4 run every 30 seconds on the SAME thread as every other feature,
# so two timing-out SEC feeds were enough to starve technical, ETF, analyst,
# heatmap and roundup jobs indefinitely while the process still looked healthy.
SEC_CONNECT_TIMEOUT = float(os.getenv("SEC_CONNECT_TIMEOUT", "5"))
SEC_READ_TIMEOUT = float(os.getenv("SEC_READ_TIMEOUT", "10"))
SEC_DEFAULT_TIMEOUT = (SEC_CONNECT_TIMEOUT, SEC_READ_TIMEOUT)
# Hard ceiling on one sec_get call including retries and throttle waiting.
SEC_REQUEST_BUDGET_SECONDS = float(os.getenv("SEC_REQUEST_BUDGET_SECONDS", "25"))

_sec_lock = threading.Lock()
_sec_state = {"last_at": 0.0, "blocked_until": 0.0}
_session = requests.Session()


def _sec_throttle(deadline=None):
    """
    Block until it is this caller's turn to talk to sec.gov.

    Returns True when the slot was acquired, False when `deadline` passed first.
    The deadline matters: `_apply_backoff` can push `blocked_until` out by as
    much as 120 seconds after a 403, and the previous version of this function
    would sit in that penalty box for the full window — on the scheduler thread,
    blocking every unrelated feature. Now the caller gives up and returns None,
    and the next scheduled cycle picks it up once the block has expired.
    """
    while True:
        with _sec_lock:
            now = time.monotonic()
            if now >= _sec_state["blocked_until"] and (now - _sec_state["last_at"]) >= SEC_MIN_GAP_SECONDS:
                _sec_state["last_at"] = now
                return True
            wait = max(_sec_state["blocked_until"] - now,
                       SEC_MIN_GAP_SECONDS - (now - _sec_state["last_at"]))
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait = min(wait, remaining)
        time.sleep(min(max(wait, 0.01), 5.0))


def _apply_backoff(seconds, reason):
    with _sec_lock:
        until = time.monotonic() + seconds
        if until > _sec_state["blocked_until"]:
            _sec_state["blocked_until"] = until
    logger.warning("[EDGAR] Backing off SEC requests for %.0fs (%s)", seconds, reason)


def sec_get(url, timeout=None, retries=2, budget=None):
    """
    Throttled, backoff-aware GET against sec.gov. Returns a Response or None.

    403/429/503 are treated as rate limiting: the whole process pauses for an
    exponentially growing window before retrying, rather than the old behaviour
    of logging and returning immediately into the next 30-second cycle.

    Every path out of this function is bounded by `budget` seconds of wall clock.
    Giving up early is correct here — EDGAR feeds are polled on a 30-second to
    10-minute cadence, so anything this call cannot get now it will get on the
    next cycle, and holding the thread costs every other feature in the process.
    """
    if timeout is None:
        timeout = SEC_DEFAULT_TIMEOUT
    elif isinstance(timeout, (int, float)):
        # Callers pass a single number meaning "read timeout". Connecting must
        # stay short regardless: a connect that hangs is a dead host, not a slow
        # document, and waiting 20s on it is pure lost scheduler time.
        timeout = (min(float(timeout), SEC_CONNECT_TIMEOUT), float(timeout))

    deadline = time.monotonic() + (budget if budget is not None else SEC_REQUEST_BUDGET_SECONDS)
    backoff = 2.0
    last_error = None

    for attempt in range(1, retries + 1):
        if time.monotonic() >= deadline:
            log_poller_error(POLLER_NAME, "sec_get",
                             f"Wall-clock budget exhausted after {attempt - 1} attempt(s)"
                             + (f": {last_error}" if last_error else ""),
                             {"url": url, "budget_seconds": budget or SEC_REQUEST_BUDGET_SECONDS})
            return None

        if not _sec_throttle(deadline):
            logger.warning("[EDGAR] Skipping %s — SEC backoff window outlasts this call's budget", url)
            return None

        try:
            r = _session.get(url, headers=HEADERS, timeout=timeout)
        except Exception as e:
            last_error = e
            if attempt == retries or time.monotonic() >= deadline:
                # This branch used to be a logger.warning and nothing else.
                # Connection and read timeouts are the most common EDGAR failure
                # mode by a wide margin, and because they never reached
                # poller_error_log, a poller timing out on every single cycle
                # was indistinguishable from one that simply had no new filings.
                logger.warning("[EDGAR] GET %s failed after %d attempt(s): %s", url, attempt, e)
                log_poller_error(POLLER_NAME, "sec_get",
                                 f"{type(e).__name__}: {e}",
                                 {"url": url, "attempts": attempt,
                                  "connect_timeout": timeout[0], "read_timeout": timeout[1]})
                return None
            time.sleep(max(0.0, min(backoff, SEC_MAX_BACKOFF_SECONDS, deadline - time.monotonic())))
            backoff *= 2
            continue

        if r.status_code == 200:
            return r

        if r.status_code in (403, 429, 503):
            retry_after = (r.headers.get("Retry-After") or "").strip()
            wait = float(retry_after) if retry_after.isdigit() else backoff
            wait = min(wait, SEC_MAX_BACKOFF_SECONDS)
            _apply_backoff(wait, f"HTTP {r.status_code}")
            backoff = min(backoff * 2, SEC_MAX_BACKOFF_SECONDS)
            if attempt == retries:
                log_poller_error(POLLER_NAME, "sec_get", f"HTTP {r.status_code} after {retries} attempts",
                                 {"url": url, "status_code": r.status_code,
                                  "body": r.text[:300]})
                return None
            continue

        if r.status_code == 404:
            return None

        logger.warning("[EDGAR] %s returned HTTP %s", url, r.status_code)
        if attempt == retries:
            log_poller_error(POLLER_NAME, "sec_get", f"HTTP {r.status_code} after {retries} attempts",
                             {"url": url, "status_code": r.status_code})
            return None
        time.sleep(max(0.0, min(backoff, SEC_MAX_BACKOFF_SECONDS, deadline - time.monotonic())))
        backoff *= 2
    return None


# ── CIK → ticker ──────────────────────────────────────────────────────────────
def load_cik_map():
    """Load SEC's full CIK↔ticker file. Called once at startup by main.py."""
    global CIK_MAP
    # Reset SEC backoff at startup so a previous rate-limit block doesn't prevent
    # the CIK map from loading, which would break all subsequent SEC polls.
    with _sec_lock:
        _sec_state["blocked_until"] = 0.0
    try:
        r = sec_get(EDGAR_CIK_URL, timeout=30, budget=60)
        if r is None:
            logger.error("[EDGAR] Could not download the CIK map; keeping %d cached entries",
                         len(CIK_MAP))
            return
        data = r.json()
        mapping = {}
        for val in data.values():
            try:
                cik = str(val["cik_str"]).zfill(10)
                ticker = str(val["ticker"]).upper().strip()
            except (KeyError, TypeError, ValueError):
                continue
            if ticker and cik not in mapping:
                # SEC lists multiple share classes against one CIK; the first is
                # the primary listing, which is what a watchlist will hold.
                mapping[cik] = ticker
        if mapping:
            CIK_MAP = mapping
            logger.info("[EDGAR] Loaded %d CIK→ticker mappings", len(CIK_MAP))
            # Debug: log sample tickers to verify map populated
            sample_tickers = sorted([CIK_MAP[k] for k in list(CIK_MAP.keys())[:50]])
            logger.info("[EDGAR] Sample tickers: %s", ", ".join(sample_tickers[:20]))
    except Exception as e:
        logger.error("[EDGAR] load_cik_map failed: %s", e)
        log_poller_error(POLLER_NAME, "load_cik_map", e)


def get_ticker_from_cik(cik):
    """
    Ticker for a CIK, from the local map only — no network call.

    The old version fell back to data.sec.gov/submissions/CIK*.json on a miss,
    one extra SEC request per unmapped filer. EDGAR's current-filings feed is
    dominated by funds, trusts and private filers that have no ticker at all, so
    that fallback spent most of the poller's rate-limit budget confirming that
    companies nobody can watch are, in fact, unwatchable. A miss here means "not
    a listed common-stock filer", which is exactly the case we want to drop.
    """
    if not cik:
        return None
    return CIK_MAP.get(str(cik).zfill(10))


# ── Text extraction ───────────────────────────────────────────────────────────
def clean_html_text(html):
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&reg;", "®").replace("&quot;", '"'))
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"This page uses Javascript.*?Javascript enabled browser\.", "", text,
                  flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _accession_and_cik(index_url):
    acc = re.search(r"(\d{10}-\d{2}-\d{6})", index_url or "")
    cik = re.search(r"/data/(\d+)/", index_url or "")
    if not acc or not cik:
        return None, None
    return acc.group(1), cik.group(1)


def fetch_filing_text(index_url, form_type=""):
    """
    Pull the primary document out of a filing. Two SEC requests in the good case
    (the accession index, then the document), three in the fallback.
    """
    accession, cik = _accession_and_cik(index_url)
    if not accession:
        return ""
    acc_nodash = accession.replace("-", "")

    primary_doc = None
    r = sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{accession}-index.json")
    if r is not None:
        try:
            files = r.json().get("directory", {}).get("item", [])
        except ValueError:
            files = []

        def _pick(predicate):
            for f in files:
                name = (f.get("name") or "").lower()
                if "index" in name:
                    continue
                if predicate(f, name):
                    return f.get("name")
            return None

        primary_doc = (
            _pick(lambda f, n: n.endswith((".htm", ".html")) and f.get("type") == form_type)
            or _pick(lambda f, n: n.endswith((".htm", ".html")) and f.get("type") in ("8-K", "S-1", "10-Q", "10-K"))
            or _pick(lambda f, n: n.endswith((".htm", ".html")))
            or _pick(lambda f, n: any(x in n for x in ("ex99", "ex-99", "exhibit99", "exhibit-99")))
        )

    if primary_doc:
        r2 = sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{primary_doc}")
        if r2 is not None:
            text = clean_html_text(r2.text)
            if len(text) > 300:
                return text[:MAX_FILING_CHARS]

    # Fallback: the complete submission text file.
    r3 = sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{accession}.txt")
    if r3 is None:
        return ""
    raw = r3.text
    pos = raw.find("<TEXT>")
    if pos != -1:
        raw = raw[pos + 6:]
        end = raw.find("</TEXT>")
        if end != -1:
            raw = raw[:end]
    elif "</SEC-HEADER>" in raw:
        raw = raw[raw.find("</SEC-HEADER>") + 13:]
    text = clean_html_text(raw)
    text = re.sub(r"</?[A-Z][A-Z\-]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_FILING_CHARS] if len(text) > 300 else ""


def _f(elem, path):
    """First matching child's stripped text, or ''."""
    node = elem.find(path) if elem is not None else None
    return (node.text or "").strip() if node is not None and node.text else ""


def fetch_form4_text(index_url, company_name, insider_name):
    """Render a Form 4's ownershipDocument XML into readable prose."""
    accession, cik = _accession_and_cik(index_url)
    if not accession:
        return ""
    acc_nodash = accession.replace("-", "")

    r = sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{accession}.txt")
    if r is None:
        return ""

    content = r.text
    start = content.find("<ownershipDocument>")
    end = content.find("</ownershipDocument>")
    if start == -1 or end == -1:
        return ""

    try:
        root = ET.fromstring(content[start:end + len("</ownershipDocument>")])
    except ET.ParseError as e:
        logger.debug("[EDGAR] Form 4 XML unparseable for %s: %s", index_url, e)
        return ""

    issuer_name = _f(root, ".//issuer/issuerName") or company_name
    issuer_ticker = _f(root, ".//issuer/issuerTradingSymbol")

    owner = root.find(".//reportingOwner")
    reporter_name = _f(owner, ".//rptOwnerName") or insider_name
    reporter_title = _f(owner, ".//officerTitle")
    if not reporter_title and owner is not None:
        if _f(owner, ".//isDirector") == "1":
            reporter_title = "Director"
        elif _f(owner, ".//isTenPercentOwner") == "1":
            reporter_title = "10% Owner"

    def _num(node_text):
        try:
            return float((node_text or "").replace(",", ""))
        except ValueError:
            return 0.0

    transactions = []
    for tag, verbs in (("nonDerivativeTransaction", ("purchased", "sold", "transacted")),
                       ("derivativeTransaction", ("acquired", "disposed", "exercised"))):
        for trans in root.findall(f".//{tag}"):
            shares = _num(_f(trans, ".//transactionShares/value"))
            if not shares:
                continue
            direction = _f(trans, ".//transactionAcquiredDisposedCode/value")
            action = verbs[0] if direction == "A" else verbs[1] if direction == "D" else verbs[2]
            price = _num(_f(trans, ".//transactionPricePerShare/value"))
            transactions.append({
                "action": action,
                "shares": shares,
                "price": price,
                "total": shares * price if price else 0.0,
                "owned_after": _num(_f(trans, ".//sharesOwnedFollowingTransaction/value")),
                "date": _f(trans, ".//transactionDate/value"),
                "security": _f(trans, ".//securityTitle/value") or "Common Stock",
            })

    if not transactions:
        return (f"{issuer_name} insider {reporter_name} "
                f"({reporter_title or 'Insider'}) filed a Form 4 disclosing a change "
                f"in beneficial ownership of {issuer_name} securities.")

    lines = [
        f"Company: {issuer_name} ({issuer_ticker})".strip(),
        f"Insider: {reporter_name}, {reporter_title or 'Insider'}",
        "Filing: Form 4 - Statement of Changes in Beneficial Ownership",
        "",
    ]
    for t in transactions:
        price_str = f"at ${t['price']:.2f} per share" if t["price"] else "at an undisclosed price"
        total_str = f" (total value ${t['total']:,.0f})" if t["total"] else ""
        date_str = f" on {t['date']}" if t["date"] else ""
        lines.append(f"{reporter_name} {t['action']} {t['shares']:,.0f} shares of "
                     f"{t['security']} {price_str}{total_str}{date_str}.")
        if t["owned_after"]:
            lines.append(f"Shares beneficially owned following the transaction: {t['owned_after']:,.0f}.")
    return "\n".join(lines)


# ── Dates and hashing ─────────────────────────────────────────────────────────
def parse_atom_date(value):
    """EDGAR's <updated> is ISO 8601 with an offset. Returns aware UTC or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def content_hash(text):
    norm = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return hashlib.sha256(norm.encode("utf-8", "ignore")).hexdigest()


# ── Supabase ──────────────────────────────────────────────────────────────────
def _chunked(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def existing_filing_urls(urls):
    """
    Batched dedup for one cycle's worth of candidate URLs.

    Raises on a database error rather than returning an empty set. The caller
    must treat that as "skip this cycle": returning "nothing exists" on an error
    is what caused whole feeds to be re-ingested during a Supabase blip.
    """
    found = set()
    for chunk in _chunked(urls, 50):
        rows = (supabase.table("raw_filings")
                .select("filing_url, ticker")
                .in_("filing_url", chunk).execute()).data or []
        for r in rows:
            found.add((r.get("filing_url"), (r.get("ticker") or "").upper()))
    return found


def filing_exists(filing_url, ticker=None):
    """
    Single-URL dedup. Fails CLOSED — returns True (treat as seen) on error.

    Retained for callers outside the batched path; the feed pollers use
    existing_filing_urls() instead.
    """
    try:
        q = supabase.table("raw_filings").select("id").eq("filing_url", filing_url)
        if ticker:
            q = q.eq("ticker", ticker)
        return bool(q.limit(1).execute().data)
    except Exception as e:
        logger.error("[EDGAR] Dedup check failed for %s: %s", filing_url, e)
        return True


def store_filing(filing_type, company_name, ticker, raw_text, filing_url,
                 filed_at, cik=None, extra=None):
    """Insert one PENDING row. Returns True when a row was actually created."""
    payload = dict(extra or {})
    payload.setdefault("cik", cik)
    payload.setdefault("form_type", filing_type)
    payload.setdefault("url", filing_url)
    try:
        supabase.table("raw_filings").insert({
            "source": SOURCE,
            "filing_type": filing_type,
            "company_name": company_name,
            "ticker": ticker,
            "cik": cik,
            "raw_text": raw_text,
            "filing_url": filing_url,
            "filed_at": filed_at,
            "status": "PENDING",
            "content_hash": content_hash(raw_text),
            "extra": tag_extra(payload, SOURCE, filing_type),
        }).execute()
        logger.info("[EDGAR] Stored %s — %s (%s)", filing_type, company_name, ticker)
        return True
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            logger.debug("[EDGAR] Already ingested: %s (%s)", filing_url, ticker)
            return False
        logger.error("[EDGAR] Insert failed for %s (%s): %s", ticker, filing_url, e)
        log_poller_error(POLLER_NAME, "store_filing", e,
                         {"filing_type": filing_type, "ticker": ticker, "filing_url": filing_url})
        return False


# ── Feed parsing ──────────────────────────────────────────────────────────────
def _entry_link(entry):
    for link in entry.findall("atom:link", ATOM_NS):
        href = (link.attrib.get("href") or "").strip()
        if href:
            return href
    return ""


def _entry_text(entry, tag):
    node = entry.find(f"atom:{tag}", ATOM_NS)
    return (node.text or "").strip() if node is not None and node.text else ""


def _parse_title(title, form_type):
    """'8-K - ACME CORP (0000012345) (Filer)' -> ('ACME CORP', '0000012345').

    The `(?:/A)?` matters. EDGAR titles amendments as '8-K/A - ACME CORP (...)',
    and the old pattern required a '-' immediately after the form type, so it
    failed on every amendment. A failed parse returns an empty CIK, which makes
    get_ticker_from_cik() return None, which makes the caller `continue` — so
    amended filings were dropped in silence with no log line. Amendments are
    frequently the material event (a restated 10-K, a corrected 8-K), so this
    was discarding exactly the filings most worth alerting on.
    """
    m = re.match(rf"{re.escape(form_type)}(?:/A)?\s*-\s*(.+?)\s*\((\d+)\)", title or "")
    if m:
        return m.group(1).strip(), m.group(2)
    # Last resort: pull any parenthesised CIK so the entry is still routable.
    m = re.match(r"[^-]+-\s*(.+?)\s*\((\d+)\)", title or "")
    if m:
        return m.group(1).strip(), m.group(2)
    logger.warning("[EDGAR] Could not parse title, dropping entry: %r", title)
    return (title or "").strip(), ""


# ── Generic EDGAR poller ──────────────────────────────────────────────────────
def poll_edgar_generic(url, form_type, label):
    """
    One pass over one EDGAR current-filings feed. Never raises.

    Order of operations is deliberate and is the cost control for this poller:
    parse -> resolve ticker from the local CIK map -> watchlist gate -> batched
    dedup -> only then fetch the filing body.
    """
    try:
        watched = get_watched_tickers()
        if not watched:
            logger.info("[EDGAR] %s: no watchlisted tickers; skipping.", label)
            return

        if not CIK_MAP:
            logger.warning("[EDGAR] CIK map empty — reloading before %s poll", label)
            load_cik_map()
            if not CIK_MAP:
                logger.error("[EDGAR] %s: cannot resolve tickers without a CIK map; skipping.", label)
                return

        r = sec_get(url)
        if r is None:
            return

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            log_poller_error(POLLER_NAME, f"parse:{label}", e, {"url": url})
            return

        entries = root.findall("atom:entry", ATOM_NS)
        candidates = []
        for entry in entries:
            title = _entry_text(entry, "title")
            filing_url = _entry_link(entry)
            if not title or not filing_url:
                continue

            company_name, cik = _parse_title(title, form_type)
            ticker = get_ticker_from_cik(cik)
            if not ticker or ticker not in watched:
                continue  # gate BEFORE spending SEC bandwidth on the document

            filed = parse_atom_date(_entry_text(entry, "updated"))
            candidates.append({
                "title": title,
                "url": filing_url,
                "summary": _entry_text(entry, "summary"),
                "company_name": company_name,
                "cik": cik,
                "ticker": ticker,
                "filed_at": (filed or datetime.now(timezone.utc)).isoformat(),
            })

        if not candidates:
            logger.info("[EDGAR] %s: %d entries, none watchlisted.", label, len(entries))
            return

        try:
            seen = existing_filing_urls({c["url"] for c in candidates})
        except Exception as e:
            logger.error("[EDGAR] %s: dedup query failed, skipping cycle: %s", label, e)
            log_poller_error(POLLER_NAME, f"dedup:{label}", e, {"url": url})
            return

        stored = 0
        for c in candidates:
            if (c["url"], c["ticker"]) in seen:
                continue

            filing_text = fetch_filing_text(c["url"], form_type)
            if not filing_text or len(filing_text) < 100:
                filing_text = f"{c['company_name']}\n\n{c['title']}\n\n{c['summary']}".strip()

            extra = {"cik": c["cik"], "form_type": form_type, "title": c["title"]}
            if form_type in ("10-Q", "10-K"):
                # Consumed by result_snapshot.py, which clears the flag when done.
                extra["needs_result_snapshot"] = True

            if store_filing(form_type, c["company_name"], c["ticker"], filing_text,
                            c["url"], c["filed_at"], cik=c["cik"], extra=extra):
                stored += 1
                seen.add((c["url"], c["ticker"]))

        logger.info("[EDGAR] %s: %d entries, %d watchlisted, %d stored.",
                    label, len(entries), len(candidates), stored)

    except Exception as e:
        logger.exception("[EDGAR] %s poll failed: %s", label, e)
        log_poller_error(POLLER_NAME, f"poll_edgar_generic:{label}", e, {"url": url})


def poll_sec_8k():
    poll_edgar_generic(EDGAR_8K_URL, "8-K", "8-K")


def poll_sec_10q():
    poll_edgar_generic(EDGAR_10Q_URL, "10-Q", "10-Q (Quarterly Report)")


def poll_sec_10k():
    poll_edgar_generic(EDGAR_10K_URL, "10-K", "10-K (Annual Report)")


def poll_sec_s1():
    poll_edgar_generic(EDGAR_S1_URL, "S-1", "S-1 (Registration)")


# ── Form 4 ────────────────────────────────────────────────────────────────────
def poll_sec_form4():
    """
    Form 4 needs its own pass because EDGAR emits one entry per party — an
    (Issuer) entry and a (Reporting) entry sharing an accession number. The
    issuer entry carries the company CIK we gate on; the reporting entry carries
    the insider's name. Never raises.
    """
    try:
        watched = get_watched_tickers()
        if not watched:
            logger.info("[EDGAR] Form 4: no watchlisted tickers; skipping.")
            return

        if not CIK_MAP:
            load_cik_map()
            if not CIK_MAP:
                logger.error("[EDGAR] Form 4: cannot resolve tickers without a CIK map; skipping.")
                return

        r = sec_get(EDGAR_FORM4_URL)
        if r is None:
            return

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            log_poller_error(POLLER_NAME, "parse:form4", e, {"url": EDGAR_FORM4_URL})
            return

        by_accession = {}
        for entry in root.findall("atom:entry", ATOM_NS):
            title = _entry_text(entry, "title")
            filing_url = _entry_link(entry)
            if not title or not filing_url:
                continue
            acc = re.search(r"(\d{10}-\d{2}-\d{6})", filing_url)
            if not acc:
                continue
            by_accession.setdefault(acc.group(1), []).append({
                "title": title,
                "url": filing_url,
                "updated": _entry_text(entry, "updated"),
            })

        candidates = []
        for acc_num, group in by_accession.items():
            issuer = next((e for e in group if "(Issuer)" in e["title"]), None)
            reporting = next((e for e in group if "(Reporting)" in e["title"]), None)
            if not issuer:
                continue

            m = re.match(r"4\s*-\s*(.+?)\s*\((\d+)\)\s*\(Issuer\)", issuer["title"])
            company_name = m.group(1).strip() if m else issuer["title"]
            cik = m.group(2) if m else ""
            ticker = get_ticker_from_cik(cik)
            if not ticker or ticker not in watched:
                continue

            insider_name = "Unknown Insider"
            if reporting:
                rm = re.match(r"4\s*-\s*(.+?)\s*\(\d+\)\s*\(Reporting\)", reporting["title"])
                if rm:
                    insider_name = rm.group(1).strip()

            filed = parse_atom_date(issuer["updated"])
            candidates.append({
                "url": issuer["url"],
                "company_name": company_name,
                "cik": cik,
                "ticker": ticker,
                "insider_name": insider_name,
                "accession": acc_num,
                "filed_at": (filed or datetime.now(timezone.utc)).isoformat(),
            })

        if not candidates:
            logger.info("[EDGAR] Form 4: %d accessions, none watchlisted.", len(by_accession))
            return

        try:
            seen = existing_filing_urls({c["url"] for c in candidates})
        except Exception as e:
            logger.error("[EDGAR] Form 4 dedup query failed, skipping cycle: %s", e)
            log_poller_error(POLLER_NAME, "dedup:form4", e)
            return

        stored = 0
        for c in candidates:
            if (c["url"], c["ticker"]) in seen:
                continue

            text = fetch_form4_text(c["url"], c["company_name"], c["insider_name"])
            if not text or len(text) < 50:
                text = (f"{c['company_name']} insider {c['insider_name']} filed a Form 4 "
                        f"disclosing a change in beneficial ownership.")

            if store_filing("4", c["company_name"], c["ticker"], text, c["url"],
                            c["filed_at"], cik=c["cik"],
                            extra={"insider_name": c["insider_name"],
                                   "accession": c["accession"],
                                   "title": f"{c['company_name']} Form 4 — {c['insider_name']}"}):
                stored += 1
                seen.add((c["url"], c["ticker"]))

        logger.info("[EDGAR] Form 4: %d accessions, %d watchlisted, %d stored.",
                    len(by_accession), len(candidates), stored)

    except Exception as e:
        logger.exception("[EDGAR] Form 4 poll failed: %s", e)
        log_poller_error(POLLER_NAME, "poll_sec_form4", e, {"url": EDGAR_FORM4_URL})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_cik_map()
    poll_sec_8k()
    poll_sec_form4()
    poll_sec_10q()
    poll_sec_10k()
    poll_sec_s1()
