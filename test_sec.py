import requests
import xml.etree.ElementTree as ET

headers = {"User-Agent": "GQFinXray/1.0 dheerajburli86@gmail.com"}
r = requests.get(
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom",
    headers=headers,
    timeout=15
)
print("Status:", r.status_code)
root = ET.fromstring(r.content)
ns = {"atom": "http://www.w3.org/2005/Atom"}
entries = root.findall("atom:entry", ns)
print("Total entries:", len(entries))
for e in entries[:5]:
    title = e.find("atom:title", ns)
    updated = e.find("atom:updated", ns)
    t = title.text if title is not None else "no title"
    u = updated.text if updated is not None else "no date"
    print(u, "--", t)
