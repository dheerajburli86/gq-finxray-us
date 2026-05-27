import requests
import xml.etree.ElementTree as ET
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import os
import time
import schedule
import re

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

SEC_HEADERS = {
    "User-Agent": "GQFinXray/1.0 contact@gqfinxray.com",
    "Accept-Encoding": "gzip, deflate"
}

CIK_MAP = {}
CIK_REVERSE_MAP = {}

def load_cik_map():
    global CIK_MAP, CIK_REVERSE_MAP
    print("[SETUP] Loading SEC CIK-to-ticker mapping...")
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS,
            timeout=15
        )
        data = r.json()
        for entry in data.values():
            ticker = entry.get("ticker", "").upper()
            cik = str(entry.get("cik_str", "")).zfill(10)
            name = entry.get("title", "")
            if ticker:
                CIK_MAP[ticker] = {"cik": cik, "name": name}
                CIK_REVERSE_MAP[cik] = {"ticker": ticker, "name": name}
        print(f"[SETUP] Loaded {len(CIK_MAP):,} ticker mappings")
    except Exception as e:
        print(f"[ERROR] Failed to load CIK map: {e}")

def get_ticker_from_cik(cik):
    cik_padded = str(cik).zfill(10)
    if cik_padded in CIK_REVERSE_MAP:
        info = CIK_REVERSE_MAP[cik_padded]
        return info["ticker"], info["name"]
    return None, None

def sec_live_lookup(cik):
    try:
        cik_padded = str(cik).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        r = requests.get(url, headers=SEC_HEADERS, timeout=10)
        if r.status_code != 200:
            return None, None, None, None, None
        data = r.json()
        ticker_list = data.get("tickers", [])
        ticker = ticker_list[0].upper() if ticker_list else None
        company_name = data.get("name", "")
        sic = data.get("sic", "")
        sic_description = data.get("sicDescription", "")
        state = data.get("stateOfIncorporation", "")
        return ticker, company_name, sic, sic_description, state
    except:
        return None, None, None, None, None

def filing_exists(filing_url):
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("filing_url", filing_url) \
            .execute()
        return len(result.data) > 0
    except Exception as e:
        print(f"[ERROR] DB check failed: {e}")
        return False

def store_filing(ticker, company_name, cik, filing_type,
                 filing_url, filed_at, raw_text, extra=None):
    try:
        supabase.table("raw_filings").insert({
            "source": "SEC_EDGAR",
            "filing_type": filing_type,
            "ticker": ticker if ticker else "UNKNOWN",
            "company_name": company_name if company_name else "UNKNOWN",
            "cik": cik,
            "raw_text": raw_text,
            "filing_url": filing_url,
            "filed_at": filed_at,
            "status": "PENDING",
            "extra": extra or {}
        }).execute()

        ticker_display = ticker if ticker else "UNKNOWN"
        extra_display = ""
        if extra:
            if extra.get("insider_name"):
                extra_display += f" | Insider: {extra['insider_name']}"
            if extra.get("sic_description"):
                extra_display += f" | {extra['sic_description']}"
            if extra.get("item_types"):
                extra_display += f" | {', '.join(extra['item_types'])}"

        print(f"[STORED] {filing_type} — {company_name} ({ticker_display}){extra_display} → PENDING")

    except Exception as e:
        print(f"[ERROR] Failed to store filing: {e}")

