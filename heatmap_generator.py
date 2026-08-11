"""
heatmap_generator.py
GQ FinXray US — Feature 9, the market-wide sector heatmap.

Renders the eleven S&P 500 GICS sector ETFs as a colour-graded grid and delivers
it as a Telegram photo to every user who has not opted out of market-wide
messages.

WHAT CHANGED, AND WHY
---------------------
1. FIXED SLOT NAMES. `filing_type` used to be built as
   f"HEATMAP_DAILY_{utcnow:%H}" while `schedule` fires on ET local time, so the
   13:00 ET job wrote "HEATMAP_DAILY_18" (or "HEATMAP_DAILY_10" once the label
   and the clock disagreed). feature_map.py knows exactly four heatmap slots, so
   every one of those rows resolved to feature 0, "Unmapped". Each entry point
   now hardcodes its own slot constant. A slot name is an identifier, not a
   timestamp.

2. A REAL TRADING CALENDAR. `is_last_trading_day_of_week()` was
   `weekday() == 4`, which published a "week in review" on Friday 2026-07-03 — a
   market holiday (Independence Day observed) — and never published on Thursday
   2026-07-02, the actual last trading day of that week.
   `is_last_trading_day_of_month()` was "tomorrow is a new month", so whenever a
   month ended on a weekend (2026-10-31 is a Saturday) it published Saturday
   using Friday's stale data and skipped the real last trading day entirely.
   Both now derive from an NYSE holiday calendar.

3. NO ZERO-FILL. Both fetchers used to append {"change_p": 0.0} on any
   exception. That made `if not sectors: return` unreachable and meant a total
   FMP outage published a heatmap reading +0.00% across all eleven tiles — a
   confident, wrong, market-wide message. Failures are now dropped, and if fewer
   than MIN_COVERAGE of the sectors resolve, nothing is published at all.

4. DEDUP ON THE DAILY SLOTS. Weekly and monthly already checked; the daily runs
   did not, so a container restart inside the scheduling minute re-published.

5. NO HARDCODED CHANNEL. Delivery goes through delivery.deliver_photo_sync,
   which applies the same audience rules as every other alert.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import fmp_client
import heatmap_style as hs
from feature_map import tag_extra
from watchlist_util import log_poller_error

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

SOURCE = "SECTOR_HEATMAP"

# The four slots feature_map.py recognises. Nothing else is a valid filing_type
# for this feature — do not derive these from a clock.
SLOT_MIDDAY = "HEATMAP_DAILY_MIDDAY"
SLOT_AFTERNOON = "HEATMAP_DAILY_AFTERNOON"
SLOT_WEEKLY = "HEATMAP_WEEKLY"
SLOT_MONTHLY = "HEATMAP_MONTHLY"

# Publish only if at least this share of sectors returned real data. Eleven
# sectors at 60% means seven must resolve; six or fewer and the picture of "how
# the market did today" is too incomplete to broadcast.
MIN_COVERAGE = 0.60

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


# ══ US market (NYSE/Nasdaq) holiday calendar ══════════════════════════════════
def _easter_sunday(year):
    """Anonymous Gregorian algorithm. Good Friday is Easter Sunday minus two days."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` (Mon=0) of the month, e.g. 3rd Monday of January."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    """Last `weekday` of the month, e.g. last Monday of May."""
    d = date(year, month, 28)
    while (d + timedelta(days=1)).month == month:
        d += timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    """Standard federal shift: Saturday -> preceding Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


_holiday_cache = {}


def market_holidays(year):
    """Set of dates the US equity market is fully closed in `year`."""
    if year in _holiday_cache:
        return _holiday_cache[year]

    days = set()

    # New Year's Day. The exchanges do NOT close the preceding Friday when
    # Jan 1 lands on a Saturday (unlike every other holiday here) — that Friday
    # is a normal, and often the last, trading day of the outgoing year.
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        days.add(date(year, 1, 2))
    elif ny.weekday() != 5:
        days.add(ny)

    days.add(_nth_weekday(year, 1, 0, 3))          # MLK Jr. Day, 3rd Monday Jan
    days.add(_nth_weekday(year, 2, 0, 3))          # Presidents' Day, 3rd Monday Feb
    days.add(_easter_sunday(year) - timedelta(days=2))   # Good Friday
    days.add(_last_weekday(year, 5, 0))            # Memorial Day, last Monday May
    days.add(_observed(date(year, 6, 19)))         # Juneteenth
    days.add(_observed(date(year, 7, 4)))          # Independence Day
    days.add(_nth_weekday(year, 9, 0, 1))          # Labor Day, 1st Monday Sep
    days.add(_nth_weekday(year, 11, 3, 4))         # Thanksgiving, 4th Thursday Nov
    days.add(_observed(date(year, 12, 25)))        # Christmas

    _holiday_cache[year] = days
    return days


def is_trading_day(d=None):
    d = d or datetime.now(ET).date()
    return d.weekday() < 5 and d not in market_holidays(d.year)


def is_last_trading_day_of_week(d=None):
    """True if `d` is a trading day and no later day in its Mon-Sun week is."""
    d = d or datetime.now(ET).date()
    if not is_trading_day(d):
        return False
    for delta in range(1, 7 - d.weekday()):
        if is_trading_day(d + timedelta(days=delta)):
            return False
    return True


def is_last_trading_day_of_month(d=None):
    """True if `d` is a trading day and no later day in its month is."""
    d = d or datetime.now(ET).date()
    if not is_trading_day(d):
        return False
    probe = d + timedelta(days=1)
    while probe.month == d.month:
        if is_trading_day(probe):
            return False
        probe += timedelta(days=1)
    return True


def previous_trading_day(d):
    probe = d - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(probe):
            return probe
        probe -= timedelta(days=1)
    return probe


def _previous_week_close_date(d):
    """Last trading day of the week before `d`'s week."""
    return previous_trading_day(d - timedelta(days=d.weekday()))


