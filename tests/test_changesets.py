"""Change-set engine — AI proposes, code validates (deep dive §4.2)."""
import json
from datetime import datetime, timezone

from app import changesets, db

NOW = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)


def _live_session(tmp_db):
    db.ensure_user("111")
    session = changesets.draft_session(db.get_user("111"), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast", "lunch", "dinner"], "food": "after", "duration_days": 3},
        {"name": "DOXICILLIN 500", "form": "capsule", "tablets_per_dose": 1,
         "slots": ["dinner"], "food": "before", "duration_days": 5},
    ], NOW)
    with db.tx() as conn:
        pid = changesets.apply_session(conn, "111", session, NOW)[0]["prescription_id"]
    return changesets.live_session("111"), pid


def test_update_medicine_partial_match(tmp_db, fixed_today):
    session, _ = _live_session(tmp_db)
    changesets.apply_operations(session, [{"op": "update_medicine", "target": "dolo",
                                           "fields": {"duration_days": 5, "food": "before"}}])
    dolo = session["prescriptions"][0]["medicines"][0]
    assert dolo["duration_days"] == 5
    assert dolo["food"] == "before"
    assert dolo["slots"] == ["breakfast", "lunch", "dinner"]  # untouched fields preserved


def test_ambiguous_match_raises_with_names(tmp_db, fixed_today):
    session, _ = _live_session(tmp_db)
    try:
        changesets.apply_operations(session, [{"op": "update_medicine", "target": "do", "fields": {"food": "before"}}])
        assert False
    except changesets.Ambiguous as e:
        assert set(e.names) == {"Dolo 650", "DOXICILLIN 500"}


def test_unmatched_target_rejected(tmp_db, fixed_today):
    session, _ = _live_session(tmp_db)
    try:
        changesets.apply_operations(session, [{"op": "remove_medicine", "target": "zzz"}])
        assert False
    except changesets.BadOperation:
        pass


def test_meal_time_and_offset_ops(tmp_db, fixed_today):
    session, _ = _live_session(tmp_db)
    changesets.apply_operations(session, [
        {"op": "set_meal_time", "target": "dinner", "new_value": "20:30"},
        {"op": "set_offset", "target": "before", "new_value": -30},
        {"op": "set_offset", "target": "after", "new_value": 60},
    ])
    u = session["user"]
    assert u["meals"]["dinner"] == "20:30"
    assert u["offset_before_min"] == -30
    assert u["offset_after_min"] == 60
    assert u["meals"]["breakfast"] == "08:00"  # untouched


def test_fifteen_minute_grid_enforced(tmp_db, fixed_today):
    """Founder rule: meal times and offsets land only on 15-minute marks."""
    session, _ = _live_session(tmp_db)
    for op in (
        {"op": "set_meal_time", "target": "dinner", "new_value": "19:40"},
        {"op": "set_offset", "target": "before", "new_value": -20},
        {"op": "set_offset", "target": "after", "new_value": 50},
    ):
        try:
            changesets.apply_operations(session, [op])
            assert False, f"should reject off-grid {op}"
        except changesets.BadOperation:
            pass
    # on-grid values pass
    changesets.apply_operations(session, [{"op": "set_meal_time", "target": "dinner", "new_value": "21:45"}])
    assert session["user"]["meals"]["dinner"] == "21:45"


def test_bad_meal_time_and_timezone(tmp_db, fixed_today):
    session, _ = _live_session(tmp_db)
    for op in (
        {"op": "set_meal_time", "target": "dinner", "new_value": "25:00"},
        {"op": "set_meal_time", "target": "brunch", "new_value": "12:00"},
        {"op": "set_timezone", "new_value": "Mars/Olympus"},
        {"op": "set_offset", "target": "before", "new_value": "many"},
    ):
        try:
            changesets.apply_operations(session, [op])
            assert False, f"should reject {op}"
        except changesets.BadOperation:
            pass


def test_operations_are_all_or_nothing(tmp_db, fixed_today):
    session, _ = _live_session(tmp_db)
    try:
        changesets.apply_operations(session, [
            {"op": "update_medicine", "target": "dolo", "fields": {"duration_days": 9}},
            {"op": "update_medicine", "target": "zzz", "fields": {"duration_days": 9}},
        ])
        assert False
    except changesets.BadOperation:
        pass
    assert session["prescriptions"][0]["medicines"][0]["duration_days"] == 3  # rollback


