"""
news_poller.py
GQ FinXray US — Feature 2, the RSS half (the FMP half lives in fmp_poller.py).

Polls a fixed set of financial-news feeds, works out which watchlisted tickers
each article is actually about, and drops the article into `raw_filings` with
status='PENDING'. ai_pipeline.py does everything after that. This file never
writes to `alerts` and never touches Telegram.

WHAT WAS WRONG BEFORE, AND WHAT CHANGED
---------------------------------------
* **Ticker extraction produced mostly false positives.** The old matcher
  uppercased the article and ran `\\b<TICKER>\\b` over it. Real listed tickers
  include A, C, T, V, ALL, ON, IT, NOW, KEY, CAR, FOR, NEW, ARE, HAS, BY, SO and
  TWO — all ordinary English words. Uppercasing the article makes every one of
  them match in almost every article, so a user watching Allstate (ALL) received
  every story containing the word "all". Matching is now tiered: strong
  contextual evidence ($TICKER, "(NASDAQ: TICKER)", "TICKER shares") is always
  accepted, while a bare standalone token is accepted only for tickers that are
  not ordinary words, and only against the ORIGINAL-CASE text so "All" and "all"
  can never match ALL.

* **Atom feeds silently yielded zero articles.** Entries were located with the
  Atom namespace (`root.findall("atom:entry", ns)`) but their children were then
  looked up unnamespaced (`item.find("title")`), which returns None for real
  Atom. Every entry was therefore skipped. Child lookups are now by local name,
  which is correct for RSS 2.0, Atom, and RDF alike.

* **The watchlist was re-queried inside the per-source loop** — one database
  round trip per feed, every cycle. It is read once per run now (and
  watchlist_util caches it for two minutes on top of that).

* **Six feeds were retired upstream and 404'd every cycle** (the five
  feeds.reuters.com endpoints and rss.cnn.com/rss/money_latest.rss). Removed.

* **Articles matching no watched ticker were stored as ticker="MARKET".** Nobody
  can watch "MARKET", so ai_pipeline's Stage 0 discarded every one of them after
  they had already cost a row. They are skipped at the source now.

* `filed_at` is the article's own publication timestamp, not the poll time, so
  the alert and the ordering downstream reflect when the news actually broke.

* `content_hash` is stored so the same wire story arriving through several
  outlets is recognised as one event rather than summarised three times.
"""

import os
import re
import time
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from dotenv import load_dotenv
from supabase import create_client

from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

POLLER_NAME = "news_poller"

