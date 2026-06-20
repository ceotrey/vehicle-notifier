"""
config.example.py — template configuration.

Copy this file to `config.py` and fill in your own credentials:
    cp config.example.py config.py

`config.py` is gitignored so real secrets never reach the repository.
"""
import os

# Gmail — the inbox to monitor for carrier delivery emails
GMAIL_ACCOUNT = "you@example.com"
GMAIL_CREDENTIALS_FILE = os.path.expanduser("~/Documents/your-oauth-credentials.json")
GMAIL_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Slack — bot token (xoxb-...) and channel IDs
SLACK_BOT_TOKEN = "xoxb-your-slack-bot-token"
SLACK_CHANNEL_INCOMING = "CXXXXXXXX"    # #vehicles-incoming
SLACK_CHANNEL_DELIVERED = "CXXXXXXXX"   # #vehicles-delivered
SLACK_CHANNEL_WATCHLIST = "CXXXXXXXX"   # #vehicle-watchlist
SLACK_CHANNEL_STATUS = "CXXXXXXXX"      # #system-alerts
SLACK_CHANNEL_ASSISTANT = "CXXXXXXXX"   # #vehicle-assistant

# Supabase — project URL and legacy anon key
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_KEY = "your-supabase-anon-key"

# NHTSA vehicle decode API (public, no key required)
NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/{vin}?format=json"

# Monitored email senders → friendly carrier name
SENDER_VEHICHAUL = "hello+autocarrierexpress@vehichaul.com"
SENDER_CENTURION = "noreply@centurionautologistics.com"
MONITORED_SENDERS = {
    SENDER_VEHICHAUL: "AutoCarrier Express",
    SENDER_CENTURION: "Centurion Auto Logistics",
}

# Email subject patterns used to classify pre-delivery vs delivery
VEHICHAUL_PREDELIVERY = "vh pre-delivery notification"
VEHICHAUL_DELIVERY = "vh delivery receipt"
CENTURION_PREDELIVERY = "delivery eta for vehicles"
CENTURION_DELIVERY = "delivery document for load"

# Polling intervals (seconds)
GMAIL_POLL_INTERVAL = 60          # 1 minute
WATCHLIST_POLL_INTERVAL = 5       # near-instant watch/unwatch response
DM_POLL_INTERVAL = 5              # DM bot only (single poller)
HEALTH_CHECK_INTERVAL = 600       # 10 minutes (normal)
HEALTH_CHECK_INTERVAL_ALERT = 70  # fast recheck after a failure

# VIN regex and per-email cap
VIN_PATTERN = r"[A-HJ-NPR-Z0-9]{17}"
MAX_VINS_PER_EMAIL = 9

# Local error log path
ERROR_LOG = os.path.join(os.path.dirname(__file__), "errors.log")
