"""Rendering — locked line format and template shapes from conversation-script-v0.2.md."""
from datetime import datetime, timedelta, timezone

from app import rendering


def med(name="AZITHROMYCIN 500", slots=None, food="after", tpd=1, days=5):
    return {"name": name, "form": "tablet", "tablets_per_dose": tpd,
            "slots": slots or ["breakfast", "dinner"], "food": food, "duration_days": days}


def test_medicine_line_format():
    assert rendering.medicine_line(med()) == "💊 AZITHROMYCIN 500 — breakfast & dinner — after food — 5 days"
    assert rendering.medicine_line(med(slots=["breakfast", "lunch", "dinner"])) == \
        "💊 AZITHROMYCIN 500 — breakfast, lunch & dinner — after food — 5 days"
    assert rendering.medicine_line(med(tpd=2, days=15, slots=["breakfast"], name="PANTOPRAZOLE 40", food="before")) == \
        "💊 PANTOPRAZOLE 40 — breakfast — before food — 2 tablets — 15 days"


def test_default_dose_never_shown():
    line = rendering.medicine_line(med(tpd=1, days=1))
    assert "tablet" not in line


def test_draft_message_shape():
    session = {
        "type": "draft",
        "user": {"timezone": "Asia/Kolkata",
                 "meals": {"breakfast": "08:00", "lunch": "14:00", "dinner": "19:00"},
                 "offset_before_min": -15, "offset_after_min": 45},
        "prescriptions": [{"id": "d", "start_date": "2026-01-20", "end_date": None,
                           "status": "active", "medicines": [med()]}],
    }
    text = rendering.draft_message(session)
    assert text.startswith("Here's what I read. Please check every line 👀")
    assert "💊 AZITHROMYCIN 500 — breakfast & dinner — after food — 5 days" in text
    assert "Breakfast 08:00 · Lunch 14:00 · Dinner 19:00" in text
    assert text.rstrip().endswith("Does this look right?")


def test_reminder_message_single_vs_grouped():
    base = "2026-01-20T03:15:00+00:00"  # 8:45 IST
    single = [{"scheduled_time_utc": base, "slot": "breakfast", "food": "after",
               "name": "Azithromycin 500", "tablets_per_dose": 1,
               "offset_before_min": -15, "offset_after_min": 45, "reminder_group": "g1"}]
    text, buttons = rendering.reminder_message(single, "Asia/Kolkata")
    assert text.splitlines()[0] == "⏰ 8:45 AM — after breakfast"
    assert "💊 Azithromycin 500 — 1 tablet" in text
    assert buttons[0][0]["text"] == "✅ Taken"
    assert buttons[0][0]["callback_data"] == "ack:taken:g1"

    double = single + [single[0] | {"name": "Dolo 650", "reminder_group": "g1"}]
    text, buttons = rendering.reminder_message(double, "Asia/Kolkata")
    assert buttons[0][0]["text"] == "✅ Taken — all 2"

    before = [single[0] | {"food": "before", "scheduled_time_utc": "2026-01-20T02:15:00+00:00"}]
    text, _ = rendering.reminder_message(before, "Asia/Kolkata")
    assert text.splitlines()[0] == "⏰ 7:45 AM — 15 min before breakfast"
    assert "— before food" in text


def test_scheduled_confirmation_groups_course_ends(tmp_db, fixed_today):
    from app import changesets, scheduling
    from datetime import timezone
    now = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
    db_test_user = None
    from app import db
    db.ensure_user("111")
    session = changesets.draft_session(db.get_user("111"), [
        med(name="Dolo 650", slots=["dinner"], days=2),
        med(name="Azithromycin 500", slots=["dinner"], days=4),
    ])
    with db.tx() as conn:
        affected = changesets.apply_session(conn, "111", session, now)
    pids = [a["prescription_id"] for a in affected]
    text = rendering.scheduled_confirmation("111", pids, now=now)
    # dinner+45 = 19:45 IST = 14:15 UTC → today's dinner (14:15 UTC) is still ahead of now=12:00 UTC
    assert "✅ All set — 6 doses scheduled over the next 1–3 days." in text
    assert "Course ends: Dolo 650 Jan 21" in text
    assert "Azithromycin 500 Jan 23" in text


def test_today_message():
    rows = [{"scheduled_time_utc": "2026-01-20T09:15:00+00:00", "slot": "lunch",
             "food": "after", "name": "Dolo 650"}]
    text = rendering.today_message(rows, "Asia/Kolkata")
    assert "• 2:45 PM (after lunch): Dolo 650" in text
    assert rendering.today_message([], "Asia/Kolkata") == "✔ Nothing left today — you're done."