def test_remove_medicine_then_apply_keeps_history(tmp_db, fixed_today):
    session, pid = _live_session(tmp_db)
    # a taken event exists for Dolo (simulate)
    med_row = db.query_one("SELECT id FROM medicines WHERE prescription_id=? AND name LIKE 'Dolo%'", (pid,))
    db.execute("UPDATE dose_events SET status='taken' WHERE medicine_id=?", (med_row["id"],))
    changesets.apply_operations(session, [{"op": "remove_medicine", "target": "dolo"}])
    with db.tx() as conn:
        changesets.apply_session(conn, "111", session, NOW)
    assert db.query_one("SELECT removed FROM medicines WHERE id=?", (med_row["id"],))["removed"] == 1
    # history rows survive; only future pending rows for it are gone
    assert db.query_one("SELECT COUNT(*) c FROM dose_events WHERE medicine_id=? AND status='taken'",
                        (med_row["id"],))["c"] >= 1
    assert db.query_one("SELECT COUNT(*) c FROM dose_events WHERE medicine_id=? AND status='pending'",
                        (med_row["id"],))["c"] == 0


def test_cancel_prescription_by_medicine_name(tmp_db, fixed_today):
    session, pid = _live_session(tmp_db)
    changesets.apply_operations(session, [{"op": "cancel_prescription", "target": "doxicillin"}])
    assert session["prescriptions"][0]["status"] == "cancelled"
    with db.tx() as conn:
        changesets.apply_session(conn, "111", session, NOW)
    assert db.query_one("SELECT status FROM prescriptions WHERE id=?", (pid,))["status"] == "cancelled"
    assert db.query_one(
        "SELECT COUNT(*) c FROM dose_events de JOIN medicines m ON m.id=de.medicine_id"
        " WHERE m.prescription_id=? AND de.status='pending'", (pid,))["c"] == 0


def test_cancel_prescription_by_number(tmp_db, fixed_today):
    session, _ = _live_session(tmp_db)
    number = session["prescriptions"][0]["number"]
    assert number == 1  # live_context_json exposes the stable rx number
    changesets.apply_operations(session, [{"op": "cancel_prescription", "target": "1"}])
    assert session["prescriptions"][0]["status"] == "cancelled"


def test_add_without_hint_lands_on_latest(tmp_db, fixed_today):
    """Founder default: an add with no target goes to the LATEST active
    prescription (highest number) instead of silently starting a new one."""
    session, pid1 = _live_session(tmp_db)
    new_rx = changesets.draft_session(db.get_user("111"), [
        {"name": "Azithro 500", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["dinner"], "food": "after", "duration_days": 5},
    ], NOW)
    with db.tx() as conn:
        pid2 = changesets.apply_session(conn, "111", new_rx, NOW)[0]["prescription_id"]
    session = changesets.live_session("111")
    assert [rx["number"] for rx in session["prescriptions"]] == [1, 2]
    changesets.apply_operations(session, [{"op": "add_medicine", "fields": {
        "name": "Cetirizine", "form": "tablet", "tablets_per_dose": 1,
        "slots": ["breakfast"], "food": "after", "days": 5}}])
    latest_meds = [m["name"] for rx in session["prescriptions"]
                   if rx["id"] == pid2 or rx.get("number") == 2 for m in rx["medicines"]]
    assert "Cetirizine" in latest_meds


def test_rx_numbers_assigned_sequentially(tmp_db, fixed_today):
    db.ensure_user("150")
    for i in range(3):
        session = changesets.draft_session(db.get_user("150"), [
            {"name": f"Med {i}", "form": "tablet", "tablets_per_dose": 1,
             "slots": ["breakfast"], "food": "after", "duration_days": 2},
        ], NOW)
        with db.tx() as conn:
            changesets.apply_session(conn, "150", session, NOW)
    nums = [r["rx_number"] for r in db.query(
        "SELECT rx_number FROM prescriptions WHERE chat_id='150' ORDER BY created_at")]
    assert nums == [1, 2, 3]
