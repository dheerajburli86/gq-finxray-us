import sys, asyncio
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from unittest.mock import MagicMock
for m in ["supabase","dotenv","scraper_common"]: sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a,**k: None

XML = """<?xml version="1.0"?>
<ownershipDocument>
 <issuer><issuerName>Apple Inc.</issuerName><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
 <reportingOwner><reportingOwnerId><rptOwnerName>COOK TIMOTHY D</rptOwnerName></reportingOwnerId>
   <reportingOwnerRelationship><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
 <nonDerivativeTable><nonDerivativeTransaction>
   <securityTitle><value>Common Stock</value></securityTitle>
   <transactionDate><value>2026-09-01</value></transactionDate>
   <transactionAmounts>
     <transactionShares><value>100000</value></transactionShares>
     <transactionPricePerShare><value>223.45</value></transactionPricePerShare>
     <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
   </transactionAmounts>
   <postTransactionAmounts><sharesOwnedFollowingTransaction><value>3200000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
 </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>"""

import sec_client, edgar_poller_async as ep
async def fake_get_text(url): return "PRE\n"+XML+"\nPOST"
sec_client.get_text = fake_get_text

text, payload = asyncio.run(ep.fetch_form4_text(
    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000077/0000320193-26-000077-index.htm",
    "Apple Inc.", "COOK TIMOTHY D"))

print("--- TEXT ---"); print(text)
print("\n--- PAYLOAD ---")
import json; print(json.dumps(payload, indent=2)[:1400])

assert payload, "payload must not be empty"
assert payload["type"] == "it"
row = payload["types"][0]["table_data"][0]
assert row["price"] == "$223.45", row["price"]
assert row["value"] == "$22.34M", row["value"]
assert row["shares"] == "100,000"
assert row["held_after"] == "3,200,000"
assert payload["general_information"]["insider_title"] == "Chief Executive Officer"
print("\n✅ FORM 4 PAYLOAD TEST PASS")
