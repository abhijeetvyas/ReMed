"""Scheduling math — expansion, offsets, recomputation (the heart)."""
from datetime import datetime, timedelta, timezone

from app import db, scheduling
from app.scheduling import ValidationError

NOW = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)  # 17:30 IST

USER = {
    "timezone": "Asia/Kolkata",
    "meals": {"breakfast": "08:00", "lunch": "14:00", "dinner": "19:00"},
    "offset_before_min": -15,
    "offset_after_min": 45,
}


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_defaults_applied_at_user_creation(tmp_db):
    from app.config import settings
    user = db.ensure_user("111")
    assert user["timezone"] == settings.default_timezone
    assert user["meal_breakfast"] == "08:00"
    assert user["offset_before_min"] == -15
    assert user["offset_after_min"] == 45


def test_expansion_offsets_and_utc(tmp_db):
    med = {"name": "Azithro", "form": "tablet", "tablets_per_dose": 1,
           "slots": ["breakfast"], "food": "after", "duration_days": 3}
    events, end_date = scheduling.expand_events(
        "111", "rx1", [med], __import__("datetime").date(2026, 1, 20), USER, NOW)
    # after-food breakfast = 8:45 IST = 03:15 UTC; day 0 (06:15 UTC? no — 03:15) is past at NOW=12:00 UTC
    times = sorted(e["scheduled_time_utc"] for e in events)
    assert len(times) == 2
    assert times[0] == "2026-01-21T03:15:00+00:00"
    assert times[1] == "2026-01-22T03:15:00+00:00"
    assert end_date.isoformat() == "2026-01-22"


def test_before_food_offset_is_negative(tmp_db):
    med = {"name": "Panto", "form": "tablet", "tablets_per_dose": 2,
           "slots": ["breakfast"], "food": "before", "duration_days": 1}
    events, _ = scheduling.expand_events("111", "rx1", [med], __import__("datetime").date(2026, 1, 20), USER, NOW)
    assert events == []  # 7:45 IST on Jan 20 = 02:15 UTC — already past at NOW


def test_no_instant_burst_on_afternoon_confirm(tmp_db):
    med = {"name": "Dolo", "form": "tablet", "tablets_per_dose": 1,
           "slots": ["breakfast", "lunch", "dinner"], "food": "after", "duration_days": 2}
    events, _ = scheduling.expand_events(
        "111", "rx1", [med], __import__("datetime").date(2026, 1, 20), USER, NOW)
    # NOW = 17:30 IST: today's breakfast (8:45 IST) and lunch (14:45 IST) are past → skipped;
    # remaining: today's dinner (19:45 IST = 14:15 UTC) + all of tomorrow (3 events)
    times = sorted(_utc(e["scheduled_time_utc"]) for e in events)
    assert times[0] == datetime(2026, 1, 20, 14, 15, tzinfo=timezone.utc)
    assert len(events) == 4
    assert all(t > NOW for t in times)
    assert len({e["reminder_group"] for e in events}) == 4  # per (day, slot, food), not per course


def test_full_apply_creates_rows_and_queue(tmp_db, fixed_today):
    from app import changesets
    db.ensure_user("111")
    session = changesets.draft_session(db.get_user("111"), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["dinner"], "food": "after", "duration_days": 2},
    ])
    with db.tx() as conn:
        affected = changesets.apply_session(conn, "111", session, NOW)
    pid = affected[0]["prescription_id"]
    rows = db.query(
        "SELECT de.*, m.name FROM dose_events de JOIN medicines m ON m.id=de.medicine_id"
        " WHERE m.prescription_id=? ORDER BY de.scheduled_time_utc", (pid,))
    assert len(rows) == 2
    assert rows[0]["scheduled_time_utc"].startswith("2026-01-20T14:15")  # dinner 19:45 IST
    q = db.query_one("SELECT * FROM reminder_queue WHERE dose_event_id=?", (rows[0]["id"],))
    assert q["state"] == "pending"
    assert q["send_at"] == rows[0]["scheduled_time_utc"]
    assert db.query_one("SELECT end_date FROM prescriptions WHERE id=?", (pid,))["end_date"] == "2026-01-21"


def test_recompute_touches_future_only(tmp_db, fixed_today):
    from app import changesets
    db.ensure_user("111")
    session = changesets.draft_session(db.get_user("111"), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["dinner"], "food": "after", "duration_days": 3},
    ])
    with db.tx() as conn:
        pid = changesets.apply_session(conn, "111", session, NOW)[0]["prescription_id"]

    # simulate the past: mark day-0 event taken
    past = db.query_one(
        "SELECT de.* FROM dose_events de JOIN medicines m ON m.id=de.medicine_id"
        " WHERE m.prescription_id=? ORDER BY de.scheduled_time_utc LIMIT 1", (pid,))
    db.execute("UPDATE dose_events SET status='taken', responded_at=? WHERE id=?", (db.now_iso(), past["id"]))

    # edit: dinner now at 20:30 → full recompute of that prescription
    user2 = {**USER, "meals": {**USER["meals"], "dinner": "20:30"}}
    with db.tx() as conn:
        scheduling.recompute_future(conn, "111", pid, user2, NOW)

    assert db.query_one("SELECT status FROM dose_events WHERE id=?", (past["id"],))["status"] == "taken"
    future = db.query(
        "SELECT de.scheduled_time_utc FROM dose_events de WHERE de.id != ? ORDER BY de.scheduled_time_utc",
        (past["id"],))
    # dinner 20:30 +45 = 21:15 IST = 15:45 UTC — day-0 dinner stays taken, the rest shift
    assert [r["scheduled_time_utc"][:16] for r in future] == ["2026-01-21T15:45", "2026-01-22T15:45"]


def test_validation_bounds(tmp_db):
    bad = [
        {"name": "", "slots": ["breakfast"], "food": "after", "duration_days": 3},
        {"name": "X", "slots": [], "food": "after", "duration_days": 3},
        {"name": "X", "slots": ["breakfast"], "food": "after", "duration_days": 0},
        {"name": "X", "slots": ["breakfast"], "food": "after", "duration_days": 91},
        {"name": "X", "slots": ["brunch"], "food": "after", "duration_days": 3},
    ]
    for b in bad:
        try:
            scheduling.validate_medicine(b)
            assert False, f"should have failed: {b}"
        except ValidationError:
            pass
    ok = scheduling.validate_medicine({"name": "X 500", "slots": ["LUNCH", "dinner"], "food": "after", "days": 90})
    assert ok["slots"] == ["lunch", "dinner"]  # canonical meal order
    assert ok["duration_days"] == 90
