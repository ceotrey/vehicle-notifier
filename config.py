"""
config.py — configuration.

Secrets (Slack bot token, Supabase key) are read from environment variables so
this file contains NO secrets and is safe to commit. Non-secret values (channel
IDs, intervals, patterns) are kept here as defaults but can be overridden by env.

Local development: put secrets in a `.env` file (gitignored) — python-dotenv
loads it automatically. Production (Railway): set the same variables in the
service's Variables tab.

Required env vars:
    SLACK_BOT_TOKEN, SUPABASE_KEY
Optional (have sensible defaults below):
    SUPABASE_URL, GMAIL_ACCOUNT, all SLACK_CHANNEL_* ids
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # load .env for local dev; no-op if the file is absent
except Exception:
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Set it in your .env file (local) or the Railway Variables tab (production)."
        )
    return val


# Gmail
GMAIL_ACCOUNT = os.environ.get("GMAIL_ACCOUNT", "twomack@anderson-auto.net")
GMAIL_CREDENTIALS_FILE = os.path.expanduser("~/Documents/vehicle-notifier-credentials.json")
GMAIL_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Slack — token is secret (env), channel IDs are not (defaults, env-overridable)
SLACK_BOT_TOKEN = _require("SLACK_BOT_TOKEN")
SLACK_CHANNEL_INCOMING = os.environ.get("SLACK_CHANNEL_INCOMING", "C0B6B0FJ5T6")
SLACK_CHANNEL_DELIVERED = os.environ.get("SLACK_CHANNEL_DELIVERED", "C0B64NCT0E9")
SLACK_CHANNEL_WATCHLIST = os.environ.get("SLACK_CHANNEL_WATCHLIST", "C0B69V40BMX")
SLACK_CHANNEL_STATUS = os.environ.get("SLACK_CHANNEL_STATUS", "C0B6EABQC7N")        # #system-alerts
SLACK_CHANNEL_ASSISTANT = os.environ.get("SLACK_CHANNEL_ASSISTANT", "C0B6NTB8UNQ")  # #vehicle-assistant

# Supabase — key is secret (env)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://rqxduqvtjcvcsxoaffir.supabase.co")
SUPABASE_KEY = _require("SUPABASE_KEY")

# NHTSA
NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/{vin}?format=json"

# Monitored senders
SENDER_VEHICHAUL = "hello+autocarrierexpress@vehichaul.com"
SENDER_CENTURION = "noreply@centurionautologistics.com"
MONITORED_SENDERS = {
    SENDER_VEHICHAUL: "AutoCarrier Express",
    SENDER_CENTURION: "Centurion Auto Logistics",
}

# Email subject patterns
VEHICHAUL_PREDELIVERY = "vh pre-delivery notification"
VEHICHAUL_DELIVERY = "vh delivery receipt"
CENTURION_PREDELIVERY = "delivery eta for vehicles"
CENTURION_DELIVERY = "delivery document for load"

# Polling intervals (seconds)
GMAIL_POLL_INTERVAL = 60        # 1 minute
WATCHLIST_POLL_INTERVAL = 5     # 5 seconds — near-instant watch/unwatch response
DM_POLL_INTERVAL = 8            # 8 seconds — DM bot only; headroom against Slack rate limits
HEALTH_CHECK_INTERVAL = 600     # 10 minutes (normal)
HEALTH_CHECK_INTERVAL_ALERT = 70  # 1 min 10 sec — fast recheck after a failure

# VIN regex
VIN_PATTERN = r"[A-HJ-NPR-Z0-9]{17}"
MAX_VINS_PER_EMAIL = 9

# Log file
ERROR_LOG = os.path.join(os.path.dirname(__file__), "errors.log")
