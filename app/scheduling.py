"""Scheduling math — the heart (deep dive §5).

All send times are precomputed at Apply: meal anchor (user local, user tz) + food
offset, stored as UTC. Food never handled at reminder time; DST is safe because
times are derived per event_date at (re)generation time.
"""
import json
import math
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .config import SLOTS, settings


class ValidationError(Exception):
    pass


# ---------------------------------------------------------------- time helpers

def parse_hhmm(value: str) -> time:
    try:
        h, m = value.strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        raise ValidationError(f"'{value}' is not a HH:MM time")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValidationError(f"'{value}' is out of range")
    return time(h, m)


def fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


def today_local(tz_name: str, now_utc: datetime) -> date:
    return now_utc.astimezone(ZoneInfo(tz_name)).date()


def fmt_time_12h(dt_local: datetime) -> str:
    return dt_local.strftime("%-I:%M %p") if hasattr(dt_local, "strftime") else str(dt_local)


# ---------------------------------------------------------------- validation (plain code, not AI)

def validate_medicine(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("medicine entry is not an object")
    name = str(raw.get("name", "")).strip()
    if not name or len(name) > 80:
        raise ValidationError("medicine name missing or too long")
    form = str(raw.get("form", "tablet")).strip().lower() or "tablet"
    if form not in ("tablet", "capsule", "syrup", "drop", "injection", "other"):
        form = "other"
    try:
        tpd = float(raw.get("tablets_per_dose", 1))
    except (TypeError, ValueError):
        raise ValidationError("tablets_per_dose must be a number")
    if not (0.25 <= tpd <= 20):
        raise ValidationError("tablets_per_dose out of range")
    slots = raw.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ValidationError("at least one slot required")
    slots = [str(s).strip().lower() for s in slots]
    if any(s not in SLOTS for s in slots):
        raise ValidationError(f"slots must be among {SLOTS}")
    # de-duplicate, keep natural meal order
    slots = [s for s in SLOTS if s in slots]
    food = str(raw.get("food", "after")).strip().lower()
    if food not in ("before", "after"):
        raise ValidationError("food must be 'before' or 'after'")
    try:
        days_raw = float(raw.get("days", raw.get("duration_days", 1)))
    except (TypeError, ValueError):
        raise ValidationError("days must be an integer")
    # quantity-derived durations can be fractional (10 tablets at 3/day = 3.33);
    # round UP — truncating would silently shorten the course (medical risk,
    # review E1 family). Exact integers pass through untouched.
    days = math.ceil(days_raw - 1e-9)
    if not (1 <= days <= settings.max_duration_days):
        raise ValidationError(f"days must be 1–{settings.max_duration_days}")
    return {
        "name": name,
        "form": form,
        "tablets_per_dose": tpd if tpd != int(tpd) else int(tpd),
        "slots": slots,
        "food": food,
        "duration_days": days,
    }


def validate_draft_medicines(raw_medicines: list) -> list[dict]:
    if not isinstance(raw_medicines, list) or not raw_medicines:
        raise ValidationError("no medicines in draft")
    # cap before validation: a hallucinating (or poisoned) model output must not
    # be able to expand into a flood of dose_events at apply time
    if len(raw_medicines) > settings.max_medicines_per_draft:
        raise ValidationError(f"too many medicines in draft (max {settings.max_medicines_per_draft})")
    return [validate_medicine(m) for m in raw_medicines]


# ---------------------------------------------------------------- expansion

def event_local_time(event_date: date, slot: str, food: str, user: dict) -> datetime:
    """Exact reminder time for one event, in the user's tz (aware datetime)."""
    meal = user["meals"][slot]
    offset = user["offset_before_min"] if food == "before" else user["offset_after_min"]
    anchor = datetime.combine(event_date, parse_hhmm(meal), tzinfo=ZoneInfo(user["timezone"]))
    return anchor + timedelta(minutes=offset)


def expand_events(chat_id: str, prescription_id: str, medicines: list[dict],
                  start_date: date, user: dict, now_utc: datetime,
                  skip_keys: set | frozenset = frozenset()) -> tuple[list[dict], date]:
    """Whole-course expansion into dose_events. Skips times already past (no
    instant burst on an afternoon confirm / recompute) and any (event_date,
    slot, medicine_id) key the user already answered — an explicit taken/skip
    is history and must never produce a second reminder. Returns (events, end_date)."""
    events: list[dict] = []
    groups: dict[tuple, str] = {}  # one reminder_group per (day, slot, food):
    # exactly the set of medicines that share one reminder message, and no more —
    # so tapping ✅ can never ack a future day's dose (see engine/_ack_group).
    end_date = start_date
    for m in medicines:
        med_end = start_date + timedelta(days=m["duration_days"] - 1)
        end_date = max(end_date, med_end)
        for day in range(m["duration_days"]):
            event_date = start_date + timedelta(days=day)
            for slot in m["slots"]:
                key = (event_date.isoformat(), slot, m.get("id"))
                if key in skip_keys:
                    continue
                group = groups.setdefault((event_date.isoformat(), slot, m["food"]),
                                          uuid.uuid4().hex[:16])
                local_time = event_local_time(event_date, slot, m["food"], user)
                send_utc = local_time.astimezone(timezone.utc)
                if send_utc <= now_utc:
                    continue  # already past — never send late
                events.append({
                    "id": uuid.uuid4().hex,
                    "medicine_id": m.get("id") or uuid.uuid4().hex,  # replaced on insert if new
                    "chat_id": chat_id,
                    "event_date": event_date.isoformat(),
                    "slot": slot,
                    "scheduled_time_utc": iso_utc(send_utc),
                    "reminder_group": group,
                })
    return events, end_date


def insert_pending_events(conn, events: list[dict]) -> None:
    for e in events:
        conn.execute(
            "INSERT INTO dose_events(id, medicine_id, chat_id, event_date, slot, scheduled_time_utc,"
            " status, reminder_group) VALUES(?,?,?,?,?,?, 'pending', ?)",
            (e["id"], e["medicine_id"], e["chat_id"], e["event_date"], e["slot"],
             e["scheduled_time_utc"], e["reminder_group"]),
        )
        conn.execute(
            "INSERT INTO reminder_queue(dose_event_id, chat_id, send_at, state) VALUES(?,?,?, 'pending')",
            (e["id"], e["chat_id"], e["scheduled_time_utc"]),
        )


# ---------------------------------------------------------------- recompute (edit path)

def recompute_future(conn, chat_id: str, prescription_id: str, user: dict, now_utc: datetime) -> None:
    """Medicine/meal/offset/timezone edits: delete FUTURE pending rows for this
    prescription and regenerate. Past rows (taken/skipped/missed) untouched."""
    med_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM medicines WHERE prescription_id=? AND removed=0", (prescription_id,)
    ).fetchall()]
    if not med_ids:
        return
    marks = ",".join("?" * len(med_ids))
    pending = conn.execute(
        f"SELECT id FROM dose_events WHERE medicine_id IN ({marks}) AND status='pending'"
        " AND scheduled_time_utc > ?", (*med_ids, iso_utc(now_utc))
    ).fetchall()
    for row in pending:
        conn.execute("DELETE FROM reminder_queue WHERE dose_event_id=?", (row["id"],))
        conn.execute("DELETE FROM dose_events WHERE id=?", (row["id"],))
    rx = conn.execute("SELECT * FROM prescriptions WHERE id=?", (prescription_id,)).fetchone()
    if rx is None:
        return
    meds = [dict(r) | {"slots": json.loads(r["slots"])} for r in
            conn.execute("SELECT * FROM medicines WHERE prescription_id=? AND removed=0 ORDER BY created_at",
                         (prescription_id,)).fetchall()]
    # doses the user explicitly answered (taken/skipped) are history — never re-remind them,
    # even when meal/offset edits move their original time slot
    marks = ",".join("?" * len(med_ids))
    skip_keys = {(r["event_date"], r["slot"], r["medicine_id"]) for r in conn.execute(
        f"SELECT event_date, slot, medicine_id FROM dose_events"
        f" WHERE medicine_id IN ({marks}) AND status IN ('taken','skipped')", med_ids).fetchall()}
    start = date.fromisoformat(rx["start_date"])
    events, end_date = expand_events(chat_id, prescription_id, meds, start, user, now_utc, skip_keys)
    conn.execute("UPDATE prescriptions SET end_date=? WHERE id=?", (end_date.isoformat(), prescription_id))
    insert_pending_events(conn, events)


# ---------------------------------------------------------------- queries for surface

def todays_remaining(chat_id: str, now_utc: datetime) -> list:
    """Pending events for the user's local 'today', still in the future."""
    user = db.get_user(chat_id)
    if not user:
        return []
    today = today_local(user["timezone"], now_utc).isoformat()
    return db.query(
        "SELECT de.*, m.name, m.tablets_per_dose, m.food FROM dose_events de"
        " JOIN medicines m ON m.id = de.medicine_id"
        " WHERE de.chat_id=? AND de.event_date=? AND de.status='pending' AND de.scheduled_time_utc > ?"
        " ORDER BY de.scheduled_time_utc",
        (chat_id, today, iso_utc(now_utc)),
    )
