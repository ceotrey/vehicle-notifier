import logging
import re
import threading
import time
from typing import Optional, Tuple
from config import WATCHLIST_POLL_INTERVAL, VIN_PATTERN
import database
import slack_notifier
import vin_lookup

logger = logging.getLogger(__name__)
# Start 10 minutes back so we catch any recent watch/unwatch messages sent
# while the script was restarting or the Slack scope was being added.
_last_ts: str = str(time.time() - 600)
_lock = threading.Lock()


def check_watchlist_matches(vin: str, description: str) -> list[dict]:
    """
    Returns list of watchlist entries that match this VIN.
    Watchlist is now VIN-only — exact match on search_term.
    """
    entries = database.get_watchlist()
    return [
        e for e in entries
        if e["search_term"].strip().upper() == vin.upper()
    ]


def process_watchlist_match(entry: dict, vin: str, vehicle_line: str, email_type: str = "delivery"):
    """
    Send appropriate watchlist alert based on email type.
    - predelivery: notify "on the way", keep watching
    - delivery: notify "here", auto-unwatch
    If the entry has a dm_channel_id, alert goes to DM; otherwise to group channel.
    """
    user_id = entry.get("user_id", "") or ""
    dm_channel_id = entry.get("dm_channel_id", "") or ""

    if email_type == "predelivery":
        if dm_channel_id:
            slack_notifier.send_watchlist_incoming_alert_dm(entry["search_term"], vehicle_line, dm_channel_id)
        else:
            slack_notifier.send_watchlist_incoming_alert(entry["search_term"], vehicle_line, user_id=user_id)
        logger.info(f"Watchlist incoming alert (still watching): {entry['search_term']} → {vin}")
    else:
        if dm_channel_id:
            sent = slack_notifier.send_watchlist_alert_dm(entry["search_term"], vehicle_line, dm_channel_id)
        else:
            sent = slack_notifier.send_watchlist_alert(entry["search_term"], vehicle_line, user_id=user_id)
        if sent:
            database.mark_watchlist_matched(entry["id"], vin)
            logger.info(f"Watchlist delivery match (unwatched): {entry['search_term']} → {vin}")
        else:
            logger.error(f"Watchlist delivery alert FAILED to send for {vin} — entry kept active for retry next delivery email")


def _parse_command(text: str) -> Optional[Tuple[str, str]]:
    """
    Returns (command, term) where command is 'watch', 'unwatch', 'unwatch_all', or 'list',
    or None if message is not a recognized command.
    Users type plain messages like:
      'watch 2026 RAV4 XSE'
      'unwatch 1HGBH41JXMN109186'
      'unwatch all'
      'list'
    """
    text = text.strip()
    # Single-word commands (no search term needed)
    if re.match(r"^list$", text, re.IGNORECASE):
        return "list", ""
    if re.match(r"^help$", text, re.IGNORECASE):
        return "help", ""
    # Check for 'unwatch all' before the general unwatch regex
    if re.match(r"^unwatch\s+all$", text, re.IGNORECASE):
        return "unwatch_all", ""
    # Check for watch/unwatch commands (require a search term)
    match = re.match(r"^(watch|unwatch)\s+(.+)$", text, re.IGNORECASE)
    if match:
        return match.group(1).lower(), match.group(2).strip()
    return None


WATCHLIST_HELP_TEXT = (
    "📋 *Watchlist Commands*\n\n"
    "`watch <VIN>` — start watching a vehicle by VIN\n"
    "_Example: watch 3TYKD5HN0TT051241_\n\n"
    "`unwatch <VIN>` — stop watching a VIN\n"
    "_Example: unwatch 3TYKD5HN0TT051241_\n\n"
    "`unwatch all` — clear the entire watchlist\n\n"
    "`list` — show all VINs currently being watched\n\n"
    "`help` — show this message"
)


