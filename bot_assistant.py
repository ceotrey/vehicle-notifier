"""
Vehicle Assistant Bot
Polls #vehicle-assistant Slack channel for command messages and responds
with vehicle status checks and recent vehicle history.

Supported commands:
  status <VIN>  — check if a VIN has been processed (incoming/delivered)
  recent [N]    — show last N processed vehicles (default 10)
  help          — list commands
"""

import logging
import re
import time
import threading
from datetime import datetime, timezone

from config import WATCHLIST_POLL_INTERVAL
import database
import slack_notifier
import vin_lookup

logger = logging.getLogger(__name__)

_last_ts: str = str(time.time() - 600)
_lock = threading.Lock()

HELP_TEXT = (
    "🤖 *Vehicle Assistant Commands*\n\n"
    "`status <VIN>` — check if a VIN is incoming or delivered\n"
    "_Example: status 3TYKD5HN0TT051241_\n\n"
    "`recent [N]` — show last N vehicles processed (default 10, max 25)\n"
    "_Example: recent 5_\n\n"
    "`help` — show this message"
)

# Full help shown in DMs — includes both assistant and watchlist commands in one place
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
    """Returns (command, args) or None if not recognized."""
    text = text.strip()
    if re.match(r"^help$", text, re.IGNORECASE):
        return "help", ""
    match = re.match(r"^(status|recent)\s*(.*)?$", text, re.IGNORECASE)
    if match:
        return match.group(1).lower(), (match.group(2) or "").strip()
    return None


def _format_vehicle_row(row: dict) -> str:
    """Format a vehicles_log row using the standard pipe format."""
    vin = row.get("vin", "")
    year = row.get("year", "")
    model = row.get("model", "")
    trim = row.get("trim", "")
    color = row.get("color", "")
    status = row.get("status", "")
    processed = row.get("processed_at", "")

    status_emoji = "✅" if status == "delivered" else "🚗💨"

    # Format timestamp
    try:
        dt = datetime.fromisoformat(processed.replace("Z", "+00:00"))
        date_str = dt.astimezone().strftime("%-m/%-d %-I:%M %p")
    except Exception:
        date_str = processed[:10] if processed else ""

    # Build pipe-separated parts (same format as all other channels)
    parts = [f"`{vin}`"]
    model_part = f"{year} {model}".strip()
    if model_part:
        parts.append(model_part)
    if trim:
        parts.append(trim)
    if color:
        # Guard against old garbled records — skip color if it looks like raw email text
        if len(color) < 50 and not any(c.isdigit() and len(color) > 20 for c in color):
            parts.append(color)
    parts.append(f"{status}, {date_str}")

    return f"{status_emoji} {' | '.join(parts)}"


def _handle_command(command: str, args: str, user_id: str, dm_channel_id: str = ""):
    """Execute a command and send a reply to the assistant channel or DM."""

    def _reply(message: str):
        if dm_channel_id:
            slack_notifier.send_dm(dm_channel_id, message)
        else:
            slack_notifier.send_assistant_reply(message, user_id=user_id)

    if command == "help":
        _reply(DM_HELP_TEXT if dm_channel_id else HELP_TEXT)

    elif command == "status":
        vin = args.strip().upper()
        if not vin:
            _reply("Please provide a VIN. Example: `status 3TYKD5HN0TT051241`")
            return
        rows = database.get_vehicle_status(vin)
        if not rows:
            _reply(f"❓ `{vin}` has not been seen in any processed emails yet.")
        else:
            # Use fresh Toyota Window Sticker lookup for clean vehicle details
            try:
                info = vin_lookup.lookup_vin(vin)
                vehicle_line = vin_lookup.build_vehicle_line(vin, info)
            except Exception:
                vehicle_line = f"`{vin}`"

            # Get status and date from DB (most recent record)
            row = rows[0]
            status = row.get("status", "unknown")
            processed = row.get("processed_at", "")
            try:
                dt = datetime.fromisoformat(processed.replace("Z", "+00:00"))
                date_str = dt.astimezone().strftime("%-m/%-d %-I:%M %p")
            except Exception:
                date_str = processed[:10] if processed else ""

            status_emoji = "✅" if status == "delivered" else "🚗💨"
            carrier = row.get("carrier", "")
            carrier_str = f" — {carrier}" if carrier else ""

            _reply(
                f"{status_emoji} {vehicle_line}\n"
                f"Status: *{status}* ({date_str}){carrier_str}"
            )

    elif command == "recent":
        try:
            limit = min(int(args), 25) if args else 10
        except ValueError:
            limit = 10
        rows = database.get_recent_vehicles(limit=limit)
        if not rows:
            _reply("No vehicles have been processed yet.")
        else:
            lines = [f"*Last {len(rows)} vehicle(s) processed:*"]
            for row in rows:
                lines.append(_format_vehicle_row(row))
            _reply("\n".join(lines))


def _process_messages(messages: list[dict], ts: str, dm_channel_id: str = "") -> str:
    """Process a list of assistant messages. Returns the highest timestamp seen."""
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
            source = f"DM({dm_channel_id})" if dm_channel_id else "channel"
            logger.info(f"Assistant command from {user_id} via {source}: {command} {args}")
            try:
                _handle_command(command, args, user_id=user_id, dm_channel_id=dm_channel_id)
            except Exception as e:
                logger.error(f"Assistant command handler crashed: {e}")
                import traceback
                logger.error(traceback.format_exc())
                err = "⚠️ Something went wrong. Please try again."
                try:
                    if dm_channel_id:
                        slack_notifier.send_dm(dm_channel_id, err)
                    else:
                        slack_notifier.send_assistant_reply(err, user_id=user_id)
                except Exception:
                    pass
            time.sleep(1)
        else:
            # In DMs, silently ignore unrecognized messages — the watchlist bot handles
            # watch/unwatch/list. Only send the hint in the group channel.
            if not dm_channel_id:
                slack_notifier.send_assistant_reply(
                    "Commands: `status <VIN>` · `recent [N]` · `help`",
                    user_id=user_id,
                )
    return latest_ts


def poll_assistant_commands():
    """Continuously poll #vehicle-assistant group channel for command messages.
    DM polling is handled exclusively by bot_dm.py to avoid double API calls."""
    from config import SLACK_CHANNEL_ASSISTANT
    global _last_ts

    if not SLACK_CHANNEL_ASSISTANT:
        logger.info("Vehicle assistant channel not configured — assistant bot disabled.")
        return

    logger.info("Vehicle assistant bot started.")

    while True:
        try:
            with _lock:
                ts = _last_ts
            messages = slack_notifier.get_recent_assistant_messages(oldest_ts=ts)
            logger.info(f"Assistant poll cycle: fetched {len(messages)} message(s)")
            new_ts = _process_messages(messages, ts, dm_channel_id="")
            with _lock:
                if new_ts > _last_ts:
                    _last_ts = new_ts

        except Exception as e:
            logger.error(f"Assistant poll error: {e}")
            import traceback
            logger.error(traceback.format_exc())

        time.sleep(WATCHLIST_POLL_INTERVAL)
