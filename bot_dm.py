"""
bot_dm.py

Dedicated DM bot — the ONLY place that polls Slack DM conversations.
Handles ALL commands in one thread, eliminating the double-polling
that caused SSL cascade failures when both watchlist and assistant
bots each fetched DMs independently every 5 seconds.

Supported commands (sent as a DM to the Vehicle Notifier bot):
  watch <VIN>     — start watching a vehicle (alerts come back to DM)
  unwatch <VIN>   — stop watching
  unwatch all     — clear entire watchlist
  list            — show all watched VINs
  status <VIN>    — check if incoming or delivered
  recent [N]      — last N processed vehicles (default 10, max 25)
  help            — show all commands
"""

import logging
import re
import time
import threading

from config import DM_POLL_INTERVAL, VIN_PATTERN
import slack_notifier
import database
import vin_lookup
import watchlist_manager
import bot_assistant

logger = logging.getLogger(__name__)

_dm_ts: dict = {}   # {channel_id: last_ts}  — per-DM timestamp tracking
_lock = threading.Lock()

DM_HELP_TEXT = (
    "🤖 *Vehicle Notifier — All Commands*\n\n"
    "*📋 Watchlist:*\n"
    "`watch <VIN>` — start watching a vehicle\n"
    "_Example: watch 3TYKD5HN0TT051241_\n\n"
    "`unwatch <VIN>` — stop watching a VIN\n"
    "`unwatch all` — clear your entire watchlist\n"
    "`list` — show all VINs currently being watched\n\n"
    "*🔍 Vehicle Lookup:*\n"
    "`status <VIN>` — check if a VIN is incoming or delivered\n"
    "_Example: status 3TYKD5HN0TT051241_\n\n"
    "`recent [N]` — show last N vehicles processed (default 10, max 25)\n"
    "_Example: recent 5_\n\n"
    "`help` — show this message"
)


def _parse_command(text: str):
    """Returns (command, args) or None if not a recognized command."""
    text = text.strip()
    if re.match(r"^help$", text, re.IGNORECASE):
        return "help", ""
    if re.match(r"^list$", text, re.IGNORECASE):
        return "list", ""
    if re.match(r"^unwatch\s+all$", text, re.IGNORECASE):
        return "unwatch_all", ""
    m = re.match(r"^(watch|unwatch|status|recent)\s*(.*)?$", text, re.IGNORECASE)
    if m:
        return m.group(1).lower(), (m.group(2) or "").strip()
    return None


def _handle_command(command: str, args: str, user_id: str, dm_channel_id: str):
    """Dispatch to the appropriate handler, routing response to the DM."""

    def _reply(msg: str):
        slack_notifier.send_dm(dm_channel_id, msg)

    if command == "help":
        _reply(DM_HELP_TEXT)

    elif command in ("watch", "unwatch", "unwatch_all", "list"):
        # Delegate to watchlist handler — it already supports dm_channel_id routing
        try:
            watchlist_manager._handle_command(
                command, args, user_id=user_id, dm_channel_id=dm_channel_id
            )
        except Exception as e:
            logger.error(f"DM bot watchlist handler error: {e}")
            _reply("⚠️ Something went wrong. Please try again.")

    elif command == "status":
        try:
            bot_assistant._handle_command(
                "status", args, user_id=user_id, dm_channel_id=dm_channel_id
            )
        except Exception as e:
            logger.error(f"DM bot status handler error: {e}")
            _reply("⚠️ Something went wrong. Please try again.")

    elif command == "recent":
        try:
            bot_assistant._handle_command(
                "recent", args, user_id=user_id, dm_channel_id=dm_channel_id
            )
        except Exception as e:
            logger.error(f"DM bot recent handler error: {e}")
            _reply("⚠️ Something went wrong. Please try again.")


def _process_dm_messages(messages: list, ts: str, dm_channel_id: str) -> str:
    """Process messages from one DM channel. Returns the highest timestamp seen."""
    latest_ts = ts
    for msg in reversed(messages):  # oldest first
        msg_ts = msg.get("ts", "")
        if msg_ts <= ts:
            continue
        if msg_ts > latest_ts:
            latest_ts = msg_ts

        # Ignore bot/system messages
        if msg.get("bot_id") or msg.get("subtype") or msg.get("app_id"):
            continue

        text = msg.get("text", "")
        user_id = msg.get("user", "")

        parsed = _parse_command(text)
        if parsed:
            command, args = parsed
            logger.info(f"DM command from {user_id} in {dm_channel_id}: {command} {args}")
            try:
                _handle_command(command, args, user_id=user_id, dm_channel_id=dm_channel_id)
            except Exception as e:
                logger.error(f"DM command handler crashed: {e}")
                import traceback
                logger.error(traceback.format_exc())
                try:
                    slack_notifier.send_dm(dm_channel_id, "⚠️ Something went wrong. Please try again.")
                except Exception:
                    pass
            time.sleep(1)  # prevent Slack from grouping rapid replies
        else:
            logger.debug(f"DM bot: unrecognized message from {user_id}: {repr(text)}")

    return latest_ts


def poll_dm_commands():
    """Daemon thread — polls all DM conversations every DM_POLL_INTERVAL seconds.
    This is the ONLY thread that reads DMs, eliminating double-polling."""
    global _dm_ts
    logger.info("DM bot started.")

    while True:
        try:
            convos = slack_notifier.get_dm_conversations()
            for convo in convos:
                dm_id = convo.get("id", "")
                if not dm_id:
                    continue
                with _lock:
                    oldest = _dm_ts.get(dm_id, str(time.time() - 600))
                messages = slack_notifier.get_dm_messages(dm_id, oldest_ts=oldest)
                if messages:
                    logger.info(f"DM bot: {dm_id} — fetched {len(messages)} message(s)")
                new_ts = _process_dm_messages(messages, oldest, dm_id)
                with _lock:
                    _dm_ts[dm_id] = new_ts

        except Exception as e:
            logger.error(f"DM bot poll error: {e}")
            import traceback
            logger.error(traceback.format_exc())

        time.sleep(DM_POLL_INTERVAL)
