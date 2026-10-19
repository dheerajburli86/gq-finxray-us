"""
gq_bot/data.py
Read-only data layer for the GQ bot.

SCOPE RULE (v1): a user can ask only about tickers on THEIR OWN watchlist.
Every public lookup below goes through `check_scope`, so the LLM layer cannot
reach an out-of-scope ticker by forgetting to ask. Two reasons it lives here and
not in the prompt:
  * FinXray only ingests events for watchlisted tickers (see watchlist_util.py),
    so outside a watchlist there is price data but no "why" to join it to.
  * A prompt instruction is a request; a function that refuses is a guarantee.

Two databases are read: FinXray (users, watchlists, alerts) and the heatmap
(instruments, returns_latest, price_history, us_stock_fundamentals). They may be
the same Supabase project; the HEATMAP_* env vars default to the FinXray ones.

Nothing here writes.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

US_UNIVERSES = ("us_stocks", "us_etfs")
RET_COLUMNS = ("ret_1d", "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_ytd",
               "ret_1y", "ret_5y", "ret_10y", "ret_all")
MAX_EVENT_DAYS = 90
MAX_EVENTS = 15
MAX_HISTORY_DAYS = 400


def make_clients():
    """(finxray, heatmap) Supabase clients from the environment."""
    from supabase import create_client
    fx_url, fx_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    hm_url = os.getenv("HEATMAP_SUPABASE_URL") or fx_url
    hm_key = os.getenv("HEATMAP_SUPABASE_KEY") or fx_key
    if not (fx_url and fx_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
    fx = create_client(fx_url, fx_key)
    hm = fx if (hm_url, hm_key) == (fx_url, fx_key) else create_client(hm_url, hm_key)
    return fx, hm


def _sym(value: str) -> str:
    return (value or "").strip().upper().lstrip("$")


class GQData:
    def __init__(self, finxray, heatmap=None):
        self.fx = finxray
        self.hm = heatmap or finxray

    # ── identity + scope ─────────────────────────────────────────────────────
    def get_user(self, chat_id) -> Optional[Dict[str, Any]]:
        # telegram_chat_id is TEXT in Postgres: always filter on a string.
        rows = (self.fx.table("users").select("id, telegram_username, is_active")
                .eq("telegram_chat_id", str(chat_id)).limit(1).execute().data or [])
        user = rows[0] if rows else None
        return user if user and user.get("is_active", True) else None

    def get_watchlist(self, user_id) -> List[str]:
        rows = (self.fx.table("watchlists").select("ticker")
                .eq("user_id", user_id).execute().data or [])
        return sorted({_sym(r["ticker"]) for r in rows if r.get("ticker")})

    def check_scope(self, user_id, ticker: str) -> Dict[str, Any]:
        t = _sym(ticker)
        wl = self.get_watchlist(user_id)
        if t in wl:
            return {"ok": True, "ticker": t}
        return {"ok": False, "ticker": t, "reason": "not_on_watchlist",
                "watchlist": wl,
                "message": f"{t} is not on your watchlist. Add it with /add {t} "
                           f"and I can answer questions about it."}

    # ── heatmap side: price context ──────────────────────────────────────────
    def _instrument(self, ticker: str) -> Optional[Dict[str, Any]]:
        rows = (self.hm.table("instruments")
                .select("id, symbol, name, universe, sector, industry, market_cap_usd, is_active")
                .eq("symbol", ticker).in_("universe", list(US_UNIVERSES))
                .eq("is_active", True).limit(1).execute().data or [])
        return rows[0] if rows else None

    def price_snapshot(self, user_id, ticker: str) -> Dict[str, Any]:
        scope = self.check_scope(user_id, ticker)
        if not scope["ok"]:
            return scope
        t = scope["ticker"]
        inst = self._instrument(t)
        if not inst:
            return {"ok": False, "ticker": t, "reason": "no_price_data",
                    "message": f"I have no US price data for {t}."}
        rows = (self.hm.table("returns_latest")
                .select("price_usd, as_of, " + ", ".join(RET_COLUMNS))
                .eq("instrument_id", inst["id"]).limit(1).execute().data or [])
        if not rows:
            return {"ok": False, "ticker": t, "reason": "no_returns",
                    "message": f"{t} has no computed returns yet."}
        r = rows[0]
        return {"ok": True, "ticker": t, "name": inst.get("name"),
                "type": "etf" if inst["universe"] == "us_etfs" else "stock",
                "sector": inst.get("sector"), "industry": inst.get("industry"),
                "market_cap_usd": inst.get("market_cap_usd"),
                "price_usd": r.get("price_usd"), "as_of": r.get("as_of"),
                "returns_pct": {k: r.get(k) for k in RET_COLUMNS}}

    def price_history(self, user_id, ticker: str, days: int = 30) -> Dict[str, Any]:
        scope = self.check_scope(user_id, ticker)
        if not scope["ok"]:
            return scope
        t = scope["ticker"]
        inst = self._instrument(t)
        if not inst:
            return {"ok": False, "ticker": t, "reason": "no_price_data",
                    "message": f"I have no US price data for {t}."}
        days = max(1, min(int(days), MAX_HISTORY_DAYS))
        since = (date.today() - timedelta(days=days)).isoformat()
        bars = (self.hm.table("price_history").select("date, close")
                .eq("instrument_id", inst["id"]).gte("date", since)
                .order("date", desc=True).limit(days + 5).execute().data or [])
        bars = sorted(({"date": str(b["date"])[:10], "close": b["close"]} for b in bars),
                      key=lambda b: b["date"])
        return {"ok": True, "ticker": t, "days": days, "bars": bars}

    def fundamentals(self, user_id, ticker: str) -> Dict[str, Any]:
        """Latest 4 quarters + 2 annual periods. Stocks only; ETFs have none."""
        scope = self.check_scope(user_id, ticker)
        if not scope["ok"]:
            return scope
        t = scope["ticker"]
        rows = (self.hm.table("us_stock_fundamentals").select("data, updated_at")
                .eq("ticker", t).limit(1).execute().data or [])
        if not rows:
            return {"ok": False, "ticker": t, "reason": "no_fundamentals",
                    "message": f"I have no fundamentals for {t} (ETFs and some "
                               f"stocks have none)."}
        data = rows[0].get("data") or {}
        return {"ok": True, "ticker": t, "updated_at": rows[0].get("updated_at"),
                "quarterly": (data.get("quarterly") or [])[:4],
                "annual": (data.get("annual") or [])[:2]}

    # ── FinXray side: what happened ──────────────────────────────────────────
    def events(self, user_id, ticker: str, days: int = 14,
               limit: int = MAX_EVENTS) -> Dict[str, Any]:
        scope = self.check_scope(user_id, ticker)
        if not scope["ok"]:
            return scope
        t = scope["ticker"]
        days = max(1, min(int(days), MAX_EVENT_DAYS))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = (self.fx.table("alerts")
                .select("id, summary, impact, source, filing_type, link, created_at, extra")
                .eq("ticker", t).gte("created_at", since)
                .order("created_at", desc=True)
                .limit(max(1, min(int(limit), MAX_EVENTS))).execute().data or [])
        return {"ok": True, "ticker": t, "days": days, "events": [
            {"id": r["id"], "when": r["created_at"], "summary": r["summary"],
             "impact": r.get("impact"), "type": r.get("filing_type"),
             "feature": (r.get("extra") or {}).get("feature_name"),
             "link": r.get("link")} for r in rows]}
