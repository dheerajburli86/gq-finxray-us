"""
watchlist_heatmap.py
GQ FinXray US — Feature 14, the personal watchlist heatmap.

One image per user, showing only the stocks that user actually watches, sent
only to that user.

WHAT THIS REPLACES
------------------
The previous version was a placeholder: ten hardcoded tickers with invented
returns, a `chat_id` argument it never used, no watchlist lookup, no quote
fetch, and no delivery — it wrote a JPEG to disk and returned. Nothing about it
was per-user except the function signature.

DESIGN NOTES
------------
* ONE QUOTE PASS FOR THE WHOLE SYSTEM. Watchlists overlap heavily (AAPL is on
  most of them), so quotes are fetched once for the union of every user's
  tickers and each user's image is rendered from that shared map. The obvious
  loop — for each user, for each ticker, fetch — costs O(users x tickers) FMP
  calls per slot and will exhaust the plan.

* THE 75% COVERAGE RULE, inherited from the India system: if fewer than three
  quarters of a user's tickers resolved to a price, the heatmap would misrepresent
  their portfolio by omission, so it is skipped and the reason logged rather than
  sent with holes in it.

* LARGE WATCHLISTS ARE TRIMMED, NOT TRUNCATED. Above 25 tickers the image shows
  the 12 best and 12 worst, and the caption says so explicitly. Silently dropping
  the middle would leave a user believing they were looking at everything.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import fmp_client
import heatmap_style as hs
from feature_map import tag_extra
from heatmap_generator import is_trading_day
from watchlist_util import log_poller_error

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

SOURCE = "WATCHLIST_HEATMAP"

# The two slots feature_map.py recognises for feature 14.
SLOT_MIDDAY = "HEATMAP_WATCHLIST_MIDDAY"
SLOT_EOD = "HEATMAP_WATCHLIST_EOD"

MIN_COVERAGE = 0.75      # share of a user's tickers that must resolve to a price
TRIM_THRESHOLD = 25      # above this many resolved tickers, show extremes only
TRIM_HEAD = 12           # top gainers kept
TRIM_TAIL = 12           # bottom losers kept


def _supabase():
    import os
    from supabase import create_client
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── Data ──────────────────────────────────────────────────────────────────────
def build_quote_map(tickers):
    """
    {TICKER: {"change_p": float, "price": float, "name": str}} for the union of
    every watchlist, in a single batched pass.
    """
    tickers = sorted({(t or "").upper() for t in tickers if t})
    if not tickers:
        return {}

    try:
        quotes = fmp_client.get_quotes(tickers)
    except Exception as e:
        logger.error("[WL-HEATMAP] Batch quote fetch failed: %s", e)
        log_poller_error("watchlist_heatmap", "build_quote_map", e,
                         {"ticker_count": len(tickers)})
        return {}

    out = {}
    for ticker, q in (quotes or {}).items():
        if not q:
            continue
        try:
            change_p = float(q.get("changePercentage") or 0.0)
            price = float(q.get("price") or 0.0)
        except (TypeError, ValueError):
            logger.warning("[WL-HEATMAP] Unparseable quote for %s", ticker)
            continue
        if price <= 0:
            # A zero price means the symbol did not really resolve; counting it
            # as covered would let the 75% rule pass on empty data.
            continue
        out[ticker.upper()] = {
            "ticker": ticker.upper(),
            "change_p": round(change_p, 2),
            "price": price,
            "name": q.get("name") or ticker.upper(),
        }

    logger.info("[WL-HEATMAP] Resolved %d/%d distinct tickers", len(out), len(tickers))
    return out


def select_user_rows(tickers, quote_map):
    """
    (rows, coverage, omitted) for one user.

    rows      sorted best -> worst, already trimmed for display
    coverage  share of the user's tickers that resolved to a price
    omitted   how many mid-table names the trim removed (0 when nothing trimmed)
    """
    unique = sorted({(t or "").upper() for t in tickers if t})
    if not unique:
        return [], 0.0, 0

    resolved = [quote_map[t] for t in unique if t in quote_map]
    coverage = len(resolved) / len(unique)
    resolved.sort(key=lambda r: r["change_p"], reverse=True)

    omitted = 0
    if len(resolved) > TRIM_THRESHOLD:
        omitted = len(resolved) - (TRIM_HEAD + TRIM_TAIL)
        resolved = resolved[:TRIM_HEAD] + resolved[-TRIM_TAIL:]

    return resolved, coverage, omitted


# ── Rendering ─────────────────────────────────────────────────────────────────
SLOT_LABELS = {SLOT_MIDDAY: "Midday", SLOT_EOD: "Market Close"}


def generate_watchlist_image(rows, username, slot, omitted=0):
    """Percentile-coloured grid of one user's watchlist. Returns a PIL image."""
    colors = hs.percentile_colors([r["change_p"] for r in rows])
    img, draw = hs.new_canvas(len(rows))
    fonts = hs.load_fonts()

    who = f"{username}'s Watchlist" if username else "Your Watchlist"
    subtitle = f"{len(rows)} holding{'s' if len(rows) != 1 else ''} · {SLOT_LABELS.get(slot, '')} Performance"
    if omitted:
        subtitle += f" · top {TRIM_HEAD} and bottom {TRIM_TAIL} shown"

    hs.draw_header(
        draw,
        f"GQ FinXray US — {who}",
        subtitle,
        datetime.now(ET).strftime("%B %d, %Y · %I:%M %p ET"),
        fonts,
    )

    for i, row in enumerate(rows):
        hs.draw_tile(draw, i, colors[i], f"#{i + 1}", row["ticker"],
                     row["change_p"], f"${row['price']:.2f}", fonts)

    hs.draw_footer(draw, img.size[1], fonts=fonts)
    return img


