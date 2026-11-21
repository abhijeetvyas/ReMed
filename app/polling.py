"""Local-dev entrypoint: long-polling instead of an HTTPS webhook.

Usage:  python -m app.polling
Requires BOT_TOKEN (+ AI key for photo intake). Skips the engine? No — the
reminder engine runs in-process here too, so this is the full bot, no TLS needed.
"""
import asyncio
import logging

from . import db, engine, handlers
from .config import settings
from .telegram import Telegram, extract_event

log = logging.getLogger("remed.polling")


async def main() -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set — put it in .env (see .env.example)")
    tg = Telegram(settings.bot_token)
    await tg.delete_webhook()  # polling and a webhook are mutually exclusive
    engine_task = asyncio.create_task(engine.run_loop(tg))
    log.info("polling started — press Ctrl+C to stop")
    offset = int(db.meta_get("last_update_id") or 0) + 1
    try:
        while True:
            try:
                updates = await tg.get_updates(offset, timeout=25)
            except Exception:
                log.exception("getUpdates failed; retrying")
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                db.meta_set("last_update_id", str(update["update_id"]))
                event = extract_event(update)
                if event is None:
                    continue
                try:
                    await handlers.handle_event(tg, event)
                except Exception:
                    log.exception("event handling failed (no contents logged)")
    finally:
        engine_task.cancel()
        await tg.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
