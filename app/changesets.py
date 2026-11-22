"""Change-set engine (Flow B). AI proposes operations; this module validates
them against the session snapshot, detects ambiguity, applies to the snapshot,
and persists on Apply — one transaction. The AI never touches live data.
"""
import json
import uuid
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, scheduling
from .config import SLOTS
from .scheduling import ValidationError, parse_hhmm, fmt_hhmm, iso_utc


class Ambiguous(Exception):
    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(", ".join(names))


class BadOperation(Exception):
    pass


def _require_grid_minutes(minute_value: int, what: str) -> None:
    """All user-facing times sit on the TIME_GRID_MIN grid (…:00/:15/:30/:45),
    so reminder delivery (engine ticks on the same grid) is never more than one
    grid step after the intended moment."""
    if minute_value % settings_time_grid() != 0:
        raise BadOperation(f"{what} must be on a 15-minute mark (…:00, …:15, …:30, …:45)")


def settings_time_grid() -> int:
    from .config import TIME_GRID_MIN
    return TIME_GRID_MIN


# ---------------------------------------------------------------- snapshots

def draft_session(user_row, medicines: list[dict]) -> dict:
    """Unapplied intake draft (also used after a failed extract, with [])."""
    today = date.today().isoformat()
    return {
        "type": "draft",
        "user": db.user_freezes_json(user_row) if user_row else {},
        "prescriptions": [{
            "id": "draft", "start_date": today, "end_date": None,
            "status": "active", "medicines": medicines,
        }],
    }


def live_session(chat_id: str) -> dict:
    ctx = db.live_context_json(chat_id)
    for rx in ctx["prescriptions"]:
        rx["status"] = "active"
    return {"type": "live", "user": ctx["user"], "prescriptions": ctx["prescriptions"]}


# ---------------------------------------------------------------- matching

def _norm(s: str) -> str:
    return s.lower().replace(" ", "").replace("-", "")


def _target_matches(session: dict, target: str) -> list[tuple[dict, dict]]:
    """(prescription, medicine) pairs whose name matches a partial target."""
    t = _norm(target or "")
    if not t:
        return []
    hits = []
    for rx in session["prescriptions"]:
        if rx.get("status") == "cancelled":
            continue
        for m in rx["medicines"]:
            if m.get("removed"):
                continue
            nm = _norm(m["name"])
            if t in nm or (len(nm) >= 4 and nm in t):
                hits.append((rx, m))
    return hits


def _require_medicine(ops: list[dict], op: dict) -> tuple[dict, dict]:
    hits = _target_matches(ops, op.get("target", ""))
    if len(hits) > 1:
        raise Ambiguous([m["name"] for _, m in hits])
    if not hits:
        raise BadOperation(f"I couldn't find a medicine called '{op.get('target', '')}'.")
    return hits[0]


# ---------------------------------------------------------------- validation & application to snapshot

def validate_operation(session: dict, op: dict) -> None:
    """Validate + apply one operation to the session snapshot (raises Ambiguous / BadOperation)."""
    kind = op.get("op")
    if kind == "update_medicine":
        rx, m = _require_medicine(session, op)
        clean = scheduling.validate_medicine(m | (op.get("fields") or {}))
        m.update(clean)
    elif kind == "add_medicine":
        clean = scheduling.validate_medicine(op.get("fields") or {})
        rx = _resolve_prescription(session, op.get("prescription"))
        if any(not mm.get("removed") and _norm(mm["name"]) == _norm(clean["name"]) for mm in rx["medicines"]):
            raise BadOperation(f"'{clean['name']}' is already in this prescription")
        clean.pop("id", None)
        rx["medicines"].append(clean)
    elif kind == "remove_medicine":
        rx, m = _require_medicine(session, op)
        m["removed"] = 1
    elif kind == "set_meal_time":
        slot = str(op.get("target", "")).lower()
        if slot not in SLOTS:
            raise BadOperation("which meal — breakfast, lunch or dinner?")
        try:
            t = parse_hhmm(str(op.get("new_value", "")))
        except ValidationError as e:
            raise BadOperation(str(e))
        _require_grid_minutes(t.minute, "meal time")
        session["user"]["meals"][slot] = fmt_hhmm(t)
        session["user_changed"] = True
    elif kind == "set_offset":
        which = str(op.get("target", "")).lower()
        if which not in ("before", "after"):
            raise BadOperation("offset must be before or after meals")
        try:
            minutes = int(op.get("new_value"))
        except (TypeError, ValueError):
            raise BadOperation("offset must be a number of minutes")
        if not (-180 <= minutes <= 180):
            raise BadOperation("offset must be within 180 minutes")
        _require_grid_minutes(abs(minutes), "offset")
        session["user"][f"offset_{which}_min"] = -abs(minutes) if which == "before" else abs(minutes)
        session["user_changed"] = True
    elif kind == "set_timezone":
        tz = str(op.get("new_value", "")).strip()
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            raise BadOperation(f"'{tz}' is not a valid timezone name")
        session["user"]["timezone"] = tz
        session["user_changed"] = True
    elif kind == "cancel_prescription":
        t = _norm(op.get("target", ""))
        hits = []
        for rx in session["prescriptions"]:
            if rx.get("status") == "cancelled":
                continue
            if rx["id"] == op.get("target", "") or any(
                not m.get("removed") and (t in _norm(m["name"]) or (len(_norm(m["name"])) >= 4 and _norm(m["name"]) in t))
                for m in rx["medicines"]
            ):
                hits.append(rx)
        if len(hits) > 1:
            raise Ambiguous([m["name"] for rx in hits for m in rx["medicines"]])
        if not hits:
            # a never-stored draft target: user says "stop X" right in the intake loop
            raise BadOperation("I couldn't work out which prescription to cancel.")
        hits[0]["status"] = "cancelled"
    else:
        raise BadOperation(f"unknown operation '{kind}'")