def _summary_line(rows, slot, total_watched, omitted):
    """Digest stored on the alerts row — readable without the image."""
    best, worst = rows[0], rows[-1]
    gainers = sum(1 for r in rows if r["change_p"] > 0)
    losers = sum(1 for r in rows if r["change_p"] < 0)
    text = (f"Watchlist heatmap ({SLOT_LABELS.get(slot, slot)}) over {len(rows)} of "
            f"{total_watched} watched tickers — {gainers} up, {losers} down. "
            f"Best: {best['ticker']} {best['change_p']:+.2f}%. "
            f"Worst: {worst['ticker']} {worst['change_p']:+.2f}%.")
    if omitted:
        text += f" {omitted} mid-range holdings omitted from the image."
    return text


def _caption(rows, slot, total_watched, omitted):
    best, worst = rows[0], rows[-1]
    gainers = sum(1 for r in rows if r["change_p"] > 0)
    losers = sum(1 for r in rows if r["change_p"] < 0)
    flat = len(rows) - gainers - losers

    lines = [
        f"<b>Your Watchlist Heatmap — {SLOT_LABELS.get(slot, '')}</b>",
        f"{datetime.now(ET).strftime('%B %d, %Y · %I:%M %p ET')}",
        "",
        f"Best: <b>{best['ticker']} {best['change_p']:+.2f}%</b> (${best['price']:.2f})",
        f"Worst: <b>{worst['ticker']} {worst['change_p']:+.2f}%</b> (${worst['price']:.2f})",
        f"{gainers} up · {losers} down" + (f" · {flat} flat" if flat else ""),
    ]
    if omitted:
        lines += ["", (f"Showing your top {TRIM_HEAD} and bottom {TRIM_TAIL} of "
                       f"{total_watched} watched tickers — {omitted} mid-range "
                       f"holdings were left out to keep the image readable.")]
    lines += ["", "<i>GQ FinXray US · Feature 14 — Watchlist Heatmap</i>"]
    return "\n".join(lines)


# ── Persistence ───────────────────────────────────────────────────────────────
def already_sent_today(slot):
    """
    Set of user_ids that already received this slot today (ET).

    Fetched in one query rather than per user: the alerts row carries the
    recipient in extra.user_id, so a restart mid-fan-out resumes without
    re-sending to the users already covered.
    """
    try:
        midnight_et = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = (_supabase().table("alerts")
                .select("extra")
                .eq("source", SOURCE)
                .eq("filing_type", slot)
                .gte("created_at", midnight_et.isoformat())
                .execute()).data or []
        return {(r.get("extra") or {}).get("user_id") for r in rows
                if (r.get("extra") or {}).get("user_id")}
    except Exception as e:
        # Fail open — a dedup outage should not silence the feature.
        logger.warning("[WL-HEATMAP] Dedup lookup failed for %s (%s); proceeding", slot, e)
        return set()


