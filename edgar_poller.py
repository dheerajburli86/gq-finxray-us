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
from zoneinfo import ZoneInfo

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

# Manual overrides for tickers that may not yet be in SEC's company_tickers.json
# Used when SEC's official CIK file hasn't caught up to a recent ticker rename.
# Example: Block Inc. (formerly Square) renamed from SQ to XYZ in Jan 2025.
MANUAL_TICKER_CIK_OVERRIDES = {
    "XYZ": "0001616707",  # Block Inc. (formerly Square) — renamed Jan 2025
}

# ── Per-company filing index ──────────────────────────────────────────────────
# WHY THIS EXISTS (2026-08-19)
#
# The five URLs above are EDGAR's `action=getcurrent` FIREHOSE: the N most
# recent filings of a type across every reporting company, with count=40. That
# is the wrong data structure for a watchlist, and the arithmetic is brutal:
#
#   * ~600 8-Ks are filed market-wide on a normal day.
#   * The 21 watched megacaps file ~0.85 8-Ks per day between them.
#   * Watchlist share of the stream is therefore ~0.14%.
#   * Expected watched entries inside a 40-entry window: 40 x 0.0014 = 0.06.
#
# So "40 total entries, 34 skipped (not watched), 0 candidates" every single
# cycle is not a bug in the gate — it is exactly what the base rate predicts.
# The poller was working perfectly and could still never see a watched filing.
#
# It is worse for the others. Form 4 emits 2+ Atom entries per accession and
# ~1,100 accessions land between 16:00 and 18:00 ET, so a 40-entry window is
# under two minutes of peak tape. 10-Q polls every 5 minutes against deadline
# days that produce thousands of filings in an afternoon. Anything filed by a
# watched company in the gap between two polls is lost permanently — which is
# why raw_filings has never held a single 10-Q or 10-K row.
#
# data.sec.gov/submissions/CIK##########.json is the correct endpoint: it is
# that ONE company's filing history, so nothing can fall out of a window at any
# filing rate. One request per watched ticker covers every form type at once —
# 21 requests replaces 5 firehose requests and actually finds things.
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
TICKER_TO_CIK = {}

# Only surface filings this fresh. The product promise is "less than 24 hours
# old"; a slightly wider window absorbs weekend and holiday gaps without ever
# delivering something the user would call old news.
EDGAR_MAX_AGE_HOURS = float(os.getenv("EDGAR_MAX_AGE_HOURS", "36"))

# Per-ticker high-water mark, so a company that files twice in a day produces
# two alerts and a company that files nothing costs one cheap JSON read.
_last_seen_accession = {}


def _rebuild_ticker_index(raw_cik_json):
    """
    ticker -> zero-padded CIK, from SEC's own company_tickers.json.

    Built alongside CIK_MAP rather than inverted from it: CIK_MAP keeps only the
    FIRST ticker per CIK, so inverting it loses every secondary share class.
    Alphabet files one 8-K under CIK 0001652044 and that CIK maps to whichever
    of GOOGL/GOOG the file happened to list first — if it listed GOOG, a
    GOOGL watcher silently never receives an Alphabet filing. Indexing every
    ticker separately removes that entire failure mode.
    """
    global TICKER_TO_CIK
    index = {}
    for val in (raw_cik_json or {}).values():
        try:
            cik = str(val["cik_str"]).zfill(10)
            ticker = str(val["ticker"]).upper().strip()
        except (KeyError, TypeError, ValueError):
            continue
        if ticker:
            index[ticker] = cik

    # Apply manual overrides for recently-renamed tickers
    for ticker, cik_raw in MANUAL_TICKER_CIK_OVERRIDES.items():
        ticker_upper = ticker.upper().strip()
        if ticker_upper not in index:  # Only override if not already in SEC's file
            index[ticker_upper] = str(cik_raw).zfill(10)

    if index:
        TICKER_TO_CIK = index

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
            _rebuild_ticker_index(data)
            logger.info("[EDGAR] Loaded %d CIK→ticker mappings (%d tickers indexed)",
                        len(CIK_MAP), len(TICKER_TO_CIK))
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