def _previous_month_close_date(d):
    """Last trading day of the month before `d`'s month."""
    return previous_trading_day(d.replace(day=1))


# ══ Data ══════════════════════════════════════════════════════════════════════
def fetch_sector_data_daily():
    """
    Intraday/most-recent % change per sector ETF.

    Only sectors that actually returned a quote are included. A missing sector is
    a hole in the picture, not a 0.00% sector.
    """
    results = []
    for etf in SECTOR_ETFS:
        ticker = etf["ticker"]
        try:
            q = fmp_client.get_quote(ticker)
        except Exception as e:
            logger.error("[HEATMAP] Quote failed for %s: %s", ticker, e)
            continue
        if not q:
            logger.warning("[HEATMAP] No quote returned for %s", ticker)
            continue
        try:
            change_p = float(q.get("changePercentage") or 0.0)
            close = float(q.get("price") or 0.0)
        except (TypeError, ValueError):
            logger.warning("[HEATMAP] Unparseable quote for %s: %r", ticker, q)
            continue
        results.append({"ticker": ticker, "name": etf["name"],
                        "change_p": round(change_p, 2), "close": close})
        logger.info("[HEATMAP] %s: %+.2f%%", ticker, change_p)
    return results


def fetch_sector_data_period(period, as_of=None):
    """
    % change from the previous week's / previous month's closing trading day to
    the latest available close.

    The comparison point is a real trading date taken from the market calendar,
    not an array index and not "N calendar days ago". Historical rows contain
    only trading days, so indexing back 30 rows is really ~6 weeks, and
    subtracting 30 calendar days can land on a weekend with no row at all.
    """
    as_of = as_of or datetime.now(ET).date()
    if period == "WEEKLY":
        baseline = _previous_week_close_date(as_of)
    else:
        baseline = _previous_month_close_date(as_of)

    # Buffer the window so a holiday cluster around the baseline still leaves a
    # trading row on or before it.
    from_date = (baseline - timedelta(days=12)).isoformat()
    to_date = as_of.isoformat()
    baseline_iso = baseline.isoformat()

    results = []
    for etf in SECTOR_ETFS:
        ticker = etf["ticker"]
        try:
            data = fmp_client.get_historical_prices(ticker, from_date, to_date)
        except Exception as e:
            logger.error("[HEATMAP] %s history failed for %s: %s", period, ticker, e)
            continue
        if not data or len(data) < 2:
            logger.warning("[HEATMAP] %s: insufficient history for %s", period, ticker)
            continue

        try:
            rows = sorted((r for r in data if r.get("date") and r.get("close") is not None),
                          key=lambda r: r["date"], reverse=True)
            if len(rows) < 2:
                continue
            current_close = float(rows[0]["close"])
            base_row = next((r for r in rows if r["date"][:10] <= baseline_iso), rows[-1])
            base_close = float(base_row["close"])
            if base_close <= 0:
                continue
            change_p = (current_close - base_close) / base_close * 100.0
        except (TypeError, ValueError, KeyError) as e:
            logger.warning("[HEATMAP] %s: bad history rows for %s (%s)", period, ticker, e)
            continue

        results.append({"ticker": ticker, "name": etf["name"],
                        "change_p": round(change_p, 2), "close": current_close})
    return results


