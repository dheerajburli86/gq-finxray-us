"""
heatmap_style.py
GQ FinXray US — shared rendering layer for every heatmap image.

Both the market-wide sector heatmap (Feature 9) and the per-user watchlist
heatmap (Feature 14) render the same visual object: a black header, a grid of
coloured tiles sorted best-to-worst, a footer. Keeping the geometry, the colour
scale and the font loading in one place means the two features cannot drift
apart visually, and a fix to one is a fix to both.

COLOUR MODEL
------------
Colour is assigned by PERCENTILE RANK within the batch being rendered, not by
absolute return ("glocom" scale):

    top 20%      dark green
    20% - 40%    light green
    40% - 60%    neutral / near-white
    60% - 80%    light red
    bottom 20%   dark red

This keeps a heatmap readable on a flat day, where an absolute scale would render
eleven near-identical off-white tiles and communicate nothing.

One guard is layered on top: rank never overrides SIGN. A positive return is
never painted red and a negative return is never painted green — it is demoted to
neutral instead. Without this, a day where every sector is up would paint the two
weakest *gainers* dark red, which reads as a loss and is simply wrong.
"""

import os
import glob
import time
import logging

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ── Geometry (matches the original India FinXray spec) ────────────────────────
IMG_W = 958
COLS = 5
CELL_H = 180
GAP = 12
HEADER_H = 110
FOOTER_H = 36
CELL_W = (IMG_W - (COLS + 1) * GAP) // COLS
CORNER_RADIUS = 10

# ── Palette ───────────────────────────────────────────────────────────────────
BG_COLOR = (10, 10, 10)
TEXT_WHITE = (255, 255, 255)
TEXT_GRAY = (160, 160, 160)
TEXT_DIM = (100, 100, 100)
TEXT_DARK = (20, 20, 20)
RULE_COLOR = (40, 40, 40)

DARK_GREEN = (20, 108, 46)
LIGHT_GREEN = (116, 190, 128)
NEUTRAL = (236, 236, 236)
LIGHT_RED = (232, 132, 132)
DARK_RED = (152, 28, 28)

_GREENS = (DARK_GREEN, LIGHT_GREEN)
_REDS = (LIGHT_RED, DARK_RED)

# (upper percentile bound, colour). Evaluated in order; first match wins.
_BUCKETS = (
    (0.20, DARK_GREEN),
    (0.40, LIGHT_GREEN),
    (0.60, NEUTRAL),
    (0.80, LIGHT_RED),
    (1.01, DARK_RED),
)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/TTF",
    "/Library/Fonts",
)
_BOLD_NAMES = ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf")
_REG_NAMES = ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEATMAP_DIR = os.path.join(BASE_DIR, "media", "heatmaps")


# ── Fonts ─────────────────────────────────────────────────────────────────────
def _find_font(names):
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    # matplotlib ships DejaVu and is usually present even on slim images.
    try:
        import matplotlib
        mpl_dir = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
        for n in names:
            p = os.path.join(mpl_dir, n)
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return None


