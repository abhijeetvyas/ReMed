"""Register the Telegram webhook (run once after deploying with HTTPS):
    python -m app.webhook_setup https://your-domain.example/webhook/<WEBHOOK_SECRET>
"""
import asyncio
import sys

from .config import settings  # loads .env
from .telegram import Telegram


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: python -m app.webhook_setup https://host/webhook/{settings.webhook_secret}")
    tg = Telegram(settings.bot_token)
    await tg.set_webhook(sys.argv[1], settings.webhook_secret)
    print("webhook registered")
    await tg.close()


if __name__ == "__main__":
    asyncio.run(main())