def _resolve_prescription(session: dict, hint: str | None) -> dict:
    """Which prescription does add_medicine belong to?"""
    active = [rx for rx in session["prescriptions"] if rx.get("status") != "cancelled"]
    if hint:
        h = _norm(hint)
        if h == "new":
            return _new_prescription(session)
        for rx in active:
            if rx["id"] == hint or any(not m.get("removed") and h in _norm(m["name"]) for m in rx["medicines"]):
                return rx
    if len(active) == 1:
        return active[0]
    return _new_prescription(session)


def _new_prescription(session: dict) -> dict:
    today = date.today()
    rx = {"id": "new-" + uuid.uuid4().hex[:8], "start_date": today.isoformat(),
          "end_date": None, "status": "active", "medicines": []}
    session["prescriptions"].append(rx)
    return rx


def apply_operations(session: dict, ops: list) -> None:
    """Validate every operation first (all-or-nothing snapshot edit), then apply."""
    if not isinstance(ops, list):
        raise BadOperation("operations must be a list")
    import copy
    candidate = copy.deepcopy(session)
    for op in ops:
        if not isinstance(op, dict):
            raise BadOperation("bad operation")
        validate_operation(candidate, op)
    # keep any pre-existing user_changed flag
    candidate["user_changed"] = candidate.pop("user_changed", session.get("user_changed", False))
    session.clear()
    session.update(candidate)


def session_view(session: dict) -> dict:
    """Snapshot reduced to what we persist in conversation_state."""
    view = {"type": session["type"], "user": session["user"], "prescriptions": session["prescriptions"]}
    return view


# ---------------------------------------------------------------- persistence (Apply)

