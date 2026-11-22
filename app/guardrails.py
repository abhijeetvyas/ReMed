"""Guardrails — anti-abuse layer (per chat).

The bot is one attacker (or one stuck loop) away from an AI bill, a bloated
SQLite file, and a flooded reminder queue: every photo is a paid vision call
plus a permanently-persisted image BLOB, every edit text a paid interpret call.
This module adds cheap, realistic guardrails:

1. Per-chat sliding-window rate limits, per action bucket (photo / edit /
   apply): hourly + daily caps, plus a minimum gap between consecutive actions
   — throttles bursts without touching a genuine user's flow.
2. Photo-failure backoff — repeated unreadable photos stop spending AI calls
   and stop persisting blobs after a few failures in the window.
3. Super users — a chat verified against the SUPER_USER_KEY secret (via the
   /super command) bypasses every limit; meant for founder testing.
4. Global AI daily budget — when today's ai_calls rows reach AI_DAILY_BUDGET,
   new AI calls short-circuit with an "at capacity" message (protects the one
   shared free-tier API key that every user spends from).
5. Auto-quarantine — AUTO_BLOCK_TRIPS guardrail blocks in an hour marks the
   chat blocked in the DB (persisted; /unblock by a super user lifts it).
6. Concurrency slot — a bounded global semaphore plus per-chat serialization,
   so events are handled in arrival order per chat and a flood cannot spawn
   unbounded parallel work.
7. Memory cap on this module's own tables — eviction at 80% of
   GUARDRAIL_WINDOW_CAP, oldest 10% evicted (same policy as retention.py;
   covers the rate-limit window table and the per-chat lock table).

In-memory only, which fits the single-process deployment (webhook + engine in
one process). A restart clears the windows — the worst case is one bucket of
extra calls right after a deploy, which is acceptable here.
"""
import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger("remed.guardrails")

WARN_FRACTION = 0.7     # matches retention.py policy
TRIGGER_FRACTION = 0.8
PRUNE_FRACTION = 0.1


@dataclass
class _Window:
    hour: deque = field(default_factory=deque)    # timestamps of accepted actions (last 60 min)
    day: deque = field(default_factory=deque)     # timestamps of accepted actions (last 24 h)
    last_action: float = 0.0
    photo_fails: deque = field(default_factory=deque)  # timestamps of failed extractions
    trips: deque = field(default_factory=deque)   # timestamps of guardrail blocks (auto-quarantine)


# (chat_id, bucket) -> window. defaultdict keeps it allocation-free per event.
_windows: defaultdict[tuple[str, str], _Window] = defaultdict(_Window)

_MAX_AGE_HOUR = 3600.0
_MAX_AGE_DAY = 86400.0

# ---- user-facing copy (kept here so handlers stay flow-only) ----------------

_MSGS = {
    "photo": ("📷 You've sent a lot of prescription photos in a short time. "
              "Give me about {mins} minute{s} and try again."),
    "photo_day": "📷 You've hit today's limit of {limit} photos. Please try again tomorrow.",
    "photo_fails": ("I keep having trouble reading your photos. Please send a "
                    "single, clear, well-lit photo of just the prescription — "
                    "try again in a few minutes."),
    "edit": "✏️ That's a lot of edits at once — give me a moment, then try again in about {mins} minute{s}.",
    "edit_day": "✏️ You've hit today's limit of {limit} edits. Please try again tomorrow.",
    "apply": "⏳ Hold on — you've applied a lot of changes in a short time. Try again in about {mins} minute{s}.",
    "apply_day": "⏳ You've hit today's limit of {limit} applies. Please try again tomorrow.",
    "too_long": "📝 That message is very long. Please break it into a shorter edit, one change at a time.",
    "budget": "🤖 I'm at capacity for today — Google's free AI quota is used up. Please try again tomorrow.",
    "quarantined": ("🚫 Too many blocked attempts — this chat is now blocked. "
                    "If you think that's a mistake, contact the bot owner."),
}


def _fmt_wait(seconds: float) -> str:
    mins = max(1, round(seconds / 60))
    return mins, "" if mins == 1 else "s"


# ---- buckets ----------------------------------------------------------------

_PHOTO = "photo"
_EDIT = "edit"
_APPLY = "apply"


def _window_for(chat_id: str, bucket: str) -> _Window:
    """Get-or-create; when the table would exceed 80% of its cap, evict the
    10% least-recently-active chats (same policy as retention.py, in memory)."""
    key = (chat_id, bucket)
    w = _windows.get(key)
    if w is not None:
        return w
    cap = settings.guardrail_window_cap
    if cap and len(_windows) >= int(TRIGGER_FRACTION * cap):
        n = max(1, int(len(_windows) * PRUNE_FRACTION))
        stale = sorted(_windows.items(), key=lambda kv: kv[1].last_action)[:n]
        for k, _ in stale:
            _windows.pop(k, None)
        log.warning("guardrail windows at cap %d — evicted %d least-active chats", cap, len(stale))
    return _windows.setdefault(key, _Window())


