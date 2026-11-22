"""SQLite layer — 6 data tables (+ meta, ai_calls, user_messages logs), plain SQL. Times stored as UTC ISO-8601 strings."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users(
  chat_id TEXT PRIMARY KEY,          -- channel identity (telegram). Planned rename → channel_ref at WhatsApp time.
  id TEXT UNIQUE,                    -- internal user id (uuid); the stable cross-channel key for the future
  channel TEXT DEFAULT 'telegram',   -- which channel this chat_id belongs to
  full_name TEXT,                    -- future PII fields; empty today (bot stores no names)
  phone TEXT,
  email TEXT,
  timezone TEXT NOT NULL,
  meal_breakfast TEXT NOT NULL DEFAULT '08:00',
  meal_lunch TEXT NOT NULL DEFAULT '14:00',
  meal_dinner TEXT NOT NULL DEFAULT '19:00',
  offset_before_min INTEGER NOT NULL DEFAULT -15,
  offset_after_min INTEGER NOT NULL DEFAULT 45,
  paused INTEGER NOT NULL DEFAULT 0, -- user-level master switch; see prescriptions.paused
  created_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS prescriptions(
  id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL REFERENCES users(chat_id),
  start_date TEXT NOT NULL,
  end_date TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('draft','active','cancelled','expired')),
  paused INTEGER NOT NULL DEFAULT 0, -- prescription-level pause (founder: pause is per prescription)
  image_deleted INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medicines(
  id TEXT PRIMARY KEY,
  prescription_id TEXT NOT NULL REFERENCES prescriptions(id),
  name TEXT NOT NULL,
  form TEXT NOT NULL DEFAULT 'tablet',
  tablets_per_dose REAL NOT NULL DEFAULT 1,
  slots TEXT NOT NULL,               -- JSON array
  food TEXT NOT NULL CHECK(food IN ('before','after')),
  duration_days INTEGER NOT NULL,
  removed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dose_events(
  id TEXT PRIMARY KEY,
  medicine_id TEXT NOT NULL REFERENCES medicines(id),
  chat_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  slot TEXT NOT NULL,
  scheduled_time_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','taken','skipped','missed')),
  responded_at TEXT,
  reminder_message_id INTEGER,
  reminder_group TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_queue(
  dose_event_id TEXT PRIMARY KEY REFERENCES dose_events(id),
  chat_id TEXT NOT NULL,
  send_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','sent','failed','expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS conversation_state(
  chat_id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK(state IN ('pending_draft','pending_edit','pending_delete')),
  payload TEXT,
  expires_at TEXT NOT NULL
);

-- AI observability (founder ask): every AI call ① extract / ② interpret, with
-- input, output and metadata, to build a golden dataset and tune prompts later.
CREATE TABLE IF NOT EXISTS ai_calls(
  id TEXT PRIMARY KEY,
  chat_id TEXT,                      -- null only if identity creation itself failed
  kind TEXT NOT NULL CHECK(kind IN ('extract','interpret')),
  model TEXT,
  input_text TEXT,                   -- interpret: user request + context; extract: null
  input_image BLOB,                  -- extract: the photo bytes (kept for the golden set — privacy exception, see README)
  output TEXT,                       -- raw model JSON, or null on failure
  ok INTEGER NOT NULL,
  error TEXT,
  latency_ms INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_chat ON ai_calls(chat_id, created_at);

-- Full inbound message log (founder ask): every command / free text / photo
-- the user sends, for debugging and future model improvement.
CREATE TABLE IF NOT EXISTS user_messages(
  id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- 'command' | 'free_text' | 'photo'
  text TEXT,                         -- null for photos (kind='photo')
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_messages_chat ON user_messages(chat_id, created_at);

-- Activity log (founder ask): what the user did to their prescriptions —
-- created / edited / cancelled / paused / resumed / deleted — with a short
-- JSON detail. Complements ai_calls (AI I/O) and user_messages (raw inbound).
CREATE TABLE IF NOT EXISTS activity_log(
  id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  action TEXT NOT NULL,              -- prescription_created | prescription_updated | prescription_cancelled
                                     -- | prescriptions_paused | prescriptions_resumed | user_settings_updated
                                     -- | all_data_deleted
  detail TEXT,                       -- small JSON summary (names, counts)
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_chat ON activity_log(chat_id, created_at);

CREATE INDEX IF NOT EXISTS idx_queue_due ON reminder_queue(send_at, state);
CREATE INDEX IF NOT EXISTS idx_events_user_date ON dose_events(chat_id, event_date);
CREATE INDEX IF NOT EXISTS idx_events_medicine ON dose_events(medicine_id);
CREATE INDEX IF NOT EXISTS idx_medicines_rx ON medicines(prescription_id);
"""


