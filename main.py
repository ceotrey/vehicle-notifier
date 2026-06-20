import logging
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WATCHDOG_INTERVAL = 30  # seconds between liveness checks


def main():
    import database
    import gmail_monitor
    import watchlist_manager
    import health_monitor
    import bot_assistant
    import bot_dm
    import slack_notifier

    # Step 1: Initialize Supabase tables
    logger.info("Initializing Supabase tables…")
    database.init_tables()

    # Step 2: Build Gmail service once (shared with health monitor)
    gmail_service = gmail_monitor.get_gmail_service()

    # Thread specs: (name, target_fn, args)
    # Stored so the watchdog can recreate any thread that dies.
    thread_specs = [
        ("watchlist-listener", watchlist_manager.poll_watchlist_commands, ()),
        ("gmail-monitor",      gmail_monitor.poll_gmail,                  (gmail_service,)),
        ("vehicle-assistant",  bot_assistant.poll_assistant_commands,     ()),
        ("dm-bot",             bot_dm.poll_dm_commands,                   ()),
    ]

    # Start all worker threads
    threads = {}
    for name, target, args in thread_specs:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        t.start()
        threads[name] = (t, target, args)
        logger.info(f"Thread '{name}' started.")

    # Health monitor needs references to the other threads; start it separately
    # so we can pass live thread objects. It's also watched by the watchdog.
    wl_thread   = threads["watchlist-listener"][0]
    gmail_thread = threads["gmail-monitor"][0]

    health_thread = threading.Thread(
        target=health_monitor.poll_health,
        args=(gmail_service, gmail_thread, wl_thread),
        daemon=True,
        name="health-monitor",
    )
    health_thread.start()
    threads["health-monitor"] = (
        health_thread,
        health_monitor.poll_health,
        (gmail_service, gmail_thread, wl_thread),
    )
    logger.info("Thread 'health-monitor' started.")

    print("Vehicle Notifier is running. Monitoring twomack@anderson-auto.net every 5 minutes.")
    logger.info("All threads started. Watchdog active — checking every 30 s. Press Ctrl+C to stop.")

    # Watchdog loop — runs in the main thread forever.
    # Detects dead threads, posts a Slack alert, and restarts them automatically.
    try:
        while True:
            time.sleep(WATCHDOG_INTERVAL)
            for name, (t, target, args) in list(threads.items()):
                if not t.is_alive():
                    logger.warning(f"[WATCHDOG] Thread '{name}' is dead — restarting…")
                    try:
                        slack_notifier.send_health_alert(
                            name,
                            "Thread crashed and is being auto-restarted by the watchdog.",
                            recovered=False,
                        )
                    except Exception as alert_err:
                        logger.error(f"[WATCHDOG] Could not send Slack alert: {alert_err}")

                    new_t = threading.Thread(target=target, args=args, name=name, daemon=True)
                    new_t.start()
                    threads[name] = (new_t, target, args)
                    logger.info(f"[WATCHDOG] Thread '{name}' restarted successfully.")
    except KeyboardInterrupt:
        logger.info("Vehicle Notifier stopped.")


if __name__ == "__main__":
    main()