def _recent_filings_for_cik(cik):
    """
    That company's recent filings, newest first, from data.sec.gov.

    Returns a list of dicts: form, accession, filed (date), primary_doc, url.
    Returns None when the request failed, so the caller can tell "no filings"
    apart from "could not ask" — the firehose path could not make that
    distinction and reported both as silence.
    """
    r = sec_get(SEC_SUBMISSIONS_URL.format(cik=cik))
    if r is None:
        return None
    try:
        recent = (r.json().get("filings") or {}).get("recent") or {}
    except Exception:
        return None

    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    accepted = recent.get("acceptanceDateTime") or []
    primary = recent.get("primaryDocument") or []
    if not forms or not accessions:
        return []

    bare_cik = str(int(cik))  # archive paths use the unpadded CIK
    out = []
    for i, form in enumerate(forms):
        try:
            acc = accessions[i]
        except IndexError:
            continue
        acc_nodash = acc.replace("-", "")
        doc = primary[i] if i < len(primary) else ""
        out.append({
            "form": (form or "").upper().strip(),
            "accession": acc,
            "filed": dates[i] if i < len(dates) else "",
            "accepted": accepted[i] if i < len(accepted) else "",
            "primary_doc": doc,
            "url": (f"https://www.sec.gov/Archives/edgar/data/{bare_cik}/"
                    f"{acc_nodash}/{doc}" if doc else
                    f"https://www.sec.gov/Archives/edgar/data/{bare_cik}/"
                    f"{acc_nodash}/{acc}-index.htm"),
        })
    return out


