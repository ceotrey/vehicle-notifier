import base64
import logging
import re
import time
from datetime import date
from email import message_from_bytes
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    GMAIL_SCOPES,
    GMAIL_POLL_INTERVAL,
    MONITORED_SENDERS,
    SENDER_VEHICHAUL,
    SENDER_CENTURION,
    VEHICHAUL_PREDELIVERY,
    VEHICHAUL_DELIVERY,
    CENTURION_PREDELIVERY,
    CENTURION_DELIVERY,
    VIN_PATTERN,
    MAX_VINS_PER_EMAIL,
)
import database
import pdf_extractor
import vin_lookup
import slack_notifier
import watchlist_manager
import heartbeat

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail auth
# ---------------------------------------------------------------------------

def _bootstrap_token_from_env():
    """On hosts where token.json isn't shipped (e.g. Railway, where it's gitignored),
    write it from the GMAIL_TOKEN_JSON environment variable if the file is missing.
    The token carries a refresh_token + client_id/secret, so Gmail auth then refreshes
    headlessly with no browser."""
    import os
    if os.path.exists(GMAIL_TOKEN_FILE):
        return
    token_json = os.environ.get("GMAIL_TOKEN_JSON")
    if token_json:
        try:
            with open(GMAIL_TOKEN_FILE, "w") as f:
                f.write(token_json)
            logger.info("Wrote token.json from GMAIL_TOKEN_JSON environment variable.")
        except Exception as e:
            logger.error(f"Failed to write token.json from env: {e}")


def get_gmail_service():
    """Public — builds and returns an authenticated Gmail API service object."""
    _bootstrap_token_from_env()
    creds = None
    try:
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    except Exception:
        pass

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(
                port=0,  # port=0 lets OS pick any free port — avoids "address in use" errors
                open_browser=True,
                login_hint="twomack@anderson-auto.net",
            )
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Email fetching
# ---------------------------------------------------------------------------

def _get_unread_messages(service) -> list[dict]:
    """Return UNREAD emails from monitored senders from today onwards.
    Filtering for unread means processed emails (marked read after processing)
    are never seen again — even across restarts."""
    sender_query = " OR ".join(f"from:{s}" for s in MONITORED_SENDERS)
    today = date.today().strftime("%Y/%m/%d")
    query = f"({sender_query}) after:{today} is:unread"
    result = service.users().messages().list(userId="me", q=query, maxResults=50).execute(num_retries=3)
    return result.get("messages", [])


def _mark_email_read(service, msg_id: str):
    """Mark an email as read in Gmail by removing the UNREAD label."""
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute(num_retries=3)
        logger.debug(f"Marked email {msg_id} as read in Gmail.")
    except Exception as e:
        logger.warning(f"Could not mark email {msg_id} as read: {e}")


def _fetch_full_message(service, msg_id: str) -> dict:
    return service.users().messages().get(userId="me", id=msg_id, format="raw").execute(num_retries=3)


def _decode_raw(raw_msg: dict) -> bytes:
    return base64.urlsafe_b64decode(raw_msg["raw"].encode("ASCII"))


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------

def _header(msg, name: str) -> str:
    for hdr in msg.get("payload", {}).get("headers", []):
        if hdr["name"].lower() == name.lower():
            return hdr["value"]
    return ""


def _get_sender_normalized(email_obj) -> Optional[str]:
    from_field = ""
    for key, val in email_obj.items():
        if key.lower() == "from":
            from_field = val
            break
    match = re.search(r"[\w.+]+@[\w.]+", from_field)
    if match:
        return match.group(0).lower()
    return None


def _get_subject(email_obj) -> str:
    return email_obj.get("Subject", "")


def _get_body_text(email_obj) -> str:
    """Recursively extract plain-text body from email. Falls back to HTML stripped of tags."""
    plain_parts = []
    html_parts = []

    def _walk(part):
        ct = part.get_content_type()
        if part.is_multipart():
            for sub in part.get_payload():
                _walk(sub)
        elif ct == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                plain_parts.append(payload.decode("utf-8", errors="replace"))
        elif ct == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                html_parts.append(payload.decode("utf-8", errors="replace"))

    _walk(email_obj)

    if plain_parts:
        return "\n".join(plain_parts)

    # Fallback: strip HTML tags to get raw text (VINs survive this)
    if html_parts:
        raw_html = "\n".join(html_parts)
        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&#\d+;", " ", text)
        text = re.sub(r"&[a-z]+;", " ", text)
        return text

    return ""