def _coverage_ok(sectors, slot):
    """Gate on data completeness. Returns True when it is safe to publish."""
    got, want = len(sectors), len(SECTOR_ETFS)
    if got >= want * MIN_COVERAGE:
        if got < want:
            logger.warning("[HEATMAP] %s: publishing with %d/%d sectors", slot, got, want)
        return True

    msg = (f"Only {got}/{want} sector ETFs resolved "
           f"(need {MIN_COVERAGE:.0%}); refusing to publish {slot}")
    logger.error("[HEATMAP] %s", msg)
    log_poller_error("heatmap_generator", slot, msg,
                     {"resolved": got, "expected": want,
                      "tickers": [s["ticker"] for s in sectors]})
    return False


# ══ Rendering ═════════════════════════════════════════════════════════════════
PERIOD_LABELS = {"DAILY": "Today", "WEEKLY": "This Week", "MONTHLY": "This Month"}
PERIOD_PHRASE = {"DAILY": "Today's", "WEEKLY": "This Week's", "MONTHLY": "This Month's"}


def generate_heatmap_image(sectors, period="DAILY"):
    """Sorted best-to-worst, percentile-coloured grid. Returns a PIL image."""
    sectors = sorted(sectors, key=lambda s: s["change_p"], reverse=True)
    colors = hs.percentile_colors([s["change_p"] for s in sectors])
    phrase = PERIOD_PHRASE.get(period, PERIOD_PHRASE["DAILY"])

    img, draw = hs.new_canvas(len(sectors))
    fonts = hs.load_fonts()

    hs.draw_header(
        draw,
        "GQ FinXray US — Sector Heatmap",
        f"S&P 500 GICS Sectors · {phrase} Performance",
        datetime.now(ET).strftime("%B %d, %Y · %I:%M %p ET"),
        fonts,
    )

    for i, sector in enumerate(sectors):
        sub = f"${sector['close']:.2f}" if sector.get("close", 0) > 0 else None
        hs.draw_tile(draw, i, colors[i], sector["ticker"], sector["name"],
                     sector["change_p"], sub, fonts)

    hs.draw_footer(draw, img.size[1], fonts=fonts)
    return img


def _summary_line(sectors, period_label):
    """
    Human-readable digest stored on the alerts row.

    This is what a user or an operator sees when reading the alerts table, so it
    has to carry the actual content of the heatmap. "Sector Heatmap sent at
    13:00" says nothing and cannot be audited after the image expires.
    """
    ranked = sorted(sectors, key=lambda s: s["change_p"], reverse=True)
    fmt = lambda s: f"{s['name']} {s['change_p']:+.2f}%"
    top = " · ".join(fmt(s) for s in ranked[:3])
    bottom = " · ".join(fmt(s) for s in ranked[-3:][::-1])
    advancing = sum(1 for s in ranked if s["change_p"] > 0)
    return (f"US Sector Heatmap ({period_label}) — {advancing}/{len(ranked)} sectors advancing. "
            f"Leaders: {top}. Laggards: {bottom}.")


