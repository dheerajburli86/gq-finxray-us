#!/usr/bin/env python3
"""
watchlist_heatmap.py
GQ FinXray US — Generate watchlist performance heatmap with glocom color coding.

Color scheme (based on return percentile):
- Top 20% (0-20th percentile): Dark Green (#006400)
- 20-40%: Light Green (#00AA00)
- 40-60%: White (#FFFFFF)
- 60-80%: Light Red (#FF6666)
- Bottom 20% (80-100th percentile): Dark Red (#CC0000)

Stocks sorted descending by return (best → worst).
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client
from PIL import Image, ImageDraw, ImageFont
import textwrap

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Heatmap settings
HEATMAP_WIDTH = 958
HEADER_HEIGHT = 80
CELL_WIDTH = 170
CELL_HEIGHT = 150
GAP = 12
COLS = 5

# Glocom color scheme (RGB)
COLOR_DARK_GREEN = (0, 100, 0)        # Top 20%
COLOR_LIGHT_GREEN = (0, 170, 0)       # 20-40%
COLOR_WHITE = (240, 240, 240)         # 40-60%
COLOR_LIGHT_RED = (255, 102, 102)     # 60-80%
COLOR_DARK_RED = (204, 0, 0)          # Bottom 20%

COLOR_TEXT_DARK = (255, 255, 255)     # White text
COLOR_TEXT_LIGHT = (0, 0, 0)          # Black text
COLOR_HEADER_BG = (20, 20, 20)        # Dark header
COLOR_HEADER_TEXT = (255, 255, 255)   # White header text
COLOR_BORDER = (100, 100, 100)        # Gray borders


def get_percentile_rank(value, all_values):
    """Calculate percentile rank (0-100) for a value in a list."""
    if not all_values:
        return 50
    sorted_vals = sorted(all_values)
    rank = sum(1 for v in sorted_vals if v <= value) / len(sorted_vals) * 100
    return rank


def get_color_for_return(return_pct, all_returns):
    """Get color based on glocom scheme (percentile rank)."""
    percentile = get_percentile_rank(return_pct, all_returns)

    if percentile <= 20:
        return COLOR_DARK_GREEN, COLOR_TEXT_DARK
    elif percentile <= 40:
        return COLOR_LIGHT_GREEN, COLOR_TEXT_DARK
    elif percentile <= 60:
        return COLOR_WHITE, COLOR_TEXT_LIGHT
    elif percentile <= 80:
        return COLOR_LIGHT_RED, COLOR_TEXT_LIGHT
    else:
        return COLOR_DARK_RED, COLOR_TEXT_DARK


def get_watchlist_performance(chat_id: int, period: str = "DAILY"):
    """
    Fetch watchlist stocks and their performance.

    Returns: list of dicts with ticker, return_pct, sector, exchange
    """
    # Get in-memory watchlist (hardcoded for now)
    # TODO: Integrate with persistent watchlist storage
    return []


def generate_watchlist_heatmap(chat_id: int, period: str = "DAILY"):
    """
    Generate watchlist heatmap image.

    Args:
        chat_id: User's Telegram chat ID
        period: "DAILY", "WEEKLY", or "MONTHLY"

    Returns:
        Path to generated heatmap image or None if watchlist empty
    """

    # For now, use hardcoded watchlist for testing
    # TODO: Fetch from in-memory bot watchlist or DB
    hardcoded_stocks = [
        {"ticker": "AAPL", "return_pct": 5.2, "sector": "Technology", "exchange": "NASDAQ"},
        {"ticker": "MSFT", "return_pct": 3.8, "sector": "Technology", "exchange": "NASDAQ"},
        {"ticker": "NVDA", "return_pct": 8.5, "sector": "Semiconductors", "exchange": "NASDAQ"},
        {"ticker": "TSLA", "return_pct": -2.1, "sector": "Automotive", "exchange": "NASDAQ"},
        {"ticker": "JPM", "return_pct": 1.5, "sector": "Finance", "exchange": "NYSE"},
        {"ticker": "BAC", "return_pct": -1.2, "sector": "Finance", "exchange": "NYSE"},
        {"ticker": "PG", "return_pct": 2.3, "sector": "Consumer", "exchange": "NYSE"},
        {"ticker": "JNJ", "return_pct": 0.8, "sector": "Healthcare", "exchange": "NYSE"},
        {"ticker": "LLY", "return_pct": 12.5, "sector": "Pharma", "exchange": "NYSE"},
        {"ticker": "AMD", "return_pct": 6.7, "sector": "Semiconductors", "exchange": "NASDAQ"},
    ]

    if not hardcoded_stocks:
        logger.warning(f"[HEATMAP] No stocks in watchlist for user {chat_id}")
        return None

    # Sort by return descending (best to worst)
    stocks = sorted(hardcoded_stocks, key=lambda x: x["return_pct"], reverse=True)

    # Extract all returns for percentile calculation
    all_returns = [s["return_pct"] for s in stocks]

    # Calculate grid dimensions
    num_stocks = len(stocks)
    rows = (num_stocks + COLS - 1) // COLS
    total_height = HEADER_HEIGHT + (rows * CELL_HEIGHT) + ((rows - 1) * GAP) + 20

    # Create image
    img = Image.new("RGB", (HEATMAP_WIDTH, total_height), color=(250, 250, 250))
    draw = ImageDraw.Draw(img)

    # Try to load fonts
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        font_cell = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        font_return = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except:
        # Fallback to default font
        font_title = ImageFont.load_default()
        font_header = ImageFont.load_default()
        font_cell = ImageFont.load_default()
        font_return = ImageFont.load_default()

    # Draw header
    draw.rectangle([(0, 0), (HEATMAP_WIDTH, HEADER_HEIGHT)], fill=COLOR_HEADER_BG)

    # Add Finxray logo text
    title_text = "GQ FinXray US — Watchlist Heatmap"
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    draw.text((15, 10), title_text, fill=COLOR_HEADER_TEXT, font=font_title)
    draw.text((15, 45), f"{period} Performance | {time_str}", fill=COLOR_HEADER_TEXT, font=font_header)

    # Draw stock tiles
    for idx, stock in enumerate(stocks):
        row = idx // COLS
        col = idx % COLS

        x = 10 + col * (CELL_WIDTH + GAP)
        y = HEADER_HEIGHT + 10 + row * (CELL_HEIGHT + GAP)

        ticker = stock["ticker"]
        return_pct = stock["return_pct"]

        # Get color based on percentile
        bg_color, text_color = get_color_for_return(return_pct, all_returns)

        # Draw cell background
        draw.rectangle(
            [(x, y), (x + CELL_WIDTH, y + CELL_HEIGHT)],
            fill=bg_color,
            outline=COLOR_BORDER,
            width=1
        )

        # Draw ticker
        draw.text(
            (x + 10, y + 15),
            ticker,
            fill=text_color,
            font=font_cell
        )

        # Draw return percentage
        return_str = f"{return_pct:+.2f}%"
        return_color = (0, 150, 0) if return_pct >= 0 else (200, 0, 0)
        draw.text(
            (x + 10, y + 50),
            return_str,
            fill=return_color,
            font=font_return
        )

        # Draw sector
        sector = stock["sector"][:12]  # Truncate if too long
        draw.text(
            (x + 10, y + 80),
            sector,
            fill=text_color,
            font=font_return
        )

        # Draw exchange
        exchange = stock["exchange"]
        draw.text(
            (x + 10, y + 110),
            exchange,
            fill=text_color,
            font=font_return
        )

    # Save image
    output_dir = "media/heatmaps"
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"watchlist_heatmap_{period.lower()}_{chat_id}_{timestamp}.jpg"
    filepath = os.path.join(output_dir, filename)

    img.save(filepath, quality=95)
    logger.info(f"[HEATMAP] Generated: {filepath}")

    return filepath


def run_watchlist_heatmap_daily():
    """Generate daily watchlist heatmap (for testing)."""
    logger.info("[HEATMAP] Generating daily watchlist heatmap...")

    # For hardcoded test: use chat_id = 0
    filepath = generate_watchlist_heatmap(chat_id=0, period="DAILY")

    if filepath:
        logger.info(f"[HEATMAP] Daily heatmap generated: {filepath}")
    else:
        logger.warning("[HEATMAP] Failed to generate daily heatmap")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_watchlist_heatmap_daily()