def check(chat_id: str, bucket: str, is_super: bool = False) -> str | None:
    """Record the action if allowed; return a user-facing block message if not.

    Blocks on: hourly count exceeded, daily count exceeded, minimum gap between
    actions, or (photo only) too many failed extractions. Super users bypass
    everything. Call it BEFORE doing expensive work — a returned message means
    the caller must send it and stop.
    """
    if is_super:
        return None
    w = _window_for(chat_id, bucket)
    now = time.time()

    # photo-failure backoff: repeated unreadable photos look like spam (or a
    # very blurry shelf) — either way, stop paying for them
    if bucket == _PHOTO and len(w.photo_fails) >= settings.photo_fails_per_hour:
        first = w.photo_fails[0]
        if now - first < _MAX_AGE_HOUR:
            mins, plural = _fmt_wait(_MAX_AGE_HOUR - (now - first))
            return _MSGS["photo_fails"].format(mins=mins, s=plural)
        w.photo_fails.clear()

    while w.hour and now - w.hour[0] > _MAX_AGE_HOUR:
        w.hour.popleft()
    while w.day and now - w.day[0] > _MAX_AGE_DAY:
        w.day.popleft()

    limit_hour = {
        _PHOTO: settings.photo_rate_per_hour,
        _EDIT: settings.edit_rate_per_hour,
        _APPLY: settings.apply_rate_per_hour,
    }[bucket]
    limit_day = {
        _PHOTO: settings.photo_rate_per_day,
        _EDIT: settings.edit_rate_per_day,
        _APPLY: settings.apply_rate_per_day,
    }[bucket]
    if len(w.day) >= limit_day:
        return _MSGS[f"{bucket}_day"].format(limit=limit_day)
    if len(w.hour) >= limit_hour:
        mins, plural = _fmt_wait(_MAX_AGE_HOUR - (now - w.hour[0]))
        return _MSGS[bucket].format(mins=mins, s=plural)

    gap = {
        _PHOTO: settings.photo_min_gap_seconds,
        _EDIT: settings.edit_min_gap_seconds,
        _APPLY: settings.apply_min_gap_seconds,
    }[bucket]
    if gap and now - w.last_action < gap:
        mins, plural = _fmt_wait(gap - (now - w.last_action))
        return _MSGS[bucket].format(mins=mins, s=plural)

    w.hour.append(now)
    w.day.append(now)
    w.last_action = now
    return None


def record_photo_failure(chat_id: str) -> None:
    """Count a failed photo extraction toward the backoff limit (super users
    still get backoff: it signals a real extraction problem, not abuse)."""
    w = _window_for(chat_id, _PHOTO)
    now = time.time()
    w.photo_fails.append(now)
    while len(w.photo_fails) > settings.photo_fails_per_hour:
        w.photo_fails.popleft()


def record_trip(chat_id: str) -> bool:
    """Called whenever a guardrail block message is sent. Returns True when the
    chat has collected AUTO_BLOCK_TRIPS blocks in the last hour — the caller
    then sets users.blocked (auto-quarantine: the per-chat limits are working
    'correctly', but the pattern is collectively abusive)."""
    w = _window_for(chat_id, "trips")
    now = time.time()
    w.trips.append(now)
    while w.trips and now - w.trips[0] > _MAX_AGE_HOUR:
        w.trips.popleft()
    return len(w.trips) >= settings.auto_block_trips


def ai_capacity_block() -> str | None:
    """Global daily AI budget across ALL users (super users included — it
    protects the one shared API key whose free-tier quota everyone competes
    for). Counts today's ai_calls rows (an index-only scan); every budget-exceeding
    day short-circuits BEFORE the call so the pool is preserved. Returns the
    user-facing message to send, or None to proceed."""
    if settings.ai_daily_budget <= 0:
        return None
    from . import db
    start_of_day = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    used = db.query_one(
        "SELECT COUNT(*) AS c FROM ai_calls WHERE created_at >= ?", (start_of_day,))["c"]
    if used >= settings.ai_daily_budget:
        return _MSGS["budget"]
    return None


def text_too_long(text: str) -> bool:
    return len(text) > settings.max_text_len


def text_too_long_message() -> str:
    return _MSGS["too_long"]


def quarantined_message() -> str:
    return _MSGS["quarantined"]


# ---- concurrency ------------------------------------------------------------

_sem = None          # asyncio.Semaphore, (re)created per running event loop
_sem_loop = None
_chat_locks: dict[tuple[int, str], asyncio.Lock] = {}  # (id(loop), chat_id) -> lock


@asynccontextmanager
async def slot(chat_id: str):
    """Bounded concurrency + per-chat ordering.

    One chat's events are processed one at a time (in arrival order — a user
    typing fast must not have edits applied out of sequence), and at most
    MAX_CONCURRENT_HANDLERS chats proceed at once.

    The per-chat lock table is capped like the window table (7. memory cap):
    at 80% of GUARDRAIL_WINDOW_CAP the idle (unlocked) 10% are evicted; a held
    lock is never evicted, so per-chat serialization can't be broken under it.
    """
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(settings.max_concurrent_handlers)
        _sem_loop = loop
    cap = settings.guardrail_window_cap
    if cap and len(_chat_locks) >= cap:
        n = max(1, len(_chat_locks) // 10)
        evicted = 0
        for k in [k for k, v in _chat_locks.items() if not v.locked()]:
            if evicted >= n:
                break
            _chat_locks.pop(k, None)
            evicted += 1
        if evicted:
            log.warning("guardrail chat-locks at cap %d — evicted %d idle chats", cap, evicted)
    async with _sem:
        lock = _chat_locks.setdefault((id(loop), chat_id), asyncio.Lock())
        async with lock:
            yield


# ---- tests / ops ------------------------------------------------------------

def reset() -> None:
    _windows.clear()
    _chat_locks.clear()
    global _sem, _sem_loop
    _sem = None
    _sem_loop = None