def _caption(sectors, headline, period):
    ranked = sorted(sectors, key=lambda s: s["change_p"], reverse=True)
    best, worst = ranked[0], ranked[-1]
    advancing = sum(1 for s in ranked if s["change_p"] > 0)
    phrase = PERIOD_PHRASE.get(period, PERIOD_PHRASE["DAILY"])
    return (
        f"<b>{headline}</b>\n"
        f"S&amp;P 500 GICS Sectors · {phrase} Performance\n\n"
        f"Best: <b>{best['name']} {best['change_p']:+.2f}%</b>\n"
        f"Worst: <b>{worst['name']} {worst['change_p']:+.2f}%</b>\n"
        f"{advancing} of {len(ranked)} sectors advancing\n\n"
        f"<i>GQ FinXray US · Feature 9 — Sector Heatmap</i>"
    )


# ══ Persistence ═══════════════════════════════════════════════════════════════
def _supabase():
    import os
    from supabase import create_client
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def heatmap_already_sent(filing_type):
    """
    Has this exact slot already published today (ET)?

    Applied to the daily slots too. Without it a restart inside the scheduled
    minute re-runs the job and posts the same image twice.
    """
    try:
        midnight_et = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        res = (_supabase().table("alerts")
               .select("id")
               .eq("source", SOURCE)
               .eq("filing_type", filing_type)
               .gte("created_at", midnight_et.isoformat())
               .limit(1)
               .execute())
        return bool(res.data)
    except Exception as e:
        # Fail open: a dedup-lookup outage should not silence the feature. A
        # duplicate heatmap is a far smaller problem than a missing one.
        logger.warning("[HEATMAP] Dedup check failed for %s (%s); proceeding", filing_type, e)
        return False


def save_heatmap_record(filing_type, summary, sectors, sent, failed):
    """
    One alerts row per published heatmap, already marked delivered.

    delivered=True is load-bearing: the image has been sent by this module, so
    the text fan-out in delivery.py must never pick the row up and re-post it as
    an image-less text stub. That is exactly what happened ~12 times in
    production when these rows were written with delivered=False.
    """
    try:
        extra = tag_extra({
            "sent": sent,
            "failed": failed,
            "sector_count": len(sectors),
            "sectors": [{"ticker": s["ticker"], "name": s["name"], "change_p": s["change_p"]}
                        for s in sorted(sectors, key=lambda s: s["change_p"], reverse=True)],
        }, SOURCE, filing_type)

        _supabase().table("alerts").insert({
            "ticker": "MARKET",
            "summary": summary,
            "impact": "LOW",
            "source": SOURCE,
            "filing_type": filing_type,
            "delivered": True,
            "extra": extra,
        }).execute()
    except Exception as e:
        logger.error("[HEATMAP] Failed to save alerts row for %s: %s", filing_type, e)
        log_poller_error("heatmap_generator", filing_type, e, {"stage": "save_record"})


# ══ Shared run body ═══════════════════════════════════════════════════════════
def _publish(slot, period, headline, sectors):
    period_label = PERIOD_LABELS.get(period, "Today")

    img = generate_heatmap_image(sectors, period=period)
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
    path = hs.save_image(img, f"sector_heatmap_{period}_{stamp}.jpg")

    caption = _caption(sectors, headline, period)

    # No alert_id is passed: the alerts row is written only after a successful
    # send, so it does not exist yet. Per-recipient ledger rows are skipped for
    # market-wide heatmaps by design — the alerts row carries sent/failed counts.
    from delivery import deliver_photo_sync
    sent, failed = deliver_photo_sync(path, caption, SOURCE, slot)

    if sent == 0:
        logger.warning("[HEATMAP] %s reached 0 recipients (failed=%d); not recording", slot, failed)
        if failed:
            log_poller_error("heatmap_generator", slot,
                             f"heatmap send failed for all {failed} recipients",
                             {"path": path})
        return

    save_heatmap_record(slot, _summary_line(sectors, period_label), sectors, sent, failed)
    logger.info("[HEATMAP] %s published to %d user(s), %d failure(s)", slot, sent, failed)


