import logging
import time
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from config import (
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_INCOMING,
    SLACK_CHANNEL_DELIVERED,
    SLACK_CHANNEL_WATCHLIST,
    SLACK_CHANNEL_STATUS,
    SLACK_CHANNEL_ASSISTANT,
)

logger = logging.getLogger(__name__)
_client = WebClient(token=SLACK_BOT_TOKEN)


def _send(channel: str, text: str, retry: bool = True) -> bool:
    """Send a message to a Slack channel. Returns True if successful, False if all attempts fail."""
    try:
        _client.chat_postMessage(channel=channel, text=text)
        return True
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        logger.error(f"Slack send failed: {err}")
        if retry:
            wait = 60 if err == "ratelimited" else 30
            logger.info(f"Retrying Slack send in {wait}s…")
            time.sleep(wait)
            return _send(channel, text, retry=False)
        return False


def send_predelivery(
    load_number: str,
    carrier_name: str,
    vehicle_lines: list[str],
    delivery_eta: str = "",
):
    eta_part = f" | ETA: {delivery_eta}" if delivery_eta else ""
    lines = "\n".join(vehicle_lines)
    count = len(vehicle_lines)
    text = (
        f"🚗💨 Heads up! Vehicles on the Way!\n"
        f"Load #: {load_number}{eta_part}\n"
        f"Carrier: {carrier_name}\n"
        f"{lines}\n"
        f"{count} vehicle(s) inbound"
    )
    _send(SLACK_CHANNEL_INCOMING, text)


def send_delivery(
    load_number: str,
    carrier_name: str,
    vehicle_lines: list[str],
):
    lines = "\n".join(vehicle_lines)
    count = len(vehicle_lines)
    text = (
        f"✅ Vehicles Delivered!\n"
        f"Load #: {load_number}\n"
        f"Carrier: {carrier_name}\n"
        f"{lines}\n"
        f"{count} vehicle(s) arrived on site"
    )
    _send(SLACK_CHANNEL_DELIVERED, text)


def send_watchlist_incoming_alert(search_term: str, vehicle_line: str, user_id: str = ""):
    """Fired when a watched VIN appears in a pre-delivery (incoming) email."""
    tag = f"<@{user_id}> " if user_id else ""
    text = (
        f"{tag}🚗💨 *Your Watched Car is On the Way!*\n"
        f"You were waiting for: *{search_term}*\n"
        f"{vehicle_line}\n"
        f"👀 Still on your watchlist — I'll alert you again when it's delivered."
    )
    _send(SLACK_CHANNEL_WATCHLIST, text)


def send_watchlist_alert(search_term: str, vehicle_line: str, user_id: str = "") -> bool:
    """Fired when a watched VIN appears in a delivery (delivered) email. Returns True if sent."""
    tag = f"<@{user_id}> " if user_id else ""
    text = (
        f"{tag}🚗 *Your Watched Car is Here!* 🚗\n"
        f"You were waiting for: *{search_term}*\n"
        f"{vehicle_line}\n"
        f"✅ Removed from your watchlist automatically."
    )
    return _send(SLACK_CHANNEL_WATCHLIST, text)


def send_watchlist_confirmation(message: str):
    _send(SLACK_CHANNEL_WATCHLIST, message)


def send_health_alert(service_name: str, error_msg: str, recovered: bool = False):
    """Posts a health alert to #system-alerts on failure or recovery."""
    from datetime import datetime
    now = datetime.now().strftime("%-I:%M %p")

    if recovered:
        text = (
            f"✅ *{service_name} — Back Online*\n"
            f"Resolved at {now}\n"
            f"Vehicle Notifier is operating normally."
        )
    else:
        # Provide a useful action hint per service
        action_hints = {
            "Gmail API": "Re-run `python3 main.py` to re-authorize Gmail.",
            "Slack API": "Check the bot token in config.py.",
            "Supabase": "Check Supabase dashboard at supabase.com.",
            "NHTSA API": "NHTSA may be temporarily down — will retry next check.",
            "Toyota Window Sticker": "Toyota portal may be temporarily down — will retry next check.",
            "Gmail Thread": "The Gmail monitor thread crashed. Restart the script.",
            "Watchlist Thread": "The watchlist listener thread crashed. Restart the script.",
        }
        hint = action_hints.get(service_name, "Check the terminal logs for details.")
        text = (
            f"🔴 *SYSTEM ALERT — {service_name}*\n"
            f"Vehicle Notifier detected a problem at {now}.\n"
            f"Error: {error_msg or 'Unknown error'}\n"
            f"Action: {hint}"
        )

    _send(SLACK_CHANNEL_STATUS, text)


def send_extraction_failure(carrier_name: str):
    text = (
        f"⚠️ Received vehicle email from {carrier_name} but could not extract VINs. "
        f"Please check manually."
    )
    _send(SLACK_CHANNEL_INCOMING, text)


