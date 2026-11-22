"""Router + flows: intake (A), edit (B), commands (6), callbacks (C).

The AI only proposes (extraction / edit change-sets); every mutation that
touches live data passes a confirmation gate or is an explicit command.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from . import changesets, db, rendering, scheduling
from .ai import AIError, extract_prescription, interpret_edit
from .config import settings  # noqa: F401
from .scheduling import ValidationError
from .telegram import Telegram, TelegramError, download_photo

log = logging.getLogger("remed.handlers")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _buttons(rows: list[list[tuple[str, str]]]) -> list[list[dict]]:
    return [[{"text": t, "callback_data": d} for t, d in row] for row in rows]


DRAFT_BUTTONS = _buttons([[
    ("✅ Yes, start my reminders", "apply_draft"),
    ("✏️ Something's wrong", "draft_edit"),
]])
EDIT_BUTTONS = _buttons([[
    ("✅ Apply", "apply_edit"),
    ("✏️ Wrong — try again", "edit_retry"),
]])


# ---------------------------------------------------------------- dispatch

async def handle_event(tg: Telegram, event: dict) -> None:
    chat_id = event["chat_id"]
    db.ensure_user(chat_id)  # identity = chat ID, created silently on first contact
    if event["kind"] == "photo":
        db.log_user_message(chat_id, "photo", None)
        await _intake_photo(tg, chat_id, event["file_id"])
    elif event["kind"] == "callback":
        await _handle_callback(tg, chat_id, event)
    elif event["kind"] == "text":
        db.log_user_message(chat_id, "command" if event["text"].startswith("/") else "free_text",
                            event["text"])
        await _handle_text(tg, chat_id, event["text"])


# ---------------------------------------------------------------- AI call wrappers (logged for the golden set)

async def _call_extract_logged(chat_id: str, image: bytes) -> dict:
    """extract ① with full input/output persisted to ai_calls."""
    import time
    t0 = time.perf_counter()
    try:
        out = await extract_prescription(image)
        db.log_ai_call(chat_id, "extract", model=settings.ai_model, input_image=image,
                       output=json.dumps(out), ok=True,
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        return out
    except Exception as e:
        db.log_ai_call(chat_id, "extract", model=settings.ai_model, input_image=image,
                       ok=False, error=f"{type(e).__name__}: {e}",
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        raise


async def _call_interpret_logged(chat_id: str, text: str, context: dict) -> dict:
    """interpret ② with request + current state + output persisted to ai_calls."""
    import time
    t0 = time.perf_counter()
    try:
        out = await interpret_edit(text, context)
        db.log_ai_call(chat_id, "interpret", model=settings.ai_model,
                       input_text=json.dumps({"request": text, "context": context}),
                       output=json.dumps(out), ok=True,
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        return out
    except Exception as e:
        db.log_ai_call(chat_id, "interpret", model=settings.ai_model,
                       input_text=json.dumps({"request": text, "context": context}),
                       ok=False, error=f"{type(e).__name__}: {e}",
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        raise


def _redirect_edit(text: str) -> bool:
    t = text.lower()
    return t == "/edit" or "edit prescription" in t


# ---------------------------------------------------------------- text & commands

async def _handle_text(tg: Telegram, chat_id: str, text: str) -> None:
    if text.startswith("/"):
        await _handle_command(tg, chat_id, text)
        return

    state = db.get_state(chat_id)

    # typed YES gate for /delete
    if state and state["state"] == "pending_delete":
        await _handle_delete_confirm(tg, chat_id, text)
        return

    if _redirect_edit(text):
        await _start_edit(tg, chat_id)
        return

    if state and state["state"] == "pending_edit":
        await _handle_edit_text(tg, chat_id, text)
        return

    if state and state["state"] == "pending_draft":
        # free text while a draft is open: treat as edits/typed input on the draft
        await _handle_edit_text(tg, chat_id, text)
        return

    # stray free text: honor the "type it in your own words" promise
    await _handle_stray_text(tg, chat_id, text)


async def _handle_command(tg: Telegram, chat_id: str, text: str) -> None:
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    user = db.get_user(chat_id)
    tz = user["timezone"] if user else settings.default_timezone

    if cmd == "/start":
        await tg.send_text(chat_id, rendering.start_message())
    elif cmd == "/today":
        rows = scheduling.todays_remaining(chat_id, _utcnow())
        await tg.send_text(chat_id, rendering.today_message(rows, tz))
    elif cmd == "/edit":
        await _start_edit(tg, chat_id)
    elif cmd == "/prescriptions":
        await tg.send_text(chat_id, rendering.prescriptions_message(chat_id))
    elif cmd in ("/pause", "/resume"):
        val = 1 if cmd == "/pause" else 0
        arg = arg.strip()
        if arg:
            # prescription-level pause (founder): /pause Azithro pauses only
            # that prescription's reminders, on top of the user master switch
            chosen = [rx for rx in db.active_prescriptions(chat_id)
                      if any(arg.lower() in m["name"].lower() for m in db.medicines_for(rx["id"]))]
            if not chosen:
                await tg.send_text(chat_id, f"I couldn't find an active prescription containing '{arg}'.")
                return
            labels = []
            rx_ids = []
            with db.tx() as conn:
                for rx in chosen:
                    conn.execute("UPDATE prescriptions SET paused=? WHERE id=?", (val, rx["id"]))
                    labels += [m["name"] for m in db.medicines_for(rx["id"])]
                    rx_ids.append(rx["id"])
                db.log_activity(conn, chat_id,
                                "prescriptions_paused" if val else "prescriptions_resumed",
                                {"scope": "named", "match": arg, "prescription_ids": rx_ids})
            word = "reminders off" if val else "reminders back on"
            icon = "⏸" if val else "▶️"
            await tg.send_text(chat_id, f"{icon} {word} — {', '.join(sorted(set(labels)))}.")
        else:
            db.execute("UPDATE users SET paused=? WHERE chat_id=?", (val, chat_id))
            db.execute("UPDATE prescriptions SET paused=? WHERE chat_id=? AND status='active'", (val, chat_id))
            db.log_activity_now(chat_id, "prescriptions_paused" if val else "prescriptions_resumed",
                                {"scope": "all"})
            if val:
                await tg.send_text(chat_id, "⏸ Reminders paused for all your prescriptions. Your schedules are "
                                            "untouched — /resume any time. You can still edit your prescription.")
            else:
                await tg.send_text(chat_id, "▶️ Reminders resumed.")
    elif cmd == "/cancel":
        await _handle_cancel(tg, chat_id, arg.strip())
    elif cmd == "/delete":
        db.set_state(chat_id, "pending_delete", {})
        await tg.send_text(chat_id,
                           "⚠️ This will permanently delete ALL your data — prescriptions, medicines, "
                           "reminders, and dose history. Nothing is recoverable.\n\n"
                           "Type YES to confirm (anything else cancels).")
    else:
        await tg.send_text(chat_id, rendering.help_message())


# ---------------------------------------------------------------- Flow 1/2 — photo intake

async def _intake_photo(tg: Telegram, chat_id: str, file_id: str) -> None:
    await tg.send_text(chat_id, "📷 Got it. Reading your prescription…")
    try:
        image = await download_photo(settings.bot_token, file_id)
        raw = await _call_extract_logged(chat_id, image)
        medicines = scheduling.validate_draft_medicines(raw.get("medicines", []))
    except (AIError, ValidationError, TelegramError) as e:
        log.warning("intake failed (no contents logged): %s", type(e).__name__)
        medicines = None

    if not medicines:
        # generic failure copy + keep an empty draft open so free text still works
        user = db.get_user(chat_id)
        session = changesets.draft_session(user, [])
        db.set_state(chat_id, "pending_draft", {"session": session})
        await tg.send_text(chat_id, rendering.GENERIC_FAILURE)
        return

    user = db.get_user(chat_id)
    session = changesets.draft_session(user, medicines)
    db.set_state(chat_id, "pending_draft", {"session": session})
    await tg.send_message_with_buttons(chat_id, rendering.draft_message(session), DRAFT_BUTTONS)


# ---------------------------------------------------------------- Flow 5 — edit sessions

def _context_for(session: dict) -> dict:
    return {"user": session["user"], "prescriptions": session["prescriptions"]}


async def _start_edit(tg: Telegram, chat_id: str) -> None:
    state = db.get_state(chat_id)
    if state and state["state"] == "pending_draft":
        session = json.loads(state["payload"])["session"]
        if not any(m for rx in session["prescriptions"] for m in rx["medicines"] if not m.get("removed")):
            await tg.send_text(chat_id, "There's nothing to edit yet — send a 📷 photo of your prescription first.")
            return
    else:
        if state and state["state"] == "pending_edit":
            session = json.loads(state["payload"])["session"]  # continue an open session
        else:
            if not db.active_prescriptions(chat_id):
                await tg.send_text(chat_id, "There's nothing to edit yet — send a 📷 photo of your prescription first.")
                return
            session = changesets.live_session(chat_id)
    db.set_state(chat_id, "pending_edit", {"session": session})
    draft_mode = session["type"] == "draft"
    await tg.send_text(chat_id, rendering.EDIT_ONBOARDING_DRAFT if draft_mode else rendering.EDIT_ONBOARDING)


async def _handle_edit_text(tg: Telegram, chat_id: str, text: str) -> None:
    state = db.get_state(chat_id)
    if not state or state["state"] not in ("pending_edit", "pending_draft"):
        return
    session = json.loads(state["payload"])["session"]
    try:
        result = await _call_interpret_logged(chat_id, text, _context_for(session))
        ops = result.get("operations") or []
        clarify = result.get("clarify")
        if clarify:
            await tg.send_text(chat_id, str(clarify))
            return
        if not ops:
            await tg.send_text(chat_id, rendering.EDIT_FAILURE)
            return
        changesets.apply_operations(session, ops)
    except changesets.Ambiguous as e:
        await tg.send_text(chat_id, rendering.clarifying_message(e.names))
        return
    except (changesets.BadOperation, ValidationError):
        await tg.send_text(chat_id, rendering.EDIT_FAILURE)
        return
    except AIError:
        log.warning("interpret_edit AI failure")
        await tg.send_text(chat_id, rendering.EDIT_FAILURE)
        return

    draft_mode = session["type"] == "draft"
    db.set_state(chat_id, "pending_edit", {"session": session})
    await tg.send_message_with_buttons(chat_id, rendering.edit_confirm_message(session),
                                       DRAFT_BUTTONS if draft_mode else EDIT_BUTTONS)


async def _handle_stray_text(tg: Telegram, chat_id: str, text: str) -> None:
    session = changesets.draft_session(db.get_user(chat_id), [])
    try:
        result = await _call_interpret_logged(chat_id, text, _context_for(session))
        ops = result.get("operations") or []
        clarify = result.get("clarify")
        if clarify:
            await tg.send_text(chat_id, str(clarify))
            return
        if not ops:
            await tg.send_text(chat_id, rendering.NUDGE)
            return
        changesets.apply_operations(session, ops)
    except changesets.Ambiguous as e:
        await tg.send_text(chat_id, rendering.clarifying_message(e.names))
        return
    except (changesets.BadOperation, ValidationError, AIError):
        await tg.send_text(chat_id, rendering.NUDGE)
        return
    # anything the AI mapped becomes a confirmable draft — same gate as a photo
    db.set_state(chat_id, "pending_draft", {"session": session})
    await tg.send_message_with_buttons(chat_id, rendering.draft_message(session), DRAFT_BUTTONS)


# ---------------------------------------------------------------- Flow 3 — apply draft

async def _apply_draft(tg: Telegram, chat_id: str) -> None:
    state = db.get_state(chat_id)
    if not state or state["state"] not in ("pending_draft", "pending_edit"):
        await tg.send_text(chat_id, rendering.STATE_EXPIRED)
        return
    session = json.loads(state["payload"])["session"]
    active_meds = [m for rx in session["prescriptions"] for m in rx["medicines"] if not m.get("removed")]
    if not active_meds:
        await tg.send_text(chat_id, "There's nothing scheduled in this draft — send a photo or type your medicines.")
        db.clear_state(chat_id)
        return
    with db.tx() as conn:
        affected = changesets.apply_session(conn, chat_id, session, _utcnow())
    db.clear_state(chat_id)
    pids = [a["prescription_id"] for a in affected if not a["cancelled"]]
    await tg.send_text(chat_id, rendering.scheduled_confirmation(chat_id, pids) if pids
                       else "✅ Applied — nothing to schedule.")


# ---------------------------------------------------------------- Flow B apply (live)

async def _apply_edit(tg: Telegram, chat_id: str) -> None:
    state = db.get_state(chat_id)
    if not state or state["state"] != "pending_edit":
        await tg.send_text(chat_id, rendering.STATE_EXPIRED)
        return
    session = json.loads(state["payload"])["session"]
    if session["type"] == "draft":
        await _apply_draft(tg, chat_id)
        return
    with db.tx() as conn:
        changesets.apply_session(conn, chat_id, session, _utcnow())
    db.clear_state(chat_id)
    await tg.send_text(chat_id, "✅ Applied — future reminders updated.")


# ---------------------------------------------------------------- /cancel, /delete

async def _handle_cancel(tg: Telegram, chat_id: str, arg: str) -> None:
    from .changesets import _cancel_in_db  # reuse the cancel mechanics

    def _meds_label(rx) -> str:
        names = [m["name"] for m in db.medicines_for(rx["id"])]
        return f"{rendering.fmt_local_date(rx['start_date'])} → {rendering.fmt_local_date(rx['end_date'])} · " + \
               (", ".join(names[:3]) + ("…" if len(names) > 3 else "") if names else "empty")

    active = db.active_prescriptions(chat_id)
    if not active:
        await tg.send_text(chat_id, "No active prescriptions to cancel.")
        return
    chosen = active
    if arg:
        t = arg.lower()
        chosen = [rx for rx in active if any(t in m["name"].lower() for m in db.medicines_for(rx["id"]))]
        if not chosen:
            await tg.send_text(chat_id, f"I couldn't find a prescription containing '{arg}'. "
                                        "Use /edit if you want to change one instead.")
            return
    if len(chosen) == 1:
        rx = chosen[0]
        with db.tx() as conn:
            _cancel_in_db(conn, rx["id"])
        await tg.send_text(chat_id, f"🚫 Cancelled — {_meds_label(rx)}. Its future reminders are removed.")
        return
    # ambiguous / no arg → picker
    rows = [[("🚫 " + _meds_label(rx), f"cancelrx:{rx['id']}")] for rx in chosen]
    await tg.send_message_with_buttons(chat_id, "Which prescription should I cancel?", rows)


async def _handle_delete_confirm(tg: Telegram, chat_id: str, text: str) -> None:
    if text.strip().upper() == "YES":
        with db.tx() as conn:
            conn.execute("DELETE FROM reminder_queue WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM dose_events WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM medicines WHERE prescription_id IN"
                         " (SELECT id FROM prescriptions WHERE chat_id=?)", (chat_id,))
            conn.execute("DELETE FROM prescriptions WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM conversation_state WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM user_messages WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM ai_calls WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM activity_log WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        await tg.send_text(chat_id, "🗑 Everything deleted. Send a photo any time to start fresh.")
    else:
        db.clear_state(chat_id)
        await tg.send_text(chat_id, "Cancelled — nothing deleted.")


# ---------------------------------------------------------------- callbacks (reminder acks & gates)

async def _handle_callback(tg: Telegram, chat_id: str, event: dict) -> None:
    data = event["data"]
    cb_id = event["callback_id"]
    log.info("callback: %s", data)

    if data.startswith("ack:"):
        _, verdict, group = data.split(":", 2)
        if verdict not in ("taken", "skip"):
            await tg.answer_callback(cb_id)
            return
        await _ack_group(tg, chat_id, group, "taken" if verdict == "taken" else "skipped", event["message_id"])
        await tg.answer_callback(cb_id)
    elif data == "apply_draft":
        await _apply_draft(tg, chat_id)
        await tg.answer_callback(cb_id)
    elif data == "apply_edit":
        await _apply_edit(tg, chat_id)
        await tg.answer_callback(cb_id)
    elif data in ("edit_retry", "draft_edit"):
        state = db.get_state(chat_id)
        if state and state["state"] in ("pending_draft", "pending_edit"):
            session = json.loads(state["payload"])["session"]
            draft_mode = session["type"] == "draft"
            db.set_state(chat_id, "pending_edit", {"session": session})
            await tg.send_text(chat_id, rendering.EDIT_ONBOARDING_DRAFT if draft_mode else rendering.EDIT_ONBOARDING)
        await tg.answer_callback(cb_id)
    elif data.startswith("cancelrx:"):
        rx_id = data.split(":", 1)[1]
        from .changesets import _cancel_in_db
        with db.tx() as conn:
            _cancel_in_db(conn, rx_id)
        await tg.send_text(chat_id, "🚫 Cancelled — its future reminders are removed.")
        await tg.answer_callback(cb_id)
    else:
        await tg.answer_callback(cb_id)


async def _ack_group(tg: Telegram, chat_id: str, group: str, status: str, message_id: int | None) -> None:
    """Silent ack: update statuses, clear the buttons — no reply message (script v0.2)."""
    rows = db.query(
        "SELECT id FROM dose_events WHERE reminder_group=? AND chat_id=? AND status='pending'",
        (group, chat_id),
    )
    if not rows:
        return  # already answered — ignore the press
    now = db.now_iso()
    with db.tx() as conn:
        for row in rows:
            conn.execute("UPDATE dose_events SET status=?, responded_at=? WHERE id=?", (status, now, row["id"]))
    if message_id:
        try:
            await tg.update_message_buttons(chat_id, message_id, [])
        except TelegramError as e:
            log.warning("could not clear buttons: %s", e)


