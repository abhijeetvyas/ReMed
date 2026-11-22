"""End-to-end slice: photo → draft → confirm → apply → dose_events + queue →
engine tick sends reminder → silent ack → no repeat send. Simulated clock."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db, engine, handlers, scheduling
from app.telegram import extract_event

NOW = datetime(2026, 1, 20, 1, 0, tzinfo=timezone.utc)  # 6:30 IST — day-0 slots still ahead

SAMPLE = {"medicines": [
    {"name": "Pantoprazole 40", "form": "tablet", "tablets_per_dose": 2,
     "slots": ["breakfast"], "food": "before", "days": 3},
    {"name": "Azithromycin 500", "form": "tablet", "tablets_per_dose": 1,
     "slots": ["breakfast"], "food": "after", "days": 5},
], "notes": []}


@pytest.fixture()
def e2e(tmp_db, monkeypatch):
    async def fake_extract(image_bytes, mime="image/jpeg"):
        return SAMPLE, "fake-model"

    async def fake_download(token, file_id):
        return b"not-a-real-image"

    monkeypatch.setattr(handlers, "extract_prescription", fake_extract)
    monkeypatch.setattr(handlers, "download_photo", fake_download)
    monkeypatch.setattr(handlers, "_utcnow", lambda: NOW)  # simulated clock
    # send pacing is separately tested in test_guardrails — keep e2e snappy
    from app.config import settings
    monkeypatch.setattr(settings, "send_spacing_seconds", 0)
    monkeypatch.setattr(settings, "send_jitter_seconds", 0)
    return handlers


def _all_texts(tg):
    return [t for _, t in tg.texts] + [t for _, t, _ in tg.buttoned]


def test_photo_to_draft_to_apply_to_reminder_to_ack(tmp_db, tg, e2e, fixed_today):
    chat = "999"

    # 1 · photo in → Flow 1 ack + Flow 2 draft with confirmation gate
    update = {"update_id": 1, "message": {"chat": {"id": 999},
                                          "photo": [{"file_id": "f", "width": 1, "height": 1}]}}
    import asyncio
    asyncio.run(handlers.handle_event(tg, extract_event(update)))
    assert tg.texts[0][1] == "📷 Got it. Reading your prescription…"
    draft = tg.buttoned[0][1]
    assert "💊 PANTOPRAZOLE 40 — breakfast — before food — 2 tablets — 3 days" in draft  # names uppercase in tables
    assert "💊 AZITHROMYCIN 500 — breakfast — after food — 5 days" in draft
    assert "Does this look right?" in draft
    callbacks = [b["callback_data"] for row in tg.buttoned[0][2] for b in row]
    assert "apply_draft" in callbacks and "draft_edit" in callbacks
    state = db.get_state(chat)
    assert state["state"] == "pending_draft"

    # nothing scheduled yet
    assert db.query_one("SELECT COUNT(*) c FROM dose_events")["c"] == 0

    # 2 · user taps ✅ → Apply expands the whole course
    asyncio.run(handlers.handle_event(tg, extract_event({
        "update_id": 2,
        "callback_query": {"id": "cb1", "data": "apply_draft",
                           "message": {"chat": {"id": 999}, "message_id": 500},
                           "from": {"id": 999}},
    })))
    assert "✅ All set" in tg.last_text()
    assert db.get_state(chat) is None
    events = db.query(
        "SELECT de.*, m.name FROM dose_events de JOIN medicines m ON m.id=de.medicine_id"
        " ORDER BY de.scheduled_time_utc")
    # start = Jan 20 (fixed today); first before/after slots (02:15/03:15 UTC) lie ahead of real now
    assert len(events) == 8
    assert all(r["status"] == "pending" for r in events)

    # 3 · engine tick at each day-0 due moment → one message per slot group
    tick_0 = datetime.fromisoformat(events[0]["scheduled_time_utc"]) + timedelta(seconds=10)
    tick_1 = datetime.fromisoformat(events[1]["scheduled_time_utc"]) + timedelta(seconds=10)
    asyncio.run(engine.tick(tg, now=tick_0))
    assert len(tg.buttoned) == 2  # (draft confirm) + first reminder
    asyncio.run(engine.tick(tg, now=tick_1))
    assert len(tg.buttoned) == 3  # before-breakfast and after-breakfast are separate messages
    before_msg, after_msg = tg.buttoned[1], tg.buttoned[2]
    assert "Reminder to eat medicines BEFORE Breakfast" in before_msg[1]
    assert "Reminder to eat medicines AFTER Breakfast" in after_msg[1]
    assert "💊 Pantoprazole 40 — 2 tablets" in before_msg[1]
    assert "💊 Azithromycin 500 — 1 tablet" in after_msg[1]
    assert any(b["text"].startswith("✅") for row in before_msg[2] for b in row)

    # 4 · re-tick: no duplicate sends (delivery ack = state sent)
    asyncio.run(engine.tick(tg, now=tick_1 + timedelta(minutes=1)))
    assert len(tg.buttoned) == 3

    # 5 · user taps ✅ Taken on the after-breakfast reminder → silent ack, buttons clear
    group = after_msg[2][0][0]["callback_data"].split(":")[2]
    asyncio.run(handlers.handle_event(tg, extract_event({
        "update_id": 3,
        "callback_query": {"id": "cb2", "data": f"ack:taken:{group}",
                           "message": {"chat": {"id": 999}, "message_id": after_msg[0]},
                           "from": {"id": 999}},
    })))
    assert tg.answered[-1] == "cb2"  # cb1 was the Apply gate, cb2 is the dose ack
    assert tg.button_updates[-1][2] == []  # buttons cleared
    acked = db.query("SELECT status FROM dose_events WHERE reminder_group=?", (group,))
    assert {r["status"] for r in acked} == {"taken"}

    # 6 · window passes on the unanswered before-breakfast reminder → silent missed
    asyncio.run(engine.tick(tg, now=tick_0 + timedelta(hours=4)))
    other = next(r for r in events if r["reminder_group"] != group)
    assert db.query_one("SELECT status FROM dose_events WHERE id=?", (other["id"],))["status"] == "missed"

    # 7 · a replayed callback press on the answered reminder is ignored
    n_updates = len(tg.button_updates)
    asyncio.run(handlers.handle_event(tg, extract_event({
        "update_id": 4,
        "callback_query": {"id": "cb3", "data": f"ack:skip:{group}",
                           "message": {"chat": {"id": 999}, "message_id": after_msg[0]},
                           "from": {"id": 999}},
    })))
    assert len(tg.button_updates) == n_updates  # no second button clear
    assert {r["status"] for r in db.query(
        "SELECT status FROM dose_events WHERE reminder_group=?", (group,))} == {"taken"}


def test_edit_flow_full_cycle(tmp_db, tg, fixed_today, monkeypatch):
    """Flow B: /edit → free text → change-set validated → FULL table → Apply →
    future rows recomputed, history untouched."""
    import asyncio
    from app import changesets
    monkeypatch.setattr(handlers, "_utcnow", lambda: NOW)  # simulated clock
    chat = "555"
    _ = db.ensure_user(chat)
    session = changesets.draft_session(db.get_user(chat), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["dinner"], "food": "after", "duration_days": 3},
    ], NOW)
    with db.tx() as conn:
        pid = changesets.apply_session(conn, chat, session, NOW)[0]["prescription_id"]
    old_future = db.query(
        "SELECT de.scheduled_time_utc FROM dose_events de WHERE de.status='pending' ORDER BY de.scheduled_time_utc")
    assert len(old_future) == 3

    async def fake_interpret(text, ctx):
        assert ctx["prescriptions"][0]["medicines"][0]["name"] == "Dolo 650"
        return ({"operations": [{"op": "update_medicine", "target": "dolo",
                                 "fields": {"duration_days": 5, "food": "before"}}], "clarify": None},
                "fake-model")

    monkeypatch.setattr(handlers, "interpret_edit", fake_interpret)
    for uid, msg in ((1, "/edit"), (2, "Dolo is actually 5 days and before food")):
        asyncio.run(handlers.handle_event(tg, extract_event(
            {"update_id": uid, "message": {"chat": {"id": 555}, "text": msg}})))
    confirm = tg.buttoned[-1][1]  # the FULL updated table arrives as the confirm message
    assert "💊 DOLO 650 — dinner — before food — 5 days" in confirm
    assert any(b["callback_data"] == "apply_edit" for row in tg.buttoned[-1][2] for b in row)

    # Apply via the edit-confirm buttons
    asyncio.run(handlers.handle_event(tg, extract_event({
        "update_id": 3,
        "callback_query": {"id": "cb10", "data": "apply_edit",
                           "message": {"chat": {"id": 555}, "message_id": 1}, "from": {"id": 555}},
    })))
    assert "✅ Applied" in tg.last_text()
    med = db.query_one("SELECT * FROM medicines WHERE prescription_id=?", (pid,))
    assert med["duration_days"] == 5 and med["food"] == "before"
    new_future = db.query(
        "SELECT de.scheduled_time_utc FROM dose_events de WHERE de.status='pending' ORDER BY de.scheduled_time_utc")
    # dinner-before = 18:45 IST = 13:15 UTC, 5 days
    assert [r["scheduled_time_utc"][:16] for r in new_future] == [
        "2026-01-20T13:15", "2026-01-21T13:15", "2026-01-22T13:15",
        "2026-01-23T13:15", "2026-01-24T13:15"]


def test_webhook_endpoint_dedup_and_secret(tmp_db, monkeypatch):
    """HTTP surface: secret check, fast 200, update_id replay is dropped."""
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "_tg", None)  # don't send anything from the webhook task
    client = TestClient(main.app)
    assert client.get("/health").json() == {"ok": True, "service": "remed"}

    update = {"update_id": 42, "message": {"chat": {"id": 1}, "text": "/start"}}
    r = client.post("/webhook/bad-secret", json=update)
    assert r.json() == {"ok": False}
    r = client.post("/webhook/remed-dev-secret", json=update,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "remed-dev-secret"})
    assert r.json() == {"ok": True}
    r = client.post("/webhook/remed-dev-secret", json=update,  # replay
                    headers={"X-Telegram-Bot-Api-Secret-Token": "remed-dev-secret"})
    assert r.json() == {"ok": True}  # accepted but dropped by the dedup watermark


def test_pause_blocks_sending_but_events_expire_silently(tmp_db, tg, fixed_today):
    import asyncio
    chat = "888"
    update = {"update_id": 1, "message": {"chat": {"id": 888}, "text": "/start"}}
    asyncio.run(handlers.handle_event(tg, extract_event(update)))
    # seed a draft+apply directly
    from app import changesets
    session = changesets.draft_session(db.get_user(chat), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast"], "food": "after", "duration_days": 2},
    ], NOW)
    with db.tx() as conn:
        changesets.apply_session(conn, chat, session, NOW)
    due = datetime.fromisoformat(db.query_one(
        "SELECT MIN(scheduled_time_utc) t FROM dose_events")["t"]) + timedelta(seconds=5)

    asyncio.run(handlers.handle_event(tg, extract_event({
        "update_id": 2, "message": {"chat": {"id": 888}, "text": "/pause"}})))
    asyncio.run(engine.tick(tg, now=due))
    assert len(tg.buttoned) == 0  # paused → nothing sent
    asyncio.run(engine.tick(tg, now=due + timedelta(hours=4)))
    assert db.query_one(
        "SELECT status FROM dose_events ORDER BY scheduled_time_utc LIMIT 1")["status"] == "missed"
    asyncio.run(handlers.handle_event(tg, extract_event({
        "update_id": 3, "message": {"chat": {"id": 888}, "text": "/resume"}})))
    assert db.get_user(chat)["paused"] == 0


def test_send_failure_retries_then_expires_silently(tmp_db, fixed_today):
    from tests.conftest import FakeTelegram
    import asyncio
    chat = "777"
    _ = db.ensure_user(chat)
    from app import changesets
    session = changesets.draft_session(db.get_user(chat), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast"], "food": "after", "duration_days": 2},
    ], NOW)
    with db.tx() as conn:
        changesets.apply_session(conn, chat, session, NOW)
    due = datetime.fromisoformat(db.query_one("SELECT MIN(scheduled_time_utc) t FROM dose_events")["t"])
    tg = FakeTelegram(fail_until_attempt=99)
    asyncio.run(engine.tick(tg, now=due + timedelta(seconds=1)))
    asyncio.run(engine.tick(tg, now=due + timedelta(seconds=40)))
    asyncio.run(engine.tick(tg, now=due + timedelta(seconds=80)))
    # 3 attempts inside the window, then expire silently — user never nagged
    assert tg.send_attempts == 3
    assert db.query_one("SELECT state FROM reminder_queue") ["state"] == "expired"
    asyncio.run(engine.tick(tg, now=due + timedelta(seconds=120)))
    assert tg.send_attempts == 3


def test_prescription_level_pause_blocks_sends(tmp_db, tg, fixed_today):
    """Founder rule: pause lives on the prescription. A paused prescription is
    silent even while the user itself is not paused."""
    import asyncio
    chat = "555"
    _ = db.ensure_user(chat)
    from app import changesets
    session = changesets.draft_session(db.get_user(chat), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast"], "food": "after", "duration_days": 2},
    ], NOW)
    with db.tx() as conn:
        changesets.apply_session(conn, chat, session, NOW)
    due = datetime.fromisoformat(db.query_one(
        "SELECT MIN(scheduled_time_utc) t FROM dose_events")["t"]) + timedelta(seconds=5)

    # pause only the prescription, not the user
    db.execute("UPDATE prescriptions SET paused=1 WHERE chat_id=?", (chat,))
    asyncio.run(engine.tick(tg, now=due))
    assert len(tg.buttoned) == 0  # prescription paused → nothing sent

    db.execute("UPDATE prescriptions SET paused=0 WHERE chat_id=?", (chat,))
    asyncio.run(engine.tick(tg, now=due))
    assert len(tg.buttoned) == 1  # resumed → sends


# ---------------------------------------------------------------- review fixes

def _photo_update(update_id: int, chat_id: int) -> dict:
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id},
                        "photo": [{"file_id": "f", "width": 1, "height": 1}]}}


def _callback_update(update_id: int, chat_id: int, data: str) -> dict:
    return {"update_id": update_id,
            "callback_query": {"id": f"cb-{update_id}", "data": data,
                               "message": {"chat": {"id": chat_id}, "message_id": 1},
                               "from": {"id": chat_id}}}


def test_intake_ai_crash_still_answers(tmp_db, tg, e2e, fixed_today, monkeypatch):
    """E2 regression: a wrong-typed AI failure (raw JSONDecodeError) previously
    escaped every handler — the user saw '📷 Got it…' and then silence, and
    Telegram (200 already returned) never redelivered. Now: generic failure
    copy, and the empty draft stays open so typing still works."""
    import asyncio

    async def bad_extract(image_bytes, mime="image/jpeg"):
        raise json.JSONDecodeError("Expecting value", "{ broken", 2)

    monkeypatch.setattr(e2e, "extract_prescription", bad_extract)
    asyncio.run(handlers.handle_event(tg, extract_event(_photo_update(1, 830))))
    assert any("didn't work" in t for _, t in tg.texts)
    assert db.get_state("830") is not None  # empty draft open → typed input still works


def test_edit_ai_crash_still_answers(tmp_db, tg, e2e, fixed_today, monkeypatch):
    """E2 regression, edit side: a live `/edit` session whose interpret call
    explodes with a wrong-typed error still answers — the generic failure
    voice, never silence."""
    import asyncio

    async def bad_interpret(text, context):
        raise json.JSONDecodeError("Expecting value", "{ broken", 2)

    asyncio.run(handlers.handle_event(tg, extract_event(_photo_update(1, 831))))
    asyncio.run(handlers.handle_event(tg, extract_event(_callback_update(2, 831, "apply_draft"))))
    monkeypatch.setattr(handlers, "interpret_edit", bad_interpret)
    asyncio.run(handlers.handle_event(tg, extract_event(
        {"update_id": 3, "message": {"chat": {"id": 831}, "text": "/edit"}})))
    asyncio.run(handlers.handle_event(tg, extract_event(
        {"update_id": 4, "message": {"chat": {"id": 831}, "text": "make it 5 days"}})))
    assert any("didn't work" in t for _, t in tg.texts)


def test_second_photo_of_same_prescription_warns_before_confirm(tmp_db, tg, e2e, fixed_today, monkeypatch):
    """PM2 regression: the PRD's 'medicine-count reconciliation happens in the
    confirm step'. A re-sent photo must warn about existing coverage BEFORE the
    confirm gate — never silently schedule twice."""
    import asyncio
    from app.config import settings
    monkeypatch.setattr(settings, "photo_min_gap_seconds", 0)
    monkeypatch.setattr(settings, "apply_min_gap_seconds", 0)

    asyncio.run(handlers.handle_event(tg, extract_event(_photo_update(1, 832))))
    asyncio.run(handlers.handle_event(tg, extract_event(_callback_update(2, 832, "apply_draft"))))
    assert len(db.active_prescriptions("832")) == 1  # first pass: no warning, gate as usual
    first = tg.buttoned[0][1]
    assert "already have active reminders" not in first

    tg.buttoned.clear()
    asyncio.run(handlers.handle_event(tg, extract_event(_photo_update(3, 832))))  # same prescription again
    second = tg.buttoned[0][1]
    assert "already have active reminders" in second          # warning block present
    assert "Azithromycin 500" in second                       # names from live prescriptions
    callbacks = [b["callback_data"] for row in tg.buttoned[0][2] for b in row]
    assert "apply_draft" in callbacks                         # still confirmable — conscious choice


def test_named_resume_after_master_pause_is_honest(tmp_db, tg, e2e, fixed_today, monkeypatch):
    """E4 regression: '/pause' then '/resume Dolo' used to claim 'reminders
    back on' while the master switch still blocked everything. Now it says so."""
    import asyncio
    from app import changesets
    chat = "833"
    _ = db.ensure_user(chat)
    session = changesets.draft_session(db.get_user(chat), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast"], "food": "after", "duration_days": 2},
    ], NOW)
    with db.tx() as conn:
        changesets.apply_session(conn, chat, session, NOW)
    due = datetime.fromisoformat(db.query_one(
        "SELECT MIN(scheduled_time_utc) t FROM dose_events")["t"]) + timedelta(seconds=5)

    for uid, msg in ((1, "/pause"), (2, "/resume Dolo")):
        asyncio.run(handlers.handle_event(tg, extract_event(
            {"update_id": uid, "message": {"chat": {"id": 833}, "text": msg}})))
    reply = tg.last_text()
    assert "master pause" in reply and "nothing sends" in reply
    # and the engine agrees: nothing sent while the master switch is on
    asyncio.run(engine.tick(tg, now=due))
    assert len(tg.buttoned) == 0
    # full /resume lifts both switches → the dose (still inside its window) sends
    asyncio.run(handlers.handle_event(tg, extract_event(
        {"update_id": 3, "message": {"chat": {"id": 833}, "text": "/resume"}})))
    asyncio.run(engine.tick(tg, now=due + timedelta(seconds=15)))
    assert len(tg.buttoned) == 1


