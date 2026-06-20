"""
health_monitor.py

Runs every 30 minutes and checks all external services the Vehicle Notifier
depends on. Posts to #system-alerts (Slack) only when a service changes
state (healthy → broken, or broken → recovered). Silent when everything is fine.
"""
import logging
import threading
import time
from datetime import datetime

import requests

from config import (
    NHTSA_URL,
    HEALTH_CHECK_INTERVAL,
    HEALTH_CHECK_INTERVAL_ALERT,
    SLACK_CHANNEL_STATUS,
)

logger = logging.getLogger(__name__)

# Known-good Toyota VIN used purely as a probe (2022 Camry)
_TEST_VIN = "4T1C11AK8NU661442"
_TOYOTA_STICKER_URL = "https://www.toyota.com/t3Portal/prodpage/getWindowSticker?vin={vin}"

# Tracks last known state for each service so we only alert on changes.
_status: dict = {
    "Gmail API": True,
    "Slack API": True,
    "Supabase": True,
    "NHTSA API": True,
    "Toyota Window Sticker": True,
    "Gmail Thread": True,
    "Watchlist Thread": True,
}
# Consecutive failure counts — alert only after 2 back-to-back failures so brief
# network blips (VPN, WiFi handoff) don't flood #system-alerts.
_failure_counts: dict = {k: 0 for k in _status}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_gmail(gmail_service) -> tuple:
    """Verifies Gmail API is reachable and token is valid."""
    try:
        gmail_service.users().getProfile(userId="me").execute()
        return True, ""
    except Exception as e:
        return False, str(e)


def _check_slack() -> tuple:
    """Calls Slack auth.test to verify bot token and connectivity."""
    try:
        from slack_sdk import WebClient
        from config import SLACK_BOT_TOKEN
        client = WebClient(token=SLACK_BOT_TOKEN)
        resp = client.auth_test()
        if resp.get("ok"):
            return True, ""
        return False, resp.get("error", "auth.test returned not-ok")
    except Exception as e:
        return False, str(e)


def _check_supabase() -> tuple:
    """Queries the processed_emails table to verify Supabase is reachable."""
    try:
        import database
        client = database.get_client()
        client.table("processed_emails").select("id").limit(1).execute()
        return True, ""
    except Exception as e:
        return False, str(e)


def _check_nhtsa() -> tuple:
    """Decodes a known VIN to verify NHTSA API is reachable."""
    try:
        url = NHTSA_URL.format(vin=_TEST_VIN)
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200 and resp.json().get("Results"):
            return True, ""
        return False, f"Unexpected response: HTTP {resp.status_code}"
    except requests.exceptions.ConnectTimeout:
        return False, "Connection timed out — will retry next check."
    except requests.exceptions.ConnectionError:
        return False, "Connection refused — network may be down."
    except Exception as e:
        # Truncate long error messages to the first sentence only
        msg = str(e).split("\n")[0][:120]
        return False, msg


def _check_toyota_sticker() -> tuple:
    """HEAD-requests the Toyota window sticker endpoint.
    200 or 404 both mean the service is up (404 just means VIN not found)."""
    try:
        url = _TOYOTA_STICKER_URL.format(vin=_TEST_VIN)
        resp = requests.head(url, timeout=10, allow_redirects=True)
        if resp.status_code in (200, 404, 302):
            return True, ""
        return False, f"Unexpected HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


def _check_thread(thread: threading.Thread) -> tuple:
    if thread.is_alive():
        return True, ""
    return False, f"Thread '{thread.name}' has stopped unexpectedly"


# ---------------------------------------------------------------------------
# Alert sending
# ---------------------------------------------------------------------------

def _send_alert(service_name: str, error_msg: str, recovered: bool = False):
    """Posts a health alert to #system-alerts. Imported lazily to avoid circular import."""
    try:
        import slack_notifier
        slack_notifier.send_health_alert(service_name, error_msg, recovered=recovered)
    except Exception as e:
        logger.error(f"Failed to send health alert to Slack: {e}")


# ---------------------------------------------------------------------------
# Core health check runner
# ---------------------------------------------------------------------------

