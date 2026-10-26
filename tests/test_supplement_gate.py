"""
The supplement gate (fund_manager_extractor.is_supplement) decides whether a
Form 497 document is an ANNOUNCEMENT worth reading or a full prospectus
re-filing worth ignoring. Four features sit behind it:

    15  ETF manager changes        fund_manager_poller
    16  ETF fund actions           etf_fund_events_poller
    19  mutual fund manager changes mf_manager_poller
    21  mutual fund actions        mf_events_poller

Measured 2026-10-26: 39 of 63 manager-change candidates in one week were
rejected here, and those four features had produced no alerts at all. The
rejected shapes below are all real supplement openings.

Run: python tests/test_supplement_gate.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x",
             "TELEGRAM_TOKEN": "1:x", "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x",
             "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


import fund_manager_extractor as fx   # noqa: E402

PROSPECTUS = "\n\nAcme Growth Fund Prospectus dated March 1, 2026. " + ("filler text. " * 5000)

print("1. Openings that were already accepted still are")
for head in ("Supplement dated October 1, 2026 to the Prospectus",
             "PROSPECTUS SUPPLEMENT",
             "Supplement No. 2 to the Summary Prospectus",
             "ACME FUNDS TRUST  Supplement to the Summary Prospectus and Prospectus",
             "STATEMENT OF ADDITIONAL INFORMATION SUPPLEMENT"):
    check(f"accepted: {head[:46]!r}", fx.is_supplement(head + PROSPECTUS, "497"))

print("2. Announcement openings that carry no 'supplement' wording")
# A liquidation or merger usually reaches holders as a notice, not a sticker.
for head in ("IMPORTANT NOTICE TO SHAREHOLDERS OF THE ACME GROWTH FUND",
             "Notice to Shareholders",
             "NOTICE TO THE UNITHOLDERS",
             "Amendment No. 1 to the Prospectus dated May 1, 2026",
             "AMENDMENT TO THE STATEMENT OF ADDITIONAL INFORMATION"):
    check(f"accepted: {head[:46]!r}", fx.is_supplement(head + PROSPECTUS, "497"))

print("3. A short 497 is a sticker whatever its first words are")
# The word "supplement" is often only inside a header image, or sits past the
# cover page of a trust that lists fifty funds before it says anything.
short = ("ACME FUNDS TRUST\n" +
         "\n".join(f"Acme Capital Appreciation Series Fund Number {i}, Class A, Class C, Class R"
                   for i in range(80)) +
         "\nThe Board of Trustees has approved a plan of liquidation for the "
         "Acme Growth Fund. The Fund will be liquidated on December 15, 2026.")
check("short 497 with the header past 4,000 chars is accepted",
      len(short) > fx.SUPPLEMENT_HEAD_CHARS and fx.is_supplement(short, "497"),
      len(short))
check("one-paragraph 497 is accepted",
      fx.is_supplement("The Fund will be liquidated on December 15, 2026.", "497"))

print("4. Full prospectus re-filings are still rejected")
check("a long 497 with no supplement wording is rejected",
      not fx.is_supplement(PROSPECTUS, "497"), len(PROSPECTUS))
check("length alone never passes a 497K summary prospectus",
      not fx.is_supplement("Acme Growth Fund Summary Prospectus " + "y" * 9000, "497K"))
check("a 497K that says it is a supplement still passes",
      fx.is_supplement("Supplement to the Summary Prospectus " + "y" * 9000, "497K"))
check("empty document is not a supplement", not fx.is_supplement("", "497"))
check("the cut-off is a real bound", fx.SUPPLEMENT_MAX_CHARS >= 5000)
# A short filed excerpt that calls itself a prospectus is still a prospectus:
# length alone must not promote it. (tests/test_etf_alerts.py R2 covers the
# live path -- a prospectus that merely mentions "plan of liquidation".)
check("a SHORT document headed 'PROSPECTUS' is rejected",
      not fx.is_supplement("PROSPECTUS. iShares MSCI China ETF. "
                           "A fund may adopt a plan of liquidation if its assets fall.", "497"))
check("a short 'Statement of Additional Information' is rejected",
      not fx.is_supplement("Statement of Additional Information. The Fund may be liquidated.", "497"))
check("but 'Supplement to the Prospectus' still passes",
      fx.is_supplement("Supplement to the Prospectus dated March 1, 2026. "
                       "The Fund will be liquidated.", "497"))

print("5. N-14 is always in scope: the form itself is the event")
check("N-14", fx.is_supplement(PROSPECTUS, "N-14"))
check("N-14 8C", fx.is_supplement(PROSPECTUS, "N-14 8C"))

print("6. Every caller still routes through this one gate")
import inspect   # noqa: E402
import mf_events_poller as MEV        # noqa: E402
import mf_manager_poller as MMG       # noqa: E402
import etf_fund_events_poller as EFE  # noqa: E402
import fund_manager_poller as FMP     # noqa: E402
for mod in (MEV, MMG, EFE, FMP):
    check(f"{mod.__name__} calls is_supplement",
          "is_supplement" in inspect.getsource(mod))

print("7. A mutual fund cash line is not a portfolio change")
# N-PORT puts the instrument in `title` for many funds; the ETF helper reads
# only `security_name`, so the sweep vehicle looked like a position.
import mf_poller as MP   # noqa: E402
for row, want in (
        ({"security_name": "NVIDIA Corp", "title": "NVIDIA Corp", "security_key": "US67066G1040"}, True),
        ({"security_name": "Microsoft Corp", "title": None, "security_key": "US5949181045"}, True),
        ({"security_name": "Goldman Sachs", "title": "Goldman Sachs FS Treasury Obligations Fund",
          "security_key": "X1"}, False),
        ({"security_name": "", "title": "U.S. Treasury Bill 0% 11/13/2026", "security_key": "X2"}, False)):
    label = row["title"] or row["security_name"]
    check(f"{'position' if want else 'cash'}: {label[:40]!r}", MP._alertable(row) is want)

print("8. One failed phrase search does not abort the whole sweep")
# 2026-10-26: a single connection reset killed run_mf_fund_events_poller and
# run_mf_manager_events in the same second, 4 times in 3 days each, discarding
# every candidate already collected by ~60 phrase searches.
import mf_common as mc            # noqa: E402
import etf_fund_events_poller as EFE2  # noqa: E402

real_hits, real_log = mc.efts_hits, mc.log_poller_error
calls = []


def _flaky(query, forms, start, end, max_pages=10):
    calls.append(query)
    if "boom" in query:
        raise RuntimeError("ConnectionTerminated error_code:1")
    yield {"_source": {"ciks": ["0000000001"], "form": "497", "file_date": "2026-10-26",
                       "display_names": ["Acme Trust"]},
           "_id": f"0000000001-26-00000{len(calls)}:d.htm"}


mc.efts_hits, mc.log_poller_error = _flaky, (lambda *a, **k: None)
try:
    failed = []
    got = [list(mc.efts_hits_safe(q, "497", "a", "b", failed=failed))
           for q in ('"good one"', '"boom"', '"good two"')]
finally:
    mc.efts_hits, mc.log_poller_error = real_hits, real_log
check("the failing phrase yields nothing", got[1] == [], got[1])
check("phrases after it still run", len(got[0]) == 1 and len(got[2]) == 1, got)
check("the failure is reported, not swallowed", failed == ['"boom"'], failed)

check("Feature 21 collect_candidates returns (candidates, failed)",
      len(inspect.signature(MEV.collect_candidates).parameters) == 3
      and "return cands, failed" in inspect.getsource(MEV.collect_candidates))
check("Feature 16 collect_candidates returns (candidates, failed)",
      "return cands, failed" in inspect.getsource(EFE2.collect_candidates))
for mod in (MEV, MMG, EFE2, FMP):
    check(f"{mod.__name__} uses the safe sweep", "efts_hits_safe" in inspect.getsource(mod))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
