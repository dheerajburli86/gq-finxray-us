"""
heatmap_generator.py
GQ FinXray US — Feature 9. v2.

WHAT WAS WRONG WITH v1
----------------------
1. TILES WERE NEVER SORTED. They rendered in SECTOR_ETFS declaration order
   (XLK, XLF, XLV, XLE, ...), so green and red scattered at random across the
   grid. That is the "jumbled up, no heatmap structure" problem -- a heatmap
   only reads as one when the eye can follow a gradient. The India original
   sorts best-to-worst. One line, and it's the single biggest visual fix here.

2. FAILED FETCHES RENDERED AS REAL DATA. Any ETF whose quote failed was
   appended with change_p = 0.0, which painted a near-white "+0.00%" tile
   claiming the sector was flat when in fact we had no idea. Worse, those
   zeros stayed in the max_abs calculation and dragged the colour scale.
   Missing sectors are now DROPPED and reported in the caption.

3. ONE FMP CALL PER ETF, EVERY RUN. Now served from the shared Massive
   snapshot via market_data, with FMP fallback per ticker only where needed.

4. NO DEDUP GUARD ON THE DAILY RUN. Weekly and monthly both checked
   heatmap_already_sent(); daily didn't, so a process restart between the
   09:30 and 13:00 slots could re-send the same image.

5. COLOUR SCALE COLLAPSED ON QUIET DAYS. Intensity is normalised against the
   largest absolute move, so on a day where everything moved 0.1% the tiles
   were all near-white. A floor keeps quiet days legible.

6. RGBA FILL ON AN RGB CANVAS. The ticker label passed a 4-tuple fill to a
   PIL image opened in "RGB" mode.
"""

import os
import io
import logging
from datetime import datetime, timezone, date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import market_data

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
TEXT_WHITE  = (255, 255, 255)
TEXT_GRAY   = (160, 160, 160)
TEXT_DARK   = (20, 20, 20)

# Below this, normalising against the max move makes every tile near-white.
MIN_COLOR_SCALE = 0.75

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


def _et_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc)


# ── Data ──────────────────────────────────────────────────────────────────────
def fetch_sector_data_daily():
    """Live % change per sector ETF, from the shared market snapshot."""
    rows = market_data.sector_performance(SECTOR_ETFS)
    for r in rows:
        if r["change_p"] is not None:
            logger.info(f"[HEATMAP] {r['ticker']}: {r['change_p']:+.2f}% ({r.get('source')})")
        else:
            logger.warning(f"[HEATMAP] {r['ticker']}: NO DATA — will be omitted from the image")
    return rows


def fetch_sector_data_period(period="WEEKLY"):
    days_back = 7 if period == "WEEKLY" else 30
    return market_data.sector_performance_period(SECTOR_ETFS, days_back)


def prepare_sectors(rows):
    """Drop sectors with no data, then sort best-to-worst.

    The sort is what makes it read as a heatmap: gainers cluster top-left,
    losers bottom-right, and the eye follows one continuous gradient instead
    of hunting through a scatter of red and green.
    """
    usable = [r for r in rows if r.get("change_p") is not None]
    dropped = [r["ticker"] for r in rows if r.get("change_p") is None]
    usable.sort(key=lambda r: r["change_p"], reverse=True)
    if dropped:
        logger.warning(f"[HEATMAP] Omitted (no data): {', '.join(dropped)}")
    return usable, dropped


# ── Colour ────────────────────────────────────────────────────────────────────
def get_tile_color(change_p, max_abs_change):
    scale = max(max_abs_change, MIN_COLOR_SCALE)
    intensity = min(abs(change_p) / scale, 1.0) if scale else 0.0
    neutral = (235, 235, 235)
    if change_p >= 0:
        target = (27, 94, 32)     # deep green
    else:
        target = (127, 0, 0)      # deep red
    return tuple(int(neutral[i] + intensity * (target[i] - neutral[i])) for i in range(3))


def get_text_color(bg_rgb):
    r, g, b = bg_rgb
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return TEXT_DARK if luminance > 0.55 else TEXT_WHITE


