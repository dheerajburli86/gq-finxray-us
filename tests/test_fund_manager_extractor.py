"""
Offline tests for fund_manager_extractor. Fixtures mirror the real layouts seen
in EDGAR filings (iShares table, American Funds experience table, Vanguard /
Fidelity / generic prose, 497 stickers, SGML headers).

Run:  python -m pytest -q tests/
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fund_manager_extractor as fx  # noqa: E402

SGML_MULTI = """
<SEC-HEADER>
<SERIES-AND-CLASSES-CONTRACTS-DATA>
<EXISTING-SERIES-AND-CLASSES-CONTRACTS>
<SERIES>
<OWNER-CIK>0001100663
<SERIES-ID>S000090001
<SERIES-NAME>iShares 1-10 Year Treasury Bond ETF
<CLASS-CONTRACT>
<CLASS-CONTRACT-ID>C000200001
<CLASS-CONTRACT-NAME>iShares 1-10 Year Treasury Bond ETF
<CLASS-CONTRACT-TICKER-SYMBOL>TENY
</CLASS-CONTRACT>
</SERIES>
<SERIES>
<OWNER-CIK>0001100663
<SERIES-ID>S000090002
<SERIES-NAME>iShares Core Muni Plus ETF
<CLASS-CONTRACT>
<CLASS-CONTRACT-ID>C000200002
<CLASS-CONTRACT-NAME>iShares Core Muni Plus ETF
<CLASS-CONTRACT-TICKER-SYMBOL>MUNP
</CLASS-CONTRACT>
</SERIES>
</EXISTING-SERIES-AND-CLASSES-CONTRACTS>
</SERIES-AND-CLASSES-CONTRACTS-DATA>
</SEC-HEADER>
"""

ISHARES_485 = """
<html><body>
<p>iShares 1-10 Year Treasury Bond ETF | TENY</p>
<p>Investment Objective</p><p>The fund seeks to track ...</p>
<p>Management</p>
<p>Investment Adviser. BlackRock Fund Advisors.</p>
<p><b>Portfolio Managers</b></p>
<p>Jonathan Graves, James Mauro and Marcus Tom (the "Portfolio Managers") are primarily
responsible for the day-to-day management of the Fund.</p>
<table>
<tr><th>Portfolio Manager</th><th>Since</th><th>Title</th></tr>
<tr><td>Jonathan Graves</td><td>Inception (2026)</td><td>Managing Director</td></tr>
<tr><td>James Mauro</td><td>Inception (2026)</td><td>Managing Director</td></tr>
<tr><td>Marcus Tom</td><td>Inception (2026)</td><td>Director</td></tr>
</table>
<p>Purchase and Sale of Fund Shares</p><p>...</p>
<p>iShares Core Muni Plus ETF | MUNP</p>
<p>Portfolio Managers</p>
<table>
<tr><td>Portfolio Manager</td><td>Since</td><td>Title</td></tr>
<tr><td>Michael A. Kalinoski, CFA</td><td>2021</td><td>Director</td></tr>
<tr><td>Kristi Manidis</td><td>November 2023</td><td>Managing Director</td></tr>
</table>
<p>Tax Information</p>
<p>Portfolio Manager Compensation</p>
<table><tr><td>Jonathan Graves</td><td>12</td><td>$4.1 Billion</td></tr></table>
</body></html>
"""

AMERICAN_FUNDS = """
<p>Portfolio managers The individuals primarily responsible for the portfolio management of the fund are:</p>
<table>
<tr><td>Portfolio manager/ Series title (if applicable)</td><td>Portfolio manager experience in this fund</td><td>Primary title with investment adviser</td></tr>
<tr><td>Mark L. Casey Partner</td><td>17 years</td><td>Partner – Capital International Investors</td></tr>
<tr><td>Anne-Marie Peterson</td><td>7 years</td><td>Partner – Capital World Investors</td></tr>
</table>
<p>Purchase and sale of fund shares</p>
"""

VANGUARD_PROSE = """
<p>Investment Advisor</p><p>The Vanguard Group, Inc. (Vanguard)</p>
<p>Portfolio Managers</p>
<p>Michelle Louie, CFA, Principal of Vanguard. She has co-managed the Fund since 2017.</p>
<p>Nick Birkett, CFA, Portfolio Manager at Vanguard. He has co-managed the Fund since August 2023.</p>
<p>Purchase and Sale of Fund Shares</p>
"""

FIDELITY_PROSE = """
<p>Portfolio Manager(s)</p>
<p>Joel Tillinghast (lead portfolio manager) has managed the fund since 1989.</p>
<p>Salim Hart (co-manager) has managed the fund since 2021.</p>
<p>Purchase and Sale of Shares</p>
"""

SERVED_PROSE = """
<p>Portfolio Managers.</p>
<p>Sarah O'Neil, Managing Director of the Adviser, has served as portfolio manager of the Fund since 2015.</p>
<p>Price Morgan, Senior Vice President, has served as a portfolio manager of the Fund since 2019.</p>
<p>Tax Information</p>
"""

STICKER_REMOVE = """
<p>VANGUARD WORLD FUND</p>
<p>Vanguard Explorer Fund</p>
<p>Supplement Dated September 8, 2026, to the Prospectus and Summary Prospectus</p>
<p>Effective October 3, 2026, John P. Smith, CFA, will no longer serve as a portfolio manager
of the Fund. All references to Mr. Smith in the Prospectus are hereby deleted.</p>
<p>Effective October 3, 2026, Jane Q. Doe has been added as a portfolio manager of the Fund.</p>
<p>The Fund's investment objective, strategies and policies remain unchanged.</p>
"""

STICKER_RETIRE_MULTI = """
<p>Supplement dated September 21, 2026</p>
<p>Bridge Builder Core Bond Fund</p>
<p>Bridge Builder Municipal Bond Fund</p>
<p>Robert K. Greene has announced his intention to retire from the Adviser. Effective December 31, 2026,
Mr. Greene will no longer serve as a portfolio manager of the Funds.</p>
"""

STICKER_REPLACE_TABLE = """
<p>iShares Core Muni Plus ETF</p>
<p>The table in the section entitled "Portfolio Managers" is deleted in its entirety and replaced with the following:</p>
<table>
<tr><td>Portfolio Manager</td><td>Since</td><td>Title</td></tr>
<tr><td>Kristi Manidis</td><td>2023</td><td>Managing Director</td></tr>
<tr><td>Walter Kiernan</td><td>2026</td><td>Director</td></tr>
</table>
"""

STICKER_SUCCEED = """
<p>Effective immediately, Priya Raman will succeed Mr. Okafor as lead portfolio manager of the Fund.
David Okafor has managed the Fund since 2010.</p>
"""

NOT_A_PM_CHANGE = """
<p>Effective October 1, 2026, Class C shares of the Fund are no longer offered to new investors.</p>
<p>The Fund is no longer subject to the redemption fee.</p>
"""


def _series():
    return fx.parse_series_header(SGML_MULTI)


def test_sgml_header():
    s = _series()
    assert [x["series_id"] for x in s] == ["S000090001", "S000090002"]
    assert s[0]["classes"][0]["ticker"] == "TENY"
    assert s[1]["series_name"] == "iShares Core Muni Plus ETF"


def test_ishares_multi_fund_attribution():
    r = fx.extract_rosters(ISHARES_485, _series(), asof="2026-09-01")
    a = {m["name"]: m for m in r["S000090001"]}
    b = {m["name"]: m for m in r["S000090002"]}
    assert set(a) == {"Jonathan Graves", "James Mauro", "Marcus Tom"}
    assert a["Marcus Tom"]["title"] == "Director"
    assert a["Jonathan Graves"]["is_inception"] and a["Jonathan Graves"]["since_year"] == 2026
    assert set(b) == {"Michael A. Kalinoski", "Kristi Manidis"}
    assert b["Kristi Manidis"]["since_month"] == 11 and b["Kristi Manidis"]["since_year"] == 2023
    assert "_unattributed" not in r
    # compensation table must not leak in as a roster
    assert all("12" not in (m.get("since_raw") or "") for m in r["S000090002"])


def test_american_funds_experience_years():
    one = [{"series_id": "S1", "series_name": "The Growth Fund of America", "classes": []}]
    r = fx.extract_rosters(AMERICAN_FUNDS, one, asof="2026-06-01")["S1"]
    names = {m["name"]: m for m in r}
    assert "Mark L. Casey" in names and "Anne-Marie Peterson" in names
    assert names["Mark L. Casey"]["since_year"] == 2009
    assert names["Mark L. Casey"].get("since_is_approx")


def test_vanguard_prose():
    one = [{"series_id": "S1", "series_name": "Vanguard Explorer Fund", "classes": []}]
    r = {m["name"]: m for m in fx.extract_rosters(VANGUARD_PROSE, one, asof="2026-01-01")["S1"]}
    assert set(r) == {"Michelle Louie", "Nick Birkett"}
    assert r["Michelle Louie"]["since_year"] == 2017
    assert r["Nick Birkett"]["since_month"] == 8
    assert "Principal" in (r["Michelle Louie"]["title"] or "")


def test_fidelity_prose_roles():
    one = [{"series_id": "S1", "series_name": "Fidelity Low-Priced Stock Fund", "classes": []}]
    r = {m["name"]: m for m in fx.extract_rosters(FIDELITY_PROSE, one)["S1"]}
    assert set(r) == {"Joel Tillinghast", "Salim Hart"}
    assert r["Joel Tillinghast"]["role"].startswith("lead")
    assert r["Joel Tillinghast"]["since_year"] == 1989


def test_served_prose_and_surname_collisions():
    one = [{"series_id": "S1", "series_name": "Example Fund", "classes": []}]
    r = {m["name"]: m for m in fx.extract_rosters(SERVED_PROSE, one)["S1"]}
    assert "Sarah O'Neil" in r and r["Sarah O'Neil"]["since_year"] == 2015
    assert "Price Morgan" in r                      # real person, brand-like tokens
    assert not fx.is_person_name("T. Rowe Price")    # firm
    assert not fx.is_person_name("Morgan Stanley")
    assert not fx.is_person_name("Portfolio Managers")


def test_sticker_remove_and_add():
    one = [{"series_id": "S9", "series_name": "Vanguard Explorer Fund", "classes": []}]
    out = fx.extract_change_events(STICKER_REMOVE, one, filing_date="2026-09-08")
    ev = {(e["event_type"], e["manager"]): e for e in out["events"]}
    assert ("REMOVED", "John P. Smith") in ev
    assert ("ADDED", "Jane Q. Doe") in ev
    assert ev[("REMOVED", "John P. Smith")]["effective_date"] == "2026-10-03"
    assert ev[("REMOVED", "John P. Smith")]["series_ids"] == ["S9"]
    assert len([e for e in out["events"] if e["event_type"] == "REMOVED"]) == 1   # Mr. Smith de-duped


def test_sticker_retirement_plural_funds():
    two = [{"series_id": "A", "series_name": "Bridge Builder Core Bond Fund", "classes": []},
           {"series_id": "B", "series_name": "Bridge Builder Municipal Bond Fund", "classes": []}]
    out = fx.extract_change_events(STICKER_RETIRE_MULTI, two, filing_date="2026-09-21")
    rem = [e for e in out["events"] if e["event_type"] == "REMOVED"]
    assert [e["manager"] for e in rem] == ["Robert K. Greene"]
    assert rem[0]["series_ids"] == ["A", "B"]
    assert rem[0]["effective_date"] == "2026-12-31"


def test_sticker_replacement_table():
    one = [{"series_id": "S000090002", "series_name": "iShares Core Muni Plus ETF", "classes": []}]
    out = fx.extract_change_events(STICKER_REPLACE_TABLE, one, filing_date="2026-09-10")
    new = out["replacement_rosters"]["S000090002"]
    assert {m["name"] for m in new} == {"Kristi Manidis", "Walter Kiernan"}
    d = fx.diff_rosters([{"name": "Michael A. Kalinoski, CFA"}, {"name": "Kristi Manidis"}], new)
    assert [m["name"] for m in d["added"]] == ["Walter Kiernan"]
    assert [m["name"] for m in d["removed"]] == ["Michael A. Kalinoski, CFA"]


def test_succession_with_honorific():
    one = [{"series_id": "S1", "series_name": "Example Fund", "classes": []}]
    out = fx.extract_change_events(STICKER_SUCCEED, one, filing_date="2026-09-15")
    ev = {(e["event_type"], e["manager"]) for e in out["events"]}
    assert ("ADDED", "Priya Raman") in ev
    assert ("REMOVED", "David Okafor") in ev


def test_non_pm_no_longer_is_ignored():
    one = [{"series_id": "S1", "series_name": "Example Fund", "classes": []}]
    out = fx.extract_change_events(NOT_A_PM_CHANGE, one, filing_date="2026-09-15")
    assert out["events"] == []


def test_parse_since_variants():
    assert fx.parse_since("Inception (2026)")["since_year"] == 2026
    assert fx.parse_since("since Sept. 2019")["since_month"] == 9
    assert fx.parse_since("Less than 1 year", 2026)["since_year"] == 2026
    assert fx.normalize_name("Michael A. Kalinoski, CFA") == "michael a kalinoski"


if __name__ == "__main__":
    # run_all.sh executes each file as a script, which never calls pytest-style
    # test_ functions on its own — without this block the file "passes" while
    # running nothing.
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
        print(f"  PASS  {_fn.__name__}")
    print(f"{len(_fns)} tests passed")
