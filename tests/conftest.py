import os
import sys
from pathlib import Path

# Tests must be hermetic: pin webhook secret/dev credentials before app
# modules load dotenv from the developer's real .env file.
os.environ["WEBHOOK_SECRET"] = "remed-dev-secret"

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def tmp_db(tmp_path):
    from app import db
    db.close()
    db.init(str(tmp_path / "test.db"))
    yield
    db.close()


@pytest.fixture(autouse=True)
def reset_guardrails():
    """Fresh rate-limit windows for every test."""
    from app import guardrails
    guardrails.reset()
    yield
    guardrails.reset()


class FakeTelegram:
    """Records everything the handlers/engine would send."""

    def __init__(self, fail_until_attempt: int = 0):
        self.texts: list[tuple[str, str]] = []
        self.buttoned: list[tuple[str, str, list]] = []
        self.button_updates: list[tuple[str, int, list]] = []
        self.answered: list[str] = []
        self.send_attempts = 0
        self.fail_until_attempt = fail_until_attempt

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))
        return 1000 + len(self.texts)

    async def send_message_with_buttons(self, chat_id, text, buttons):
        self.send_attempts += 1
        self.buttoned.append((chat_id, text, buttons))
        if self.send_attempts <= self.fail_until_attempt:
            from app.telegram import TelegramError
            raise TelegramError("simulated outage")
        return 500 + len(self.buttoned)

    async def update_message_buttons(self, chat_id, message_id, buttons):
        self.button_updates.append((chat_id, message_id, buttons))

    async def answer_callback(self, callback_id, text=""):
        self.answered.append(callback_id)

    def last_text(self) -> str:
        return (self.texts[-1][1] if self.texts else self.buttoned[-1][1])


@pytest.fixture()
def tg():
    return FakeTelegram()


@pytest.fixture()
def fixed_today(monkeypatch):
    """Legacy seam: pins `changesets.date.today()` to 2026-01-20.

    Draft start dates no longer read date.today() at all — they derive from an
    explicit now_utc + the user's timezone (review E1). Call sites pass their
    simulated clock; this patch stays only so test code that assumes the epoch
    keeps working if date.today() is ever referenced again."""
    from datetime import date as real_date
    import app.changesets as cs

    class _FakeDate:
        @staticmethod
        def today():
            return real_date(2026, 1, 20)

    monkeypatch.setattr(cs, "date", _FakeDate)
    return real_date(2026, 1, 20)
