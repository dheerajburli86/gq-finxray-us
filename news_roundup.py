"""
news_roundup.py
GQ FinXray US — Feature 10 (Morning & Evening News Roundup + ETF Xray).
Rewritten on FMP (was EODHD) for the ETF Xray price/fundamentals lookups.
The DeepInfra AI-digest logic for the news roundup itself is unchanged —
that part never depended on EODHD.
"""

import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client
import os
import re
import requests

import fmp_client
from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DEEPINFRA_API_KEY = os.getenv("DEEPINFRA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
DEEPINFRA_MODEL = "google/gemini-2.5-flash"

ETF_UNIVERSE = [
    {"ticker": "SPY",  "name": "S&P 500 ETF",                    "category": "Broad Market"},
    {"ticker": "DIA",  "name": "Dow Jones Industrial Avg ETF",   "category": "Broad Market"},
    {"ticker": "IWM",  "name": "Russell 2000 ETF",                "category": "Small Cap"},
    {"ticker": "QQQ",  "name": "NASDAQ 100 ETF",                  "category": "Technology"},
    {"ticker": "XLK",  "name": "Technology Select ETF",           "category": "Technology"},
    {"ticker": "XLC",  "name": "Communication Services Select ETF", "category": "Communication Services"},
    {"ticker": "XLF",  "name": "Financial Select ETF",            "category": "Finance"},
    {"ticker": "XLE",  "name": "Energy Select ETF",                "category": "Energy"},
    {"ticker": "XLV",  "name": "Health Care Select ETF",           "category": "Healthcare"},
    {"ticker": "XLI",  "name": "Industrial Select ETF",            "category": "Industrials"},
    {"ticker": "XLY",  "name": "Consumer Discretionary Select ETF", "category": "Consumer Discretionary"},
    {"ticker": "XLP",  "name": "Consumer Staples Select ETF",      "category": "Consumer Staples"},
    {"ticker": "XLB",  "name": "Materials Select ETF",             "category": "Materials"},
    {"ticker": "XLU",  "name": "Utilities Select ETF",             "category": "Utilities"},
    {"ticker": "XLRE", "name": "Real Estate Select ETF",           "category": "Real Estate"},
    {"ticker": "GLD",  "name": "Gold ETF",                         "category": "Commodities"},
    {"ticker": "SLV",  "name": "Silver ETF",                       "category": "Commodities"},
    {"ticker": "TLT",  "name": "20+ Year Treasury ETF",            "category": "Bonds"},
    {"ticker": "HYG",  "name": "High Yield Corporate Bond ETF",    "category": "Bonds"},
    {"ticker": "EFA",  "name": "MSCI EAFE (Developed Intl) ETF",   "category": "International"},
    {"ticker": "EEM",  "name": "MSCI Emerging Markets ETF",        "category": "International"},
]


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def call_deepinfra(prompt, max_tokens=500):
    headers = {"Authorization": f"Bearer {DEEPINFRA_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": DEEPINFRA_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    try:
        r = requests.post(DEEPINFRA_URL, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            text = (r.json()["choices"][0]["message"]["content"] or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
        logger.error(f"[DEEPINFRA] Error {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"[DEEPINFRA] Request failed: {e}")
        return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.warning("[TELEGRAM] Token or channel ID not set.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHANNEL_ID, "text": message, "parse_mode": "Markdown"}, timeout=15)
        if r.status_code == 200:
            logger.info("[TELEGRAM] Message sent successfully.")
            return True
        logger.error(f"[TELEGRAM] Failed: {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        logger.error(f"[TELEGRAM] Send failed: {e}")
        return False


def roundup_already_sent(roundup_type):
    sb = get_supabase()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = sb.table("alerts").select("id").eq("ticker", "MARKET").eq("source", "NEWS_ROUNDUP") \
        .eq("filing_type", roundup_type).gte("created_at", f"{today}T00:00:00+00:00").execute()
    return len(result.data) > 0


def save_roundup_alert(roundup_type, summary):
    sb = get_supabase()
    sb.table("alerts").insert({
        "ticker": "MARKET", "summary": summary, "impact": "LOW", "source": "NEWS_ROUNDUP",
        "filing_type": roundup_type, "delivered": True,
        "extra": tag_extra({"roundup_type": roundup_type}, "NEWS_ROUNDUP", roundup_type),
        "filing_url": None
    }).execute()


def etf_xray_already_sent():
    sb = get_supabase()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = sb.table("alerts").select("id").eq("ticker", "MARKET").eq("source", "ETF_XRAY") \
        .gte("created_at", f"{today}T00:00:00+00:00").execute()
    return len(result.data) > 0


def save_etf_xray_alert(summary):
    sb = get_supabase()
    sb.table("alerts").insert({
        "ticker": "MARKET", "summary": summary, "impact": "LOW", "source": "ETF_XRAY",
        "filing_type": "ETF_XRAY", "delivered": True,
        "extra": tag_extra({}, "ETF_XRAY", "ETF_XRAY"), "filing_url": None
    }).execute()


# ── News Roundup (unchanged — never depended on EODHD) ────────────────────────
def fetch_recent_news(hours_back):
    sb = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    try:
        result = sb.table("raw_filings").select("ticker, raw_text, extra, filed_at, source") \
            .gte("filed_at", cutoff).eq("filing_type", "NEWS").order("filed_at", desc=True).limit(60).execute()
        return result.data or []
    except Exception as e:
        logger.error(f"[ROUNDUP] Failed to fetch news: {e}")
        return []


def build_news_digest(articles):
    sector_articles = {}
    for article in articles:
        extra = article.get("extra") or {}
        sector = extra.get("sector", "MARKET")
        title = extra.get("title", "") or article.get("raw_text", "")[:100]
        if not title:
            continue
        sector_articles.setdefault(sector, []).append(title)

    lines = []
    for sector, titles in sector_articles.items():
        lines.append(f"[{sector}]")
        for t in titles[:8]:
            lines.append(f"- {t[:120]}")
    return "\n".join(lines)


def generate_ai_roundup(news_text, period_label):
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
    if result:
        lines = [l.strip() for l in result.split("\n") if l.strip()]
        if lines:
            last_line = lines[-1]
            if last_line and last_line[-1] not in ".!?":
                lines = lines[:-1]
            result = "\n".join(lines)
    return result


def run_morning_roundup():
    logger.info("[ROUNDUP] Starting morning news roundup...")
    if roundup_already_sent("MORNING_ROUNDUP"):
        logger.info("[ROUNDUP] Morning roundup already sent today, skipping.")
        return

    articles = fetch_recent_news(hours_back=8)
    if not articles:
        logger.info("[ROUNDUP] No recent articles found for morning roundup.")
        return

    news_text = build_news_digest(articles)
    ai_digest = generate_ai_roundup(news_text, "Morning Market Briefing")
    if not ai_digest:
        titles = [(a.get("extra") or {}).get("title", a.get("raw_text", "")[:80]) for a in articles[:7] if a]
        ai_digest = "\n".join([f"• {t}" for t in titles if t])

    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    message = f"🌅 *Morning Market Briefing — {time_str}*\n\n{ai_digest}\n\n_GQ FinXray US · gquants.com_"

    if send_telegram(message):
        save_roundup_alert("MORNING_ROUNDUP", message)
        logger.info("[ROUNDUP] Morning roundup sent and saved.")


def run_evening_roundup():
    logger.info("[ROUNDUP] Starting evening news roundup...")
    if roundup_already_sent("EVENING_ROUNDUP"):
        logger.info("[ROUNDUP] Evening roundup already sent today, skipping.")
        return

    articles = fetch_recent_news(hours_back=10)
    if not articles:
        logger.info("[ROUNDUP] No recent articles found for evening roundup.")
        return

    news_text = build_news_digest(articles)
    ai_digest = generate_ai_roundup(news_text, "Evening Market Wrap")
    if not ai_digest:
        titles = [(a.get("extra") or {}).get("title", a.get("raw_text", "")[:80]) for a in articles[:7] if a]
        ai_digest = "\n".join([f"• {t}" for t in titles if t])

    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    message = f"🌙 *Evening Market Wrap — {time_str}*\n\n{ai_digest}\n\n_GQ FinXray US · gquants.com_"

    if send_telegram(message):
        save_roundup_alert("EVENING_ROUNDUP", message)
        logger.info("[ROUNDUP] Evening roundup sent and saved.")


# ── ETF Xray — now on FMP ─────────────────────────────────────────────────────
def format_etf_line(etf_info, quote, etf_info_data):
    ticker = etf_info["ticker"]
    name = etf_info["name"]
    category = etf_info["category"]

    if not quote:
        return f"• *{ticker}* ({name}) — data unavailable"

    price = quote.get("price", 0) or 0
    change_p = quote.get("changePercentage", 0) or 0
    volume = quote.get("volume", 0) or 0

    arrow = "🟢" if change_p >= 0 else "🔴"
    sign = "+" if change_p >= 0 else ""

    expense_ratio = ""
    net_assets = ""
    if etf_info_data:
        expense_ratio_val = etf_info_data.get("expenseRatio") or etf_info_data.get("expense_ratio")
        net_assets_val = etf_info_data.get("aum") or etf_info_data.get("netAssets")
        if expense_ratio_val:
            expense_ratio = f" · Exp: {expense_ratio_val}"
        if net_assets_val and float(net_assets_val or 0) > 0:
            na = float(net_assets_val)
            if na >= 1_000_000_000:
                net_assets = f" · AUM: ${na/1_000_000_000:.1f}B"
            elif na >= 1_000_000:
                net_assets = f" · AUM: ${na/1_000_000:.0f}M"

    vol_str = f"{int(volume):,}" if volume else "N/A"

    return (
        f"{arrow} *{ticker}* — ${price:.2f} ({sign}{change_p:.2f}%)\n"
        f"   _{name} · {category}{expense_ratio}{net_assets}_\n"
        f"   Vol: {vol_str}"
    )


def run_etf_xray():
    logger.info("[ETF XRAY] Starting ETF Xray (FMP)...")
    if etf_xray_already_sent():
        logger.info("[ETF XRAY] Already sent today, skipping.")
        return

    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y · %H:%M UTC")
    lines = []

    categories = {}
    for etf in ETF_UNIVERSE:
        categories.setdefault(etf["category"], []).append(etf)

    for category, etfs in categories.items():
        lines.append(f"\n*{category}*")
        for etf_info in etfs:
            ticker = etf_info["ticker"]
            quote = fmp_client.get_quote(ticker)
            etf_info_data = fmp_client.get_etf_info(ticker)
            line = format_etf_line(etf_info, quote, etf_info_data)
            lines.append(line)
            logger.info(f"[ETF XRAY] Processed {ticker}")

    if not lines:
        logger.error("[ETF XRAY] No ETF data fetched.")
        return

    etf_body = "\n".join(lines)
    message = (
        f"📊 *ETF Xray — {time_str}*\n"
        f"_US ETF Snapshot · Powered by FMP_\n"
        f"{etf_body}\n\n"
        f"_GQ FinXray US · gquants.com_"
    )

    if send_telegram(message):
        save_etf_xray_alert(message)
        logger.info("[ETF XRAY] ETF Xray sent and saved.")


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
        print("Running all three for testing...")
        run_morning_roundup()
        run_etf_xray()
        run_evening_roundup()