def _handle_command(command: str, term: str, user_id: str = "", dm_channel_id: str = ""):
    """Dispatch watchlist command. user_id is the Slack user ID of the sender.
    dm_channel_id is set when the command came from a DM — confirmations go there instead of group channel."""
    tag = f"<@{user_id}> " if (user_id and not dm_channel_id) else ""

    def _reply(message: str):
        """Send confirmation to DM if applicable, else group channel."""
        if dm_channel_id:
            slack_notifier.send_dm(dm_channel_id, message)
        else:
            slack_notifier.send_watchlist_confirmation(message)

    if command == "help":
        # In DMs, let the assistant bot handle `help` — it shows all commands in one message.
        # In the group watchlist channel, show the watchlist-specific help.
        if not dm_channel_id:
            _reply(f"{tag}{WATCHLIST_HELP_TEXT}")

    elif command == "list":
        entries = database.get_watchlist()
        if not entries:
            _reply(
                f"{tag}📋 *Currently Watching 0 Vehicles*\n"
                f"Type `watch <VIN>` to add one."
            )
            logger.info("Watchlist list command: no entries")
        else:
            lines = [f"{tag}📋 *Currently Watching {len(entries)} Vehicle(s):*"]
            for entry in entries:
                vin = entry["search_term"]
                desc = entry.get("vehicle_description") or f"`{vin}`"
                watcher = entry.get("user_id", "")
                watcher_str = f" — <@{watcher}>" if (watcher and not dm_channel_id) else ""
                lines.append(f"✅ {desc}{watcher_str}")
            _reply("\n".join(lines))
            logger.info(f"Watchlist list command: displayed {len(entries)} entries")

    elif command == "watch":
        vin = term.strip().upper()
        if not re.fullmatch(VIN_PATTERN, vin):
            _reply(
                f"{tag}❌ *Invalid VIN*\n"
                f"`{term}` doesn't look like a valid 17-character VIN.\n"
                f"Watchlist only accepts VINs. Example: `watch 3TYKD5HN0TT051241`"
            )
            return

        # Look up via Toyota Window Sticker + NHTSA for immediate vehicle description
        try:
            info = vin_lookup.lookup_vin(vin)
            vehicle_line = vin_lookup.build_vehicle_line(vin, info)
        except Exception as e:
            logger.warning(f"VIN lookup failed during watch for {vin}: {e}")
            vehicle_line = f"`{vin}`"

        success = database.add_watchlist_entry(
            vin, "vin", user_id=user_id, vehicle_description=vehicle_line, dm_channel_id=dm_channel_id
        )
        if success:
            _reply(
                f"{tag}✅ *Now Watching:*\n"
                f"{vehicle_line}\n"
                f"I'll alert you the moment this vehicle is incoming or delivered."
            )
        else:
            _reply(f"{tag}❌ Failed to add `{vin}` to watchlist.")

    elif command == "unwatch":
        vin = term.strip().upper()
        success = database.remove_watchlist_entry(vin)
        if success:
            _reply(f"{tag}🗑️ *Removed from Watchlist:* `{vin}`")
        else:
            _reply(
                f"{tag}❌ `{vin}` wasn't on the watchlist. It may have already been matched or removed."
            )

    elif command == "unwatch_all":
        count = database.clear_watchlist()
        _reply(
            f"{tag}🗑️ *Watchlist Cleared*\n"
            f"Removed {count} entr{'y' if count == 1 else 'ies'} from the watchlist."
        )
        logger.info(f"Watchlist cleared by user {user_id}: {count} entries removed")


def _process_messages(messages: list[dict], ts: str, dm_channel_id: str = "") -> str:
    """Process a list of messages, dispatching recognized commands.
    Returns the highest timestamp seen (to use as next oldest_ts)."""
    latest_ts = ts
    for msg in reversed(messages):  # oldest first
        msg_ts = msg.get("ts", "")
        if msg_ts <= ts:
            continue
        if msg_ts > latest_ts:
            latest_ts = msg_ts

        text = msg.get("text", "")
        # Ignore bot messages and system messages
        if msg.get("bot_id") or msg.get("subtype") or msg.get("app_id"):
            continue

        user_id = msg.get("user", "")
        parsed = _parse_command(text)
        if parsed:
            command, term = parsed
            source = f"DM({dm_channel_id})" if dm_channel_id else "group"
            logger.info(f"Watchlist command from {user_id} via {source}: {command} {term}")
            try:
                _handle_command(command, term, user_id=user_id, dm_channel_id=dm_channel_id)
            except Exception as cmd_err:
                logger.error(f"Watchlist command handler crashed: {cmd_err}")
                import traceback
                logger.error(traceback.format_exc())
                try:
                    err_msg = "⚠️ Something went wrong processing your command. Please try again."
                    if dm_channel_id:
                        slack_notifier.send_dm(dm_channel_id, err_msg)
                    else:
                        tag = f"<@{user_id}> " if user_id else ""
                        slack_notifier.send_watchlist_confirmation(f"{tag}{err_msg}")
                except Exception:
                    pass
            time.sleep(1)  # prevent Slack from grouping rapid confirmations
        else:
            logger.info(f"Watchlist: unrecognized message — raw text: {repr(text)}")
    return latest_ts


def poll_watchlist_commands():
    """Continuously poll #vehicle-watchlist group channel for commands.
    DM polling is handled exclusively by bot_dm.py to avoid double API calls."""
    global _last_ts
    logger.info("Watchlist command listener started.")

    while True:
        try:
            with _lock:
                ts = _last_ts
            messages = slack_notifier.get_recent_watchlist_messages(oldest_ts=ts)
            logger.info(f"Watchlist poll cycle: fetched {len(messages)} group message(s)")
            new_ts = _process_messages(messages, ts, dm_channel_id="")
            with _lock:
                if new_ts > _last_ts:
                    _last_ts = new_ts

        except Exception as e:
            logger.error(f"Watchlist poll error: {e}")
            import traceback
            logger.error(traceback.format_exc())

        time.sleep(WATCHLIST_POLL_INTERVAL)