# ── Rendering ─────────────────────────────────────────────────────────────────
def generate_heatmap_image(sectors, period="DAILY", omitted=None):
    from PIL import Image, ImageDraw, ImageFont

    n = len(sectors)
    if n == 0:
        return None

    rows = -(-n // COLS)
    CELL_W = (IMG_W - (COLS + 1) * GAP) // COLS
    IMG_H = HEADER_H + rows * (CELL_H + GAP) + GAP + FOOTER_H

    img = Image.new("RGB", (IMG_W, IMG_H), color=BG_COLOR)
    draw = ImageDraw.Draw(img)

    try:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        fb = next((p for p in font_paths if os.path.exists(p)), None)
        fr = fb.replace("-Bold", "") if fb else None
        f_title    = ImageFont.truetype(fb, 26) if fb else ImageFont.load_default()
        f_subtitle = ImageFont.truetype(fr, 13) if fr else ImageFont.load_default()
        f_date     = ImageFont.truetype(fr, 12) if fr else ImageFont.load_default()
        f_name     = ImageFont.truetype(fb, 15) if fb else ImageFont.load_default()
        f_pct      = ImageFont.truetype(fb, 28) if fb else ImageFont.load_default()
        f_ticker   = ImageFont.truetype(fr, 11) if fr else ImageFont.load_default()
        f_footer   = ImageFont.truetype(fr, 12) if fr else ImageFont.load_default()
    except Exception as e:
        logger.warning(f"[HEATMAP] Font load error: {e} — using default")
        f_title = f_subtitle = f_date = f_name = f_pct = f_ticker = f_footer = \
            ImageFont.load_default()

    labels = {"DAILY": "Today", "WEEKLY": "This Week", "MONTHLY": "This Month"}
    period_label = labels.get(period, "Today")
    time_str = _et_now().strftime("%B %d, %Y · %I:%M %p ET")

    draw.text((IMG_W // 2, 28), "GQ FinXray US — Sector Heatmap",
              fill=TEXT_WHITE, font=f_title, anchor="mm")
    draw.text((IMG_W // 2, 60), f"S&P 500 GICS Sectors · {period_label} Performance",
              fill=TEXT_GRAY, font=f_subtitle, anchor="mm")
    draw.text((IMG_W // 2, 82), time_str, fill=(100, 100, 100), font=f_date, anchor="mm")
    draw.line([(GAP, HEADER_H - 4), (IMG_W - GAP, HEADER_H - 4)], fill=(40, 40, 40), width=1)

    max_abs = max((abs(s["change_p"]) for s in sectors), default=1.0)

    for i, sector in enumerate(sectors):
        row, col = divmod(i, COLS)
        x = GAP + col * (CELL_W + GAP)
        y = HEADER_H + GAP + row * (CELL_H + GAP)

        bg = get_tile_color(sector["change_p"], max_abs)
        fg = get_text_color(bg)

        draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=10, fill=bg)
        # Plain RGB tuple -- v1 passed a 4-tuple (RGBA) fill on an RGB canvas.
        draw.text((x + 10, y + 10), sector["ticker"], fill=fg, font=f_ticker)
        draw.text((x + CELL_W // 2, y + 52), sector["name"], fill=fg, font=f_name, anchor="mm")

        sign = "+" if sector["change_p"] >= 0 else ""
        draw.text((x + CELL_W // 2, y + 105), f"{sign}{sector['change_p']:.2f}%",
                  fill=fg, font=f_pct, anchor="mm")

        if sector.get("close"):
            draw.text((x + CELL_W // 2, y + 148), f"${sector['close']:.2f}",
                      fill=fg, font=f_ticker, anchor="mm")

    footer_y = IMG_H - FOOTER_H + 12
    draw.line([(GAP, IMG_H - FOOTER_H + 2), (IMG_W - GAP, IMG_H - FOOTER_H + 2)],
              fill=(40, 40, 40), width=1)
    footer = "GQ FinXray US  ·  gquants.com"
    if omitted:
        footer = f"{len(omitted)} sector(s) unavailable  ·  " + footer
    draw.text((IMG_W // 2, footer_y), footer, fill=(80, 80, 80), font=f_footer, anchor="mm")

    return img


# ── Delivery ──────────────────────────────────────────────────────────────────
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
            timeout=30,
        )
        if r.status_code == 200:
            logger.info("[HEATMAP] Sent to Telegram.")
            return True
        logger.error(f"[HEATMAP] Telegram send failed: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"[HEATMAP] Telegram send error: {e}")
        return False


def _sb():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def heatmap_already_sent(filing_type):
    try:
        today = date.today().isoformat()
        result = _sb().table("alerts").select("id") \
            .eq("ticker", "MARKET").eq("source", "SECTOR_HEATMAP") \
            .eq("filing_type", filing_type) \
            .gte("created_at", f"{today}T00:00:00+00:00").execute()
        return len(result.data) > 0
    except Exception:
        return False


def save_heatmap_record(filing_type, summary):
    try:
        from feature_map import tag_extra
        _sb().table("alerts").insert({
            "ticker": "MARKET", "summary": summary, "impact": "LOW",
            "source": "SECTOR_HEATMAP", "filing_type": filing_type, "delivered": True,
            "extra": tag_extra({}, "SECTOR_HEATMAP", filing_type),
        }).execute()
    except Exception as e:
        logger.error(f"[HEATMAP] Failed to save record: {e}")


def is_last_trading_day_of_week():
    return _et_now().weekday() == 4


def is_last_trading_day_of_month():
    today = _et_now().date()
    return (today + timedelta(days=1)).month != today.month


# ── Entry points ──────────────────────────────────────────────────────────────
def _run(period, filing_type, fetch_fn, caption_line, guard=True):
    if guard and heatmap_already_sent(filing_type):
        logger.info(f"[HEATMAP] {filing_type} already sent today — skipping.")
        return

    sectors, omitted = prepare_sectors(fetch_fn())
    if not sectors:
        logger.error("[HEATMAP] No usable sector data — nothing to render.")
        return
    if len(sectors) < 6:
        logger.warning(f"[HEATMAP] Only {len(sectors)}/{len(SECTOR_ETFS)} sectors "
                       f"have data — rendering anyway, but coverage is thin.")

    img = generate_heatmap_image(sectors, period=period, omitted=omitted)
    if img is None:
        return

    best, worst = sectors[0], sectors[-1]
    caption = (
        f"📊 *US Sector Heatmap — {caption_line}*\n"
        f"S&P 500 GICS Sectors\n\n"
        f"🟢 Best: {best['name']} {best['change_p']:+.2f}%\n"
        f"🔴 Worst: {worst['name']} {worst['change_p']:+.2f}%\n"
        f"_GQ FinXray US · gquants.com_"
    )

    if send_heatmap_to_telegram(img, caption):
        save_heatmap_record(filing_type, f"US Sector Heatmap ({period}) sent {caption_line}")
        logger.info(f"[HEATMAP] {period} heatmap sent ({len(sectors)} sectors).")


def run_sector_heatmap_daily():
    # Slot label from EASTERN hour, not UTC -- v1 used UTC, so on a Railway
    # box the 09:30 ET run would have been tagged with whatever UTC hour that
    # happened to be.
    filing_type = f"HEATMAP_DAILY_{_et_now().strftime('%H')}"
    _run("DAILY", filing_type, fetch_sector_data_daily,
         _et_now().strftime("%B %d, %Y · %I:%M %p ET"))


def run_sector_heatmap_weekly():
    if not is_last_trading_day_of_week():
        logger.info("[HEATMAP] Not Friday — skipping weekly.")
        return
    _run("WEEKLY", "HEATMAP_WEEKLY", lambda: fetch_sector_data_period("WEEKLY"),
         f"Week of {_et_now().strftime('%B %d, %Y')}")


def run_sector_heatmap_monthly():
    if not is_last_trading_day_of_month():
        logger.info("[HEATMAP] Not last day of month — skipping monthly.")
        return
    _run("MONTHLY", "HEATMAP_MONTHLY", lambda: fetch_sector_data_period("MONTHLY"),
         _et_now().strftime("%B %Y"))


def run_sector_heatmap():
    run_sector_heatmap_daily()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mock = [
        {"ticker": "XLK",  "name": "Technology",    "change_p": 1.82,  "close": 245.30},
        {"ticker": "XLF",  "name": "Financials",    "change_p": 0.54,  "close": 48.20},
        {"ticker": "XLV",  "name": "Healthcare",    "change_p": -0.31, "close": 142.10},
        {"ticker": "XLE",  "name": "Energy",        "change_p": -1.45, "close": 88.50},
        {"ticker": "XLI",  "name": "Industrials",   "change_p": 0.92,  "close": 135.70},
        {"ticker": "XLY",  "name": "Cons. Disc.",   "change_p": 2.31,  "close": 198.40},
        {"ticker": "XLP",  "name": "Cons. Staples", "change_p": -0.18, "close": 79.30},
        {"ticker": "XLU",  "name": "Utilities",     "change_p": 0.43,  "close": 72.10},
        {"ticker": "XLRE", "name": "Real Estate",   "change_p": -0.87, "close": 40.20},
        {"ticker": "XLB",  "name": "Materials",     "change_p": None,  "close": None},
        {"ticker": "XLC",  "name": "Comm. Services", "change_p": 3.12, "close": 88.90},
    ]
    sectors, omitted = prepare_sectors(mock)
    print("Render order:", [f"{s['name']} {s['change_p']:+.2f}%" for s in sectors])
    print("Omitted:", omitted)
    img = generate_heatmap_image(sectors, "DAILY", omitted)
    img.save("/tmp/test_heatmap.png")
    print(f"Saved {img.size[0]}x{img.size[1]}px")
