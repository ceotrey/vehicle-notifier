import logging
from datetime import datetime, timezone
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY, ERROR_LOG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _log_error(msg: str):
    with open(ERROR_LOG, "a") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    logger.error(msg)


def get_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def init_tables():
    """Create all required tables via Supabase SQL if they don't exist."""
    client = get_client()

    tables_sql = [
        """
        CREATE TABLE IF NOT EXISTS vehicles_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            vin text,
            year text,
            make text,
            model text,
            trim text,
            color text,
            load_number text,
            carrier text,
            delivery_eta text,
            status text,
            email_sender text,
            processed_at timestamptz DEFAULT now(),
            watchlist_matched boolean DEFAULT false
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            search_term text,
            type text,
            added_at timestamptz DEFAULT now(),
            matched_vin text,
            matched_at timestamptz
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS processed_emails (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email_id text UNIQUE,
            processed_at timestamptz DEFAULT now()
        );
        """,
    ]

    for sql in tables_sql:
        try:
            client.rpc("execute_sql", {"query": sql}).execute()
        except Exception:
            # Supabase free tier may not expose execute_sql; tables may already exist.
            pass

    logger.info("Supabase tables initialized (or already exist).")


def is_email_processed(email_id: str) -> bool:
    try:
        client = get_client()
        result = client.table("processed_emails").select("id").eq("email_id", email_id).execute()
        return len(result.data) > 0
    except Exception as e:
        _log_error(f"is_email_processed error: {e}")
        return False


def mark_email_processed(email_id: str):
    try:
        client = get_client()
        client.table("processed_emails").insert({"email_id": email_id}).execute()
    except Exception as e:
        _log_error(f"mark_email_processed error: {e}")


def log_vehicle(
    vin: str,
    year: str,
    make: str,
    model: str,
    trim: str,
    color: str,
    load_number: str,
    carrier: str,
    delivery_eta: str,
    status: str,
    email_sender: str,
    watchlist_matched: bool = False,
):
    try:
        client = get_client()
        client.table("vehicles_log").insert({
            "vin": vin,
            "year": year,
            "make": make,
            "model": model,
            "trim": trim,
            "color": color,
            "load_number": load_number,
            "carrier": carrier,
            "delivery_eta": delivery_eta,
            "status": status,
            "email_sender": email_sender,
            "watchlist_matched": watchlist_matched,
        }).execute()
    except Exception as e:
        _log_error(f"log_vehicle error for VIN {vin}: {e}")


def get_watchlist() -> list[dict]:
    try:
        client = get_client()
        result = client.table("watchlist").select("*").is_("matched_at", "null").execute()
        return result.data or []
    except Exception as e:
        _log_error(f"get_watchlist error: {e}")
        return []


def add_watchlist_entry(search_term: str, entry_type: str, user_id: str = "", vehicle_description: str = "", dm_channel_id: str = "") -> bool:
    try:
        client = get_client()
        payload = {"search_term": search_term, "type": entry_type}
        if user_id:
            payload["user_id"] = user_id
        if vehicle_description:
            payload["vehicle_description"] = vehicle_description
        if dm_channel_id:
            payload["dm_channel_id"] = dm_channel_id
        client.table("watchlist").insert(payload).execute()
        return True
    except Exception as e:
        _log_error(f"add_watchlist_entry error: {e}")
        return False


def remove_watchlist_entry(search_term: str) -> bool:
    try:
        client = get_client()
        result = (
            client.table("watchlist")
            .delete()
            .ilike("search_term", search_term)
            .is_("matched_at", "null")
            .execute()
        )
        return len(result.data) > 0
    except Exception as e:
        _log_error(f"remove_watchlist_entry error: {e}")
        return False


def clear_watchlist() -> int:
    """Remove all active (unmatched) watchlist entries. Returns count removed."""
    try:
        client = get_client()
        result = client.table("watchlist").delete().is_("matched_at", "null").execute()
        return len(result.data)
    except Exception as e:
        _log_error(f"clear_watchlist error: {e}")
        return 0


def get_vehicle_status(vin: str) -> list[dict]:
    """Returns all vehicles_log rows for a VIN, newest first."""
    try:
        client = get_client()
        result = (
            client.table("vehicles_log")
            .select("*")
            .eq("vin", vin.upper())
            .order("processed_at", desc=True)
            .execute()
        )
        return result.data or []
    except Exception as e:
        _log_error(f"get_vehicle_status error for VIN {vin}: {e}")
        return []


def search_vehicles(description: str, limit: int = 10) -> list[dict]:
    """Fuzzy search vehicles_log by concatenated year+make+model+trim fields."""
    try:
        client = get_client()
        # Search across year, make, model, trim fields using ilike
        terms = description.strip().lower().split()
        query = client.table("vehicles_log").select("*").order("processed_at", desc=True)
        for term in terms:
            # Filter where any of the key fields contain the term
            query = query.or_(
                f"year.ilike.%{term}%,make.ilike.%{term}%,model.ilike.%{term}%,trim.ilike.%{term}%"
            )
        result = query.limit(limit).execute()
        return result.data or []
    except Exception as e:
        _log_error(f"search_vehicles error: {e}")
        return []


def get_recent_vehicles(limit: int = 10) -> list[dict]:
    """Returns the last N processed vehicles from vehicles_log."""
    try:
        client = get_client()
        result = (
            client.table("vehicles_log")
            .select("*")
            .order("processed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        _log_error(f"get_recent_vehicles error: {e}")
        return []


def any_vin_logged_today(vins: list, status: str) -> bool:
    """Returns True if ANY of these VINs are already logged with this status today.
    Used to detect duplicate emails (same load sent multiple times by carrier)."""
    from datetime import date
    try:
        client = get_client()
        today = date.today().isoformat()
        for vin in vins:
            result = (
                client.table("vehicles_log")
                .select("id")
                .eq("vin", vin.upper())
                .eq("status", status)
                .gte("processed_at", today)
                .limit(1)
                .execute()
            )
            if result.data:
                return True
        return False
    except Exception as e:
        _log_error(f"any_vin_logged_today error: {e}")
        return False  # fail open — better to send a duplicate than miss a real one


def mark_watchlist_matched(entry_id: str, vin: str):
    try:
        client = get_client()
        client.table("watchlist").update({
            "matched_vin": vin,
            "matched_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", entry_id).execute()
    except Exception as e:
        _log_error(f"mark_watchlist_matched error: {e}")
