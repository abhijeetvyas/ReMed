"""ReMed — Telegram medication reminder bot. FastAPI entrypoint."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from . import db, engine, handlers
from .config import settings  # noqa: F401
from .telegram import Telegram, extract_event

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("remed")

_tg: Telegram | None = None
_engine_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _tg, _engine_task
    db.init()
    if settings.bot_token:
        _tg = Telegram(settings.bot_token)
        _engine_task = asyncio.create_task(engine.run_loop(_tg))
        log.info("started: webhook mode, engine ticking every %ss", settings.tick_seconds)
    else:
        log.warning("BOT_TOKEN not set — receiver up, but nothing will be sent. "
                    "Use `python -m app.polling` for local dev.")
    yield
    if _engine_task:
        _engine_task.cancel()
    if _tg:
        await _tg.close()
    db.close()


app = FastAPI(title="ReMed", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "remed"}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request) -> dict:
    if secret != settings.webhook_secret:
        return {"ok": False}
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret:
        return {"ok": False}
    # Telegram updates are small JSON; refuse oversized bodies before parsing
    if int(request.headers.get("content-length") or 0) > 256 * 1024:
        return {"ok": False}
    update = await request.json()
    # dedup: Telegram redelivers on non-200; drop replays by update_id watermark
    update_id = update.get("update_id")
    last = db.meta_get("last_update_id")
    if update_id is not None and last is not None and int(update_id) <= int(last):
        return {"ok": True}
    if update_id is not None:
        db.meta_set("last_update_id", str(update_id))
    event = extract_event(update)
    if event is None or _tg is None:
        return {"ok": True}
    asyncio.create_task(_safe_handle(event))
    return {"ok": True}


async def _safe_handle(event: dict) -> None:
    try:
        await handlers.handle_event(_tg, event)  # type: ignore[arg-type]
    except Exception:
        log.exception("event handling failed (no contents logged)")