def test_finished_prescriptions_expire_and_stop_counting_toward_cap(tmp_db, tg, e2e, fixed_today, monkeypatch):
    """E3 regression: expired was never written — ended courses stayed 'active'
    forever, and the max-active cap eventually locked loyal users out."""
    import asyncio
    from app import changesets, rendering
    from app.config import settings
    chat = "834"
    _ = db.ensure_user(chat)
    session = changesets.draft_session(db.get_user(chat), [
        {"name": "Dolo 650", "form": "tablet", "tablets_per_dose": 1,
         "slots": ["breakfast"], "food": "after", "duration_days": 1},
    ], NOW)
    with db.tx() as conn:
        changesets.apply_session(conn, chat, session, NOW)

    # wind the clock past the course end (1-day course started Jan 20)
    late = NOW + timedelta(days=2)  # Jan 22, 01:00 UTC = Jan 22 IST
    asyncio.run(engine.tick(tg, now=late))
    rx = db.query_one("SELECT status FROM prescriptions WHERE chat_id=?", (chat,))
    assert rx["status"] == "expired"
    assert any(r["action"] == "prescription_expired" for r in db.query("SELECT action FROM activity_log"))
    assert "ended" in rendering.prescriptions_message(chat)  # /prescriptions tells the truth

    # the cap counts only live courses: a finished one no longer blocks a new apply
    monkeypatch.setattr(settings, "max_active_prescriptions", 1)
    asyncio.run(handlers.handle_event(tg, extract_event(_photo_update(1, 834))))
    asyncio.run(handlers.handle_event(tg, extract_event(_callback_update(2, 834, "apply_draft"))))
    assert "✅ All set" in tg.last_text()