def _filing_age_hours(item):
    """Hours since SEC accepted the filing. Large number when unparseable."""
    stamp = item.get("accepted") or item.get("filed")
    if not stamp:
        return 1e9
    try:
        raw = str(stamp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            # acceptanceDateTime and filingDate are both US Eastern.
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return 1e9


def poll_edgar_watchlist(form_types, label):
    """
    Poll EDGAR per watched company instead of scraping the market-wide firehose.

    One `data.sec.gov/submissions` read per watched ticker returns that
    company's complete recent filing history, so a watched filing can never be
    missed no matter how busy the tape is. See the SEC_SUBMISSIONS_URL comment
    for why the previous getcurrent?count=40 approach structurally could not
    work for a 21-name watchlist.
    """
    wanted_forms = {f.upper() for f in form_types}
    try:
        watched = get_watched_tickers()
        if not watched:
            logger.info("[EDGAR] %s: no watchlisted tickers; skipping.", label)
            return

        if not TICKER_TO_CIK:
            logger.warning("[EDGAR] Ticker index empty — reloading before %s poll", label)
            load_cik_map()
            if not TICKER_TO_CIK:
                logger.error("[EDGAR] %s: cannot resolve CIKs; skipping.", label)
                return

        candidates = []
        unresolved = []
        for ticker in sorted(watched):
            cik = TICKER_TO_CIK.get(ticker.upper())
            if not cik:
                unresolved.append(ticker)
                continue

            filings = _recent_filings_for_cik(cik)
            if filings is None:
                continue  # request failed; sec_get already logged/backed off
            if not filings:
                continue

            for item in filings:
                if item["form"] not in wanted_forms:
                    continue
                # The list is newest-first, so the first form match that is too
                # old means every later one is older still.
                if _filing_age_hours(item) > EDGAR_MAX_AGE_HOURS:
                    break
                # Don't use _last_seen_accession as a break; it can get permanently stuck
                # if a filing fails downstream. Instead, add to candidates and let the
                # dedup query (line 901) decide what's been seen before.
                candidates.append({
                    "title": f"{ticker} {item['form']} filed {item['filed']}",
                    "url": item["url"],
                    "summary": "",
                    "company_name": ticker,
                    "cik": cik,
                    "ticker": ticker,
                    "form": item["form"],
                    "accession": item["accession"],
                    "filed_at": _edgar_iso(item),
                })

        if unresolved:
            logger.warning("[EDGAR] %s: %d watchlist ticker(s) have no SEC CIK and can "
                           "never produce a filing alert: %s",
                           label, len(unresolved), ", ".join(sorted(unresolved)))

        if not candidates:
            logger.info("[EDGAR] %s: no new filings for %d watched ticker(s).",
                        label, len(watched))
            return

        try:
            seen = existing_filing_urls({c["url"] for c in candidates})
        except Exception as e:
            logger.error("[EDGAR] %s: dedup query failed, skipping cycle: %s", label, e)
            log_poller_error(POLLER_NAME, f"dedup:{label}", e, {"label": label})
            return

        stored = 0
        for c in candidates:
            if (c["url"], c["ticker"]) in seen:
                _last_seen_accession[c["ticker"]] = c["accession"]
                continue

            filing_text = fetch_filing_text(c["url"], c["form"])
            if not filing_text or len(filing_text) < 100:
                filing_text = f"{c['company_name']}\n\n{c['title']}".strip()

            extra = {"cik": c["cik"], "form_type": c["form"], "title": c["title"],
                     "accession": c["accession"], "url": c["url"]}
            if c["form"] in ("10-Q", "10-K"):
                # Consumed by result_snapshot.py, which clears the flag when done.
                extra["needs_result_snapshot"] = True

            if store_filing(c["form"], c["company_name"], c["ticker"], filing_text,
                            c["url"], c["filed_at"], cik=c["cik"], extra=extra):
                stored += 1
                seen.add((c["url"], c["ticker"]))
                _last_seen_accession[c["ticker"]] = c["accession"]

        logger.info("[EDGAR] %s: %d watched ticker(s), %d new filing(s), %d stored.",
                    label, len(watched), len(candidates), stored)

    except Exception as e:
        logger.exception("[EDGAR] %s poll failed: %s", label, e)
        log_poller_error(POLLER_NAME, f"poll_edgar_watchlist:{label}", e, {"label": label})


def _edgar_iso(item):
    """Best available timestamp for a submissions-API filing, as ISO-8601 UTC."""
    stamp = item.get("accepted") or item.get("filed")
    try:
        raw = str(stamp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def poll_sec_8k():
    # 8-K/A amendments carry the same market-moving content as the original.
    poll_edgar_watchlist(["8-K", "8-K/A"], "8-K")


def poll_sec_10q():
    poll_edgar_watchlist(["10-Q", "10-Q/A"], "10-Q (Quarterly Report)")


def poll_sec_10k():
    poll_edgar_watchlist(["10-K", "10-K/A"], "10-K (Annual Report)")


def poll_sec_s1():
    poll_edgar_watchlist(["S-1", "S-1/A"], "S-1 (Registration)")


# ── Form 4 ────────────────────────────────────────────────────────────────────
def poll_sec_form4():
    """
    Insider transactions for watched companies.

    Rewritten 2026-08-19 to use the per-company submissions API like every other
    EDGAR form. The old implementation read the market-wide `getcurrent&type=4`
    firehose with count=40 and then de-interleaved EDGAR's paired (Issuer) and
    (Reporting) Atom entries. That pairing logic was correct; the feed it read
    was not. Form 4 emits two or more entries per accession and roughly 1,100
    accessions land between 16:00 and 18:00 ET, so a 40-entry window covers
    under two minutes of peak tape — a watched insider filing at 16:30 was gone
    before the next 30-second poll. EDGAR indexes every Form 4 under the ISSUER's
    CIK as well as the reporting person's, so the issuer submissions feed sees
    all of them, in order, with nothing to de-interleave.
    """
    poll_edgar_watchlist(["4", "4/A"], "Form 4 (Insider)")



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_cik_map()
    poll_sec_8k()
    poll_sec_form4()
    poll_sec_10q()
    poll_sec_10k()
    poll_sec_s1()