def init(db_path: str | None = None) -> None:
    global _conn
    with _lock:
        if _conn is not None:
            return
        _conn = sqlite3.connect(db_path or settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate()
        _conn.commit()


def _migrate() -> None:
    """Column additions for databases created before these existed (SQLite has
    no ADD COLUMN IF NOT EXISTS — errors are expected and ignored)."""
    alts = (
        "ALTER TABLE users ADD COLUMN id TEXT",
        "ALTER TABLE users ADD COLUMN channel TEXT DEFAULT 'telegram'",
        "ALTER TABLE users ADD COLUMN full_name TEXT",
        "ALTER TABLE users ADD COLUMN phone TEXT",
        "ALTER TABLE users ADD COLUMN email TEXT",
        "ALTER TABLE prescriptions ADD COLUMN paused INTEGER NOT NULL DEFAULT 0",
    )
    for stmt in alts:
        try:
            _conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # backfill internal ids for rows created before the column existed
    _conn.execute("UPDATE users SET id=? WHERE id IS NULL", (uuid.uuid4().hex,))
    _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_id ON users(id)")


def get_conn() -> sqlite3.Connection:
    assert _conn is not None, "db.init() not called"
    return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@contextmanager
def tx():
    """Transaction context: commit on success, rollback on error."""
    conn = get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> None:
    with tx() as conn:
        conn.execute(sql, params)


# ---------------------------------------------------------------- meta / dedup

def meta_get(key: str) -> str | None:
    row = query_one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------- users

def get_user(chat_id: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM users WHERE chat_id=? AND deleted_at IS NULL", (chat_id,))


def ensure_user(chat_id: str) -> sqlite3.Row:
    """Create the identity row silently on first contact (Flow 0). Every user
    gets an internal uuid `id` in addition to the channel identity (chat_id),
    so future cross-channel / cross-table references need not key on chat_id."""
    user = get_user(chat_id)
    if user:
        return user
    s = settings
    execute(
        "INSERT INTO users(id, channel, chat_id, timezone, meal_breakfast, meal_lunch, meal_dinner,"
        " offset_before_min, offset_after_min, paused, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "telegram", chat_id, s.default_timezone, s.meal_breakfast, s.meal_lunch,
         s.meal_dinner, s.offset_before_min, s.offset_after_min, 0, now_iso()),
    )
    return get_user(chat_id)  # type: ignore[return-value]


def user_freezes_json(user: sqlite3.Row) -> dict:
    """Snapshot of user settings used for edit sessions and AI context."""
    return {
        "timezone": user["timezone"],
        "meals": {
            "breakfast": user["meal_breakfast"],
            "lunch": user["meal_lunch"],
            "dinner": user["meal_dinner"],
        },
        "offset_before_min": user["offset_before_min"],
        "offset_after_min": user["offset_after_min"],
        "paused": bool(user["paused"]),
    }


# ---------------------------------------------------------------- prescriptions & medicines

def prescriptions_for(chat_id: str, statuses: tuple[str, ...] = ("active", "cancelled", "expired")) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return query(
        f"SELECT * FROM prescriptions WHERE chat_id=? AND status IN ({marks}) ORDER BY created_at", (chat_id, *statuses)
    )


def active_prescriptions(chat_id: str) -> list[sqlite3.Row]:
    return prescriptions_for(chat_id, ("active",))


def medicines_for(prescription_id: str, include_removed: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM medicines WHERE prescription_id=?"
    if not include_removed:
        sql += " AND removed=0"
    return query(sql + " ORDER BY created_at, name", (prescription_id,))


def live_context_json(chat_id: str) -> dict:
    """Everything the edit interpreter needs, as plain JSON (deep dive §4.2)."""
    user = get_user(chat_id)
    ctx: dict = {"user": user_freezes_json(user) if user else None, "prescriptions": []}
    for rx in active_prescriptions(chat_id):
        meds = [
            {
                "id": m["id"],
                "name": m["name"],
                "form": m["form"],
                "tablets_per_dose": m["tablets_per_dose"],
                "slots": json.loads(m["slots"]),
                "food": m["food"],
                "duration_days": m["duration_days"],
            }
            for m in medicines_for(rx["id"])
        ]
        if meds:
            ctx["prescriptions"].append(
                {"id": rx["id"], "start_date": rx["start_date"], "end_date": rx["end_date"], "medicines": meds}
            )
    return ctx


# ---------------------------------------------------------------- conversation state

def get_state(chat_id: str) -> sqlite3.Row | None:
    row = query_one("SELECT * FROM conversation_state WHERE chat_id=?", (chat_id,))
    if row and row["expires_at"] <= now_iso():
        execute("DELETE FROM conversation_state WHERE chat_id=?", (chat_id,))
        return None
    return row


def set_state(chat_id: str, state: str, payload: dict) -> None:
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(minutes=settings.conversation_ttl_min)).isoformat()
    execute(
        "INSERT INTO conversation_state(chat_id,state,payload,expires_at) VALUES(?,?,?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, payload=excluded.payload, expires_at=excluded.expires_at",
        (chat_id, state, json.dumps(payload), expires),
    )


def clear_state(chat_id: str) -> None:
    execute("DELETE FROM conversation_state WHERE chat_id=?", (chat_id,))


# ---------------------------------------------------------------- logs (AI calls & user messages)

def log_ai_call(chat_id: str | None, kind: str, *, model: str | None, input_text: str | None = None,
                input_image: bytes | None = None, output: str | None = None, ok: bool,
                error: str | None = None, latency_ms: int | None = None) -> None:
    """Persist one AI call with full input/output + metadata (golden-dataset table)."""
    execute(
        "INSERT INTO ai_calls(id, chat_id, kind, model, input_text, input_image, output, ok, error,"
        " latency_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, chat_id, kind, model, input_text,
         sqlite3.Binary(input_image) if input_image is not None else None,
         output, 1 if ok else 0, error[:500] if error else None, latency_ms, now_iso()),
    )


def log_user_message(chat_id: str, kind: str, text: str | None) -> None:
    """Persist every inbound user message (command / free text / photo)."""
    execute(
        "INSERT INTO user_messages(id, chat_id, kind, text, created_at) VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex, chat_id, kind, text, now_iso()),
    )


def log_activity(conn, chat_id: str, action: str, detail: dict | None = None) -> None:
    """Activity log — must be called with the tx `conn` so it commits atomically
    with the change it records. Use logging via db.execute wrapper when no tx
    is open (see log_activity_now)."""
    conn.execute(
        "INSERT INTO activity_log(id, chat_id, action, detail, created_at) VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex, chat_id, action,
         json.dumps(detail, ensure_ascii=False) if detail else None, now_iso()),
    )


def log_activity_now(chat_id: str, action: str, detail: dict | None = None) -> None:
    """Activity log outside an open transaction."""
    with tx() as conn:
        log_activity(conn, chat_id, action, detail)
