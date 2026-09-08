import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from unittest.mock import MagicMock
for m in ["supabase","dotenv","fmp_client"]: sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a,**k: None
import earnings_transcript_poller as tp

captured={}
class T:
    def insert(self, row): captured.update(row); return self
    def execute(self): return MagicMock(data=[{}])
tp.supabase.table = lambda n: T()

ok = tp.store_transcript_for_pipeline("MU","Micron Technology",2026,2,
        {"content":"Operator: Good afternoon. "*60, "date":"2026-06-25"})
assert ok
pl = captured["extra"]["structured_payload"]
print("type          :", pl["type"])
print("name          :", pl["name"])
print("period        :", pl["period"])
print("content chars :", len(pl["content"]))
print("source_link   :", repr(pl["source_link"]), "<- must be empty (no API key leak)")
assert pl["type"]=="earning_calls"
assert "apikey" not in str(pl).lower(), "API KEY LEAK IN PAYLOAD"
assert pl["period"]=="Q2 FY2026"
print("\n✅ TRANSCRIPT PAYLOAD TEST PASS (no credential in payload)")
