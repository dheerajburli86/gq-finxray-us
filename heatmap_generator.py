"""
heatmap_generator.py
GQ FinXray US — Feature 9. Rewritten on FMP (was EODHD).
Matches original India FinXray spec (958px wide, 5 columns, 180px cells,
12px gap, black header, color intensity normalized against max absolute
return, auto text-color by luminance). Only the data-fetching functions
changed vendor; the PIL rendering pipeline is untouched.
"""

import os
import io
import logging
from datetime import datetime, timezone, date, timedelta
from dotenv import load_dotenv

import fmp_client

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

IMG_W       = 958
COLS        = 5
CELL_H      = 180
GAP         = 12
HEADER_H    = 110
FOOTER_H    = 36
BG_COLOR    = (10, 10, 10)
HEADER_BG   = (10, 10, 10)
TEXT_WHITE  = (255, 255, 255)
TEXT_GRAY   = (160, 160, 160)
TEXT_DARK   = (20, 20, 20)

SECTOR_ETFS = [
    {"ticker": "XLK",  "name": "Technology"},
    {"ticker": "XLF",  "name": "Financials"},
    {"ticker": "XLV",  "name": "Healthcare"},
    {"ticker": "XLE",  "name": "Energy"},
    {"ticker": "XLI",  "name": "Industrials"},
    {"ticker": "XLY",  "name": "Cons. Disc."},
    {"ticker": "XLP",  "name": "Cons. Staples"},
    {"ticker": "XLU",  "name": "Utilities"},
    {"ticker": "XLRE", "name": "Real Estate"},
    {"ticker": "XLB",  "name": "Materials"},
    {"ticker": "XLC",  "name": "Comm. Services"},
]


# ── Data fetching (FMP) ────────────────────────────────────────────────────────
def fetch_sector_data_daily():
    """Live % change for all sector ETFs from FMP's quote endpoint."""
    results = []
    for etf in SECTOR_ETFS:
        ticker = etf["ticker"]
        try:
            q = fmp_client.get_quote(ticker)
            if q:
                change_p = float(q.get("changePercentage", 0) or 0)
                close = float(q.get("price", 0) or 0)
                results.append({"ticker": ticker, "name": etf["name"], "change_p": change_p, "close": close})
                logger.info(f"[HEATMAP] {ticker}: {change_p:+.2f}%")
            else:
                results.append({"ticker": ticker, "name": etf["name"], "change_p": 0.0, "close": 0.0})
        except Exception as e:
            logger.error(f"[HEATMAP] Failed to fetch {ticker}: {e}")
            results.append({"ticker": ticker, "name": etf["name"], "change_p": 0.0, "close": 0.0})
    return results


def fetch_sector_data_period(period="WEEKLY"):
    """
    % change for weekly/monthly period via FMP historical-price-eod/full.
    WEEKLY: current close vs close ~7 calendar days ago. MONTHLY: vs ~30
    calendar days ago.

    IMPORTANT: `data` only contains TRADING days (no weekends/holidays), so
    using `days_back` as a raw array INDEX (as this used to) silently drifts
    the comparison point later than intended -- 7 trading-day-index entries
    back is actually ~9-10 calendar days, and 30 trading-day-index entries
    back is actually ~6 calendar weeks, not 1 month. Instead, look up the
    trading day whose date falls on/before the actual target calendar date.
    """
    days_back = 7 if period == "WEEKLY" else 30
    # Wider lookback buffer so a trading day on/before the target date is
    # guaranteed to be in range even across a long weekend/holiday cluster.
    from_date = (date.today() - timedelta(days=days_back + 15)).isoformat()
    to_date = date.today().isoformat()
    target_date = (date.today() - timedelta(days=days_back)).isoformat()
    results = []

    for etf in SECTOR_ETFS:
        ticker = etf["ticker"]
        try:
            data = fmp_client.get_historical_prices(ticker, from_date, to_date)
            if data and len(data) >= 2:
                data = sorted(data, key=lambda r: r.get("date", ""), reverse=True)
                current_close = float(data[0]["close"])
                compare_row = next((row for row in data if row.get("date", "") <= target_date), data[-1])
                compare_close = float(compare_row["close"])
                change_p = ((current_close - compare_close) / compare_close) * 100
                results.append({"ticker": ticker, "name": etf["name"], "change_p": round(change_p, 2), "close": current_close})
            else:
                results.append({"ticker": ticker, "name": etf["name"], "change_p": 0.0, "close": 0.0})
        except Exception as e:
            logger.error(f"[HEATMAP] {period} fetch failed for {ticker}: {e}")
            results.append({"ticker": ticker, "name": etf["name"], "change_p": 0.0, "close": 0.0})

    return results