def _get_pdf_attachments(email_obj) -> list[bytes]:
    """Return list of raw bytes for each PDF attachment."""
    pdfs = []

    def _walk(part):
        if part.is_multipart():
            for sub in part.get_payload():
                _walk(sub)
        else:
            fname = part.get_filename() or ""
            if fname.lower().endswith(".pdf"):
                payload = part.get_payload(decode=True)
                if payload:
                    pdfs.append(payload)

    _walk(email_obj)
    return pdfs


def _extract_vins(text: str) -> list[str]:
    found = re.findall(VIN_PATTERN, text)
    seen = []
    for v in found:
        if v not in seen:
            seen.append(v)
    return seen[:MAX_VINS_PER_EMAIL]


# ---------------------------------------------------------------------------
# Load number extraction
# ---------------------------------------------------------------------------

def _extract_load_number(subject: str, body: str) -> str:
    # Try common patterns: "Load #12345", "Load: 12345", "Load 12345"
    for pattern in [
        r"Load\s*#?\s*(\w+)",
        r"bol-(\w+)",
        r"Load\w*[-_]?(\d+)",
    ]:
        m = re.search(pattern, subject + " " + body, re.IGNORECASE)
        if m:
            return m.group(1)
    return "N/A"


def _extract_eta(body: str) -> str:
    # Patterns: "ETA: Jan 15", "Estimated Delivery: 01/15/2026"
    for pattern in [
        r"ETA[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{0,4})",
        r"ETA[:\s]+([\d/\-]+)",
        r"Estimated\s+(?:Delivery|Arrival)[:\s]+([\w/\-, ]+?)(?:\n|$)",
        r"Delivery\s+Date[:\s]+([\w/\-, ]+?)(?:\n|$)",
    ]:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# Color extraction from email body (Centurion format)
# ---------------------------------------------------------------------------