HEADERS = {
    "User-Agent": "GQFinXray/1.0 (+https://gquants.com; contact@gquants.com)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

FEED_TIMEOUT = 15
# Politeness gap between feeds. Nineteen feeds x 0.25s is under five seconds of
# added wall time on a 60-second cycle.
FEED_GAP_SECONDS = 0.25

# An article naming this many watched tickers is a market round-up, not company
# news; alerting every one of those users would be noise. Keep the best-evidenced
# few and drop the rest.
MAX_TICKERS_PER_ARTICLE = 3

# Anything older than this is backfill, not news — feeds occasionally republish
# weeks-old items when a CMS is migrated.
MAX_ARTICLE_AGE_DAYS = 3

_session = requests.Session()


# ── Feeds ─────────────────────────────────────────────────────────────────────
# `source_key` must stay inside feature_map.FEATURES[2]["sources"] or the alert
# resolves to "Unmapped".
NEWS_SOURCES = [
    # ── General market ────────────────────────────────────────────────────────
    {"name": "CNBC Markets",        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html", "source_key": "CNBC",        "sector": "MARKET"},
    {"name": "CNBC Earnings",       "url": "https://www.cnbc.com/id/15839069/device/rss/rss.html", "source_key": "CNBC",        "sector": "MARKET"},
    {"name": "CNBC Investing",      "url": "https://www.cnbc.com/id/20409666/device/rss/rss.html", "source_key": "CNBC",        "sector": "MARKET"},
    {"name": "MarketWatch Top Stories", "url": "https://feeds.marketwatch.com/marketwatch/topstories", "source_key": "MARKETWATCH", "sector": "MARKET"},
    {"name": "MarketWatch Market Pulse", "url": "https://feeds.marketwatch.com/marketwatch/marketpulse", "source_key": "MARKETWATCH", "sector": "MARKET"},
    {"name": "Yahoo Finance",       "url": "https://finance.yahoo.com/news/rssindex",               "source_key": "YAHOO",       "sector": "MARKET"},
    {"name": "Nasdaq Originals",    "url": "https://www.nasdaq.com/feed/nasdaq-originals/rss.xml",  "source_key": "NASDAQ",      "sector": "MARKET"},
    {"name": "Investor's Business Daily", "url": "https://www.investors.com/feed/",                 "source_key": "IBD",         "sector": "MARKET"},
    {"name": "Fortune Business",    "url": "https://fortune.com/feed",                              "source_key": "FORTUNE",     "sector": "MARKET"},

    # ── Sector desks ──────────────────────────────────────────────────────────
    {"name": "CNBC Technology",     "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "source_key": "CNBC", "sector": "TECHNOLOGY"},
    {"name": "CNBC Finance",        "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "source_key": "CNBC", "sector": "FINANCE"},
    {"name": "MarketWatch Banking", "url": "https://feeds.marketwatch.com/marketwatch/financialservices", "source_key": "MARKETWATCH", "sector": "FINANCE"},
    {"name": "CNBC Healthcare",     "url": "https://www.cnbc.com/id/10000108/device/rss/rss.html", "source_key": "CNBC", "sector": "HEALTHCARE"},
    {"name": "CNBC Energy",         "url": "https://www.cnbc.com/id/19836768/device/rss/rss.html", "source_key": "CNBC", "sector": "ENERGY"},
    {"name": "CNBC Retail",         "url": "https://www.cnbc.com/id/10000116/device/rss/rss.html", "source_key": "CNBC", "sector": "CONSUMER"},
    {"name": "CNBC Autos",          "url": "https://www.cnbc.com/id/10000101/device/rss/rss.html", "source_key": "CNBC", "sector": "CONSUMER"},
    {"name": "CNBC Media",          "url": "https://www.cnbc.com/id/10000110/device/rss/rss.html", "source_key": "CNBC", "sector": "COMMUNICATION"},
    {"name": "CNBC Industrials",    "url": "https://www.cnbc.com/id/10000113/device/rss/rss.html", "source_key": "CNBC", "sector": "INDUSTRIALS"},
    {"name": "CNBC Real Estate",    "url": "https://www.cnbc.com/id/10000115/device/rss/rss.html", "source_key": "CNBC", "sector": "REAL_ESTATE"},

    # ── Macro ─────────────────────────────────────────────────────────────────
    {"name": "MarketWatch Economy", "url": "https://feeds.marketwatch.com/marketwatch/economy-politics", "source_key": "MARKETWATCH", "sector": "MACRO"},
]

SECTOR_KEYWORDS = {
    "TECHNOLOGY": ["apple", "microsoft", "nvidia", "google", "alphabet", "meta", "semiconductor",
                   "software", "cloud", "artificial intelligence", "chip", "data center", "cybersecurity"],
    "FINANCE": ["bank", "federal reserve", "interest rate", "jpmorgan", "goldman", "morgan stanley",
                "wells fargo", "citigroup", "fintech", "insurance", "credit", "loan", "debt"],
    "HEALTHCARE": ["fda", "drug", "pharma", "biotech", "clinical trial", "pfizer", "moderna",
                   "merck", "abbvie", "medicare", "medicaid", "hospital", "medical"],
    "ENERGY": ["oil", "gas", "crude", "exxon", "chevron", "opec", "pipeline", "refinery",
               "solar", "wind", "renewable", "coal", "lng"],
    "CONSUMER": ["retail", "consumer", "walmart", "target", "nike", "restaurant", "food",
                 "beverage", "automaker", "tesla", "ford"],
    "INDUSTRIALS": ["boeing", "caterpillar", "defense", "manufacturing", "aerospace", "logistics",
                    "supply chain", "industrial", "infrastructure"],
    "REAL_ESTATE": ["real estate", "reit", "housing", "mortgage", "property", "construction"],
    "COMMUNICATION": ["streaming", "netflix", "disney", "comcast", "verizon", "advertising",
                      "social media", "telecom"],
    "MACRO": ["gdp", "inflation", "cpi", "unemployment", "federal reserve", "interest rate",
              "treasury", "recession", "fiscal policy", "jobs report"],
}


# ── Ticker matching ───────────────────────────────────────────────────────────
# Tickers here (plus every one- and two-letter ticker, handled by length) are
# ordinary English words. A bare occurrence of the token proves nothing, so they
# require strong evidence: a $ prefix, an exchange-qualified parenthetical, or a
# following financial noun. Every entry below is a genuinely listed US symbol —
# that is the point. ALL is Allstate, ON is ON Semiconductor, IT is Gartner,
# NOW is ServiceNow, KEY is KeyCorp, CAR is Avis, RUN is Sunrun, EAT is Brinker.
COMMON_WORD_TICKERS = {
    # explicitly called out as production false-positive sources
    "A", "C", "T", "V", "ALL", "ON", "IT", "NOW", "KEY", "CAR", "FOR", "NEW",
    "ARE", "HAS", "BY", "SO", "TWO",
    # three-letter English words that are also listed symbols
    "ACT", "ADD", "AGE", "AIR", "AMP", "AND", "ANY", "APP", "ARM", "ART", "ASK",
    "BAD", "BAR", "BET", "BID", "BIG", "BIT", "BOX", "BUY", "CAP", "CAT", "CUT",
    "DAY", "DID", "DIE", "DOG", "DUE", "EAR", "EAT", "EGO", "END", "ERA", "EYE",
    "FAR", "FEE", "FEW", "FIT", "FIX", "FLY", "FUN", "GET", "GOT", "GUN", "HIT",
    "HOT", "HOW", "HUB", "ICE", "ILL", "INN", "JOB", "JOY", "LAB", "LAW", "LAY",
    "LED", "LEG", "LET", "LOW", "MAN", "MAP", "MAX", "MAY", "MEN", "MET", "MIX",
    "NET", "NEW", "NOT", "OFF", "OIL", "OLD", "ONE", "OUR", "OUT", "OWN", "PAY",
    "PER", "PUT", "RAN", "RAW", "RUN", "SAW", "SAY", "SEA", "SEE", "SET", "SIX",
    "SKY", "SON", "SUN", "TAP", "TAX", "TEN", "THE", "TIE", "TIP", "TOO", "TOP",
    "TRY", "USE", "VIA", "WAR", "WAS", "WAY", "WHO", "WHY", "WIN", "YES", "YOU",
    # four- and five-letter English words that are also listed symbols
    "ABLE", "AREA", "AWAY", "BABY", "BACK", "BALL", "BAND", "BANK", "BASE",
    "BEAT", "BEST", "BETS", "BIRD", "BLUE", "BOLT", "BOOK", "BOTH", "CAKE",
    "CALL", "CALM", "CARE", "CASH", "CAST", "CELL", "CHEF", "CITY", "CLAR",
    "CODE", "COLD", "COME", "COOK", "COOL", "CORE", "COST", "CROP", "CURE",
    "DARE", "DATA", "DATE", "DEAL", "DEEP", "DINE", "DOCS", "DOOR", "DOWN",
    "DRAW", "DROP", "DUST", "EACH", "EARN", "EASE", "EAST", "EDGE", "EDIT",
    "ELSE", "EVEN", "EVER", "EYES", "FACE", "FACT", "FAIR", "FALL", "FAST",
    "FEEL", "FEET", "FILE", "FILL", "FILM", "FIND", "FINE", "FIRE", "FIRM",
    "FISH", "FIVE", "FLEX", "FLOW", "FOOD", "FOOT", "FORD", "FORM", "FOUR",
    "FREE", "FROM", "FUEL", "FULL", "FUND", "GAIN", "GAME", "GATE", "GAVE",
    "GEAR", "GIFT", "GIVE", "GLAD", "GOAL", "GOES", "GOLD", "GONE", "GOOD",
    "GRAB", "GREW", "GROW", "HALF", "HALL", "HAND", "HARD", "HAVE", "HEAD",
    "HEAL", "HEAR", "HEAT", "HELD", "HELP", "HERE", "HIGH", "HILL", "HIRE",
    "HOLD", "HOME", "HOPE", "HOUR", "HUGE", "IDEA", "INTO", "IRON", "ITEM",
    "JOIN", "JUMP", "JUST", "KEEP", "KIND", "KNEW", "KNOW", "LAKE", "LAND",
    "LAST", "LATE", "LEAD", "LEAF", "LEAN", "LESS", "LIFE", "LIFT", "LIKE",
    "LINE", "LINK", "LION", "LIST", "LIVE", "LOAD", "LOAN", "LOCK", "LONG",
    "LOOK", "LOSE", "LOSS", "LOST", "LOTS", "LOVE", "LUCK", "MADE", "MAIL",
    "MAIN", "MAKE", "MALL", "MANY", "MARK", "MASS", "MEAL", "MEAN", "MEET",
    "MEGA", "MIND", "MINE", "MISS", "MOOD", "MORE", "MOST", "MOVE", "MUCH",
    "MUST", "NAME", "NEAR", "NEED", "NEWS", "NEXT", "NICE", "NINE", "NONE",
    "NOTE", "OMIT", "ONCE", "ONLY", "ONTO", "OPEN", "OVER", "PACE", "PACK",
    "PAGE", "PAID", "PAIR", "PARK", "PART", "PASS", "PAST", "PATH", "PEAK",
    "PICK", "PILE", "PINK", "PLAN", "PLAY", "PLUS", "POOL", "POOR", "PORT",
    "POST", "PULL", "PURE", "PUSH", "RACE", "RAIL", "RAIN", "RARE", "RATE",
    "READ", "REAL", "RELY", "REST", "RICH", "RIDE", "RING", "RISE", "RISK",
    "ROAD", "ROCK", "ROLE", "ROLL", "ROOM", "ROOT", "RULE", "SAFE", "SAID",
    "SAIL", "SALE", "SALT", "SAME", "SAND", "SAVE", "SEAT", "SEED", "SEEK",
    "SEEM", "SEEN", "SELF", "SELL", "SEND", "SHIP", "SHOP", "SHOT", "SHOW",
    "SIDE", "SIGN", "SITE", "SIZE", "SKIP", "SLOW", "SNAP", "SOFT", "SOIL",
    "SOLD", "SOLE", "SOME", "SOON", "SORT", "SOUL", "SPOT", "STAR", "STAY",
    "STEP", "STOP", "SUCH", "SUIT", "SURE", "TAKE", "TALK", "TALL", "TASK",
    "TEAM", "TECH", "TELL", "TERM", "TEST", "TEXT", "THAN", "THAT", "THEM",
    "THEN", "THEY", "THIS", "THUS", "TIDE", "TIME", "TINY", "TOLD", "TOOK",
    "TOOL", "TOUR", "TOWN", "TREE", "TRIM", "TRIP", "TRUE", "TUNE", "TURN",
    "TYPE", "UNIT", "UPON", "USED", "USER", "VAST", "VERY", "VIEW", "VOTE",
    "WAGE", "WAIT", "WALK", "WALL", "WANT", "WARM", "WARN", "WAVE", "WAYS",
    "WEAK", "WEAR", "WEEK", "WELL", "WENT", "WERE", "WEST", "WHAT", "WHEN",
    "WILL", "WIND", "WINE", "WING", "WIRE", "WISE", "WISH", "WITH", "WOOD",
    "WORD", "WORE", "WORK", "WRAP", "YARD", "YEAR", "ZERO", "ZONE",
    "ABOUT", "AFTER", "AGAIN", "AHEAD", "ALONE", "ALONG", "AMONG", "ASSET",
    "BEGIN", "BOARD", "BOOST", "BRIEF", "BUILD", "CHAIN", "CHART", "CHECK",
    "CLEAN", "CLEAR", "CLOSE", "COULD", "COVER", "DAILY", "DEALS", "DRIVE",
    "EARLY", "EIGHT", "EMPTY", "EQUAL", "EVENT", "EVERY", "FIRST", "FOCUS",
    "FORCE", "FOUND", "FRESH", "FRONT", "GIANT", "GIVEN", "GOALS", "GRAND",
    "GREAT", "GREEN", "GROSS", "GROUP", "GUARD", "GUIDE", "HAPPY", "HEARD",
    "HEAVY", "HOUSE", "HUMAN", "INDEX", "ISSUE", "LARGE", "LATER", "LEARN",
    "LEVEL", "LIGHT", "LIMIT", "LOCAL", "LOWER", "MAJOR", "MATCH", "MAYBE",
    "MEDIA", "MIGHT", "MONEY", "MONTH", "NEVER", "NIGHT", "NOISE", "NORTH",
    "OFFER", "OFTEN", "ORDER", "OTHER", "OWNER", "PAPER", "PARTY", "PEACE",
    "PHASE", "PHONE", "PIECE", "PLACE", "PLANT", "POINT", "POWER", "PRESS",
    "PRICE", "PRIDE", "PRIME", "PRINT", "PRIOR", "QUICK", "QUITE", "RAISE",
    "RANGE", "RAPID", "RATIO", "REACH", "READY", "RIGHT", "RIVAL", "ROUND",
    "ROUTE", "RULES", "SALES", "SCALE", "SCOPE", "SENSE", "SERVE", "SEVEN",
    "SHARE", "SHARP", "SHEET", "SHIFT", "SHORT", "SIGHT", "SINCE", "SIXTY",
    "SLIDE", "SMALL", "SMART", "SOLAR", "SOLID", "SOUND", "SOUTH", "SPACE",
    "SPEED", "SPEND", "SPLIT", "STAFF", "STAGE", "STAKE", "STAND", "START",
    "STATE", "STEAM", "STEEL", "STILL", "STOCK", "STORE", "STORM", "STORY",
    "STYLE", "SUPER", "SWEET", "SWIFT", "TABLE", "TAKEN", "TERMS", "THEIR",
    "THERE", "THESE", "THING", "THINK", "THIRD", "THOSE", "THREE", "TIGHT",
    "TIMES", "TODAY", "TOTAL", "TOUCH", "TOUGH", "TRACK", "TRADE", "TRAIN",
    "TREND", "TRIAL", "TRUCK", "TRUST", "TRUTH", "UNDER", "UNION", "UNTIL",
    "UPPER", "URGED", "USING", "VALUE", "VIDEO", "VISIT", "VOICE", "WATCH",
    "WATER", "WEEKS", "WHERE", "WHICH", "WHILE", "WHITE", "WHOLE", "WORLD",
    "WORTH", "WOULD", "WRITE", "WRONG", "YEARS", "YIELD", "YOUNG", "YOURS",
}

# Nouns that, following a symbol, mean the symbol is being used as a symbol.
_TICKER_NOUNS = (
    r"shares?|stock|stocks|shareholders?|stockholders?|share price|stock price|"
    r"investors?|earnings|equity|options|ADRs?|ADS"
)
_CORP_SUFFIX = (
    r"Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|Limited|LLC|L\.L\.C\.|PLC|"
    r"N\.V\.|S\.A\.|Co\.|Company|Holdings?|Group|Technologies|Therapeutics|Systems"
)
_EXCHANGES = r"NASDAQ|NYSE|NYSE American|NYSE Arca|AMEX|CBOE|OTC|OTCQB|OTCQX|BATS"

# Compiled patterns are cached per ticker: the same few dozen watched symbols are
# tested against every article of every cycle, and re.compile is not free.
_pattern_cache = {}


def _build_patterns(ticker):
    """(strong_patterns, plain_pattern) for one ticker, both case-sensitive."""
    t = re.escape(ticker)
    strong = [
        re.compile(rf"\${t}\b"),                                        # $AAPL
        re.compile(rf"\(\s*(?:{_EXCHANGES})\s*:\s*{t}\s*\)"),           # (NASDAQ: AAPL)
        re.compile(rf"\b(?:{_EXCHANGES})\s*:\s*{t}\b"),                 # NASDAQ: AAPL
        re.compile(rf"\(\s*{t}\s*\)"),                                  # (AAPL)
        re.compile(rf"\b{t}\s+(?:{_TICKER_NOUNS})\b"),                  # AAPL shares
        re.compile(rf"\bticker\s+(?:symbol\s+)?{t}\b", re.IGNORECASE),  # ticker symbol AAPL
        # "Snap Inc", "Sunrun Inc." — only meaningful for word-shaped tickers,
        # harmless for the rest. Ticker word matched case-insensitively, the
        # corporate suffix case-sensitively so "co" in prose cannot trigger it.
        # The trailing \b is load-bearing. Without it "Corp\.?" matched the first
        # four letters of "Corporate", so text like "...discussed IT Corporate
        # spending..." registered as a STRONG hit for ticker IT (Gartner) — and
        # strong hits bypass the COMMON_WORD_TICKERS filter entirely, so the
        # article was attributed to a company it never mentioned.
        re.compile(rf"(?i:\b{t}\b)\s+(?:{_CORP_SUFFIX})\b"),
    ]
    plain = re.compile(rf"\b{t}\b")
    return strong, plain


def _patterns_for(symbol):
    if symbol not in _pattern_cache:
        _pattern_cache[symbol] = _build_patterns(symbol)
    return _pattern_cache[symbol]


def _variants(ticker):
    """
    Written forms of one symbol. Class shares are quoted inconsistently across
    outlets — Berkshire is BRK.B on Nasdaq, BRK-B on Yahoo, BRK/B in some wires —
    so all three are tested, but the watchlist's own spelling is what gets stored.
    """
    forms = {ticker}
    if not ticker.isalpha():
        forms.add(ticker.replace(".", "-"))
        forms.add(ticker.replace("-", "."))
        forms.add(ticker.replace(".", "/").replace("-", "/"))
    return forms


def extract_watched_tickers(text, watched):
    """
    Which of `watched` this article is actually about.

    Returns a list ordered strongest-evidence-first and capped at
    MAX_TICKERS_PER_ARTICLE. `text` MUST be original case — the whole design
    depends on "All" not looking like "ALL".

    Only tickers in `watched` are ever tested. Looping the 6,353-row stocks table
    here would be 6,353 regex passes per article.
    """
    if not text or not watched:
        return []

    strong_hits, plain_hits = [], []
    for raw in watched:
        ticker = (raw or "").strip().upper()
        if not ticker or not re.fullmatch(r"[A-Z][A-Z0-9.\-/]{0,6}", ticker):
            continue

        forms = _variants(ticker)
        if any(p.search(text) for f in forms for p in _patterns_for(f)[0]):
            strong_hits.append(ticker)
            continue

        # A one- or two-letter ticker is never distinguishable from prose without
        # context, and neither is an ordinary English word.
        alpha_len = len(re.sub(r"[^A-Z0-9]", "", ticker))
        if alpha_len <= 2 or ticker in COMMON_WORD_TICKERS:
            continue
        if any(_patterns_for(f)[1].search(text) for f in forms):
            plain_hits.append(ticker)

    ordered = strong_hits + plain_hits
    seen, out = set(), []
    for t in ordered:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:MAX_TICKERS_PER_ARTICLE]


def detect_sector_from_text(text):
    lowered = (text or "").lower()
    scores = {s: sum(1 for kw in kws if kw in lowered) for s, kws in SECTOR_KEYWORDS.items()}
    scores = {s: n for s, n in scores.items() if n}
    return max(scores, key=scores.get) if scores else "MARKET"


# ── XML helpers ───────────────────────────────────────────────────────────────
def _local(tag):
    """'{http://www.w3.org/2005/Atom}title' -> 'title'."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(elem, *names):
    """Every direct child whose local name is one of `names`, namespace ignored.

    This is what fixes the Atom feeds. RSS children are unnamespaced, Atom
    children live in the Atom namespace, and RDF feeds use yet another — reading
    by local name is correct for all three, and looking up `item.find("title")`
    on a namespaced element (the old behaviour) is correct for none but RSS.
    """
    wanted = set(names)
    return [c for c in elem if _local(c.tag) in wanted]


def _child_text(elem, *names):
    for c in _children(elem, *names):
        if c.text and c.text.strip():
            return c.text.strip()
        # Atom <content type="xhtml"> nests markup instead of using .text.
        inner = "".join(c.itertext()).strip()
        if inner:
            return inner
    return ""


def _entry_link(elem):
    """The article URL, from RSS <link>text</link> or Atom <link href=... />."""
    alternate, fallback = None, None
    for c in _children(elem, "link"):
        href = (c.attrib.get("href") or "").strip()
        rel = (c.attrib.get("rel") or "alternate").strip().lower()
        if href:
            if rel == "alternate" and alternate is None:
                alternate = href
            elif fallback is None:
                fallback = href
        elif c.text and c.text.strip():
            alternate = alternate or c.text.strip()
    url = alternate or fallback or ""
    if not url:
        # Some feeds only carry a permalink <guid>.
        guid = _child_text(elem, "guid", "id")
        if guid.startswith("http"):
            url = guid
    return url.strip()


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
                .replace("&apos;", "'"))
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Dates ─────────────────────────────────────────────────────────────────────
_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%a, %d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def parse_feed_date(value):
    """
    Feed timestamp -> timezone-aware UTC datetime, or None when genuinely absent
    or unparseable.

    Returning None rather than now() is deliberate: the caller needs to know the
    difference so `filed_at` only falls back to the poll time when the feed
    really gave us nothing.
    """
    if not value:
        return None
    raw = value.strip()

    # RFC 822/2822 — the RSS norm, and the only parser that handles the named
    # zones ("EST", "GMT") that %z rejects.
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass

    # ISO 8601 — the Atom norm.
    try:
        iso = raw.replace("Z", "+00:00")
        # fromisoformat rejects a fractional part longer than 6 digits.
        iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)
        dt = datetime.fromisoformat(iso)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def content_hash(title, body):
    """
    Stable identity for a story, independent of which outlet carried it.

    Lowercased and whitespace-collapsed so trivial editorial differences (a
    trailing " - Reuters", double spaces) do not defeat it.
    """
    norm = re.sub(r"\s+", " ", f"{title or ''} {body or ''}".lower()).strip()
    return hashlib.sha256(norm.encode("utf-8", "ignore")).hexdigest()


# ── Supabase ──────────────────────────────────────────────────────────────────
def _chunked(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _row_url(url, ticker):
    """The filing_url actually stored for one (article, ticker) pair.

    raw_filings.filing_url is UNIQUE on that column alone, but one article
    legitimately produces one row per company it names. The fragment keeps those
    rows distinct while still resolving to the same article in a browser.
    """
    return f"{url}#t={(ticker or '').upper()}" if url else url


def find_existing(urls, hashes):
    """
    One batched lookup for a whole feed instead of one SELECT per article.

    Returns (seen_url_ticker_pairs, seen_hash_ticker_pairs). Both are keyed by
    ticker as well as by identity because the same article legitimately produces
    one row per watched company it names.
    """
    url_pairs, hash_pairs = set(), set()
    try:
        for chunk in _chunked(sorted(urls), 50):
            rows = (supabase.table("raw_filings")
                    .select("filing_url, ticker")
                    .in_("filing_url", chunk).execute()).data or []
            for r in rows:
                # Normalise the stored value back to the bare article URL so
                # callers keep testing (article_url, ticker) exactly as before,
                # regardless of whether the row predates the per-ticker suffix.
                bare = (r.get("filing_url") or "").split("#t=")[0]
                url_pairs.add((bare, (r.get("ticker") or "").upper()))
    except Exception as e:
        logger.error("[NEWS] URL dedup lookup failed: %s", e)
        log_poller_error(POLLER_NAME, "find_existing:url", e)
        raise

    try:
        for chunk in _chunked(hashes, 50):
            rows = (supabase.table("raw_filings")
                    .select("content_hash, ticker")
                    .in_("content_hash", chunk).execute()).data or []
            for r in rows:
                hash_pairs.add((r.get("content_hash"), (r.get("ticker") or "").upper()))
    except Exception as e:
        # A content-hash miss only costs a duplicate the pipeline's V.2 stage will
        # catch, so this one is survivable — carry on with URL dedup alone.
        logger.warning("[NEWS] Content-hash dedup lookup failed: %s", e)

    return url_pairs, hash_pairs


def store_news(source_key, ticker, title, body, url, filed_at_iso, sector, chash, feed_name):
    """Insert one PENDING row. Returns True when a row was actually created."""
    try:
        # The real constraint is UNIQUE(filing_url) on that column ALONE, not the
        # UNIQUE(filing_url, ticker) the code below assumed. One article can name
        # up to MAX_TICKERS_PER_ARTICLE companies and is stored once per ticker —
        # so every ticker after the first hit a genuine unique violation, was
        # written off as an expected duplicate, and watchers of the 2nd and 3rd
        # company never saw the story. The per-ticker fragment restores that fan-
        # out; browsers ignore it, so the link still resolves to the article.
        row_url = _row_url(url, ticker)
        supabase.table("raw_filings").insert({
            "source": source_key,
            "filing_type": "NEWS",
            "ticker": ticker,
            "company_name": ticker,
            "raw_text": f"{title}\n\n{body}".strip(),
            "filing_url": row_url,
            "filed_at": filed_at_iso,
            "status": "PENDING",
            "content_hash": chash,
            "extra": tag_extra({
                "title": title,
                "source_name": source_key,
                "feed": feed_name,
                "sector": sector,
                "url": url,
            }, source_key, "NEWS"),
        }).execute()
        logger.info("[NEWS] %s | %s | %s | %s", source_key, sector, ticker, title[:70])
        return True
    except Exception as e:
        # UNIQUE(filing_url, COALESCE(ticker,'')) — a race between the batched
        # dedup read and this insert is expected, not an error.
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            logger.debug("[NEWS] Already stored: %s (%s)", url, ticker)
            return False
        logger.error("[NEWS] Insert failed for %s/%s: %s", ticker, url, e)
        log_poller_error(POLLER_NAME, "store_news", e, {"ticker": ticker, "url": url})
        return False


# ── Per-feed ──────────────────────────────────────────────────────────────────
def _parse_entries(root):
    """Every item/entry in the document, RSS or Atom."""
    items = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    return items


def poll_news_source(source, watched, cutoff):
    """
    Fetch and ingest one feed. Returns the number of rows stored.

    `watched` is passed in so the watchlist is read once per run rather than once
    per feed, and `cutoff` so every feed in a run shares one age boundary.
    """
    name = source["name"]
    source_key = source["source_key"]
    feed_sector = source.get("sector", "MARKET")

    try:
        r = _session.get(source["url"], headers=HEADERS, timeout=FEED_TIMEOUT)
    except Exception as e:
        logger.warning("[NEWS] %s unreachable: %s", name, e)
        log_poller_error(POLLER_NAME, f"fetch:{name}", e, {"url": source["url"]})
        return 0

    if r.status_code != 200:
        logger.warning("[NEWS] %s returned HTTP %s", name, r.status_code)
        log_poller_error(POLLER_NAME, f"fetch:{name}",
                         f"HTTP {r.status_code}",
                         {"url": source["url"], "status_code": r.status_code,
                          "body": r.text[:300]})
        return 0

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        logger.warning("[NEWS] %s returned unparseable XML: %s", name, e)
        log_poller_error(POLLER_NAME, f"parse:{name}", e, {"url": source["url"]})
        return 0

    entries = _parse_entries(root)
    if not entries:
        logger.warning("[NEWS] %s contained no items/entries", name)
        return 0

    # Pass 1: parse everything, decide tickers, compute hashes. No I/O.
    candidates = []
    for entry in entries:
        title = strip_html(_child_text(entry, "title"))
        url = _entry_link(entry)
        if not title or not url:
            continue

        body = strip_html(_child_text(entry, "description", "summary", "content", "encoded"))

        published = parse_feed_date(_child_text(entry, "pubDate", "published", "updated", "date"))
        if published and published < cutoff:
            continue
        if published and published > datetime.now(timezone.utc) + timedelta(hours=6):
            # A clock-skewed feed item; trust the poll time instead of a future date.
            published = None
        filed_at = (published or datetime.now(timezone.utc)).isoformat()

        tickers = extract_watched_tickers(f"{title}. {body}", watched)
        if not tickers:
            continue

        sector = feed_sector if feed_sector != "MARKET" else detect_sector_from_text(f"{title} {body}")
        candidates.append({
            "title": title, "body": body, "url": url, "filed_at": filed_at,
            "sector": sector, "tickers": tickers,
            "hash": content_hash(title, body),
        })

    if not candidates:
        return 0

    # Pass 2: one batched dedup read for the whole feed.
    try:
        # Look up both the bare URL (rows written before the per-ticker suffix
        # existed) and every per-ticker form we might be about to write, so the
        # batched read still pre-filters instead of leaving every duplicate to be
        # discovered by a failed INSERT.
        lookup_urls = {c["url"] for c in candidates}
        lookup_urls |= {_row_url(c["url"], t) for c in candidates for t in c["tickers"]}
        seen_urls, seen_hashes = find_existing(lookup_urls,
                                               {c["hash"] for c in candidates})
    except Exception:
        # Fail closed. Continuing without dedup would re-ingest the entire feed
        # and hand ai_pipeline a cycle's worth of duplicate LLM work.
        return 0

    stored = 0
    for c in candidates:
        for ticker in c["tickers"]:
            if (c["url"], ticker) in seen_urls or (c["hash"], ticker) in seen_hashes:
                continue
            if store_news(source_key, ticker, c["title"], c["body"], c["url"],
                          c["filed_at"], c["sector"], c["hash"], name):
                stored += 1
                seen_urls.add((c["url"], ticker))
                seen_hashes.add((c["hash"], ticker))
    return stored


# ── Entry point ───────────────────────────────────────────────────────────────
def poll_all_news():
    """
    One pass over every feed. Never raises — a scheduler calls this.

    Cost per cycle is one HTTP GET per feed (currently 19), independent of
    watchlist size, plus two batched dedup reads per feed that produced a
    candidate article.
    """
    try:
        watched = get_watched_tickers()
        if not watched:
            logger.info("[NEWS] No watchlisted tickers; skipping this cycle.")
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
        logger.info("[NEWS] Polling %d feeds against %d watched tickers",
                    len(NEWS_SOURCES), len(watched))

        total = 0
        for source in NEWS_SOURCES:
            try:
                stored = poll_news_source(source, watched, cutoff)
            except Exception as e:
                logger.error("[NEWS] %s failed: %s", source["name"], e)
                log_poller_error(POLLER_NAME, f"poll_news_source:{source['name']}", e,
                                 {"url": source["url"]})
                stored = 0
            if stored:
                logger.info("[NEWS] %s -> %d new", source["name"], stored)
            total += stored
            time.sleep(FEED_GAP_SECONDS)

        logger.info("[NEWS] Cycle complete — %d article rows queued for the pipeline", total)

    except Exception as e:
        logger.exception("[NEWS] Run failed: %s", e)
        log_poller_error(POLLER_NAME, "poll_all_news", e)


def run_news_poller(interval_seconds=60):
    """Standalone loop, for running this poller on its own outside main.py."""
    while True:
        poll_all_news()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    poll_all_news()
