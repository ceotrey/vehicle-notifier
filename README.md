# Vehicle Notifier

A production Python automation that monitors a dealership's Gmail inbox for vehicle
delivery emails from auto-transport carriers, enriches each vehicle with full
specifications, and posts real-time, formatted notifications to Slack. It runs 24/7
as a multi-threaded background service and is deployed on Railway.

Built for **Fred Anderson Toyota (Raleigh)** to replace a manual process where staff
had to read carrier emails, look up each VIN by hand, and message the team.

---

## What it does

1. **Watches Gmail** for delivery emails from two carriers (AutoCarrier Express and
   Centurion Auto Logistics).
2. **Extracts VINs** from PDF bills-of-lading (and email body as a fallback).
3. **Enriches each VIN** with year, make, model, trim, and exterior color by combining
   the NHTSA vehicle API with Toyota's window-sticker service.
4. **Posts to Slack** — pre-delivery emails go to `#vehicles-incoming`, completed
   deliveries to `#vehicles-delivered`, each as a clean, formatted vehicle list.
5. **Watchlist** — staff can DM the bot or post in `#vehicle-watchlist` to track a
   specific VIN and get a private alert the moment it's incoming or delivered.
6. **Self-monitors** — a health monitor checks every dependency and posts to
   `#system-alerts` only when something is genuinely wrong.

---

## Architecture

The service runs five independent daemon threads, supervised by a watchdog in the
main thread that auto-restarts any thread that crashes.

| Thread | Responsibility |
|---|---|
| `gmail-monitor` | Polls Gmail, parses emails, enriches VINs, posts delivery/incoming notifications |
| `watchlist-listener` | Polls `#vehicle-watchlist` for `watch` / `unwatch` / `list` commands |
| `vehicle-assistant` | Polls `#vehicle-assistant` for `status` / `recent` lookups |
| `dm-bot` | Single poller for all direct messages — handles every command privately |
| `health-monitor` | Checks all external services; alerts Slack on real failures only |

```
Gmail ──▶ gmail_monitor ──▶ pdf_extractor ──▶ vin_lookup ──┬──▶ database (Supabase)
                                                           ├──▶ slack_notifier
                                                           └──▶ watchlist_manager
```

### Data enrichment pipeline (`vin_lookup.py`)
1. **NHTSA vPIC API** → year / make / model (with one automatic retry on failure).
2. **Toyota window-sticker PDF** → real trim grade + exterior color.
3. **VIN-decode fallbacks** → model year from the VIN's 10th character (ISO 3779) and
   model from the WMI prefix, so a vehicle line is never blank even if an API is down.

---

## Reliability engineering

This system runs unattended, so most of the work went into graceful failure handling:

- **Self-healing watchdog** — dead threads are detected within 30s, alerted, and
  automatically restarted; the process never needs manual intervention.
- **Triple de-duplication** — Gmail `is:unread` filter, an in-memory in-flight set, and
  a Supabase check that suppresses duplicate notifications when a carrier sends the
  same load email multiple times.
- **Alert hysteresis** — the health monitor only alerts after **3 consecutive**
  failures, so brief network blips never page the team; recoveries alert immediately.
- **Guaranteed delivery of watchlist alerts** — a watched VIN is only removed from the
  list after Slack confirms the alert was actually sent; otherwise it's retried.
- **Rate-limit & connection resilience** — exponential-style backoff on Slack rate
  limits, automatic retries on Gmail connection resets, and tuned API timeouts.

---

## Tech stack

- **Python 3** — `threading` for concurrency
- **Gmail API** (OAuth2, `gmail.modify`) — email monitoring
- **Slack SDK** — notifications, commands, and DM interface
- **Supabase** (PostgreSQL) — vehicle log, watchlist, email de-duplication
- **NHTSA vPIC API** + **Toyota window-sticker service** — vehicle enrichment
- **pdfplumber** — PDF bill-of-lading parsing
- **Railway** — 24/7 cloud hosting

---

## Project layout

| File | Purpose |
|---|---|
| `main.py` | Entry point — starts threads and runs the watchdog |
| `gmail_monitor.py` | Gmail polling, email parsing, processing pipeline |
| `vin_lookup.py` | VIN enrichment (NHTSA + Toyota + VIN-decode fallbacks) |
| `pdf_extractor.py` | Extracts text/VINs from PDF attachments |
| `watchlist_manager.py` | Watchlist commands and match notifications |
| `bot_assistant.py` | `#vehicle-assistant` lookup commands |
| `bot_dm.py` | Unified direct-message command handler |
| `slack_notifier.py` | All Slack message formatting and sending |
| `database.py` | Supabase reads/writes |
| `health_monitor.py` | Dependency health checks and alerting |
| `config.example.py` | Configuration template (copy to `config.py`) |

---

## Running locally

```bash
git clone https://github.com/ceotrey/vehicle-notifier.git
cd vehicle-notifier
pip install -r requirements.txt

cp config.example.py config.py   # then fill in your credentials

python3 main.py
```

The first run opens a browser once for Gmail OAuth and saves `token.json`. Subsequent
runs (and the cloud deployment) refresh the token automatically — no browser needed.

> **Note:** `config.py` and `token.json` hold live credentials and are intentionally
> gitignored. The repository ships a `config.example.py` template instead.

---

## Deployment

Deployed on **Railway** as an always-on worker (`Procfile`: `worker: python3 main.py`).
Pushing an update is a single command:

```bash
railway up
```