def apply_session(conn, chat_id: str, session: dict, now_utc) -> list[dict]:
    """One transaction: persist the confirmed session. Returns affected prescriptions."""
    s = session["user"]
    user = db.get_user(chat_id)
    if user is None:
        conn.execute(
            "INSERT INTO users(chat_id, timezone, meal_breakfast, meal_lunch, meal_dinner,"
            " offset_before_min, offset_after_min, paused, created_at) VALUES(?,?,?,?,?,?,?,0,?)",
            (chat_id, s["timezone"], s["meals"]["breakfast"], s["meals"]["lunch"], s["meals"]["dinner"],
             s["offset_before_min"], s["offset_after_min"], db.now_iso()),
        )
    elif session.get("user_changed"):
        conn.execute(
            "UPDATE users SET timezone=?, meal_breakfast=?, meal_lunch=?, meal_dinner=?,"
            " offset_before_min=?, offset_after_min=? WHERE chat_id=?",
            (s["timezone"], s["meals"]["breakfast"], s["meals"]["lunch"], s["meals"]["dinner"],
             s["offset_before_min"], s["offset_after_min"], chat_id),
        )
    user = db.get_user(chat_id)  # re-read if we just created
    user_settings = {
        "timezone": user["timezone"],
        "meals": {"breakfast": user["meal_breakfast"], "lunch": user["meal_lunch"], "dinner": user["meal_dinner"]},
        "offset_before_min": user["offset_before_min"],
        "offset_after_min": user["offset_after_min"],
    }

    affected = []
    for rx in session["prescriptions"]:
        active_meds = [m for m in rx["medicines"] if not m.get("removed")]
        if session["type"] == "live" and (rx.get("status") == "cancelled" or not active_meds):
            _cancel_in_db(conn, rx["id"])
            affected.append({"prescription_id": rx["id"], "cancelled": True})
            continue
        if session["type"] == "draft":
            rx_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO prescriptions(id, chat_id, start_date, end_date, status, image_deleted, created_at)"
                " VALUES(?,?,?,?, 'active', 1, ?)",
                (rx_id, chat_id, rx["start_date"], rx["end_date"], db.now_iso()),
            )
            for m in rx["medicines"]:
                if m.get("removed"):
                    continue
                med_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO medicines(id, prescription_id, name, form, tablets_per_dose, slots, food,"
                    " duration_days, removed, created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
                    (med_id, rx_id, m["name"], m["form"], m["tablets_per_dose"], json.dumps(m["slots"]),
                     m["food"], m["duration_days"], db.now_iso()),
                )
            affected.append({"prescription_id": rx_id, "cancelled": False})
        else:  # live
            _persist_live_prescription(conn, chat_id, rx, user_settings)
            affected.append({"prescription_id": rx["id"], "cancelled": False})

    now_iso = iso_utc(now_utc)
    for rx_id in [a["prescription_id"] for a in affected if not a["cancelled"]]:
        scheduling.recompute_future(conn, chat_id, rx_id, user_settings, now_utc)
    # a user-settings change (meal/tz/offset) shifts every active prescription
    if session.get("user_changed") and session["type"] == "live":
        for rx in conn.execute("SELECT id FROM prescriptions WHERE chat_id=? AND status='active'", (chat_id,)).fetchall():
            scheduling.recompute_future(conn, chat_id, rx["id"], user_settings, now_utc)
    return affected


def _persist_live_prescription(conn, chat_id: str, rx: dict, user_settings: dict) -> None:
    snapshot_ids = {m["id"] for m in rx["medicines"] if m.get("id")}
    for m in rx["medicines"]:
        if m.get("removed") and m.get("id"):
            conn.execute("UPDATE medicines SET removed=1 WHERE id=?", (m["id"],))
            conn.execute(
                "DELETE FROM reminder_queue WHERE dose_event_id IN"
                " (SELECT id FROM dose_events WHERE medicine_id=? AND status='pending')", (m["id"],))
            conn.execute("DELETE FROM dose_events WHERE medicine_id=? AND status='pending'", (m["id"],))
        elif m.get("id") and not m.get("removed"):
            conn.execute(
                "UPDATE medicines SET name=?, form=?, tablets_per_dose=?, slots=?, food=?, duration_days=? WHERE id=?",
                (m["name"], m["form"], m["tablets_per_dose"], json.dumps(m["slots"]), m["food"],
                 m["duration_days"], m["id"]))
        elif not m.get("id") and not m.get("removed"):
            conn.execute(
                "INSERT INTO medicines(id, prescription_id, name, form, tablets_per_dose, slots, food,"
                " duration_days, removed, created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
                (uuid.uuid4().hex, rx["id"], m["name"], m["form"], m["tablets_per_dose"],
                 json.dumps(m["slots"]), m["food"], m["duration_days"], db.now_iso()))
    # medicines deleted from the snapshot entirely (rare: AI omitted them)
    for row in conn.execute("SELECT id FROM medicines WHERE prescription_id=? AND removed=0", (rx["id"],)).fetchall():
        if row["id"] not in snapshot_ids:
            conn.execute("UPDATE medicines SET removed=1 WHERE id=?", (row["id"],))
            conn.execute(
                "DELETE FROM reminder_queue WHERE dose_event_id IN"
                " (SELECT id FROM dose_events WHERE medicine_id=? AND status='pending')", (row["id"],))
            conn.execute("DELETE FROM dose_events WHERE medicine_id=? AND status='pending'", (row["id"],))


def _cancel_in_db(conn, prescription_id: str) -> None:
    conn.execute("UPDATE prescriptions SET status='cancelled' WHERE id=?", (prescription_id,))
    conn.execute(
        "DELETE FROM reminder_queue WHERE dose_event_id IN (SELECT de.id FROM dose_events de"
        " JOIN medicines m ON m.id=de.medicine_id WHERE m.prescription_id=? AND de.status='pending')",
        (prescription_id,))
    conn.execute(
        "DELETE FROM dose_events WHERE status='pending' AND medicine_id IN"
        " (SELECT id FROM medicines WHERE prescription_id=?)", (prescription_id,))