# ── Color helpers ─────────────────────────────────────────────────────────────
def get_tile_color(change_p, max_abs_change):
    intensity = 0.0 if max_abs_change == 0 else min(abs(change_p) / max_abs_change, 1.0)
    neutral = (235, 235, 235)
    if change_p >= 0:
        r = int(neutral[0] + intensity * (27 - neutral[0]))
        g = int(neutral[1] + intensity * (94 - neutral[1]))
        b = int(neutral[2] + intensity * (32 - neutral[2]))
    else:
        r = int(neutral[0] + intensity * (127 - neutral[0]))
        g = int(neutral[1] + intensity * (0 - neutral[1]))
        b = int(neutral[2] + intensity * (0 - neutral[2]))
    return (r, g, b)


def get_text_color(bg_rgb):
    r, g, b = bg_rgb
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return TEXT_DARK if luminance > 0.55 else TEXT_WHITE


# ── Image generation (unchanged rendering pipeline) ───────────────────────────
def generate_heatmap_image(sectors, period="DAILY"):
    from PIL import Image, ImageDraw, ImageFont

    n = len(sectors)
    rows = -(-n // COLS)
    CELL_W = (IMG_W - (COLS + 1) * GAP) // COLS
    IMG_H = HEADER_H + rows * (CELL_H + GAP) + GAP + FOOTER_H

    img = Image.new("RGB", (IMG_W, IMG_H), color=BG_COLOR)
    draw = ImageDraw.Draw(img)

    try:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/app/.venv/lib/python3.13/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
        ]
        font_path_bold = next((p for p in font_paths if os.path.exists(p)), None)
        font_path_reg = font_path_bold.replace("-Bold", "") if font_path_bold else None

        f_title    = ImageFont.truetype(font_path_bold, 26) if font_path_bold else ImageFont.load_default()
        f_subtitle = ImageFont.truetype(font_path_reg, 13)  if font_path_reg  else ImageFont.load_default()
        f_date     = ImageFont.truetype(font_path_reg, 12)  if font_path_reg  else ImageFont.load_default()
        f_name     = ImageFont.truetype(font_path_bold, 15) if font_path_bold else ImageFont.load_default()
        f_pct      = ImageFont.truetype(font_path_bold, 28) if font_path_bold else ImageFont.load_default()
        f_ticker   = ImageFont.truetype(font_path_reg, 11)  if font_path_reg  else ImageFont.load_default()
        f_footer   = ImageFont.truetype(font_path_reg, 12)  if font_path_reg  else ImageFont.load_default()
    except Exception as e:
        logger.warning(f"[HEATMAP] Font load error: {e} — using default")
        f_title = f_subtitle = f_date = f_name = f_pct = f_ticker = f_footer = ImageFont.load_default()

    period_labels = {"DAILY": "Today", "WEEKLY": "This Week", "MONTHLY": "This Month"}
    period_label = period_labels.get(period, "Today")
    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y · %H:%M UTC")

    draw.text((IMG_W // 2, 28), "GQ FinXray US — Sector Heatmap", fill=TEXT_WHITE, font=f_title, anchor="mm")
    draw.text((IMG_W // 2, 60), f"S&P 500 GICS Sectors · {period_label} Performance", fill=TEXT_GRAY, font=f_subtitle, anchor="mm")
    draw.text((IMG_W // 2, 82), time_str, fill=(100, 100, 100), font=f_date, anchor="mm")
    draw.line([(GAP, HEADER_H - 4), (IMG_W - GAP, HEADER_H - 4)], fill=(40, 40, 40), width=1)

    changes = [s["change_p"] for s in sectors]
    max_abs = max((abs(c) for c in changes), default=1.0)

    for i, sector in enumerate(sectors):
        row = i // COLS
        col = i % COLS
        x = GAP + col * (CELL_W + GAP)
        y = HEADER_H + GAP + row * (CELL_H + GAP)

        bg_color = get_tile_color(sector["change_p"], max_abs)
        text_color = get_text_color(bg_color)

        draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=bg_color)
        draw.text((x + 10, y + 10), sector["ticker"], fill=(*text_color[:3], 180), font=f_ticker)
        draw.text((x + CELL_W // 2, y + 52), sector["name"], fill=text_color, font=f_name, anchor="mm")

        sign = "+" if sector["change_p"] >= 0 else ""
        pct_text = f"{sign}{sector['change_p']:.2f}%"
        draw.text((x + CELL_W // 2, y + 105), pct_text, fill=text_color, font=f_pct, anchor="mm")

        if sector["close"] > 0:
            draw.text((x + CELL_W // 2, y + 148), f"${sector['close']:.2f}", fill=text_color, font=f_ticker, anchor="mm")

    footer_y = IMG_H - FOOTER_H + 12
    draw.line([(GAP, IMG_H - FOOTER_H + 2), (IMG_W - GAP, IMG_H - FOOTER_H + 2)], fill=(40, 40, 40), width=1)
    draw.text((IMG_W // 2, footer_y), "GQ FinXray US  ·  Powered by FMP  ·  gquants.com", fill=(80, 80, 80), font=f_footer, anchor="mm")

    return img


# ── Telegram delivery ─────────────────────────────────────────────────────────
def send_heatmap_to_telegram(img, caption):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.warning("[HEATMAP] Telegram credentials not set.")
        return False

    import requests
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "Markdown"},
            files={"photo": ("heatmap.png", buf, "image/png")},
            timeout=30
        )
        if r.status_code == 200:
            logger.info("[HEATMAP] Sent to Telegram successfully.")
            return True
        logger.error(f"[HEATMAP] Telegram send failed: {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        logger.error(f"[HEATMAP] Telegram send error: {e}")
        return False


# ── Supabase dedup ────────────────────────────────────────────────────────────
def heatmap_already_sent(filing_type):
    try:
        from supabase import create_client
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        today = date.today().isoformat()
        result = sb.table("alerts").select("id").eq("ticker", "MARKET").eq("source", "SECTOR_HEATMAP") \
            .eq("filing_type", filing_type).gte("created_at", f"{today}T00:00:00+00:00").execute()
        return len(result.data) > 0
    except Exception:
        return False


def save_heatmap_record(filing_type, summary):
    try:
        from supabase import create_client
        from feature_map import tag_extra
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        sb.table("alerts").insert({
            "ticker": "MARKET", "summary": summary, "impact": "LOW", "source": "SECTOR_HEATMAP",
            "filing_type": filing_type, "delivered": True,
            "extra": tag_extra({}, "SECTOR_HEATMAP", filing_type)
        }).execute()
    except Exception as e:
        logger.error(f"[HEATMAP] Failed to save record: {e}")


def is_last_trading_day_of_week():
    return date.today().weekday() == 4


def is_last_trading_day_of_month():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    return tomorrow.month != today.month


# ── Main entry points ─────────────────────────────────────────────────────────
def run_sector_heatmap_daily():
    logger.info("[HEATMAP] Starting daily sector heatmap...")
    filing_type = f"HEATMAP_DAILY_{datetime.now(timezone.utc).strftime('%H')}"

    sectors = fetch_sector_data_daily()
    if not sectors:
        logger.error("[HEATMAP] No data fetched.")
        return

    img = generate_heatmap_image(sectors, period="DAILY")
    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y · %H:%M UTC")
    caption = f"📊 *US Sector Heatmap — {time_str}*\nS&P 500 GICS Sectors · Today's Performance\n_GQ FinXray US · gquants.com_"

    if send_heatmap_to_telegram(img, caption):
        save_heatmap_record(filing_type, f"US Sector Heatmap sent at {time_str}")
        logger.info("[HEATMAP] Daily heatmap sent.")


def run_sector_heatmap_weekly():
    if not is_last_trading_day_of_week():
        logger.info("[HEATMAP] Not last trading day of week, skipping weekly heatmap.")
        return
    if heatmap_already_sent("HEATMAP_WEEKLY"):
        logger.info("[HEATMAP] Weekly heatmap already sent.")
        return

    logger.info("[HEATMAP] Starting weekly sector heatmap...")
    sectors = fetch_sector_data_period("WEEKLY")
    if not sectors:
        return

    img = generate_heatmap_image(sectors, period="WEEKLY")
    time_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    caption = f"📊 *US Sector Heatmap — Week of {time_str}*\nS&P 500 GICS Sectors · This Week's Performance\n_GQ FinXray US · gquants.com_"

    if send_heatmap_to_telegram(img, caption):
        save_heatmap_record("HEATMAP_WEEKLY", f"Weekly US Sector Heatmap sent {time_str}")
        logger.info("[HEATMAP] Weekly heatmap sent.")


def run_sector_heatmap_monthly():
    if not is_last_trading_day_of_month():
        logger.info("[HEATMAP] Not last trading day of month, skipping monthly heatmap.")
        return
    if heatmap_already_sent("HEATMAP_MONTHLY"):
        logger.info("[HEATMAP] Monthly heatmap already sent.")
        return

    logger.info("[HEATMAP] Starting monthly sector heatmap...")
    sectors = fetch_sector_data_period("MONTHLY")
    if not sectors:
        return

    img = generate_heatmap_image(sectors, period="MONTHLY")
    time_str = datetime.now(timezone.utc).strftime("%B %Y")
    caption = f"📊 *US Sector Heatmap — {time_str}*\nS&P 500 GICS Sectors · This Month's Performance\n_GQ FinXray US · gquants.com_"

    if send_heatmap_to_telegram(img, caption):
        save_heatmap_record("HEATMAP_MONTHLY", f"Monthly US Sector Heatmap sent {time_str}")
        logger.info("[HEATMAP] Monthly heatmap sent.")


def run_sector_heatmap():
    run_sector_heatmap_daily()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mock_sectors = [
        {"ticker": "XLK",  "name": "Technology",     "change_p": 1.82,  "close": 245.30},
        {"ticker": "XLF",  "name": "Financials",      "change_p": 0.54,  "close": 48.20},
        {"ticker": "XLV",  "name": "Healthcare",      "change_p": -0.31, "close": 142.10},
        {"ticker": "XLE",  "name": "Energy",          "change_p": -1.45, "close": 88.50},
        {"ticker": "XLI",  "name": "Industrials",     "change_p": 0.92,  "close": 135.70},
        {"ticker": "XLY",  "name": "Cons. Disc.",     "change_p": 2.31,  "close": 198.40},
        {"ticker": "XLP",  "name": "Cons. Staples",   "change_p": -0.18, "close": 79.30},
        {"ticker": "XLU",  "name": "Utilities",       "change_p": 0.43,  "close": 72.10},
        {"ticker": "XLRE", "name": "Real Estate",     "change_p": -0.87, "close": 40.20},
        {"ticker": "XLB",  "name": "Materials",       "change_p": 0.67,  "close": 93.80},
        {"ticker": "XLC",  "name": "Comm. Services",  "change_p": 3.12,  "close": 88.90},
    ]
    img = generate_heatmap_image(mock_sectors, period="DAILY")
    img.save("/tmp/test_heatmap.png")
    print(f"Test heatmap saved: {img.size[0]}x{img.size[1]}px")