def _extract_colors_from_body(body: str) -> dict[str, str]:
    """Returns {vin: color} map extracted from Centurion email body."""
    color_map = {}
    # Pattern: VIN ... Color: COLOR
    pattern = re.compile(
        r"([A-HJ-NPR-Z0-9]{17})[^\n]*?Color:\s*([^\n,]+)", re.IGNORECASE
    )
    for m in pattern.finditer(body):
        vin = m.group(1).upper()
        color = m.group(2).strip()
        color_map[vin] = color
    return color_map


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def _process_email(raw_bytes: bytes, msg_id: str):
    email_obj = message_from_bytes(raw_bytes)

    sender = _get_sender_normalized(email_obj)
    if not sender:
        return

    # Normalize Centurion sender (case-insensitive)
    matched_sender = None
    for monitored in MONITORED_SENDERS:
        if sender == monitored.lower():
            matched_sender = monitored
            break

    if not matched_sender:
        return

    carrier_name = MONITORED_SENDERS[matched_sender]
    subject = _get_subject(email_obj)
    subject_lower = subject.lower()
    body = _get_body_text(email_obj)

    # Determine email type
    if matched_sender == SENDER_VEHICHAUL:
        if VEHICHAUL_PREDELIVERY in subject_lower:
            email_type = "predelivery"
        elif VEHICHAUL_DELIVERY in subject_lower:
            email_type = "delivery"
        else:
            logger.info(f"Vehichaul email with unrecognized subject: {subject}")
            return
    else:  # Centurion
        if CENTURION_PREDELIVERY in subject_lower:
            email_type = "predelivery"
        elif CENTURION_DELIVERY in subject_lower:
            email_type = "delivery"
        else:
            logger.info(f"Centurion email with unrecognized subject: {subject}")
            return

    logger.info(f"Processing {email_type} from {carrier_name}: {subject}")

    # Extract VINs
    pdf_bytes_list = _get_pdf_attachments(email_obj)
    vins = []

    if pdf_bytes_list:
        for pdf_bytes in pdf_bytes_list:
            try:
                text = pdf_extractor.extract_text_from_pdf(pdf_bytes)
                vins.extend(_extract_vins(text))
            except Exception as e:
                logger.error(f"PDF extraction failed: {e}")

    if not vins:
        # No PDFs or PDF had no VINs — fall back to body text
        vins = _extract_vins(body)
        # Note: color/trim always come from Toyota Window Sticker via vin_lookup.lookup_vin()
        # Email body color extraction removed — it was unreliable across carrier formats

    vins = list(dict.fromkeys(vins))[:MAX_VINS_PER_EMAIL]  # deduplicate, cap

    if not vins:
        logger.warning(f"No VINs found in email from {carrier_name}")
        slack_notifier.send_extraction_failure(carrier_name)
        database.mark_email_processed(msg_id)
        return

    status = "incoming" if email_type == "predelivery" else "delivered"

    # Duplicate email guard — carrier sometimes sends the same load 2-3 times
    if database.any_vin_logged_today(vins, status):
        logger.info(f"Duplicate email from {carrier_name} — VINs already logged as '{status}' today. Skipping Slack.")
        database.mark_email_processed(msg_id)
        return

    load_number = _extract_load_number(subject, body)
    delivery_eta = _extract_eta(body) if email_type == "predelivery" else ""

    # Look up each VIN and build vehicle lines
    # Color and trim always sourced from Toyota Window Sticker via lookup_vin()
    vehicle_lines = []
    for vin in vins:
        info = vin_lookup.lookup_vin(vin)
        line = vin_lookup.build_vehicle_line(vin, info)
        vehicle_lines.append(line)

        # Check watchlist
        matches = watchlist_manager.check_watchlist_matches(vin, info.get("description", ""))
        wl_matched = False
        for entry in matches:
            watchlist_manager.process_watchlist_match(entry, vin, line, email_type=email_type)
            wl_matched = True

        # Log to Supabase
        database.log_vehicle(
            vin=vin,
            year=info.get("year", ""),
            make=info.get("make", ""),
            model=info.get("model", ""),
            trim=info.get("trim", ""),
            color=info.get("color", ""),
            load_number=load_number,
            carrier=carrier_name,
            delivery_eta=delivery_eta,
            status=status,
            email_sender=matched_sender,
            watchlist_matched=wl_matched,
        )

    # Send Slack notification
    if email_type == "predelivery":
        slack_notifier.send_predelivery(
            load_number=load_number,
            carrier_name=carrier_name,
            vehicle_lines=vehicle_lines,
            delivery_eta=delivery_eta,
        )
    else:
        slack_notifier.send_delivery(
            load_number=load_number,
            carrier_name=carrier_name,
            vehicle_lines=vehicle_lines,
        )

    database.mark_email_processed(msg_id)
    logger.info(f"Processed {len(vins)} VIN(s) from {carrier_name} email.")


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def poll_gmail(service=None):
    """Main polling loop. Rebuilds the Gmail service on every poll to avoid
    stale HTTP connections (Broken pipe / Connection reset errors)."""
    logger.info("Gmail monitor started.")
    heartbeat.beat("gmail-monitor")

    # In-memory set of email IDs currently being processed.
    # Prevents duplicate processing when poll cycles overlap (e.g. slow VIN lookups
    # take longer than the 1-minute poll interval).
    _in_flight: set = set()

    while True:
        # Always build a fresh service object each cycle — credentials are
        # cached in token.json so this is instant, but it opens a clean
        # HTTP connection every time rather than reusing a stale one.
        try:
            service = get_gmail_service()
        except Exception as e:
            logger.error(f"Gmail service build failed: {e}")
            time.sleep(GMAIL_POLL_INTERVAL)
            continue

        try:
            messages = _get_unread_messages(service)
            logger.info(f"Found {len(messages)} unread message(s) from monitored senders.")

            for msg_meta in messages:
                msg_id = msg_meta["id"]

                if msg_id in _in_flight:
                    logger.debug(f"Email {msg_id} already in progress — skipping.")
                    continue

                if database.is_email_processed(msg_id):
                    logger.debug(f"Email {msg_id} already processed — skipping.")
                    continue

                _in_flight.add(msg_id)
                try:
                    raw_msg = _fetch_full_message(service, msg_id)
                    raw_bytes = _decode_raw(raw_msg)
                    _process_email(raw_bytes, msg_id)
                    # Rebuild service before marking read — the connection may have
                    # gone stale during VIN lookups / PDF extraction above.
                    try:
                        fresh_service = get_gmail_service()
                        _mark_email_read(fresh_service, msg_id)
                    except Exception as mark_err:
                        logger.warning(f"Could not mark email {msg_id} as read: {mark_err}")
                except Exception as e:
                    logger.error(f"Error processing email {msg_id}: {e}")
                finally:
                    _in_flight.discard(msg_id)

            heartbeat.beat("gmail-monitor")

        except Exception as e:
            logger.error(f"Gmail poll loop error: {e}")

        time.sleep(GMAIL_POLL_INTERVAL)
