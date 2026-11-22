"""ReMed configuration — everything from environment variables / .env."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments). Never overrides real env."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        break


_load_dotenv()

SLOTS = ("breakfast", "lunch", "dinner")
FOODS = ("before", "after")
FORMS = ("tablet", "capsule", "syrup", "other")

# All user-facing times live on this grid: meal times, food offsets, and the
# engine tick. Keeps reminder delivery predictable (≤1 tick late) and the
# mental model simple (:00 / :15 / :30 / :45).
TIME_GRID_MIN = 15


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    # Telegram (founder account — see README)
    bot_token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    webhook_secret: str = field(default_factory=lambda: os.environ.get("WEBHOOK_SECRET", "remed-dev-secret"))

    # AI layer: any OpenAI-compatible provider (OpenAI, Mistral, Groq, OpenRouter…)
    ai_base_url: str = field(default_factory=lambda: os.environ.get("AI_BASE_URL", "https://api.mistral.ai/v1"))
    ai_api_key: str = field(default_factory=lambda: os.environ.get("AI_API_KEY", ""))
    ai_model: str = field(default_factory=lambda: os.environ.get("AI_MODEL", "mistral-medium-latest"))

    # Defaults a new user starts with (per-user values — nothing hard-coded in logic)
    default_timezone: str = field(default_factory=lambda: os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata"))
    meal_breakfast: str = field(default_factory=lambda: os.environ.get("MEAL_BREAKFAST", "08:00"))
    meal_lunch: str = field(default_factory=lambda: os.environ.get("MEAL_LUNCH", "14:00"))
    meal_dinner: str = field(default_factory=lambda: os.environ.get("MEAL_DINNER", "19:00"))
    offset_before_min: int = field(default_factory=lambda: _int("OFFSET_BEFORE_MIN", -15))
    offset_after_min: int = field(default_factory=lambda: _int("OFFSET_AFTER_MIN", 45))

    # Reminder engine (tick default: every 15 min, matching the time grid)
    tick_seconds: int = field(default_factory=lambda: _int("TICK_SECONDS", 900))
    reminder_window_min: int = field(default_factory=lambda: _int("REMINDER_WINDOW_MIN", 180))  # after this, silent missed
    max_send_attempts: int = field(default_factory=lambda: _int("MAX_SEND_ATTEMPTS", 3))
    retry_backoff_seconds: int = field(default_factory=lambda: _int("RETRY_BACKOFF_SECONDS", 30))

    # Limits
    max_duration_days: int = field(default_factory=lambda: _int("MAX_DURATION_DAYS", 90))
    conversation_ttl_min: int = field(default_factory=lambda: _int("CONVERSATION_TTL_MIN", 30))

    # Storage / ops
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "remed.db"))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_api_key and self.ai_model)


settings = Settings()
