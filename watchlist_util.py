import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def get_watched_tickers():
    """Get all unique tickers from watchlists."""
    try:
        rows = supabase.table("watchlists").select("ticker").execute().data
        return set(r.get("ticker", "").upper() for r in rows if r.get("ticker"))
    except:
        return set()

def log_poller_error(job_name, error, context=None):
    """Log poller errors to Supabase."""
    try:
        supabase.table("poller_error_log").insert({
            "poller_name": job_name,
            "error_message": str(error)[:2000],
            "context": context or {}
        }).execute()
    except:
        pass