def _run_daily(slot, headline):
    """Body shared by the midday and afternoon slots. Never raises."""
    try:
        if heatmap_already_sent(slot):
            logger.info("[HEATMAP] %s already sent today; skipping", slot)
            return
        if not is_trading_day():
            logger.info("[HEATMAP] %s skipped — market closed today", slot)
            return

        sectors = fetch_sector_data_daily()
        if not _coverage_ok(sectors, slot):
            return

        _publish(slot, "DAILY", headline, sectors)
    except Exception as e:
        logger.exception("[HEATMAP] %s failed: %s", slot, e)
        log_poller_error("heatmap_generator", slot, e)
    finally:
        hs.cleanup_old_images()


# ══ Entry points (imported by main.py — names are a contract) ═════════════════
def run_sector_heatmap_midday():
    _run_daily(SLOT_MIDDAY, "US Sector Heatmap — Midday")


def run_sector_heatmap_afternoon():
    _run_daily(SLOT_AFTERNOON, "US Sector Heatmap — Afternoon")


def run_sector_heatmap_weekly():
    try:
        today = datetime.now(ET).date()
        if not is_last_trading_day_of_week(today):
            logger.info("[HEATMAP] %s skipped — %s is not the last trading day of its week",
                        SLOT_WEEKLY, today)
            return
        if heatmap_already_sent(SLOT_WEEKLY):
            logger.info("[HEATMAP] %s already sent today; skipping", SLOT_WEEKLY)
            return

        sectors = fetch_sector_data_period("WEEKLY", as_of=today)
        if not _coverage_ok(sectors, SLOT_WEEKLY):
            return

        _publish(SLOT_WEEKLY, "WEEKLY",
                 f"US Sector Heatmap — Week ending {today.strftime('%B %d, %Y')}", sectors)
    except Exception as e:
        logger.exception("[HEATMAP] %s failed: %s", SLOT_WEEKLY, e)
        log_poller_error("heatmap_generator", SLOT_WEEKLY, e)
    finally:
        hs.cleanup_old_images()


def run_sector_heatmap_monthly():
    try:
        today = datetime.now(ET).date()
        if not is_last_trading_day_of_month(today):
            logger.info("[HEATMAP] %s skipped — %s is not the last trading day of its month",
                        SLOT_MONTHLY, today)
            return
        if heatmap_already_sent(SLOT_MONTHLY):
            logger.info("[HEATMAP] %s already sent today; skipping", SLOT_MONTHLY)
            return

        sectors = fetch_sector_data_period("MONTHLY", as_of=today)
        if not _coverage_ok(sectors, SLOT_MONTHLY):
            return

        _publish(SLOT_MONTHLY, "MONTHLY",
                 f"US Sector Heatmap — {today.strftime('%B %Y')}", sectors)
    except Exception as e:
        logger.exception("[HEATMAP] %s failed: %s", SLOT_MONTHLY, e)
        log_poller_error("heatmap_generator", SLOT_MONTHLY, e)
    finally:
        hs.cleanup_old_images()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Calendar regressions that shipped to production.
    for d in (date(2026, 7, 2), date(2026, 7, 3), date(2026, 10, 30), date(2026, 10, 31)):
        print(f"{d} {d:%a}  trading={is_trading_day(d)!s:<5} "
              f"last_of_week={is_last_trading_day_of_week(d)!s:<5} "
              f"last_of_month={is_last_trading_day_of_month(d)}")
