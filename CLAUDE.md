# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This System Does

Vehicle Notifier is a Python automation that monitors a Gmail inbox (`twomack@anderson-auto.net`) for vehicle delivery emails from two auto carriers, extracts VINs, looks up full vehicle details, and posts formatted notifications to Slack channels. It runs as three persistent background threads.

## Running the System

```bash
cd ~/vehicle-notifier
python3 main.py
```

First run opens a browser for Gmail OAuth authorization (saves `token.json`). Subsequent runs are silent. Stop with `Ctrl+C`.

If Gmail auth fails with a network timeout, simply re-run — the token refresh retries cleanly.

## Thread Architecture

Three daemon threads run in parallel:

| Thread | Function | Interval |
|---|---|---|
| `gmail-monitor` | Polls Gmail, processes emails, sends Slack notifications | 5 min |
| `watchlist-listener` | Polls `#vehicle-watchlist` Slack channel for `watch`/`unwatch` messages | 60 sec |
| `health-monitor` | Checks all 7 external services, alerts `#system-alerts` on status change | 30 min |

`main.py` builds the Gmail service object once and passes it to both the gmail thread and health monitor thread so auth is shared.

## Email Processing Flow

`gmail_monitor.py` → `pdf_extractor.py` → `vin_lookup.py` → `database.py` + `slack_notifier.py` + `watchlist_manager.py`

1. Gmail query: `(from:vehichaul OR from:centurion) after:TODAY` — reads/unread agnostic, deduplication handled by Supabase `processed_emails` table
2. PDF attachments checked first; falls back to HTML-stripped email body if no PDF or PDF has no text
3. VIN regex: `[A-HJ-NPR-Z0-9]{17}`, max 9 per email
4. Each VIN: NHTSA API for year/make/model → Toyota Window Sticker PDF for real trim + exterior color
5. Vehicle description never uses the NHTSA "Series" field (e.g. "40 Series") — only Trim

## Carrier Email Patterns

| Carrier | Sender | Pre-delivery subject | Delivery subject | VIN source |
|---|---|---|---|---|
| AutoCarrier Express (Vehichaul) | `hello+autocarrierexpress@vehichaul.com` | "VH Pre-Delivery Notification" | "VH Delivery Receipt" | Body table (pre) / `bol-XXXXX.pdf` (delivery) |
| Centurion Auto Logistics | `noreply@centurionautologistics.com` | "Delivery ETA for vehicles" | "Delivery Document for Load" | Body text (pre) / `LoadXXXXX_Deli....pdf` (delivery) |

## Slack Channels

| Channel | ID | Purpose |
|---|---|---|
| `#vehicles-incoming` | `C0B6B0FJ5T6` | Pre-delivery notifications |
| `#vehicles-delivered` | `C0B64NCT0E9` | Delivery confirmations |
| `#vehicle-watchlist` | `C0B69V40BMX` | Watch/unwatch commands + alerts |
| `#system-alerts` | `C0B6EABQC7N` | Health monitor failures/recoveries |

## Watchlist Commands (typed in #vehicle-watchlist)

- `watch 2026 RAV4 XSE` — fuzzy description match, case-insensitive
- `watch 1HGBH41JXMN109186` — exact VIN match
- `unwatch 2026 RAV4 XSE` — removes entry

Pre-delivery match → "On the Way" alert, stays on watchlist. Delivery match → "Here" alert, auto-removed from watchlist.

## Supabase Tables

- `processed_emails` — email deduplication (Gmail message ID as unique key)
- `vehicles_log` — record of every processed vehicle
- `watchlist` — active watch entries with type (`vin` or `description`)

Tables must be created manually via Supabase SQL Editor — the `init_tables()` RPC approach doesn't work on the free tier. Permissions: `GRANT SELECT, INSERT, UPDATE, DELETE ON public.<table> TO anon`.

Use the **Legacy anon/service_role API keys** tab in Supabase settings — the new `sb_publishable_` format is not supported by the Python SDK.

## Credentials Location

- Gmail OAuth credentials: `~/Documents/vehicle-notifier-credentials.json` (Google Cloud project: `vehicle-tracker-497516`)
- Gmail token (auto-generated): `token.json` in project root
- All other credentials hardcoded in `config.py`

## VIN Lookup Priority

1. NHTSA vPIC API → year, make, model (reliable)
2. Toyota Window Sticker PDF (`toyota.com/t3Portal/prodpage/getWindowSticker?vin=`) → trim grade + exterior color
3. Email body color parsing (`Color: ICE CAP` format) → fallback color for Centurion emails

All vehicles are Toyota. Series-number trims (e.g. "62 Series", "40 Series") are filtered out — if no real trim is found, description is just "Year Make Model".

## Health Monitor Checks

`health_monitor.py` checks: Gmail API, Slack API (`auth.test`), Supabase (test query), NHTSA API (test VIN decode), Toyota window sticker (HEAD request), Gmail thread alive, Watchlist thread alive. Only posts to `#system-alerts` on state change (failure or recovery).