def send_assistant_reply(message: str, user_id: str = ""):
    """Post a reply to the #vehicle-assistant channel, tagging the user if provided."""
    if not SLACK_CHANNEL_ASSISTANT:
        return
    tag = f"<@{user_id}> " if user_id else ""
    _send(SLACK_CHANNEL_ASSISTANT, f"{tag}{message}")


def get_recent_assistant_messages(oldest_ts: str = None) -> list[dict]:
    """Fetch messages from the vehicle assistant channel for command parsing."""
    if not SLACK_CHANNEL_ASSISTANT:
        return []
    try:
        kwargs = {"channel": SLACK_CHANNEL_ASSISTANT, "limit": 50}
        if oldest_ts:
            kwargs["oldest"] = oldest_ts
        result = _client.conversations_history(**kwargs)
        messages = result.get("messages", [])
        logger.debug(f"Assistant messages API returned {len(messages)} message(s)")
        return messages
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        logger.error(f"Slack API error fetching assistant messages: {err}")
        if err == "not_in_channel":
            logger.error(f"Bot is not a member of assistant channel {SLACK_CHANNEL_ASSISTANT}. Add the bot to the channel in Slack.")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching assistant messages: {e}")
        return []


def send_dm(channel_id: str, text: str) -> bool:
    """Send a message directly to a DM channel (IM channel ID). Returns True if successful."""
    return _send(channel_id, text)


def send_watchlist_alert_dm(search_term: str, vehicle_line: str, dm_channel_id: str) -> bool:
    """Delivery alert sent to user's DM instead of group channel. Returns True if sent."""
    text = (
        f"🚗 *Your Watched Car is Here!* 🚗\n"
        f"You were waiting for: *{search_term}*\n"
        f"{vehicle_line}\n"
        f"✅ Removed from your watchlist automatically."
    )
    return _send(dm_channel_id, text)


def send_watchlist_incoming_alert_dm(search_term: str, vehicle_line: str, dm_channel_id: str):
    """Incoming (pre-delivery) alert sent to user's DM instead of group channel."""
    text = (
        f"🚗💨 *Your Watched Car is On the Way!*\n"
        f"You were waiting for: *{search_term}*\n"
        f"{vehicle_line}\n"
        f"👀 Still on your watchlist — I'll alert you again when it's delivered."
    )
    _send(dm_channel_id, text)


def get_dm_conversations() -> list[dict]:
    """Returns list of open IM (DM) conversations the bot is part of."""
    try:
        result = _client.conversations_list(types="im", limit=100)
        return result.get("channels", [])
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "ratelimited":
            # Honor Retry-After if present, but cap small so we never wedge the
            # single DM thread — the normal poll interval will retry shortly.
            retry_after = min(int(e.response.headers.get("Retry-After", 3) or 3), 5)
            logger.warning(f"Slack rate limited on conversations.list — waiting {retry_after}s")
            time.sleep(retry_after)
        else:
            logger.error(f"Slack API error listing DM conversations: {err}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error listing DM conversations: {e}")
        return []


def get_dm_messages(channel_id: str, oldest_ts: str = None) -> list[dict]:
    """Fetch messages from a DM channel."""
    try:
        kwargs = {"channel": channel_id, "limit": 50}
        if oldest_ts:
            kwargs["oldest"] = oldest_ts
        result = _client.conversations_history(**kwargs)
        return result.get("messages", [])
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "ratelimited":
            retry_after = min(int(e.response.headers.get("Retry-After", 3) or 3), 5)
            logger.warning(f"Slack rate limited on DM {channel_id} — waiting {retry_after}s")
            time.sleep(retry_after)
        else:
            logger.error(f"Slack API error fetching DM messages from {channel_id}: {err}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching DM messages: {e}")
        return []


def get_recent_watchlist_messages(oldest_ts: str = None) -> list[dict]:
    """Fetch messages from the watchlist channel for command parsing.
    Works for both public and private channels via conversations.history."""
    try:
        kwargs = {"channel": SLACK_CHANNEL_WATCHLIST, "limit": 50}
        if oldest_ts:
            kwargs["oldest"] = oldest_ts
        result = _client.conversations_history(**kwargs)
        messages = result.get("messages", [])
        logger.debug(f"Watchlist messages API returned {len(messages)} message(s) from channel {SLACK_CHANNEL_WATCHLIST}")
        return messages
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        logger.error(f"Slack API error calling conversations_history: {err}")
        if err == "missing_scope":
            logger.error(
                "Slack missing scope for watchlist channel. "
                "Add 'groups:history' (private) AND 'channels:history' (public) "
                "scopes at api.slack.com/apps, then reinstall the app."
            )
        elif err == "channel_not_found":
            logger.error(f"Watchlist channel {SLACK_CHANNEL_WATCHLIST} not found. Check channel ID in config.py")
        elif err == "not_in_channel":
            logger.error(f"Bot is not a member of watchlist channel {SLACK_CHANNEL_WATCHLIST}. Add the bot to the channel manually in Slack.")
        else:
            logger.error(f"Failed to fetch watchlist messages: {err}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching watchlist messages: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []
