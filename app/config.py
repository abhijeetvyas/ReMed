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


_GEMINI_CHAIN = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite"


def _ai_models() -> list[str]:
    """Model fallback chain: AI_MODELS (comma-separated). Falls back to the old
    single AI_MODEL var, else the default Gemini chain."""
    raw = os.environ.get("AI_MODELS", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    legacy = os.environ.get("AI_MODEL", "").strip()
    if legacy:
        return [legacy]
    return [m for m in _GEMINI_CHAIN.split(",") if m]


@dataclass
class Settings:
    # Telegram (founder account — see README)
    bot_token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    webhook_secret: str = field(default_factory=lambda: os.environ.get("WEBHOOK_SECRET", "remed-dev-secret"))

    # AI layer: any OpenAI-compatible provider. Default: Google Gemini via its
    # OpenAI-compatible endpoint (key from AI Studio). A FALLBACK CHAIN of
    # models handles per-model rate limits: try in order, fall through on
    # 429/5xx/timeout. Chain is data (environment), not code — add models freely.
    ai_base_url: str = field(default_factory=lambda: os.environ.get(
        "AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"))
    ai_api_key: str = field(default_factory=lambda: os.environ.get(
        "GOOGLE_AI_API_KEY", "") or os.environ.get("AI_API_KEY", ""))
    ai_models: list[str] = field(default_factory=_ai_models)
    # deprecated single-model var, kept as fallback when AI_MODELS is unset
    ai_model: str = field(default_factory=lambda: os.environ.get("AI_MODEL", ""))

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

    # Guardrails (anti-abuse, per chat — generous for real users, tight vs floods).
    # Each photo is a paid vision call + a persisted image BLOB; each edit text a
    # paid interpret call; each /type description a paid text-extract call.
    # Limits are hourly + daily — the hourly default is 24 for now (matching the
    # daily cap, i.e. effectively non-binding until tuned) — plus a minimum gap
    # (cooldown) between consecutive actions: 30s for the heavy buckets (paid AI
    # calls + photo download/compress), 5s for the cheap apply mutation (plain
    # SQLite work behind a button confirm). Super users verified against
    # SUPER_USER_KEY (see /super) bypass every limit including the cooldowns —
    # for founder testing. Empty key = feature off.
    photo_rate_per_day: int = field(default_factory=lambda: _int("PHOTO_RATE_PER_DAY", 24))
    photo_rate_per_hour: int = field(default_factory=lambda: _int("PHOTO_RATE_PER_HOUR", 24))
    photo_min_gap_seconds: int = field(default_factory=lambda: _int("PHOTO_MIN_GAP_SECONDS", 30))
    photo_fails_per_hour: int = field(default_factory=lambda: _int("PHOTO_FAILS_PER_HOUR", 4))
    edit_rate_per_day: int = field(default_factory=lambda: _int("EDIT_RATE_PER_DAY", 24))
    edit_rate_per_hour: int = field(default_factory=lambda: _int("EDIT_RATE_PER_HOUR", 24))
    edit_min_gap_seconds: int = field(default_factory=lambda: _int("EDIT_MIN_GAP_SECONDS", 30))
    apply_rate_per_day: int = field(default_factory=lambda: _int("APPLY_RATE_PER_DAY", 24))
    apply_rate_per_hour: int = field(default_factory=lambda: _int("APPLY_RATE_PER_HOUR", 24))
    apply_min_gap_seconds: int = field(default_factory=lambda: _int("APPLY_MIN_GAP_SECONDS", 5))
    super_user_key: str = field(default_factory=lambda: os.environ.get("SUPER_USER_KEY", "").strip())
    max_text_len: int = field(default_factory=lambda: _int("MAX_TEXT_LEN", 1200))
    max_image_mb: int = field(default_factory=lambda: _int("MAX_IMAGE_MB", 10))
    # compression before the AI call and before persisting (see app/images.py)
    image_max_dim_px: int = field(default_factory=lambda: _int("IMAGE_MAX_DIM_PX", 1600))
    image_jpeg_quality: int = field(default_factory=lambda: _int("IMAGE_JPEG_QUALITY", 85))
    # decompression-bomb guard: refuse photos whose decoded pixel count exceeds
    # this many megapixels (Telegram itself caps photo kind at ~1280px, this is
    # defense in depth) — see app/images.py
    image_max_pixels_mp: float = field(default_factory=lambda: _float("IMAGE_MAX_PIXELS_MP", 25.0))
    max_medicines_per_draft: int = field(default_factory=lambda: _int("MAX_MEDICINES_PER_DRAFT", 15))
    max_active_prescriptions: int = field(default_factory=lambda: _int("MAX_ACTIVE_PRESCRIPTIONS", 10))
    max_concurrent_handlers: int = field(default_factory=lambda: _int("MAX_CONCURRENT_HANDLERS", 8))

    # Global AI budget (the #1 free-tier protection): the Gemini free-tier daily
    # request pool is SHARED by every user through the one API key, and a chat's
    # own limits say nothing about total spend. When today's ai_calls rows reach
    # this many, new AI calls short-circuit with an "at capacity" message
    # instead of spending (0 = off). One logical call may cost up to 3 provider
    # attempts via the fallback chain — set the budget accordingly.
    ai_daily_budget: int = field(default_factory=lambda: _int("AI_DAILY_BUDGET", 800))

    # Capacity-triggered retention (app/retention.py): NO pruning below 80% of
    # a store's cap; at 80% the oldest 10% is dropped (70% logs a warning
    # first). Byte cap for ai_calls images (the heavy store); row caps for the
    # message/activity logs. Golden set: export before prunes touch it.
    ai_image_budget_kb: int = field(default_factory=lambda: _int("AI_IMAGE_BUDGET_KB", 1024 * 1024))
    logs_max_rows: int = field(default_factory=lambda: _int("LOGS_MAX_ROWS", 1_000_000))

    # Reminder-send pacing: the default meal grid is identical for every user,
    # so without pacing the whole user base's reminders fire in one burst and
    # press Telegram's ~30 msg/s global limit. Sends are spread inside the
    # engine tick: a fixed gap between chat groups plus a small deterministic
    # per-chat jitter (hash of chat_id, stable across restarts).
    send_spacing_seconds: float = field(default_factory=lambda: _float("SEND_SPACING_SECONDS", 0.25))
    send_jitter_seconds: float = field(default_factory=lambda: _float("SEND_JITTER_SECONDS", 2.0))

    # Guardrails memory cap: eviction when the per-chat window table exceeds 80%
    # of this many chats (see app/guardrails.py).
    guardrail_window_cap: int = field(default_factory=lambda: _int("GUARDRAIL_WINDOW_CAP", 50_000))

    # Auto-quarantine: this many guardrail blocks (rate-limit trips) in one hour
    # marks the chat blocked — all further events are ignored until /unblock
    # (founder, via /super key first).
    auto_block_trips: int = field(default_factory=lambda: _int("AUTO_BLOCK_TRIPS", 10))

    # Storage / ops
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "remed.db"))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_api_key and self.ai_models)


settings = Settings()