def _default_font(size):
    """Pillow >= 9.2 can scale the built-in bitmap font; older versions cannot."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            logger.warning("[HEATMAP] Could not load %s at %dpt (%s); using default", path, size, e)
    return _default_font(size)


_SIZES = {
    "title": ("bold", 26),
    "subtitle": ("reg", 13),
    "date": ("reg", 12),
    "name": ("bold", 15),
    "name_sm": ("bold", 13),
    "pct": ("bold", 28),
    "small": ("reg", 11),
    "footer": ("reg", 12),
    "corner": ("reg", 11),
}

_font_cache = None


def load_fonts():
    """Dict of the fonts every heatmap needs. Cached — TrueType parsing is not free."""
    global _font_cache
    if _font_cache is not None:
        return _font_cache

    bold_path = _find_font(_BOLD_NAMES)
    reg_path = _find_font(_REG_NAMES) or bold_path
    if not bold_path:
        logger.warning("[HEATMAP] No DejaVu/Liberation font found; falling back to the "
                       "built-in bitmap font. Image will render but look plain.")

    _font_cache = {
        key: _font(bold_path if kind == "bold" else reg_path, size)
        for key, (kind, size) in _SIZES.items()
    }
    return _font_cache


# ── Colour ────────────────────────────────────────────────────────────────────
def percentile_colors(values):
    """
    Colour per value by percentile rank within `values`, sign-guarded.
    Returns a list of RGB tuples parallel to the input.
    """
    n = len(values)
    if n == 0:
        return []

    # rank[i] = 0 for the best performer, n-1 for the worst.
    order = sorted(range(n), key=lambda i: values[i], reverse=True)
    rank = [0] * n
    for position, idx in enumerate(order):
        rank[idx] = position

    out = []
    for i, v in enumerate(values):
        frac = (rank[i] + 0.5) / n
        color = next(c for bound, c in _BUCKETS if frac < bound)
        out.append(_sign_guard(color, v))
    return out


def _sign_guard(color, value):
    """Never paint a gain red or a loss green; demote to neutral instead."""
    if value > 0 and color in _REDS:
        return NEUTRAL
    if value < 0 and color in _GREENS:
        return NEUTRAL
    if value == 0:
        return NEUTRAL
    return color


def text_color_for(bg_rgb):
    """Dark ink on light tiles, white ink on dark tiles (Rec. 601 luminance)."""
    r, g, b = bg_rgb[:3]
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return TEXT_DARK if luminance > 0.55 else TEXT_WHITE


def _blend(fg, bg, alpha):
    """Flatten a translucent colour onto an opaque one — the image is RGB, and
    passing a 4-tuple straight to draw.text() is not portable across Pillow
    versions."""
    return tuple(int(f * alpha + b * (1 - alpha)) for f, b in zip(fg[:3], bg[:3]))


# ── Canvas ────────────────────────────────────────────────────────────────────
def canvas_height(n_tiles):
    rows = max(1, -(-n_tiles // COLS))
    return HEADER_H + rows * (CELL_H + GAP) + GAP + FOOTER_H


def new_canvas(n_tiles):
    img = Image.new("RGB", (IMG_W, canvas_height(n_tiles)), color=BG_COLOR)
    return img, ImageDraw.Draw(img)


def tile_origin(index):
    """Top-left (x, y) of the index-th tile in the grid."""
    row, col = divmod(index, COLS)
    x = GAP + col * (CELL_W + GAP)
    y = HEADER_H + GAP + row * (CELL_H + GAP)
    return x, y


def draw_header(draw, title, subtitle, timestamp_text, fonts=None):
    fonts = fonts or load_fonts()
    draw.text((IMG_W // 2, 28), title, fill=TEXT_WHITE, font=fonts["title"], anchor="mm")
    draw.text((IMG_W // 2, 60), subtitle, fill=TEXT_GRAY, font=fonts["subtitle"], anchor="mm")
    draw.text((IMG_W // 2, 82), timestamp_text, fill=TEXT_DIM, font=fonts["date"], anchor="mm")
    draw.line([(GAP, HEADER_H - 4), (IMG_W - GAP, HEADER_H - 4)], fill=RULE_COLOR, width=1)


def draw_footer(draw, img_h, text="GQ FinXray US  ·  Powered by FMP  ·  gquants.com", fonts=None):
    fonts = fonts or load_fonts()
    draw.line([(GAP, img_h - FOOTER_H + 2), (IMG_W - GAP, img_h - FOOTER_H + 2)],
              fill=RULE_COLOR, width=1)
    draw.text((IMG_W // 2, img_h - FOOTER_H + 12), text,
              fill=(80, 80, 80), font=fonts["footer"], anchor="mm")


def _fit(draw, text, font, max_width):
    """Trim `text` with an ellipsis until it fits inside max_width pixels."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def draw_tile(draw, index, bg_color, corner_text, title_text, pct_value,
              sub_text=None, fonts=None):
    """
    One heatmap cell. Layout is fixed so sector and watchlist tiles are
    interchangeable at a glance:

        corner_text   small, top-left, dimmed   (rank, or the ETF symbol)
        title_text    bold, centred             (sector name, or the ticker)
        pct_value     large, centred            (the return, signed)
        sub_text      small, centred, bottom    (last price, optional)
    """
    fonts = fonts or load_fonts()
    x, y = tile_origin(index)
    ink = text_color_for(bg_color)
    dim_ink = _blend(ink, bg_color, 0.62)
    inner = CELL_W - 20

    draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=CORNER_RADIUS, fill=bg_color)

    if corner_text:
        draw.text((x + 10, y + 10), _fit(draw, str(corner_text), fonts["corner"], inner),
                  fill=dim_ink, font=fonts["corner"])

    if title_text:
        title_font = fonts["name"]
        if draw.textlength(str(title_text), font=title_font) > inner:
            title_font = fonts["name_sm"]
        draw.text((x + CELL_W // 2, y + 52), _fit(draw, str(title_text), title_font, inner),
                  fill=ink, font=title_font, anchor="mm")

    sign = "+" if pct_value >= 0 else ""
    draw.text((x + CELL_W // 2, y + 105), f"{sign}{pct_value:.2f}%",
              fill=ink, font=fonts["pct"], anchor="mm")

    if sub_text:
        draw.text((x + CELL_W // 2, y + 148), _fit(draw, str(sub_text), fonts["small"], inner),
                  fill=dim_ink, font=fonts["small"], anchor="mm")


# ── Disk ──────────────────────────────────────────────────────────────────────
def ensure_heatmap_dir():
    os.makedirs(HEATMAP_DIR, exist_ok=True)
    return HEATMAP_DIR


def save_image(img, filename):
    """Write into media/heatmaps and return the absolute path."""
    ensure_heatmap_dir()
    path = os.path.join(HEATMAP_DIR, filename)
    img.save(path, format="JPEG", quality=92, optimize=True)
    return path


def cleanup_old_images(max_age_days=7, directory=None):
    """
    Delete generated heatmaps older than max_age_days.

    The watchlist heatmap writes one image PER USER PER SLOT, so an unattended
    box accumulates thousands of JPEGs within a month. Never raises — a failure
    to tidy up must not fail the run that produced the image.
    """
    directory = directory or HEATMAP_DIR
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        for path in glob.glob(os.path.join(directory, "*.jpg")) + \
                    glob.glob(os.path.join(directory, "*.png")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except Exception as e:
        logger.warning("[HEATMAP] Image cleanup failed: %s", e)
    if removed:
        logger.info("[HEATMAP] Cleaned up %d heatmap image(s) older than %d days",
                    removed, max_age_days)
    return removed
