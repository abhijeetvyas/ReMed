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
        return SAMPLE

    async def fake_download(token, file_id):
        return b"not-a-real-image"

    monkeypatch.setattr(handlers, "extract_prescription", fake_extract)
    monkeypatch.setattr(handlers, "download_photo", fake_download)
    monkeypatch.setattr(handlers, "_utcnow", lambda: NOW)  # simulated clock
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
    assert "before breakfast" in before_msg[1]
    assert "after breakfast" in after_msg[1]
    assert "💊 Pantoprazole 40 — 2 tablets — before food" in before_msg[1]
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
    ])
    with db.tx() as conn:
        pid = changesets.apply_session(conn, chat, session, NOW)[0]["prescription_id"]
    old_future = db.query(
        "SELECT de.scheduled_time_utc FROM dose_events de WHERE de.status='pending' ORDER BY de.scheduled_time_utc")
    assert len(old_future) == 3

    async def fake_interpret(text, ctx):
        assert ctx["prescriptions"][0]["medicines"][0]["name"] == "Dolo 650"
        return {"operations": [{"op": "update_medicine", "target": "dolo",
                                "fields": {"duration_days": 5, "food": "before"}}], "clarify": None}

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
    ])
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
    ])
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
