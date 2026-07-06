"""
news_roundup.py
GQ FinXray US — Morning & Evening News Roundup + ETF Xray
Round 3 features:
  - Morning News Roundup: 7:00 AM EST — AI digest of last 8 hours of news
  - Evening News Roundup: 6:00 PM EST — AI digest of full trading day news
  - ETF Xray: 9:00 AM EST — structured ETF fundamentals alert
"""

import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger("news_roundup")
if not logger.handlers:
    import logging.handlers, os as _os
    _os.makedirs("logs", exist_ok=True)
    _h = logging.handlers.RotatingFileHandler(
        "logs/news_roundup.log", maxBytes=5*1024*1024, backupCount=3
    )
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
EODHD_API_KEY = os.getenv("EODHD_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = "google/gemini-2.5-flash"

# ETFs to cover in ETF Xray
ETF_UNIVERSE = [
    {"ticker": "SPY",  "name": "S&P 500 ETF",           "category": "Broad Market"},
    {"ticker": "QQQ",  "name": "NASDAQ 100 ETF",         "category": "Technology"},
    {"ticker": "IWM",  "name": "Russell 2000 ETF",       "category": "Small Cap"},
    {"ticker": "XLK",  "name": "Technology Select ETF",  "category": "Technology"},
    {"ticker": "XLF",  "name": "Financial Select ETF",   "category": "Finance"},
    {"ticker": "XLE",  "name": "Energy Select ETF",      "category": "Energy"},
    {"ticker": "XLV",  "name": "Health Care Select ETF", "category": "Healthcare"},
    {"ticker": "XLI",  "name": "Industrial Select ETF",  "category": "Industrials"},
    {"ticker": "GLD",  "name": "Gold ETF",               "category": "Commodities"},
    {"ticker": "TLT",  "name": "20+ Year Treasury ETF",  "category": "Bonds"},
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def call_deepinfra(prompt, max_tokens=500):
    """Call DeepInfra API with Gemini 2.0 Flash."""
    if not DEEPINFRA_API_KEY:
        # AI mode disabled for now — skip the network call and let the caller's
        # fallback (headline-only digest) handle it. No wasted request, no cost.
        logger.info("[DEEPINFRA] Skipped — DEEPINFRA_API_KEY not set (AI disabled for now).")
        return None
    headers = {
        "Authorization": f"Bearer {DEEPINFRA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPINFRA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }
    try:
        r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            import re
            text = (r.json()["choices"][0]["message"]["content"] or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
        else:
            logger.error(f"[DEEPINFRA] Error {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"[DEEPINFRA] Request failed: {e}")
        return None


def send_telegram(message):
    """Send message to Telegram channel."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.warning("[TELEGRAM] Token or channel ID not set.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            logger.info("[TELEGRAM] Message sent successfully.")
            return True
        else:
            logger.error(f"[TELEGRAM] Failed: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        logger.error(f"[TELEGRAM] Send failed: {e}")
        return False


def roundup_already_sent(roundup_type):
    """Check if today's roundup of this type was already sent."""
    sb = get_supabase()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = sb.table("alerts") \
        .select("id") \
        .eq("ticker", "MARKET") \
        .eq("source", "NEWS_ROUNDUP") \
        .eq("filing_type", roundup_type) \
        .gte("created_at", f"{today}T00:00:00+00:00") \
        .execute()
    return len(result.data) > 0


def save_roundup_alert(roundup_type, summary):
    """Save roundup alert to Supabase so we don't send it twice."""
    sb = get_supabase()
    sb.table("alerts").insert({
        "ticker": "MARKET",
        "summary": summary,
        "impact": "LOW",
        "source": "NEWS_ROUNDUP",
        "filing_type": roundup_type,
        "delivered": True,  # mark delivered since we send directly
        "extra": {"roundup_type": roundup_type},
        "filing_url": None
    }).execute()


def etf_xray_already_sent():
    """Check if today's ETF Xray was already sent."""
    sb = get_supabase()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = sb.table("alerts") \
        .select("id") \
        .eq("ticker", "MARKET") \
        .eq("source", "ETF_XRAY") \
        .gte("created_at", f"{today}T00:00:00+00:00") \
        .execute()
    return len(result.data) > 0


def save_etf_xray_alert(summary):
    """Save ETF Xray alert to Supabase."""
    sb = get_supabase()
    sb.table("alerts").insert({
        "ticker": "MARKET",
        "summary": summary,
        "impact": "LOW",
        "source": "ETF_XRAY",
        "filing_type": "ETF_XRAY",
        "delivered": True,
        "extra": {},
        "filing_url": None
    }).execute()


# ── News Roundup ──────────────────────────────────────────────────────────────

def fetch_recent_news(hours_back):
    """Fetch recent news articles from raw_filings."""
    sb = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    try:
        result = sb.table("raw_filings") \
            .select("ticker, raw_text, extra, filed_at, source") \
            .gte("filed_at", cutoff) \
            .eq("filing_type", "NEWS") \
            .order("filed_at", desc=True) \
            .limit(60) \
            .execute()
        return result.data or []
    except Exception as e:
        logger.error(f"[ROUNDUP] Failed to fetch news: {e}")
        return []


def build_news_digest(articles):
    """Group articles by sector and extract titles for AI digest."""
    sector_articles = {}
    for article in articles:
        extra = article.get("extra") or {}
        sector = extra.get("sector", "MARKET")
        title = extra.get("title", "") or article.get("raw_text", "")[:100]
        if not title:
            continue
        if sector not in sector_articles:
            sector_articles[sector] = []
        sector_articles[sector].append(title)

    # Build compact text for AI
    lines = []
    for sector, titles in sector_articles.items():
        lines.append(f"[{sector}]")
        for t in titles[:8]:  # cap per sector
            lines.append(f"- {t[:120]}")

    return "\n".join(lines)


def generate_ai_roundup(news_text, period_label):
    """Use DeepInfra to generate a concise news digest."""
    prompt = f"""You are a financial news analyst writing a {period_label} briefing for US stock market investors.

Below are news headlines from the past few hours, grouped by sector.
Write a concise digest of 5-7 bullet points covering the most important market-moving stories.
Each bullet must be a SHORT, COMPLETE sentence under 20 words. Never cut off mid-sentence.
Focus on what matters for investors. Use plain English. No fluff. No intro sentence.
Just the bullets. No reasoning. No explanation. No markdown formatting beyond the bullet character.

Headlines:
{news_text[:3000]}

Return ONLY the bullet points, each starting with •, each a short complete sentence. Nothing else."""

    result = call_deepinfra(prompt, max_tokens=700)

    # Safety: if result ends mid-sentence (no terminal punctuation on last line), trim that line
    if result:
        lines = [l.strip() for l in result.split("\n") if l.strip()]
        if lines:
            last_line = lines[-1]
            if last_line and last_line[-1] not in ".!?":
                # Last bullet got cut off — drop it rather than show broken text
                lines = lines[:-1]
            result = "\n".join(lines)

    return result


def run_morning_roundup():
    """Generate and send morning news roundup — called at 7:00 AM EST."""
    logger.info("[ROUNDUP] Starting morning news roundup...")

    if roundup_already_sent("MORNING_ROUNDUP"):
        logger.info("[ROUNDUP] Morning roundup already sent today, skipping.")
        return

    articles = fetch_recent_news(hours_back=8)
    if not articles:
        logger.info("[ROUNDUP] No recent articles found for morning roundup.")
        return

    logger.info(f"[ROUNDUP] Found {len(articles)} articles for morning digest.")
    news_text = build_news_digest(articles)

    ai_digest = generate_ai_roundup(news_text, "Morning Market Briefing")

    if not ai_digest:
        # Fallback: just list top headlines without AI
        titles = [
            (a.get("extra") or {}).get("title", a.get("raw_text", "")[:80])
            for a in articles[:7] if a
        ]
        ai_digest = "\n".join([f"• {t}" for t in titles if t])

    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    message = (
        f"🌅 *Morning Market Briefing — {time_str}*\n\n"
        f"{ai_digest}\n\n"
        f"_GQ FinXray US · gquants.com_"
    )

    if send_telegram(message):
        save_roundup_alert("MORNING_ROUNDUP", message)
        logger.info("[ROUNDUP] Morning roundup sent and saved.")
    else:
        logger.error("[ROUNDUP] Failed to send morning roundup.")


def run_evening_roundup():
    """Generate and send evening news roundup — called at 6:00 PM EST."""
    logger.info("[ROUNDUP] Starting evening news roundup...")

    if roundup_already_sent("EVENING_ROUNDUP"):
        logger.info("[ROUNDUP] Evening roundup already sent today, skipping.")
        return

    articles = fetch_recent_news(hours_back=10)
    if not articles:
        logger.info("[ROUNDUP] No recent articles found for evening roundup.")
        return

    logger.info(f"[ROUNDUP] Found {len(articles)} articles for evening digest.")
    news_text = build_news_digest(articles)

    ai_digest = generate_ai_roundup(news_text, "Evening Market Wrap")

    if not ai_digest:
        titles = [
            (a.get("extra") or {}).get("title", a.get("raw_text", "")[:80])
            for a in articles[:7] if a
        ]
        ai_digest = "\n".join([f"• {t}" for t in titles if t])

    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    message = (
        f"🌙 *Evening Market Wrap — {time_str}*\n\n"
        f"{ai_digest}\n\n"
        f"_GQ FinXray US · gquants.com_"
    )

    if send_telegram(message):
        save_roundup_alert("EVENING_ROUNDUP", message)
        logger.info("[ROUNDUP] Evening roundup sent and saved.")
    else:
        logger.error("[ROUNDUP] Failed to send evening roundup.")


# ── ETF Xray ──────────────────────────────────────────────────────────────────

def fetch_etf_data(ticker):
    """Fetch ETF real-time data from EODHD."""
    symbol = f"{ticker}.US"
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{symbol}",
            params={"api_token": EODHD_API_KEY, "fmt": "json"},
            timeout=15
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"[ETF XRAY] Failed to fetch {ticker}: {e}")
    return None


def fetch_etf_fundamentals(ticker):
    """Fetch ETF fundamentals from EODHD."""
    symbol = f"{ticker}.US"
    try:
        r = requests.get(
            f"https://eodhd.com/api/fundamentals/{symbol}",
            params={"api_token": EODHD_API_KEY, "fmt": "json"},
            timeout=15
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"[ETF XRAY] Fundamentals fetch failed for {ticker}: {e}")
    return None


def format_etf_line(etf_info, price_data, fund_data):
    """Format a single ETF line for the Xray alert."""
    ticker = etf_info["ticker"]
    name = etf_info["name"]
    category = etf_info["category"]

    if not price_data:
        return f"• *{ticker}* ({name}) — data unavailable"

    price = price_data.get("close", 0)
    change_p = price_data.get("change_p", 0)
    volume = price_data.get("volume", 0)

    arrow = "🟢" if change_p >= 0 else "🔴"
    sign = "+" if change_p >= 0 else ""

    # Try to get expense ratio and net assets from fundamentals
    expense_ratio = ""
    net_assets = ""
    if fund_data:
        etf_data = fund_data.get("ETF_Data", {}) or {}
        expense_ratio_val = etf_data.get("Expense_Ratio", "")
        net_assets_val = etf_data.get("Net_Assets", 0)
        if expense_ratio_val:
            expense_ratio = f" · Exp: {expense_ratio_val}"
        if net_assets_val and float(net_assets_val or 0) > 0:
            na = float(net_assets_val)
            if na >= 1_000_000_000:
                net_assets = f" · AUM: ${na/1_000_000_000:.1f}B"
            elif na >= 1_000_000:
                net_assets = f" · AUM: ${na/1_000_000:.0f}M"

    vol_str = f"{volume:,}" if volume else "N/A"

    return (
        f"{arrow} *{ticker}* — ${price:.2f} ({sign}{change_p:.2f}%)\n"
        f"   _{name} · {category}{expense_ratio}{net_assets}_\n"
        f"   Vol: {vol_str}"
    )


def run_etf_xray():
    """Generate and send ETF Xray alert — called at 9:00 AM EST."""
    logger.info("[ETF XRAY] Starting ETF Xray...")

    if etf_xray_already_sent():
        logger.info("[ETF XRAY] Already sent today, skipping.")
        return

    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y · %H:%M UTC")
    lines = []

    # Group by category
    categories = {}
    for etf in ETF_UNIVERSE:
        cat = etf["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(etf)

    for category, etfs in categories.items():
        lines.append(f"\n*{category}*")
        for etf_info in etfs:
            ticker = etf_info["ticker"]
            price_data = fetch_etf_data(ticker)
            fund_data = fetch_etf_fundamentals(ticker)
            line = format_etf_line(etf_info, price_data, fund_data)
            lines.append(line)
            logger.info(f"[ETF XRAY] Processed {ticker}")

    if not lines:
        logger.error("[ETF XRAY] No ETF data fetched.")
        return

    etf_body = "\n".join(lines)
    message = (
        f"📊 *ETF Xray — {time_str}*\n"
        f"_US ETF Snapshot · Powered by EODHD_\n"
        f"{etf_body}\n\n"
        f"_GQ FinXray US · gquants.com_"
    )

    if send_telegram(message):
        save_etf_xray_alert(message)
        logger.info("[ETF XRAY] ETF Xray sent and saved.")
    else:
        logger.error("[ETF XRAY] Failed to send ETF Xray.")


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "morning":
            run_morning_roundup()
        elif cmd == "evening":
            run_evening_roundup()
        elif cmd == "etf":
            run_etf_xray()
        else:
            print("Usage: python news_roundup.py [morning|evening|etf]")
    else:
        print("Usage: python news_roundup.py [morning|evening|etf]")
        print("Running all three for testing...")
        run_morning_roundup()
        run_etf_xray()
        run_evening_roundup()
