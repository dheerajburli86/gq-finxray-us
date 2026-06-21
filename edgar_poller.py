import requests
import xml.etree.ElementTree as ET
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import os
import time
import re

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

HEADERS = {
    "User-Agent": "GQFinXray/1.0 dheerajburli86@gmail.com",
    "Accept-Encoding": "gzip, deflate"
}

CIK_MAP = {}

EDGAR_8K_URL   = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_FORM4_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_10Q_URL  = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-Q&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_10K_URL  = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-K&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_S1_URL   = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-1&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_CIK_URL  = "https://www.sec.gov/files/company_tickers.json"

def load_cik_map():
    global CIK_MAP
    print("[SETUP] Loading SEC CIK-to-ticker mapping...")
    try:
        r = requests.get(EDGAR_CIK_URL, headers=HEADERS, timeout=30)
        data = r.json()
        for key, val in data.items():
            cik = str(val["cik_str"]).zfill(10)
            ticker = val["ticker"].upper()
            CIK_MAP[cik] = ticker
        print(f"[SETUP] Loaded {len(CIK_MAP):,} ticker mappings")
    except Exception as e:
        print(f"[ERROR] Failed to load CIK map: {e}")

def get_ticker_from_cik(cik: str) -> str:
    padded = str(cik).zfill(10)
    if padded in CIK_MAP:
        return CIK_MAP[padded]
    try:
        url = f"https://data.sec.gov/submissions/CIK{padded}.json"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            tickers = data.get("tickers", [])
            if tickers:
                return tickers[0].upper()
    except:
        pass
    return "UNKNOWN"

