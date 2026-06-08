import requests
import xml.etree.ElementTree as ET
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import os
import time
import re
import json

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

HEADERS = {
    "User-Agent": "GQFinXray/1.0 dheerajburli86@gmail.com",
    "Accept-Encoding": "gzip, deflate"
}

CIK_MAP = {}

EDGAR_8K_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_FORM4_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_CIK_URL = "https://www.sec.gov/files/company_tickers.json"

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

def extract_cik_from_url(url: str) -> str:
    match = re.search(r'CIK=?(\d+)', url, re.IGNORECASE)
    if match:
        return match.group(1).zfill(10)
    match = re.search(r'/(\d{10})/', url)
    if match:
        return match.group(1)
    return ""

def fetch_filing_text(filing_url: str) -> str:
    """Fetch the actual text content of a filing from SEC EDGAR."""
    try:
        # Convert filing index URL to actual document
        # e.g. https://www.sec.gov/Archives/edgar/data/320193/000032019326000123/0000320193-26-000123-index.htm
        r = requests.get(filing_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return ""

        content = r.text

        # If it's an index page find the main document
        if "index" in filing_url.lower() or filing_url.endswith(".htm"):
            # Look for the primary document link
            doc_match = re.search(
                r'href="(/Archives/edgar/data/[^"]+\.htm)"',
                content, re.IGNORECASE
            )
            if doc_match:
                doc_url = "https://www.sec.gov" + doc_match.group(1)
                r2 = requests.get(doc_url, headers=HEADERS, timeout=15)
                if r2.status_code == 200:
                    content = r2.text

        # Strip HTML tags
        text = re.sub(r'<[^>]+>', ' ', content)
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Return first 6000 chars — enough for AI to summarise
        return text[:6000]

    except Exception as e:
        return ""

def filing_exists(filing_url: str) -> bool:
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("filing_url", filing_url) \
            .execute()
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
        print(f"[STORED] {filing_type} — {company_name} ({ticker}) → PENDING")
    except Exception as e:
        print(f"[ERROR] Failed to store filing: {e}")

def poll_sec_8k():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling SEC EDGAR for 8-K...")
    try:
        r = requests.get(EDGAR_8K_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] SEC EDGAR returned {r.status_code}")
            return

        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)

        new_count = 0
        for entry in entries:
            title_elem = entry.find("atom:title", ns)
            link_elem = entry.find("atom:link", ns)
            summary_elem = entry.find("atom:summary", ns)

            if title_elem is None or link_elem is None:
                continue

            title = title_elem.text or ""
            filing_url = link_elem.attrib.get("href", "")
            summary_text = summary_elem.text if summary_elem is not None else ""

            if not filing_url or filing_exists(filing_url):
                continue

            # Extract company name and item types from title
            # Title format: "8-K - COMPANY NAME (CIK) (Reporting)"
            company_match = re.match(r'8-K\s*-\s*(.+?)\s*\((\d+)\)', title)
            company_name = company_match.group(1).strip() if company_match else title
            cik = company_match.group(2) if company_match else ""

            ticker = get_ticker_from_cik(cik) if cik else "UNKNOWN"

            # Extract item types from summary
            item_types = re.findall(r'Item \d+\.\d+[^,<\n]*', summary_text)

            # Fetch actual filing content for better summarisation
            filing_text = fetch_filing_text(filing_url)
            if not filing_text:
                # Fallback to title + summary if fetch fails
                filing_text = f"{company_name}\n\n{title}\n\n{summary_text}"

            store_filing(
                filing_type="8-K",
                company_name=company_name,
                ticker=ticker,
                raw_text=filing_text,
                filing_url=filing_url,
                extra={
                    "item_types": item_types,
                    "cik": cik
                }
            )
            new_count += 1
            time.sleep(0.5)

        if new_count == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] No new 8-K filings.")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Stored {new_count} new 8-K filings.")

    except Exception as e:
        print(f"[ERROR] 8-K poll failed: {e}")

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

        # Group Form 4 entries by accession number to match issuer + reporting
        accession_map = {}
        for entry in entries:
            title_elem = entry.find("atom:title", ns)
            link_elem = entry.find("atom:link", ns)
            if title_elem is None or link_elem is None:
                continue
            title = title_elem.text or ""
            filing_url = link_elem.attrib.get("href", "")
            # Extract accession number from URL
            acc_match = re.search(r'(\d{18})', filing_url.replace("-", ""))
            if not acc_match:
                continue
            acc_num = acc_match.group(1)
            if acc_num not in accession_map:
                accession_map[acc_num] = []
            accession_map[acc_num].append({
                "title": title,
                "url": filing_url
            })

        new_count = 0
        processed_urls = set()

        for acc_num, entries_list in accession_map.items():
            # Find issuer entry
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

            # Extract company info from issuer title
            # Title: "4 - COMPANY NAME (CIK) (Issuer)"
            issuer_match = re.match(r'4\s*-\s*(.+?)\s*\((\d+)\)\s*\(Issuer\)', issuer["title"])
            company_name = issuer_match.group(1).strip() if issuer_match else issuer["title"]
            cik = issuer_match.group(2) if issuer_match else ""
            ticker = get_ticker_from_cik(cik) if cik else "UNKNOWN"

            # Extract insider name from reporting title
            insider_name = "Unknown Insider"
            if reporting:
                rep_match = re.match(r'4\s*-\s*(.+?)\s*\(\d+\)\s*\(Reporting\)', reporting["title"])
                if rep_match:
                    insider_name = rep_match.group(1).strip()

            # Fetch actual filing content
            filing_text = fetch_filing_text(filing_url)
            if not filing_text:
                filing_text = f"{company_name} insider {insider_name} filed Form 4."

            store_filing(
                filing_type="4",
                company_name=company_name,
                ticker=ticker,
                raw_text=filing_text,
                filing_url=filing_url,
                extra={
                    "insider_name": insider_name,
                    "cik": cik
                }
            )
            new_count += 1
            time.sleep(0.5)

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
        time.sleep(30)