def test_cancelrx_button_from_another_chat_cancels_nothing(tmp_db, tg, e2e, fixed_today):
    """Ownership guard regression: `cancelrx:<id>` button data names the
    prescription id — a replayed callback from a different chat must never
    cancel someone else's course (belt-and-suspenders; buttons are
    bot-authored per chat)."""
    import asyncio
    from app import changesets

    def _apply(chat: str, uid: int, name: str) -> None:
        session = changesets.draft_session(db.ensure_user(chat), [
            {"name": name, "form": "tablet", "tablets_per_dose": 1,
             "slots": ["breakfast"], "food": "after", "duration_days": 2},
        ], NOW)
        with db.tx() as conn:
            changesets.apply_session(conn, chat, session, NOW)

    _apply("840", 1, "Dolo 650")
    _apply("840", 2, "Azithro 500")
    rx_ids = [rx["id"] for rx in db.active_prescriptions("840")]
    assert len(rx_ids) == 2

    # a *different* chat presses chat 840's picker button → ignored, nothing cancelled
    asyncio.run(handlers.handle_event(tg, extract_event(
        _callback_update(3, 841, f"cancelrx:{rx_ids[0]}"))))
    assert len(db.active_prescriptions("840")) == 2
    assert not any("Cancelled" in t for _, t in tg.texts)

    # the owner pressing their own button cancels exactly the chosen course
    asyncio.run(handlers.handle_event(tg, extract_event(
        _callback_update(4, 840, f"cancelrx:{rx_ids[0]}"))))
    assert len(db.active_prescriptions("840")) == 1
    assert any("Cancelled" in t for _, t in tg.texts)