def save_user_record(user, slot, summary, rows, total_watched, omitted, sent, failed):
    """
    One alerts row per user per slot, already marked delivered.

    delivered=True matters: the image has been sent from here, and the text
    fan-out in delivery.py must not pick the row up and re-post it as an
    image-less stub.
    """
    try:
        extra = tag_extra({
            "user_id": user["user_id"],
            "username": user.get("username"),
            "rendered": len(rows),
            "watched": total_watched,
            "omitted": omitted,
            "sent": sent,
            "failed": failed,
            "holdings": [{"ticker": r["ticker"], "change_p": r["change_p"]} for r in rows],
        }, SOURCE, slot)

        _supabase().table("alerts").insert({
            "ticker": "WATCHLIST",
            "summary": summary,
            "impact": "LOW",
            "source": SOURCE,
            "filing_type": slot,
            "delivered": True,
            "extra": extra,
        }).execute()
    except Exception as e:
        logger.error("[WL-HEATMAP] Failed to save alerts row for user %s: %s",
                     user.get("user_id"), e)
        log_poller_error("watchlist_heatmap", slot, e,
                         {"stage": "save_record", "user_id": user.get("user_id")})


# ── Run body ──────────────────────────────────────────────────────────────────
def _run(slot):
    """Shared body for both slots. Never raises."""
    stats = {"sent": 0, "skipped_empty": 0, "skipped_coverage": 0, "skipped_dup": 0, "failed": 0}
    try:
        if not is_trading_day():
            logger.info("[WL-HEATMAP] %s skipped — market closed today", slot)
            return

        from delivery import list_users_with_watchlists, deliver_photo_sync

        pairs = list_users_with_watchlists()
        if not pairs:
            logger.info("[WL-HEATMAP] %s: no active users with a watchlist", slot)
            return

        done = already_sent_today(slot)
        pending = [(u, t) for u, t in pairs if u["user_id"] not in done]
        stats["skipped_dup"] = len(pairs) - len(pending)
        if not pending:
            logger.info("[WL-HEATMAP] %s already sent to all %d user(s)", slot, len(pairs))
            return

        # One quote pass across every distinct ticker any pending user watches.
        universe = {t for _, tickers in pending for t in tickers}
        quote_map = build_quote_map(universe)
        if not quote_map:
            msg = f"No quotes resolved for {len(universe)} watchlist tickers; skipping {slot}"
            logger.error("[WL-HEATMAP] %s", msg)
            log_poller_error("watchlist_heatmap", slot, msg, {"ticker_count": len(universe)})
            return

        stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")

        for user, tickers in pending:
            uid = user["user_id"]
            if not tickers:
                stats["skipped_empty"] += 1
                logger.info("[WL-HEATMAP] User %s skipped — empty watchlist", uid)
                continue

            rows, coverage, omitted = select_user_rows(tickers, quote_map)
            total_watched = len({t.upper() for t in tickers if t})

            if not rows or coverage < MIN_COVERAGE:
                stats["skipped_coverage"] += 1
                logger.info("[WL-HEATMAP] User %s skipped — only %.0f%% of %d watched tickers "
                            "resolved to a price (need %.0f%%)",
                            uid, coverage * 100, total_watched, MIN_COVERAGE * 100)
                continue

            try:
                img = generate_watchlist_image(rows, user.get("username"), slot, omitted)
                path = hs.save_image(img, f"watchlist_heatmap_{slot}_{uid}_{stamp}.jpg")
            except Exception as e:
                stats["failed"] += 1
                logger.error("[WL-HEATMAP] Render failed for user %s: %s", uid, e)
                log_poller_error("watchlist_heatmap", slot, e,
                                 {"stage": "render", "user_id": uid})
                continue

            caption = _caption(rows, slot, total_watched, omitted)
            sent, failed = deliver_photo_sync(path, caption, SOURCE, slot, user_id=uid)

            if sent == 0:
                stats["failed"] += 1
                logger.warning("[WL-HEATMAP] User %s: photo not delivered (failed=%d)", uid, failed)
                continue

            save_user_record(user, slot,
                             _summary_line(rows, slot, total_watched, omitted),
                             rows, total_watched, omitted, sent, failed)
            stats["sent"] += 1

        logger.info("[WL-HEATMAP] %s — sent=%d skipped(dup)=%d skipped(empty)=%d "
                    "skipped(coverage)=%d failed=%d",
                    slot, stats["sent"], stats["skipped_dup"], stats["skipped_empty"],
                    stats["skipped_coverage"], stats["failed"])
    except Exception as e:
        logger.exception("[WL-HEATMAP] %s failed: %s", slot, e)
        log_poller_error("watchlist_heatmap", slot, e, stats)
    finally:
        hs.cleanup_old_images()


# ── Entry points (imported by main.py — names are a contract) ─────────────────
def run_watchlist_heatmap_midday():
    _run(SLOT_MIDDAY)


def run_watchlist_heatmap_eod():
    _run(SLOT_EOD)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_watchlist_heatmap_midday()
