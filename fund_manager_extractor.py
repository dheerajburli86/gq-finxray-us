"""
fund_manager_extractor.py
GQ FinXray US — Fund Manager feature, pure parsing layer (no network, no DB).

Everything here takes raw SEC documents (HTML prospectus / supplement, SGML
filing header) and returns plain dicts, so it can be unit-tested offline and
reused by any poller.

What it extracts
----------------
1. Rosters  — named portfolio managers per fund, from Form N-1A Item 5(b)
   ("Portfolio Managers" in the summary prospectus). Found in 485BPOS, 497K,
   497. Handles the two shapes SEC filings actually use:
     * table:  Portfolio Manager | Since | Title          (iShares, Schwab, ...)
               Portfolio manager | experience in this fund | title (American Funds)
     * prose:  "X, Y and Z (the "Portfolio Managers") are primarily responsible..."
               "Jane Doe, CFA, Principal of Vanguard. She has managed the Fund since 2019."
               "John Roe (lead portfolio manager) has managed the fund since 2018."  (Fidelity)
               "... has served as portfolio manager of the Fund since 2015."
2. Change events — from Form 497 / 497K "portfolio manager change stickers":
     * REMOVED: "no longer serves as a portfolio manager", retire/leave/step down,
                "All references to X ... are hereby deleted"
     * ADDED:   "has been added as a portfolio manager", "will join ... as portfolio manager"
     * ROSTER_REPLACED: "... table ... is deleted and replaced with the following"
       -> returns the new roster; the poller diffs it against the stored roster.
3. Series/class/ticker map from the EDGAR SGML header (<SERIES-AND-CLASSES-
   CONTRACTS-DATA>), which is how a multi-fund filing is attributed to the
   exact funds and tickers it covers.

Why regex + structure and not an LLM: Item 5(b) is prescriptive (SEC requires
name, title and length of service), so the text is regular enough to parse
deterministically. That keeps this feature at zero token cost, unlike the
~16-18k tokens per alert the summarisation pipeline spends.

Stdlib only (html.parser) — no new dependencies.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date, datetime
from html.parser import HTMLParser

# ─────────────────────────────────────────────────────────────────────────────
# 1. HTML -> text lines, tables preserved as "cell | cell | cell"
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "tr", "section", "article", "header", "footer", "blockquote",
    "center", "hr", "dl", "dt", "dd", "pre", "title",
}
_SKIP_TAGS = {"script", "style", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._cells: list[str] | None = None   # current table row cells
        self._cell_buf: list[str] | None = None

    # -- helpers --
    def _flush(self):
        text = _clean_ws("".join(self._buf))
        if text:
            self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "tr":
            self._flush()
            self._cells = []
        elif tag in ("td", "th"):
            if self._cells is None:          # cell outside a <tr> (broken HTML)
                self._cells = []
            self._cell_buf = []
        elif tag == "br" and self._cell_buf is not None:
            self._cell_buf.append(" ")
        elif tag in _BLOCK_TAGS and self._cell_buf is None:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("td", "th") and self._cell_buf is not None:
            self._cells.append(_clean_ws("".join(self._cell_buf)))
            self._cell_buf = None
        elif tag == "tr" and self._cells is not None:
            cells = [c for c in self._cells if c]
            if cells:
                self.lines.append(" | ".join(cells) if len(cells) > 1 else cells[0])
            self._cells = None
        elif tag in _BLOCK_TAGS and self._cell_buf is None:
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell_buf is not None:
            self._cell_buf.append(data)
        else:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _clean_ws(s: str) -> str:
    s = s.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


def html_to_lines(raw: str) -> list[str]:
    """Convert an EDGAR HTML (or plain-text) document into clean text lines."""
    if not raw:
        return []
    if "<" not in raw[:5000] and ">" not in raw[:5000]:
        return [l for l in (_clean_ws(x) for x in raw.splitlines()) if l]
    p = _TextExtractor()
    try:
        p.feed(raw)
        p.close()
    except Exception:
        # malformed HTML: fall back to tag stripping
        txt = re.sub(r"<[^>]+>", "\n", raw)
        return [l for l in (_clean_ws(html_lib.unescape(x)) for x in txt.splitlines()) if l]
    return p.lines


def lines_to_text(lines: list[str]) -> str:
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Person-name recognition
# ─────────────────────────────────────────────────────────────────────────────

_TOK = r"(?:Mc|Mac|O['’]|D['’])?[A-Z][a-zA-Z'’\-]*[a-z][a-zA-Z'’\-]*"
_INIT = r"[A-Z]\.(?:[A-Z]\.)?"
_PART = r"(?:de|del|della|der|den|van|von|da|di|du|la|le|st\.)"
_SUFFIX = r"(?:,?[ \t]+(?:Jr\.?|Sr\.?|II|III|IV))"
_CRED = (r"(?:CFA|CPA|CAIA|FRM|FSA|CFP|CMT|CIPM|ChFC|CLU|PRM|ASA|EA|"
         r"Ph\.\s?D\.?|PhD|M\.?B\.?A\.?|J\.D\.|Esq\.?|MD|M\.D\.)")
PERSON = rf"(?:{_TOK}|{_INIT})(?:[ \t]+(?:{_TOK}|{_INIT}|{_PART})){{1,4}}{_SUFFIX}?"
PERSON_RE = re.compile(PERSON)
_CRED_TAIL = rf"(?:,[ \t]*{_CRED})*"

# Words that make a capitalised phrase a firm / role / fund, not a person.
_STOP = {
    "fund", "funds", "trust", "portfolio", "portfolios", "manager", "managers",
    "management", "investment", "investments", "adviser", "advisers", "advisor",
    "advisors", "advisory", "capital", "asset", "assets", "partners", "partner",
    "group", "llc", "l.l.c.", "inc", "inc.", "company", "corporation", "corp",
    "director", "managing", "vice", "president", "senior", "chief", "officer",
    "principal", "head", "team", "shares", "share", "class", "effective",
    "series", "etf", "etfs", "index", "global", "international", "strategy",
    "strategies", "income", "growth", "equity", "equities", "bond", "bonds",
    "treasury", "research", "americas", "america", "securities", "services",
    "holdings", "bank", "financial", "prospectus", "statement", "additional",
    "information", "summary", "section", "supplement", "dated", "all",
    "references", "the", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
    "limited", "ltd", "plc", "sa", "ag", "gmbh", "lp", "l.p.", "investors",
    "associates", "markets", "market", "value", "small", "large", "mid",
    "cap", "total", "return", "municipal", "government", "money", "reserve",
    "reserves", "select", "core", "plus", "yield", "high", "short", "long",
    "term", "duration", "dividend", "emerging", "world", "u.s.", "us",
    "north", "south", "east", "west", "sub-adviser", "subadviser", "co",
    "co.", "mr.", "ms.", "mrs.", "dr.", "since", "title", "inception",
    "biographical", "experience", "primary", "committee", "board", "trustees",
    "trustee", "officers", "sec", "act", "rule", "item", "page", "table",
    "contents", "compensation", "ownership", "other", "accounts", "conflicts",
    "interest", "day-to-day", "responsible", "jointly", "primarily", "each",
    "following", "and", "of", "for", "in", "on", "at", "to",
    # brand words that are never surnames
    "vanguard", "fidelity", "blackrock", "ishares", "schwab", "invesco",
    "nuveen", "pimco", "spdr", "jpmorgan", "allspring", "mfs", "guggenheim",
    "proshares", "direxion", "wisdomtree", "vaneck", "dimensional",
    "threadneedle", "columbia", "americas",
}
# Firm names that look exactly like "Firstname Surname". Matched as whole phrases
# so real managers called Price, Morgan, Wells, Henderson, Cox... still pass.
_FIRM_PHRASES = {
    "t rowe price", "rowe price", "neuberger berman", "baillie gifford",
    "wells fargo", "morgan stanley", "lord abbett", "eaton vance",
    "janus henderson", "franklin templeton", "goldman sachs", "dodge cox",
    "state street", "charles schwab", "first trust", "van eck",
    "capital group", "american century", "thornburg", "cohen steers",
    "harris associates", "davis selected", "royce", "calamos advisors",
    "federated hermes", "virtus", "artisan partners", "brown advisory",
    "william blair", "hotchkis wiley", "jennison", "loomis sayles",
    "grantham mayo", "putnam", "john hancock", "ameriprise", "principal",
}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_MONTH_RE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sept?\.?|Oct\.?|Nov\.?|Dec\.?)"
_DATE_RE = rf"{_MONTH_RE}\s+\d{{1,2}},\s+\d{{4}}"
_YEAR_RE = r"(?:19[5-9]\d|20[0-4]\d)"


def _strip_creds(name: str) -> str:
    name = re.sub(rf",?\s*{_CRED}\b\.?", "", name)
    return _clean_ws(name.strip(" ,;:"))


def is_person_name(name: str) -> bool:
    name = _strip_creds(name)
    toks = [t for t in re.split(r"\s+", name) if t]
    if len(toks) < 2 or len(toks) > 6:
        return False
    words = [t.lower().strip(",.") for t in toks if not re.fullmatch(_INIT, t)]
    if len(words) < 1:
        return False
    if any(w in _STOP for w in words):
        return False
    last = toks[-1].strip(",")
    if re.fullmatch(_INIT, last) or last.lower() in ("de", "van", "von", "da", "di", "le", "la"):
        if not re.fullmatch(r"(?:Jr\.?|Sr\.?|II|III|IV)", last):
            return False
    if name.isupper():                   # "PORTFOLIO MANAGERS" style headings
        return False
    norm = normalize_name(name)
    if norm in _FIRM_PHRASES or any(norm == p or norm.endswith(" " + p) for p in _FIRM_PHRASES if " " in p):
        return False
    return True


def normalize_name(name: str) -> str:
    """Stable key for a manager: lowercase, no credentials/punctuation/initials-dots."""
    n = _strip_creds(name)
    n = re.sub(r"(?:,?\s+(?:Jr\.?|Sr\.?|II|III|IV))$", "", n)
    n = n.lower().replace("’", "'")
    n = re.sub(r"[^a-z' \-]", " ", n)
    return _clean_ws(n)


def _best_person(cand: str) -> str | None:
    """Longest valid person name inside an over-greedy capitalised run."""
    cand = _strip_creds(cand)
    if is_person_name(cand):
        return cand
    toks = cand.split()
    for i in range(0, len(toks) - 1):            # drop leading junk ("Effective John Smith")
        for j in range(len(toks), i + 1, -1):    # drop trailing junk ("Casey Partner")
            sub = " ".join(toks[i:j])
            if is_person_name(sub):
                return sub
    return None


def find_people(segment: str) -> list[str]:
    """All person names in a text segment, in order, de-duplicated."""
    out, seen = [], set()
    for m in PERSON_RE.finditer(segment):
        cand = _best_person(m.group(0))
        if cand:
            k = normalize_name(cand)
            if k and k not in seen:
                seen.add(k)
                out.append(cand)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tenure ("since") normalisation
# ─────────────────────────────────────────────────────────────────────────────

def parse_since(text: str, asof_year: int | None = None) -> dict:
    """
    'Inception (2026)' -> {since_year: 2026, since_month: None, is_inception: True}
    'since November 2021' -> {since_year: 2021, since_month: 11}
    '17 years' (American Funds 'experience in this fund') -> since_year = asof - 17
    """
    t = (text or "").strip()
    res = {"since_raw": t[:120] or None, "since_year": None, "since_month": None,
           "is_inception": False}
    if not t:
        return res
    low = t.lower()
    if "inception" in low:
        res["is_inception"] = True
    m = re.search(rf"({_MONTH_RE})\s+(?:\d{{1,2}},\s+)?({_YEAR_RE})", t)
    if m:
        mon = m.group(1).lower().rstrip(".")
        for full, num in _MONTHS.items():
            if full.startswith(mon[:3]):
                res["since_month"] = num
                break
        res["since_year"] = int(m.group(2))
        return res
    m = re.search(_YEAR_RE, t)
    if m:
        res["since_year"] = int(m.group(0))
        return res
    if re.search(r"less than (?:one|1) year", low) and asof_year:
        res["since_year"] = asof_year
        res["since_is_approx"] = True
        return res
    m = re.search(r"(\d{1,2})\s+years?", low)
    if m and asof_year:
        res["since_year"] = asof_year - int(m.group(1))
        res["since_is_approx"] = True
    return res


def parse_date(text: str) -> str | None:
    m = re.search(rf"({_MONTH_RE})\s+(\d{{1,2}}),\s+(\d{{4}})", text or "")
    if not m:
        return None
    mon = m.group(1).lower().rstrip(".")[:3]
    month = next((n for f, n in _MONTHS.items() if f.startswith(mon)), None)
    try:
        return date(int(m.group(3)), month, int(m.group(2))).isoformat()
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. SGML filing header -> series / classes / tickers
# ─────────────────────────────────────────────────────────────────────────────

def parse_series_header(sgml: str) -> list[dict]:
    """
    Parse <SERIES> blocks from an EDGAR .hdr.sgml / full-submission header.
    Returns [{series_id, series_name, owner_cik, classes:[{class_id, class_name, ticker}]}]
    Covers EXISTING-, NEW- and MERGER-SERIES blocks.
    """
    out = []
    if not sgml:
        return out
    for block in re.findall(r"<SERIES>(.*?)</SERIES>", sgml, flags=re.S | re.I):
        sid = _sgml_field(block, "SERIES-ID")
        if not sid:
            continue
        series = {
            "series_id": sid,
            "series_name": _sgml_field(block, "SERIES-NAME"),
            "owner_cik": _sgml_field(block, "OWNER-CIK"),
            "classes": [],
        }
        for cblock in re.findall(r"<CLASS-CONTRACT>(.*?)</CLASS-CONTRACT>", block, flags=re.S | re.I):
            series["classes"].append({
                "class_id": _sgml_field(cblock, "CLASS-CONTRACT-ID"),
                "class_name": _sgml_field(cblock, "CLASS-CONTRACT-NAME"),
                "ticker": (_sgml_field(cblock, "CLASS-CONTRACT-TICKER-SYMBOL") or "").upper() or None,
            })
        out.append(series)
    # de-dupe (a series can appear in multiple owner blocks)
    seen, uniq = set(), []
    for s in out:
        if s["series_id"] not in seen:
            seen.add(s["series_id"])
            uniq.append(s)
    return uniq


def _sgml_field(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*([^\n<]+)", block, flags=re.I)
    return m.group(1).strip() if m else None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Attribution: which fund (series) does a text position belong to?
# ─────────────────────────────────────────────────────────────────────────────

# How a 497(e) sticker opens: "Supplement dated October 1, 2026 to the
# Prospectus…", "SUPPLEMENT TO THE SUMMARY PROSPECTUS…", "Supplement No. 3
# dated…". The bare word is not enough — full and summary prospectuses say
# "as supplemented" on their cover pages and carry manager bios ("joined the
# Fund as co-portfolio manager in 2021") that read like change events.
_SUPPLEMENT_HEAD = re.compile(
    r"\bsupplement\s+(?:no\.?\s*\d+\s+)?(?:dated|to)\b"
    r"|\b(?:prospectus|sai|statement\s+of\s+additional\s+information)\s+supplement\b")
SUPPLEMENT_HEAD_CHARS = 4000


def is_supplement(text: str, form: str | None = None) -> bool:
    """True if the document opens as a prospectus supplement. N-14 (merger
    registration) is always in scope — the form itself is the event."""
    if (form or "").upper().startswith("N-14"):
        return True
    head = re.sub(r"\s+", " ", _norm_for_search((text or "")[:SUPPLEMENT_HEAD_CHARS]))
    return bool(_SUPPLEMENT_HEAD.search(head))


def _norm_for_search(s: str) -> str:
    """Length-preserving normalisation so offsets stay aligned with the source."""
    table = str.maketrans({"®": " ", "™": " ", "℠": " ", "’": "'", "‘": "'",
                           "“": '"', "”": '"', "–": "-", "—": "-"})
    return s.translate(table).lower()


def _name_variants(series_name: str) -> list[str]:
    n = _norm_for_search(series_name or "").strip()
    n = re.sub(r"\s+", " ", n)
    variants = {n}
    # "Vanguard 500 Index Fund" also appears as "500 Index Fund"
    for prefix in ("vanguard ", "fidelity ", "ishares ", "schwab ", "invesco ",
                   "american funds ", "t. rowe price ", "spdr ", "jpmorgan "):
        if n.startswith(prefix) and len(n) - len(prefix) > 12:
            variants.add(n[len(prefix):])
    return [v for v in variants if len(v) >= 6]


class SeriesLocator:
    """Finds every mention of each series name in a document."""

    def __init__(self, text: str, series: list[dict]):
        self.series = series
        self.norm = _norm_for_search(text)
        self.hits: list[tuple[int, str]] = []    # (offset, series_id)
        for s in series:
            for v in _name_variants(s.get("series_name") or ""):
                start = 0
                while True:
                    i = self.norm.find(v, start)
                    if i < 0:
                        break
                    self.hits.append((i, s["series_id"]))
                    start = i + len(v)
        self.hits.sort()
        self.mentioned = {sid for _, sid in self.hits}

    def nearest_before(self, pos: int) -> str | None:
        best = None
        for off, sid in self.hits:
            if off <= pos:
                best = sid
            else:
                break
        return best

    def in_span(self, a: int, b: int) -> set[str]:
        return {sid for off, sid in self.hits if a <= off < b}

    def all_ids(self) -> list[str]:
        return [s["series_id"] for s in self.series]


# ─────────────────────────────────────────────────────────────────────────────
# 6. Roster extraction (Item 5(b) "Portfolio Managers")
# ─────────────────────────────────────────────────────────────────────────────

_SECTION_START = re.compile(
    r"^\s*(?:the\s+)?(?:portfolio\s+managers?|portfolio\s+management(?:\s+team)?|"
    r"fund\s+managers?|management\s+of\s+the\s+fund|investment\s+adviser\s+and\s+portfolio\s+managers?|"
    r"portfolio\s+manager\(s\))\b",
    re.I,
)
_SECTION_EXCLUDE = re.compile(
    r"compensation|other\s+accounts|ownership\s+of|conflicts?\s+of\s+interest|"
    r"potential\s+conflicts|securities\s+owned|material\s+conflicts",
    re.I,
)
_SECTION_END = re.compile(
    r"^\s*(?:purchase\s+and\s+sale|tax\s+information|payments\s+to\s+broker|"
    r"buying\s+and\s+selling|financial\s+intermediary\s+compensation|"
    r"summary\s+of\s+other\s+important|additional\s+information\s+about|"
    r"how\s+to\s+(?:buy|purchase)|investment\s+objectives?|fees\s+and\s+expenses|"
    r"principal\s+(?:investment\s+)?(?:strategies|risks)|performance\b)",
    re.I,
)
_HEADER_ROW = re.compile(r"(?:since|title|experience|tenure|position)", re.I)
_SINCE_CELL = re.compile(rf"(?:inception|{_YEAR_RE}|\d{{1,2}}\s+years?|less than (?:one|1) year|since)", re.I)

# prose patterns
_P_RESPONSIBLE = re.compile(
    r"(?P<names>[^\n]{3,500}?)\s*(?:\((?:collectively,?\s*|each,?\s*)?(?:the\s+)?[\"“]?"
    r"(?:Portfolio\s+Managers?|Co-Portfolio\s+Managers?)[\"”]?\)\s*,?\s*)?"
    r"(?:are|is)\s+(?:jointly\s+and\s+primarily|primarily\s+and\s+jointly|jointly|primarily|co-)?\s*"
    r"responsible\s+for\s+the\s+day[-\s]to[-\s]day\s+management",
    re.I,
)
_P_MANAGED_SINCE = re.compile(
    rf"(?P<name>{PERSON}){_CRED_TAIL}(?P<mid>[^\n]{{0,220}}?)\b(?i:(?:has|have)\s+"
    rf"(?:co-)?(?:managed|been\s+(?:a\s+|the\s+|lead\s+|co-)*(?:portfolio\s+)?manager\s+of)\s+"
    rf"(?:the\s+)?(?:fund|portfolio|series|trust))(?P<tail>[^\n]{{0,80}}?)\b(?i:since)\s+(?P<since>[^.;\n]{{2,40}})",
)
_P_SERVED_SINCE = re.compile(
    rf"(?P<name>{PERSON}){_CRED_TAIL}(?P<mid>[^\n]{{0,220}}?)\b(?i:(?:has\s+)?(?:served|serves|been)\s+as\s+"
    rf"(?:a\s+|the\s+|lead\s+|co-|senior\s+)*portfolio\s+manager\s+(?:of|for)\s+the\s+"
    rf"(?:fund|portfolio|series)\s+since)\s+(?P<since>[^.;\n]{{2,40}})",
)
# Vanguard: "Jane Doe, CFA, Principal of Vanguard. She has managed the Fund since 2019."
_P_VANGUARD = re.compile(
    rf"(?P<name>{PERSON}){_CRED_TAIL},\s*(?P<title>[^.\n]{{3,160}}?)\.\s+"
    rf"(?:He|She|They|Mr\.\s+\S+|Ms\.\s+\S+)\s+(?i:(?:has|have)\s+(?:co-)?managed\s+"
    rf"(?:the\s+)?(?:fund|portfolio))[^.\n]{{0,60}}?(?i:since)\s+(?P<since>[^.;\n]{{2,40}})",
)
_ROLE_RE = re.compile(r"\((?P<role>(?:lead|co-?lead|co-?|senior|associate)?\s*(?:portfolio\s+)?(?:manager|co-manager)[^)]{0,30})\)", re.I)


def _section_windows(lines: list[str]) -> list[tuple[int, int]]:
    """Return (start_line, end_line) windows that look like PM sections."""
    wins = []
    for i, line in enumerate(lines):
        if len(line) > 700:
            continue
        if not _SECTION_START.match(line) or _SECTION_EXCLUDE.search(line[:160]):
            continue
        end = min(len(lines), i + 60)
        chars = 0
        for j in range(i + 1, end):
            chars += len(lines[j])
            if _SECTION_END.match(lines[j]) or chars > 8000:
                end = j
                break
            # a new PM heading closes this one (next fund's section)
            if j > i + 2 and _SECTION_START.match(lines[j]) and len(lines[j]) < 60:
                end = j
                break
        wins.append((i, end))
    return wins


def _parse_table_row(line: str, asof_year: int | None) -> dict | None:
    cells = [c.strip() for c in line.split("|") if c.strip()]
    if len(cells) < 2:
        return None
    first = cells[0]
    # header row?
    if _HEADER_ROW.search(line) and not re.search(_YEAR_RE, line) and not re.search(r"\d+\s+years?", line, re.I):
        return None
    people = find_people(first)
    if not people:
        return None
    name = people[0]
    since_cell = next((c for c in cells[1:] if _SINCE_CELL.search(c)), None)
    if since_cell is None:
        return None
    title_cells = [c for c in cells[1:] if c is not since_cell and not _SINCE_CELL.fullmatch(c.strip())]
    # American Funds packs the title after the name in the first cell
    rest_of_first = _clean_ws(first.replace(name, "", 1)).strip(" ,;")
    title = title_cells[0] if title_cells else (rest_of_first or None)
    rec = {"name": name, "title": (title or None) and title[:200], "role": None}
    rec.update(parse_since(since_cell, asof_year))
    return rec


def _parse_prose(segment: str, asof_year: int | None) -> list[dict]:
    recs: dict[str, dict] = {}

    def add(name, title=None, since=None, role=None):
        name = _strip_creds(name)
        if not is_person_name(name):
            return
        k = normalize_name(name)
        r = recs.get(k) or {"name": name, "title": None, "role": None,
                            "since_raw": None, "since_year": None,
                            "since_month": None, "is_inception": False}
        if title and not r["title"]:
            r["title"] = _clean_ws(title)[:200]
        if role and not r["role"]:
            r["role"] = _clean_ws(role).lower()
        if since and not r["since_year"]:
            r.update({k2: v for k2, v in parse_since(since, asof_year).items() if v is not None})
        recs[k] = r

    for m in _P_VANGUARD.finditer(segment):
        add(m.group("name"), title=m.group("title"), since=m.group("since"))
    for pat in (_P_MANAGED_SINCE, _P_SERVED_SINCE):
        for m in pat.finditer(segment):
            mid = m.group("mid") or ""
            role = None
            rm = _ROLE_RE.search(mid)
            if rm:
                role = rm.group("role")
            title = None
            tm = re.match(r"\s*,\s*([^,.;]{3,120}?)\s*,", mid)
            if tm and not _ROLE_RE.search(tm.group(1)):
                title = tm.group(1)
            add(m.group("name"), title=title, since=m.group("since"), role=role)
    for m in _P_RESPONSIBLE.finditer(segment):
        names_part = m.group("names")
        # keep only the clause right before the verb (drop a preceding sentence)
        names_part = re.split(r"(?<=[a-z]{3})\.\s+", names_part)[-1]
        for n in find_people(names_part):
            add(n)
    return list(recs.values())


def extract_roster_sections(text: str, asof_year: int | None = None) -> list[dict]:
    """
    Returns [{start: char_offset, end: char_offset, managers: [manager dicts]}]
    for every Portfolio Managers section in a document.
    """
    lines = text.split("\n")
    offsets, acc = [], 0
    for l in lines:
        offsets.append(acc)
        acc += len(l) + 1
    sections = []
    for a, b in _section_windows(lines):
        seg_lines = lines[a:b]
        managers: dict[str, dict] = {}
        for l in seg_lines:
            if "|" in l:
                rec = _parse_table_row(l, asof_year)
                if rec:
                    managers.setdefault(normalize_name(rec["name"]), rec)
        segment = "\n".join(seg_lines)
        for rec in _parse_prose(segment, asof_year):
            k = normalize_name(rec["name"])
            if k in managers:
                for f in ("title", "role", "since_year", "since_month", "since_raw"):
                    if not managers[k].get(f) and rec.get(f):
                        managers[k][f] = rec[f]
            else:
                managers[k] = rec
        if managers:
            sections.append({
                "start": offsets[a],
                "end": offsets[b - 1] + len(lines[b - 1]) if b - 1 < len(lines) else acc,
                "managers": list(managers.values()),
            })
    return sections


def extract_rosters(doc_html: str, series: list[dict], asof: str | None = None) -> dict:
    """
    Full roster extraction for one prospectus document.

    Returns {series_id: [manager dicts]} plus key "_unattributed" for sections
    that could not be tied to a series in a multi-series filing.
    """
    asof_year = int(asof[:4]) if asof else datetime.utcnow().year
    text = lines_to_text(html_to_lines(doc_html))
    sections = extract_roster_sections(text, asof_year)
    loc = SeriesLocator(text, series)
    single = series[0]["series_id"] if len(series) == 1 else None
    out: dict[str, list[dict]] = {}
    for sec in sections:
        sid = single or loc.nearest_before(sec["start"])
        key = sid or "_unattributed"
        bucket = {normalize_name(m["name"]): m for m in out.get(key, [])}
        for m in sec["managers"]:
            k = normalize_name(m["name"])
            if k not in bucket:
                bucket[k] = m
            else:
                for f in ("title", "role", "since_year", "since_month", "since_raw"):
                    if not bucket[k].get(f) and m.get(f):
                        bucket[k][f] = m[f]
        out[key] = list(bucket.values())
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 7. Change-event extraction (497 / 497K stickers)
# ─────────────────────────────────────────────────────────────────────────────
# Verb-first: find the change verb, then read the subject names from the start
# of that sentence. (Name-first regexes swallow a leading "Effective October 3,"
# clause and lose the manager — this avoids that entirely.)

_HONORIFIC = re.compile(r"\b(?:Mr|Ms|Mrs|Dr)\.\s+(?P<surname>(?:Mc|Mac|O['’])?[A-Z][a-zA-Z'’\-]+)")
# sentence boundary: ". X" not after an initial or honorific
_SENT_BOUND = re.compile(r"(?<![A-Z])(?<!Mr)(?<!Ms)(?<!Dr)(?<!Mrs)(?<!Jr)(?<!Sr)(?<!Inc)(?<!No)\.\s+(?=[A-Z(\"“])|\n")

_REMOVE_VERB_RE = re.compile(
    r"\b(?:will\s+no\s+longer\s+(?:serve|be)|no\s+longer\s+serves?|(?:is|are)\s+no\s+longer|"
    r"no\s+longer\s+(?:be|serve)|(?:has|have)\s+retired|will\s+retire|plans?\s+to\s+retire|"
    r"intends?\s+to\s+retire|(?:has|have)\s+announced\s+(?:his|her|their)\s+(?:intention\s+to\s+retire|"
    r"planned\s+retirement|retirement|departure|resignation)|(?:has|have)\s+(?:left|departed|resigned)|"
    r"will\s+(?:leave|depart|resign|step\s+down)|(?:has|have)\s+stepped\s+down|(?:ceased|will\s+cease)\s+to\s+serve|"
    r"(?:is|are)\s+(?:retiring|leaving|stepping\s+down)|(?:has|have)\s+been\s+removed)\b",
    re.I,
)
_ADD_VERB_RE = re.compile(
    r"\b(?:(?:has|have)\s+been\s+(?:added|named|appointed|designated|promoted)|will\s+be\s+(?:added|named|appointed|designated)|"
    r"(?:was|were)\s+(?:added|named|appointed)|(?:is|are)\s+(?:added|named|appointed|joining)|(?:has|have)\s+joined|"
    r"will\s+join|joined|joins|will\s+(?:become|serve)|became|becomes|will\s+assume)\b",
    re.I,
)
_REPLACE_RE = re.compile(
    rf"(?P<new>{PERSON}){_CRED_TAIL}[^.;\n]{{0,120}}?\s+(?:will\s+(?:replace|succeed)|(?:has|have)\s+(?:replaced|succeeded)|replaces|succeeds)\s+"
    rf"(?P<old>(?:(?:Mr|Ms|Mrs|Dr)\.\s+[A-Z][a-zA-Z'’\-]+)|{PERSON})",
)
_PM_ROLE_AFTER = re.compile(
    r"^[^.\n]{0,80}?(?:portfolio\s+manager|co-portfolio\s+manager|portfolio\s+management\s+team|"
    r"manager\s+of\s+the\s+(?:fund|portfolio)|co-manager)",
    re.I,
)
_PM_CONTEXT = re.compile(r"portfolio\s+manag|management\s+team|co-manag|manager\s+of\s+the\s+(?:fund|portfolio)", re.I)
_RE_ALLREFS = re.compile(
    r"all\s+references\s+to\s+(?P<names>[^\n]{3,300}?)\s+(?:in\s+the\s+[^\n]{0,250}?\s+)?(?:are|is)\s+(?:hereby\s+)?(?:deleted|removed)",
    re.I,
)
_RE_REPLACED = re.compile(
    r"(?:portfolio\s+managers?|management)[^.]{0,200}?(?:is|are)\s+(?:hereby\s+)?(?:deleted|revised|amended|restated)"
    r"\s+(?:in\s+(?:its|their)\s+entirety\s+)?and\s+replaced\s+with\s+the\s+following",
    re.I,
)
_RE_EFFECTIVE = re.compile(
    rf"(?:effective|as\s+of)\s+(?:as\s+of\s+|on\s+|on\s+or\s+about\s+|at\s+the\s+close\s+of\s+business\s+on\s+)?(?P<date>{_DATE_RE}|immediately)",
    re.I,
)


def _sentence_start(text: str, pos: int, maxback: int = 400) -> int:
    lo = max(0, pos - maxback)
    last = lo
    for m in _SENT_BOUND.finditer(text, lo, pos):
        last = m.end()
    return last


def _sentence_end(text: str, pos: int, maxfwd: int = 300) -> int:
    m = _SENT_BOUND.search(text, pos, min(len(text), pos + maxfwd))
    return m.start() + 1 if m else min(len(text), pos + maxfwd)


def _resolve_surname(surname: str, text: str) -> str | None:
    """'Mr. Smith' -> 'John A. Smith' using any full mention in the doc."""
    for m in PERSON_RE.finditer(text):
        cand = _strip_creds(m.group(0))
        if is_person_name(cand) and cand.split()[-1].strip(",") == surname:
            return cand
    return None


def _subject_names(segment: str, text: str) -> list[str]:
    """Names acting as the subject of a clause (full names + honorific surnames)."""
    # drop clauses introduced by "with"/"alongside"/"replacing" — those are other people
    segment = re.split(r"\b(?:with|alongside|together\s+with|replacing|succeeding|who\s+replaces)\b", segment)[0]
    names = find_people(segment)
    keys = {normalize_name(n) for n in names}
    for hm in _HONORIFIC.finditer(segment):
        full = _resolve_surname(hm.group("surname"), text)
        if full and normalize_name(full) not in keys:
            names.append(full)
            keys.add(normalize_name(full))
    return names


def _effective_date(text: str, pos: int, filing_date: str | None) -> tuple[str | None, bool]:
    """Nearest 'Effective <date>' to pos (same paragraph), else filing date."""
    lo, hi = max(0, pos - 500), min(len(text), pos + 300)
    best, best_d = None, None
    for m in _RE_EFFECTIVE.finditer(text, lo, hi):
        d = abs(m.start() - pos)
        if best_d is None or d < best_d:
            best, best_d = m, d
    if best:
        dt = best.group("date")
        if dt.lower() == "immediately":
            return filing_date, False
        return parse_date(dt) or filing_date, parse_date(dt) is None
    return filing_date, True


def extract_change_events(doc_html: str, series: list[dict], filing_date: str | None = None) -> dict:
    """
    Parse a 497/497K supplement for portfolio-manager changes.

    Returns {
      "events": [{event_type: ADDED|REMOVED, manager, manager_key, series_ids,
                  effective_date, effective_date_is_filing_date, evidence}],
      "replacement_rosters": {series_id: [manager dicts]}   # "replaced with the following"
    }
    """
    text = lines_to_text(html_to_lines(doc_html))
    loc = SeriesLocator(text, series)
    asof_year = int(filing_date[:4]) if filing_date else datetime.utcnow().year
    by_key: dict[tuple, dict] = {}

    def targets(a: int, b: int) -> list[str]:
        direct = loc.in_span(a, b)
        if direct:
            return sorted(direct)
        if re.search(r"\b(?:funds|each\s+fund|all\s+funds|portfolios|each\s+of\s+the)\b", text[a:b], re.I):
            return sorted(loc.mentioned) or loc.all_ids()
        near = loc.nearest_before(a)
        if near:
            return [near]
        # Nothing names a fund. Only a single-series filing is unambiguous;
        # for a multi-series trust "every series" would alert holders of every
        # fund the trust runs about a change at one of them.
        ids = loc.all_ids()
        return ids if len(ids) == 1 else []

    def push(etype, name, a, b):
        key = (etype, normalize_name(name))
        tg = targets(a, b)
        if key in by_key:
            by_key[key]["series_ids"] = sorted(set(by_key[key]["series_ids"]) | set(tg))
            return
        eff, fallback = _effective_date(text, a, filing_date)
        by_key[key] = {
            "event_type": etype,
            "manager": name,
            "manager_key": normalize_name(name),
            "series_ids": tg,
            "effective_date": eff,
            "effective_date_is_filing_date": fallback,
            "evidence": _clean_ws(text[a: min(len(text), b + 200)])[:600],
        }

    # explicit replacement: "Jane Doe will replace John Smith as portfolio manager"
    for m in _REPLACE_RE.finditer(text):
        if not _PM_CONTEXT.search(text[m.end(): m.end() + 120]):
            continue
        a = _sentence_start(text, m.start())
        new = _strip_creds(m.group("new"))
        old = m.group("old")
        hm = _HONORIFIC.match(old)
        old = _resolve_surname(hm.group("surname"), text) if hm else _strip_creds(old)
        if is_person_name(new):
            push("ADDED", new, a, m.end())
        if old and is_person_name(old):
            push("REMOVED", old, a, m.end())

    for m in _REMOVE_VERB_RE.finditer(text):
        after = text[m.end(): m.end() + 260]
        a = _sentence_start(text, m.start())
        before = text[a: m.start()]
        if not (_PM_CONTEXT.search(after) or _PM_CONTEXT.search(before)):
            continue            # e.g. "the Fund is no longer offered"
        for n in _subject_names(before, text):
            push("REMOVED", n, a, _sentence_end(text, m.end()))

    for m in _RE_ALLREFS.finditer(text):
        for n in _subject_names(m.group("names"), text):
            push("REMOVED", n, m.start(), m.end())

    removed_keys = {k[1] for k in by_key if k[0] == "REMOVED"}
    for m in _ADD_VERB_RE.finditer(text):
        after = text[m.end(): m.end() + 120]
        if not _PM_ROLE_AFTER.search(after):
            continue
        a = _sentence_start(text, m.start())
        for n in _subject_names(text[a: m.start()], text):
            if normalize_name(n) not in removed_keys:
                push("ADDED", n, a, _sentence_end(text, m.end()))

    # "... table is deleted in its entirety and replaced with the following:"
    replacement: dict[str, list[dict]] = {}
    for m in _RE_REPLACED.finditer(text):
        tail = text[m.end(): m.end() + 5000]
        rows = []
        for line in tail.split("\n")[1:40]:
            if "|" in line:
                r = _parse_table_row(line, asof_year)
                if r:
                    rows.append(r)
            elif rows:
                break
        if not rows:
            rows = _parse_prose(tail[:2500], asof_year)
        if rows:
            for sid in targets(_sentence_start(text, m.start()), m.end()):
                replacement[sid] = rows

    # "The following information is added to the table ..." -> ADDED rows
    for m in re.finditer(r"following\s+(?:information\s+|row\s+)?(?:is|are)\s+(?:hereby\s+)?added", text, re.I):
        for line in text[m.end(): m.end() + 3000].split("\n")[1:15]:
            if "|" in line:
                r = _parse_table_row(line, asof_year)
                if r and normalize_name(r["name"]) not in removed_keys:
                    push("ADDED", r["name"], _sentence_start(text, m.start()), m.end())

    return {"events": list(by_key.values()), "replacement_rosters": replacement}


def diff_rosters(old: list[dict], new: list[dict]) -> dict:
    """Compare two rosters by normalised name."""
    o = {normalize_name(m["name"]): m for m in old or []}
    n = {normalize_name(m["name"]): m for m in new or []}
    return {
        "added": [n[k] for k in n.keys() - o.keys()],
        "removed": [o[k] for k in o.keys() - n.keys()],
        "unchanged": [n[k] for k in n.keys() & o.keys()],
    }