def fetch_filing_text(filing_url):
    try:
        r = requests.get(filing_url, headers=SEC_HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text[:50000]
        return None
    except:
        return None

def extract_8k_items(summary_text):
    if not summary_text:
        return []
    items = re.findall(r'Item\s+\d+\.\d+[^<\n]*', summary_text)
    return [item.strip() for item in items]

def extract_cik_from_url(filing_url):
    try:
        parts = filing_url.split("/edgar/data/")
        if len(parts) > 1:
            return parts[1].split("/")[0].zfill(10)
    except:
        pass
    return ""

def extract_accession(entry_id):
    # id format: urn:tag:sec.gov,2008:accession-number=0001193125-26-240391
    try:
        return entry_id.split("accession-number=")[-1].strip()
    except:
        return ""

def poll_sec_8k():
    filing_type = "8-K"
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling SEC EDGAR for {filing_type}...")
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=8-K"
        "&dateb=&owner=include&count=20&search_text=&output=atom"
    )
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] SEC returned {r.status_code}")
            return

        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)

        new_count = 0
        for entry in entries:
            link_elem = entry.find("atom:link", ns)
            filing_url = link_elem.attrib.get("href", "") if link_elem is not None else ""
            if not filing_url or filing_exists(filing_url):
                continue

            cik = extract_cik_from_url(filing_url)
            ticker, company_name = get_ticker_from_cik(cik)
            sic = sic_description = state = None

            if not ticker:
                ticker, company_name, sic, sic_description, state = sec_live_lookup(cik)
                time.sleep(0.1)

            title_elem = entry.find("atom:title", ns)
            title = title_elem.text if title_elem is not None else ""
            if not company_name:
                try:
                    company_name = title.split(" - ", 1)[1].rsplit("(", 1)[0].strip()
                except:
                    company_name = title

            if not ticker:
                ticker = "UNKNOWN"

            updated_elem = entry.find("atom:updated", ns)
            filed_at = updated_elem.text if updated_elem is not None else datetime.now().isoformat()

            summary_elem = entry.find("atom:summary", ns)
            summary_text = summary_elem.text if summary_elem is not None else ""
            item_types = extract_8k_items(summary_text)

            raw_text = summary_text
            full_text = fetch_filing_text(filing_url)
            if full_text:
                raw_text = full_text

            store_filing(
                ticker=ticker,
                company_name=company_name,
                cik=cik,
                filing_type=filing_type,
                filing_url=filing_url,
                filed_at=filed_at,
                raw_text=raw_text,
                extra={
                    "sic": sic,
                    "sic_description": sic_description,
                    "state": state,
                    "item_types": item_types
                }
            )
            new_count += 1
            time.sleep(0.5)

        if new_count == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] No new 8-K filings.")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Stored {new_count} new 8-K filings.")

    except Exception as e:
        print(f"[ERROR] 8-K polling failed: {e}")

def poll_sec_form4():
    filing_type = "4"
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling SEC EDGAR for Form 4...")
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=4"
        "&dateb=&owner=include&count=40&search_text=&output=atom"
    )
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] SEC returned {r.status_code}")
            return

        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)

        # ── Key insight: RSS publishes two entries per Form 4 ──────────
        # One for the Reporting person, one for the Issuer company.
        # Same accession number links them together.
        # We only want Issuer entries — they have the company CIK.

        # Step 1: collect all issuer entries by accession number
        issuers = {}
        reporters = {}

        for entry in entries:
            title_elem = entry.find("atom:title", ns)
            title = title_elem.text if title_elem is not None else ""
            id_elem = entry.find("atom:id", ns)
            accession = extract_accession(id_elem.text if id_elem is not None else "")
            link_elem = entry.find("atom:link", ns)
            filing_url = link_elem.attrib.get("href", "") if link_elem is not None else ""
            updated_elem = entry.find("atom:updated", ns)
            filed_at = updated_elem.text if updated_elem is not None else datetime.now().isoformat()

            if "(Issuer)" in title:
                issuers[accession] = {
                    "title": title,
                    "filing_url": filing_url,
                    "filed_at": filed_at,
                    "cik": extract_cik_from_url(filing_url)
                }
            elif "(Reporting)" in title:
                # Extract insider name from title
                # Format: "4 - Kitamura Takumi (0002120494) (Reporting)"
                try:
                    insider_name = title.split(" - ", 1)[1].rsplit("(", 2)[0].strip()
                except:
                    insider_name = ""
                reporters[accession] = {"insider_name": insider_name}

        # Step 2: process issuer entries, enrich with reporter name
        new_count = 0
        for accession, issuer in issuers.items():
            filing_url = issuer["filing_url"]
            if filing_exists(filing_url):
                continue

            cik = issuer["cik"]
            ticker, company_name = get_ticker_from_cik(cik)

            if not ticker:
                ticker, company_name, _, _, _ = sec_live_lookup(cik)
                time.sleep(0.1)

            if not company_name:
                try:
                    company_name = issuer["title"].split(" - ", 1)[1].rsplit("(", 1)[0].strip()
                except:
                    company_name = issuer["title"]

            if not ticker:
                ticker = "UNKNOWN"

            insider_name = reporters.get(accession, {}).get("insider_name", "")

            raw_text = fetch_filing_text(filing_url) or ""

            store_filing(
                ticker=ticker,
                company_name=company_name,
                cik=cik,
                filing_type=filing_type,
                filing_url=filing_url,
                filed_at=issuer["filed_at"],
                raw_text=raw_text,
                extra={"insider_name": insider_name}
            )
            new_count += 1
            time.sleep(0.5)

        if new_count == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] No new Form 4 filings.")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Stored {new_count} new Form 4 filings.")

    except Exception as e:
        print(f"[ERROR] Form 4 polling failed: {e}")

def run_poller():
    load_cik_map()
    poll_sec_8k()
    poll_sec_form4()
    schedule.every(30).seconds.do(poll_sec_8k)
    schedule.every(30).seconds.do(poll_sec_form4)
    print("\n[RUNNING] Poller started — checking every 30 seconds. Press Ctrl+C to stop.\n")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    run_poller()