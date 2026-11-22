"""Reminder engine — one async loop, DB-backed (restart-safe, deep dive §8).

tick(now): pick due rows → render → send → delivery ack. Failures retry with
backoff within the slot window, then expire silently. A window that passes
unanswered marks the dose silently missed. Paused users get nothing sent;
their windows still pass (recorded silently, per PRD v0.4). Finished courses
transition active → expired (computed per deep dive §6; expiry notifications
remain out of scope).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import db, rendering, retention, scheduling
from .config import settings
from .telegram import Telegram, TelegramError

log = logging.getLogger("remed.engine")


def _missed_sweep(now: datetime) -> None:
    cutoff = scheduling.iso_utc(now - timedelta(minutes=settings.reminder_window_min))
    with db.tx() as conn:
        # stale 'sending' rows = process died mid-send; release them for retry or expiry
        conn.execute(
            "UPDATE reminder_queue SET state='pending', attempts=attempts+1"
            " WHERE state='sending' AND send_at < ?", (cutoff,))
        conn.execute(
            "UPDATE dose_events SET status='missed'"
            " WHERE status='pending' AND scheduled_time_utc < ?", (cutoff,))
        conn.execute(
            "UPDATE reminder_queue SET state='expired'"
            " WHERE state IN ('pending','sending') AND send_at < ?", (cutoff,))


def _expire_prescriptions(now: datetime) -> None:
    """Courses whose last day is over transition active → expired (deep dive §6:
    expiry is computed). Without this, /prescriptions reports ended courses as
    'active' forever and MAX_ACTIVE_PRESCRIPTIONS silently locks long-term
    users out (review E3). A course is expired once the user's LOCAL date has
    moved past end_date — the reminder window has long passed by then."""
    rows = db.query(
        "SELECT p.id, p.chat_id, p.end_date, u.timezone FROM prescriptions p"
        " JOIN users u ON u.chat_id = p.chat_id"
        " WHERE p.status='active' AND p.end_date IS NOT NULL")
    if not rows:
        return
    local_today: dict[str, str | None] = {}
    with db.tx() as conn:
        for r in rows:
            tz = r["timezone"]
            if tz not in local_today:
                try:
                    local_today[tz] = scheduling.today_local(tz, now).isoformat()
                except Exception:  # a broken stored tz must never stall the tick
                    local_today[tz] = None
            if local_today[tz] and r["end_date"] < local_today[tz]:
                conn.execute("UPDATE prescriptions SET status='expired' WHERE id=?", (r["id"],))
                db.log_activity(conn, r["chat_id"], "prescription_expired",
                                {"prescription_id": r["id"]})


async def tick(tg: Telegram, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    _missed_sweep(now)
    _expire_prescriptions(now)
    now_s = scheduling.iso_utc(now)

    due = db.query(
        "SELECT rq.dose_event_id, rq.chat_id, rq.attempts, de.reminder_group,"
        "       de.scheduled_time_utc, de.slot, m.name, m.tablets_per_dose, m.food,"
        "       u.timezone, u.meal_breakfast, u.meal_lunch, u.meal_dinner,"
        "       u.offset_before_min, u.offset_after_min"
        " FROM reminder_queue rq"
        " JOIN dose_events de ON de.id = rq.dose_event_id"
        " JOIN medicines m ON m.id = de.medicine_id"
        " JOIN prescriptions p ON p.id = m.prescription_id"
        " JOIN users u ON u.chat_id = rq.chat_id"
        " WHERE rq.state='pending' AND rq.send_at <= ? AND de.status='pending'"
        "   AND u.paused=0 AND p.paused=0 AND p.status='active'"
        " ORDER BY de.scheduled_time_utc LIMIT 200",
        (now_s,),
    )
    if not due:
        return

    groups: dict[str, list] = {}
    for row in due:
        groups.setdefault(row["reminder_group"], []).append(row)

    for group, rows in groups.items():
        chat_id = rows[0]["chat_id"]
        # send pacing: every user's default grid is identical, so a burst of N
        # chats would otherwise fire back-to-back and press Telegram's ~30 msg/s
        # global limit (429s → retries → pile-up). A fixed spacing plus a small
        # deterministic per-chat jitter (stable per chat across restarts) spreads
        # the burst inside the tick — delivery within a few minutes of the slot.
        if settings.send_spacing_seconds > 0 or settings.send_jitter_seconds > 0:
            jitter = (abs(int(chat_id)) % 100) / 100 * settings.send_jitter_seconds
            await asyncio.sleep(settings.send_spacing_seconds + jitter)
        with db.tx() as conn:  # mark in-flight so a concurrent tick can't double-send
            for row in rows:
                conn.execute("UPDATE reminder_queue SET state='sending' WHERE dose_event_id=?",
                             (row["dose_event_id"],))
        events = []
        for r in rows:
            events.append({
                "scheduled_time_utc": r["scheduled_time_utc"],
                "slot": r["slot"],
                "food": r["food"],
                "name": r["name"],
                "tablets_per_dose": r["tablets_per_dose"],
                "offset_before_min": r["offset_before_min"],
                "offset_after_min": r["offset_after_min"],
                "reminder_group": r["reminder_group"],
            })
        try:
            text, buttons = rendering.reminder_message(events, rows[0]["timezone"])
            message_id = await tg.send_message_with_buttons(chat_id, text, buttons)
        except TelegramError as e:
            log.warning("send failed for chat (no contents logged): %s", e)
            attempts = rows[0]["attempts"] + 1
            failed = attempts >= settings.max_send_attempts
            next_at = scheduling.iso_utc(now + timedelta(seconds=settings.retry_backoff_seconds))
            with db.tx() as conn:
                for row in rows:
                    conn.execute(
                        "UPDATE reminder_queue SET state=?, attempts=?, send_at=?, last_error=?"
                        " WHERE dose_event_id=?",
                        ("expired" if failed else "pending", attempts, next_at, str(e)[:200], row["dose_event_id"]))
            continue
        with db.tx() as conn:
            for row in rows:
                conn.execute("UPDATE reminder_queue SET state='sent' WHERE dose_event_id=?", (row["dose_event_id"],))
                conn.execute("UPDATE dose_events SET reminder_message_id=? WHERE id=?",
                             (message_id, row["dose_event_id"]))


async def run_loop(tg: Telegram) -> None:
    log.info("reminder engine started (tick=%ss)", settings.tick_seconds)
    while True:
        try:
            now = datetime.now(timezone.utc)
            await tick(tg, now)
            retention.maintenance_if_due(now)  # capacity-triggered pruning, once/day
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("engine tick failed")
        await asyncio.sleep(settings.tick_seconds)