def run_health_checks(gmail_service, gmail_thread: threading.Thread, watchlist_thread: threading.Thread) -> bool:
    """
    Runs all checks. Compares results to last known state.
    Posts to Slack only when state changes.
    Returns True if any service is currently down.
    """
    checks = [
        ("Gmail API",             lambda: _check_gmail(gmail_service)),
        ("Slack API",             _check_slack),
        ("Supabase",              _check_supabase),
        ("NHTSA API",             _check_nhtsa),
        ("Toyota Window Sticker", _check_toyota_sticker),
        ("Gmail Thread",          lambda: _check_thread(gmail_thread)),
        ("Watchlist Thread",      lambda: _check_thread(watchlist_thread)),
    ]

    any_down = False
    all_just_recovered = True  # tracks if everything went from down → up this cycle

    for service_name, check_fn in checks:
        try:
            healthy, error = check_fn()
        except Exception as e:
            msg = str(e).split("\n")[0][:120]
            healthy, error = False, msg

        with _lock:
            was_healthy = _status.get(service_name, True)
            fail_count = _failure_counts.get(service_name, 0)

        if not healthy:
            any_down = True
            all_just_recovered = False

        if not healthy and was_healthy:
            # Increment consecutive failure count — only alert after 3 in a row.
            # This prevents brief network blips from flooding #system-alerts.
            new_count = fail_count + 1
            with _lock:
                _failure_counts[service_name] = new_count
            if new_count >= 3:
                logger.error(f"[HEALTH] {service_name} FAILED (x{new_count}): {error}")
                _send_alert(service_name, error, recovered=False)
                with _lock:
                    _status[service_name] = False
            else:
                logger.warning(f"[HEALTH] {service_name} failure #{new_count} — waiting for 3rd before alerting: {error}")

        elif healthy and not was_healthy:
            # Recovered — alert immediately, reset counter
            logger.info(f"[HEALTH] {service_name} RECOVERED")
            _send_alert(service_name, "", recovered=True)
            with _lock:
                _status[service_name] = True
                _failure_counts[service_name] = 0

        elif healthy and was_healthy:
            # Still healthy — reset failure count silently
            with _lock:
                _failure_counts[service_name] = 0
            logger.info(f"[HEALTH] {service_name}: OK")

        else:
            # Still down after already alerting
            with _lock:
                _failure_counts[service_name] = fail_count + 1
            logger.warning(f"[HEALTH] {service_name}: STILL DOWN — {error}")

    # If everything is now healthy and at least one thing recovered this cycle,
    # post a single "all clear" summary message
    if not any_down and all_just_recovered:
        _send_all_clear()

    return any_down


def _send_all_clear():
    """Posts a single 'all systems clear' message after full recovery."""
    try:
        import slack_notifier
        now = datetime.now().strftime("%-I:%M %p")
        slack_notifier._send(
            SLACK_CHANNEL_STATUS,
            f"👍 *All Systems Clear — Vehicle Notifier Running Normally*\n"
            f"Resolved at {now} — all services back online.",
        )
        logger.info("[HEALTH] All systems clear — posted all-clear to Slack.")
    except Exception as e:
        logger.error(f"Failed to send all-clear alert: {e}")


# ---------------------------------------------------------------------------
# Polling loop (runs in its own thread)
# ---------------------------------------------------------------------------

def poll_health(gmail_service, gmail_thread: threading.Thread, watchlist_thread: threading.Thread):
    """Daemon thread target. Runs health checks immediately on start, then on a smart interval.
    Normal interval: 10 minutes. After any failure: 1 min 10 sec for fast recovery detection."""
    logger.info("Health monitor started.")

    # Run immediately on startup
    try:
        any_down = run_health_checks(gmail_service, gmail_thread, watchlist_thread)
    except Exception as e:
        logger.error(f"Health check error on startup: {e}")
        any_down = False

    while True:
        # Use fast interval if anything is currently down, normal interval otherwise
        interval = HEALTH_CHECK_INTERVAL_ALERT if any_down else HEALTH_CHECK_INTERVAL
        logger.info(f"[HEALTH] Next check in {interval // 60}m {interval % 60}s.")
        time.sleep(interval)
        try:
            any_down = run_health_checks(gmail_service, gmail_thread, watchlist_thread)
        except Exception as e:
            logger.error(f"Health check loop error: {e}")