def clean_html_text(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&reg;', '®', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'This page uses Javascript.*?Javascript enabled browser\.', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def fetch_filing_text(index_url: str) -> str:
    try:
        acc_match = re.search(r'(\d{10}-\d{2}-\d{6})', index_url)
        if not acc_match:
            return ""
        accession = acc_match.group(1)
        accession_nodash = accession.replace("-", "")

        cik_match = re.search(r'/data/(\d+)/', index_url)
        if not cik_match:
            return ""
        cik = cik_match.group(1)

        index_json_url = f"https://data.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{accession}-index.json"
        r = requests.get(index_json_url, headers=HEADERS, timeout=15)

        primary_doc = None
        if r.status_code == 200:
            files = r.json().get("directory", {}).get("item", [])
            for f in files:
                name = f.get("name", "").lower()
                doc_type = f.get("type", "")
                if "index" in name:
                    continue
                if (name.endswith(".htm") or name.endswith(".html")) and doc_type in ("8-K", "S-1", "10-Q", "10-K"):
                    primary_doc = f.get("name", "")
                    break
            if not primary_doc:
                for f in files:
                    name = f.get("name", "").lower()
                    if (name.endswith(".htm") or name.endswith(".html")) and "index" not in name:
                        primary_doc = f.get("name", "")
                        break
            if not primary_doc:
                for f in files:
                    name = f.get("name", "").lower()
                    if any(x in name for x in ["ex99", "ex-99", "exhibit99", "exhibit-99"]):
                        primary_doc = f.get("name", "")
                        break

        if primary_doc:
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{primary_doc}"
            r2 = requests.get(doc_url, headers=HEADERS, timeout=15)
            if r2.status_code == 200:
                text = clean_html_text(r2.text)
                if len(text) > 300:
                    return text[:6000]

        txt_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{accession}.txt"
        r3 = requests.get(txt_url, headers=HEADERS, timeout=15)
        if r3.status_code == 200:
            raw = r3.text
            text_tag_pos = raw.find("<TEXT>")
            if text_tag_pos != -1:
                raw = raw[text_tag_pos + 6:]
                end_pos = raw.find("</TEXT>")
                if end_pos != -1:
                    raw = raw[:end_pos]
            elif "</SEC-HEADER>" in raw:
                raw = raw[raw.find("</SEC-HEADER>") + 13:]
            text = clean_html_text(raw)
            text = re.sub(r'<[A-Z][A-Z\-]*>', ' ', text)
            text = re.sub(r'</[A-Z][A-Z\-]*>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 300:
                return text[:6000]

        return ""
    except Exception as e:
        return ""

def fetch_form4_text(index_url: str, company_name: str, insider_name: str) -> str:
    try:
        acc_match = re.search(r'(\d{10}-\d{2}-\d{6})', index_url)
        if not acc_match:
            return ""
        accession = acc_match.group(1)
        cik_match = re.search(r'/data/(\d+)/', index_url)
        if not cik_match:
            return ""
        cik = cik_match.group(1)
        acc_nodash = accession.replace("-", "")
        txt_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{accession}.txt"
        r = requests.get(txt_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return ""
        content = r.text
        xml_start = content.find("<ownershipDocument>")
        xml_end = content.find("</ownershipDocument>")
        if xml_start == -1 or xml_end == -1:
            return ""
        xml_text = content[xml_start:xml_end + len("</ownershipDocument>")]
        root = ET.fromstring(xml_text)
        issuer_name = company_name
        issuer_ticker = ""
        issuer_elem = root.find(".//issuer")
        if issuer_elem is not None:
            name_elem = issuer_elem.find("issuerName")
            tick_elem = issuer_elem.find("issuerTradingSymbol")
            if name_elem is not None and name_elem.text:
                issuer_name = name_elem.text.strip()
            if tick_elem is not None and tick_elem.text:
                issuer_ticker = tick_elem.text.strip()
        reporter_name = insider_name
        reporter_title = ""
        reporting_owner = root.find(".//reportingOwner")
        if reporting_owner is not None:
            rp_name = reporting_owner.find(".//rptOwnerName")
            if rp_name is not None and rp_name.text:
                reporter_name = rp_name.text.strip()
            rel_elem = reporting_owner.find(".//officerTitle")
            if rel_elem is not None and rel_elem.text:
                reporter_title = rel_elem.text.strip()
            if not reporter_title:
                is_director = reporting_owner.find(".//isDirector")
                is_ten_pct = reporting_owner.find(".//isTenPercentOwner")
                if is_director is not None and is_director.text == "1":
                    reporter_title = "Director"
                elif is_ten_pct is not None and is_ten_pct.text == "1":
                    reporter_title = "10% Owner"
        transactions = []
        for trans in root.findall(".//nonDerivativeTransaction"):
            security = trans.find(".//securityTitle/value")
            trans_date = trans.find(".//transactionDate/value")
            trans_code = trans.find(".//transactionCode")
            shares = trans.find(".//transactionShares/value")
            price = trans.find(".//transactionPricePerShare/value")
            shares_owned = trans.find(".//sharesOwnedFollowingTransaction/value")
            acquired_disposed = trans.find(".//transactionAcquiredDisposedCode/value")
            if shares is not None and trans_code is not None:
                direction = acquired_disposed.text.strip() if acquired_disposed is not None and acquired_disposed.text else ""
                action = "purchased" if direction == "A" else "sold" if direction == "D" else "transacted"
                try:
                    share_count = float(shares.text.replace(",", "")) if shares.text else 0
                    price_val = float(price.text.replace(",", "")) if price is not None and price.text and price.text not in ("0", "") else 0
                    total_val = share_count * price_val if price_val > 0 else 0
                    owned_after = float(shares_owned.text.replace(",", "")) if shares_owned is not None and shares_owned.text else 0
                    date_str = trans_date.text.strip() if trans_date is not None and trans_date.text else ""
                    security_name = security.text.strip() if security is not None and security.text else "Common Stock"
                    transactions.append({"action": action, "shares": share_count, "price": price_val, "total": total_val, "owned_after": owned_after, "date": date_str, "security": security_name})
                except:
                    pass
        for trans in root.findall(".//derivativeTransaction"):
            security = trans.find(".//securityTitle/value")
            trans_date = trans.find(".//transactionDate/value")
            shares = trans.find(".//transactionShares/value")
            price = trans.find(".//transactionPricePerShare/value")
            acquired_disposed = trans.find(".//transactionAcquiredDisposedCode/value")
            if shares is not None:
                direction = acquired_disposed.text.strip() if acquired_disposed is not None and acquired_disposed.text else ""
                action = "acquired" if direction == "A" else "disposed" if direction == "D" else "exercised"
                try:
                    share_count = float(shares.text.replace(",", "")) if shares.text else 0
                    price_val = float(price.text.replace(",", "")) if price is not None and price.text and price.text not in ("0", "") else 0
                    total_val = share_count * price_val if price_val > 0 else 0
                    date_str = trans_date.text.strip() if trans_date is not None and trans_date.text else ""
                    security_name = security.text.strip() if security is not None and security.text else "Derivative Security"
                    transactions.append({"action": action, "shares": share_count, "price": price_val, "total": total_val, "owned_after": 0, "date": date_str, "security": security_name})
                except:
                    pass
        if not transactions:
            return f"{issuer_name} insider {reporter_name} ({reporter_title}) filed Form 4 disclosing changes in ownership of {issuer_name} shares."
        lines = [f"Company: {issuer_name} ({issuer_ticker})", f"Insider: {reporter_name}, {reporter_title or 'Insider'}", f"Filing: Form 4 – Insider Transaction Report", ""]
        for t in transactions:
            share_str = f"{t['shares']:,.0f} shares"
            price_str = f"at ${t['price']:.2f} per share" if t['price'] > 0 else "at undisclosed price"
            total_str = f"(total value ${t['total']:,.0f})" if t['total'] > 0 else ""
            owned_str = f"Total shares owned after transaction: {t['owned_after']:,.0f}" if t['owned_after'] > 0 else ""
            date_str = f"on {t['date']}" if t['date'] else ""
            lines.append(f"{reporter_name} {t['action']} {share_str} of {t['security']} {price_str} {total_str} {date_str}.")
            if owned_str:
                lines.append(owned_str)
        return "\n".join(lines)
    except Exception as e:
        print(f"[FORM4] Parse error: {e}")
        return f"{company_name} insider {insider_name} filed Form 4 disclosing changes in ownership."

def filing_exists(filing_url: str) -> bool:
    try:
        result = supabase.table("raw_filings").select("id").eq("filing_url", filing_url).execute()
        return len(result.data) > 0
    except:
        return False

def store_filing(filing_type, company_name, ticker, raw_text, filing_url, extra=None):
    try:
        supabase.table("raw_filings").insert({
            "source": "SEC_EDGAR",
            "filing_type": filing_type,
            "company_name": company_name,
            "ticker": ticker,
            "raw_text": raw_text,
            "filing_url": filing_url,
            "filed_at": datetime.now().isoformat(),
            "status": "PENDING",
            "extra": extra or {}
        }).execute()
        print(f"[STORED] {filing_type} – {company_name} ({ticker}) → PENDING")
    except Exception as e:
        print(f"[ERROR] Failed to store filing: {e}")

# ── Generic EDGAR poller ──────────────────────────────────────────────────────
def poll_edgar_generic(url, form_type, label):
    """Generic EDGAR RSS poller — works for 8-K, 10-Q, 10-K, S-1."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling SEC EDGAR for {label}...")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] SEC EDGAR returned {r.status_code}")
            return

        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)

        new_count = 0
        for entry in entries:
            title_elem = entry.find("atom:title", ns)
            link_elem  = entry.find("atom:link", ns)
            summary_elem = entry.find("atom:summary", ns)

            if title_elem is None or link_elem is None:
                continue

            title = title_elem.text or ""
            filing_url = link_elem.attrib.get("href", "")
            summary_text = summary_elem.text if summary_elem is not None else ""

            if not filing_url or filing_exists(filing_url):
                continue

            pattern = rf'{re.escape(form_type)}\s*-\s*(.+?)\s*\((\d+)\)'
            company_match = re.match(pattern, title)
            company_name = company_match.group(1).strip() if company_match else title
            cik = company_match.group(2) if company_match else ""
            ticker = get_ticker_from_cik(cik) if cik else "UNKNOWN"

            filing_text = fetch_filing_text(filing_url)
            if not filing_text or len(filing_text) < 100:
                filing_text = f"{company_name}\n\n{title}\n\n{summary_text}"

            # Tag 10-Q and 10-K for Result Snapshot processing
            extra = {"cik": cik, "form_type": form_type}
            if form_type in ("10-Q", "10-K"):
                extra["needs_result_snapshot"] = True

            store_filing(
                filing_type=form_type,
                company_name=company_name,
                ticker=ticker,
                raw_text=filing_text,
                filing_url=filing_url,
                extra=extra
            )
            new_count += 1
            time.sleep(0.3)

        if new_count == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] No new {label} filings.")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Stored {new_count} new {label} filings.")

    except Exception as e:
        print(f"[ERROR] {label} poll failed: {e}")

def poll_sec_8k():
    poll_edgar_generic(EDGAR_8K_URL, "8-K", "8-K")

def poll_sec_10q():
    poll_edgar_generic(EDGAR_10Q_URL, "10-Q", "10-Q (Quarterly Results)")

def poll_sec_10k():
    poll_edgar_generic(EDGAR_10K_URL, "10-K", "10-K (Annual Results)")

def poll_sec_s1():
    poll_edgar_generic(EDGAR_S1_URL, "S-1", "S-1 (IPO Filing)")

def poll_sec_form4():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling SEC EDGAR for Form 4...")
    try:
        r = requests.get(EDGAR_FORM4_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] SEC EDGAR returned {r.status_code}")
            return

        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)

        accession_map = {}
        for entry in entries:
            title_elem = entry.find("atom:title", ns)
            link_elem  = entry.find("atom:link", ns)
            if title_elem is None or link_elem is None:
                continue
            title = title_elem.text or ""
            filing_url = link_elem.attrib.get("href", "")
            acc_match = re.search(r'(\d{10}-\d{2}-\d{6})', filing_url)
            if not acc_match:
                continue
            acc_num = acc_match.group(1)
            if acc_num not in accession_map:
                accession_map[acc_num] = []
            accession_map[acc_num].append({"title": title, "url": filing_url})

        new_count = 0
        processed_urls = set()

        for acc_num, entries_list in accession_map.items():
            issuer = None
            reporting = None
            for e in entries_list:
                if "(Issuer)" in e["title"]:
                    issuer = e
                elif "(Reporting)" in e["title"]:
                    reporting = e

            if not issuer:
                continue

            filing_url = issuer["url"]
            if filing_url in processed_urls or filing_exists(filing_url):
                continue
            processed_urls.add(filing_url)

            issuer_match = re.match(r'4\s*-\s*(.+?)\s*\((\d+)\)\s*\(Issuer\)', issuer["title"])
            company_name = issuer_match.group(1).strip() if issuer_match else issuer["title"]
            cik = issuer_match.group(2) if issuer_match else ""
            ticker = get_ticker_from_cik(cik) if cik else "UNKNOWN"

            insider_name = "Unknown Insider"
            if reporting:
                rep_match = re.match(r'4\s*-\s*(.+?)\s*\(\d+\)\s*\(Reporting\)', reporting["title"])
                if rep_match:
                    insider_name = rep_match.group(1).strip()

            filing_text = fetch_form4_text(filing_url, company_name, insider_name)
            if not filing_text or len(filing_text) < 50:
                filing_text = f"{company_name} insider {insider_name} filed Form 4 disclosing changes in ownership."

            store_filing(
                filing_type="4",
                company_name=company_name,
                ticker=ticker,
                raw_text=filing_text,
                filing_url=filing_url,
                extra={"insider_name": insider_name, "cik": cik}
            )
            new_count += 1
            time.sleep(0.3)

        if new_count == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] No new Form 4 filings.")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Stored {new_count} new Form 4 filings.")

    except Exception as e:
        print(f"[ERROR] Form 4 poll failed: {e}")

if __name__ == "__main__":
    load_cik_map()
    while True:
        poll_sec_8k()
        poll_sec_form4()
        poll_sec_10q()
        poll_sec_10k()
        poll_sec_s1()
        time.sleep(30)
