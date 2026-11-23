"""Router + flows: intake (A), edit (B), commands (6), callbacks (C).

The AI only proposes (extraction / edit change-sets); every mutation that
touches live data passes a confirmation gate or is an explicit command.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from . import changesets, db, guardrails, images, rendering, scheduling
from .ai import AIError, extract_prescription, extract_text_prescription, interpret_edit
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

def _is_super(chat_id: str) -> bool:
    user = db.get_user(chat_id)
    return bool(user and user["is_super"])


async def _guard_fail(tg: Telegram, chat_id: str, message: str) -> None:
    """Send a guardrail block message and feed auto-quarantine: after
    AUTO_BLOCK_TRIPS blocks in an hour the chat is marked blocked and every
    further event is ignored (start of handle_event)."""
    await tg.send_text(chat_id, message)
    if guardrails.record_trip(chat_id):
        db.execute("UPDATE users SET blocked=1 WHERE chat_id=?", (chat_id,))
        db.log_activity_now(chat_id, "user_blocked", {"reason": "guardrail"})
        await tg.send_text(chat_id, guardrails.quarantined_message())


async def handle_event(tg: Telegram, event: dict) -> None:
    chat_id = event["chat_id"]
    db.ensure_user(chat_id)  # identity = chat ID, created silently on first contact
    user = db.get_user(chat_id)
    if user and user["blocked"]:
        return  # auto-quarantined — silent, not even a receipt
    super_user = bool(user and user["is_super"])
    # guardrail slot: bounded concurrency, ordered per chat
    async with guardrails.slot(chat_id):
        if event["kind"] == "photo":
            blocked = guardrails.check(chat_id, "photo", super_user)
            if blocked:  # too many photos — decline before any AI spend or blob write
                await _guard_fail(tg, chat_id, blocked)
                return
            db.log_user_message(chat_id, "photo", None)
            await _intake_photo(tg, chat_id, event["file_id"])
        elif event["kind"] == "callback":
            await _guard_callback(tg, chat_id, event)
        elif event["kind"] == "text":
            db.log_user_message(chat_id, "command" if event["text"].startswith("/") else "free_text",
                                event["text"])
            if guardrails.text_too_long(event["text"]):
                await tg.send_text(chat_id, guardrails.text_too_long_message())
                return
            await _handle_text(tg, chat_id, event["text"])


async def _guard_callback(tg: Telegram, chat_id: str, event: dict) -> None:
    """Callback gate: applies are throttled like any other mutation."""
    if event["data"] in ("apply_draft", "apply_edit"):
        blocked = guardrails.check(chat_id, "apply", _is_super(chat_id))
        if blocked:
            await _guard_fail(tg, chat_id, blocked)
            await tg.answer_callback(event["callback_id"])
            return
    await _handle_callback(tg, chat_id, event)


# ---------------------------------------------------------------- AI call wrappers (logged for the golden set)

async def _call_extract_logged(chat_id: str, image: bytes) -> dict:
    """extract ① with full input/output persisted to ai_calls (model = the one
    in the fallback chain that actually answered)."""
    import time
    t0 = time.perf_counter()
    try:
        out, model = await extract_prescription(image)
        db.log_ai_call(chat_id, "extract", model=model, input_image=image,
                       output=json.dumps(out), ok=True,
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        return out
    except Exception as e:
        db.log_ai_call(chat_id, "extract", model=settings.ai_models[0] if settings.ai_models else None,
                       input_image=image,
                       ok=False, error=f"{type(e).__name__}: {e}",
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        raise


async def _call_interpret_logged(chat_id: str, text: str, context: dict) -> dict:
    """interpret ② with request + current state + output persisted to ai_calls."""
    import time
    t0 = time.perf_counter()
    try:
        out, model = await interpret_edit(text, context)
        db.log_ai_call(chat_id, "interpret", model=model,
                       input_text=json.dumps({"request": text, "context": context}),
                       output=json.dumps(out), ok=True,
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        return out
    except Exception as e:
        db.log_ai_call(chat_id, "interpret", model=settings.ai_models[0] if settings.ai_models else None,
                       input_text=json.dumps({"request": text, "context": context}),
                       ok=False, error=f"{type(e).__name__}: {e}",
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        raise


def _redirect_edit(text: str) -> bool:
    t = text.lower()
    return t == "/edit" or "edit prescription" in t


async def _call_type_extract_logged(chat_id: str, text: str) -> dict:
    """type-extract ③ (typed prescription description, same JSON contract as
    the photo extract) with request + output persisted to ai_calls."""
    import time
    t0 = time.perf_counter()
    try:
        out, model = await extract_text_prescription(text)
        db.log_ai_call(chat_id, "type_extract", model=model,
                       input_text=text,
                       output=json.dumps(out), ok=True,
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        return out
    except Exception as e:
        db.log_ai_call(chat_id, "type_extract",
                       model=settings.ai_models[0] if settings.ai_models else None,
                       input_text=text,
                       ok=False, error=f"{type(e).__name__}: {e}",
                       latency_ms=int((time.perf_counter() - t0) * 1000))
        raise


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

    # bare "/type" → the next free message is the prescription description
    if state and state["state"] == "pending_type":
        db.clear_state(chat_id)
        await _intake_text(tg, chat_id, text)
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
    elif cmd == "/type":
        # AI-intake like a photo: same guardrail bucket + global budget before
        # any spend
        blocked = guardrails.check(chat_id, "edit", _is_super(chat_id)) or \
            guardrails.ai_capacity_block()
        if blocked:
            await _guard_fail(tg, chat_id, blocked)
            return
        arg = arg.strip()
        if arg:
            await _intake_text(tg, chat_id, arg)
        else:
            # no description inline: open a typing session — the next free
            # message is treated as the prescription description
            db.set_state(chat_id, "pending_type", {})
            await tg.send_text(chat_id, rendering.TYPE_ONBOARDING)
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
            msg = f"{icon} {word} — {', '.join(sorted(set(labels)))}."
            if val == 0:
                # the user master switch still gates the engine (two-switch
                # model): after /pause then /resume Azithro the honest answer
                # is "flagged, but master pause still on" — never a plain
                # "back on" while nothing will send (review E4)
                u2 = db.get_user(chat_id)
                if u2 and u2["paused"]:
                    await tg.send_text(
                        chat_id,
                        f"▶️ Ready to resume: {', '.join(sorted(set(labels)))}. "
                        "⚠️ Your master pause is still on though — nothing sends until you use /resume.")
                    return
            await tg.send_text(chat_id, msg)
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
    elif cmd == "/super":
        # founder/testing escape hatch: a one-time key lifts all rate limits
        # for this chat. SUPER_USER_KEY unset/empty → feature is off.
        if settings.super_user_key and arg.strip() == settings.super_user_key:
            db.execute("UPDATE users SET is_super=1 WHERE chat_id=?", (chat_id,))
            await tg.send_text(chat_id, "🔓 Unlocked — rate limits lifted for this chat. Have fun testing.")
        else:
            await tg.send_text(chat_id, "That key isn't right.")
    elif cmd in ("/block", "/unblock"):
        # founder tool (requires /super verification first): manual quarantine
        if not (user and user["is_super"]):
            await tg.send_text(chat_id, rendering.help_message())
            return
        target = arg.strip()
        if not target:
            await tg.send_text(chat_id, f"Usage: {cmd} <chat_id>")
            return
        if cmd == "/block":
            db.execute("UPDATE users SET blocked=1 WHERE chat_id=?", (target,))
            db.log_activity_now(chat_id, "user_blocked", {"target": target, "manual": True})
            await tg.send_text(chat_id, f"🚫 Chat {target} is blocked — its events are now ignored.")
        else:
            db.execute("UPDATE users SET blocked=0 WHERE chat_id=?", (target,))
            db.log_activity_now(chat_id, "user_unblocked", {"target": target})
            await tg.send_text(chat_id, f"🔓 Chat {target} unblocked.")
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
    blocked = guardrails.ai_capacity_block()  # global daily budget — protects the shared free-tier key
    if blocked:
        await tg.send_text(chat_id, blocked)
        return
    await tg.send_text(chat_id, "📷 Got it. Reading your prescription…")
    try:
        image = images.compress(await download_photo(settings.bot_token, file_id))
        raw = await _call_extract_logged(chat_id, image)
        medicines = scheduling.validate_draft_medicines(raw.get("medicines", []))
    except Exception as e:
        # ANY failure after the "Got it…" ack — AI error, malformed model JSON,
        # Telegram file error — must answer with the generic failure voice,
        # never silence (the webhook already returned 200; Telegram will not
        # redeliver. AIError was already handled here; catch-all covers the
        # wrong-typed escapes, review E2.)
        log.warning("intake failed (no contents logged): %s: %s", type(e).__name__, e)
        guardrails.record_photo_failure(chat_id)  # unreadable-photo backoff
        medicines = None

    if not medicines:
        # generic failure copy + keep an empty draft open so free text still works
        user = db.get_user(chat_id)
        session = changesets.draft_session(user, [], _utcnow())
        db.set_state(chat_id, "pending_draft", {"session": session})
        await tg.send_text(chat_id, rendering.GENERIC_FAILURE)
        return

    user = db.get_user(chat_id)
    session = changesets.draft_session(user, medicines, _utcnow())
    db.set_state(chat_id, "pending_draft", {"session": session})
    duplicates = changesets.duplicate_medicine_names(chat_id, session)
    await tg.send_message_with_buttons(chat_id, rendering.draft_message(session, duplicates), DRAFT_BUTTONS)


# ---------------------------------------------------------------- Flow 1c — typed-text intake (/type)

async def _intake_text(tg: Telegram, chat_id: str, text: str) -> None:
    """'/type' intake: the user described the prescription in words. Same
    draft flow as the photo path (confirm gate, duplicates check, Apply) —
    only the extractor differs."""
    await tg.send_text(chat_id, "📝 Got it. Setting up your prescription…")
    try:
        raw = await _call_type_extract_logged(chat_id, text)
        medicines = scheduling.validate_draft_medicines(raw.get("medicines", []))
    except Exception as e:
        # same contract as photo intake: any post-ack failure answers with the
        # generic failure voice, never silence
        log.warning("type-intake failed (no contents logged): %s: %s", type(e).__name__, e)
        medicines = None

    if not medicines:
        await tg.send_text(chat_id, rendering.GENERIC_FAILURE)
        return

    user = db.get_user(chat_id)
    session = changesets.draft_session(user, medicines, _utcnow())
    db.set_state(chat_id, "pending_draft", {"session": session})
    duplicates = changesets.duplicate_medicine_names(chat_id, session)
    await tg.send_message_with_buttons(chat_id, rendering.draft_message(session, duplicates), DRAFT_BUTTONS)


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
    blocked = guardrails.check(chat_id, "edit", _is_super(chat_id)) or \
        guardrails.ai_capacity_block()
    if blocked:
        await tg.send_text(chat_id, blocked)
        return
    state = db.get_state(chat_id)
    if not state or state["state"] not in ("pending_edit", "pending_draft"):
        return
    session = json.loads(state["payload"])["session"]
    try:
        result = await _call_interpret_logged(chat_id, text, _context_for(session))
        ops = result.get("operations") or []
        clarify = result.get("clarify")
        # clarify no longer discards ops: clear parts apply, unclear part asks
        if not ops:
            await tg.send_text(chat_id, str(clarify) if clarify else rendering.EDIT_FAILURE)
            return
        changesets.apply_operations(session, ops, _utcnow())
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
    except Exception as e:
        # wrong-typed failure (e.g. an unexpected provider payload) — answer with
        # the generic failure voice instead of leaving the user in silence (E2)
        log.warning("edit handling failed (no contents logged): %s", type(e).__name__)
        await tg.send_text(chat_id, rendering.EDIT_FAILURE)
        return

    draft_mode = session["type"] == "draft"
    db.set_state(chat_id, "pending_edit", {"session": session})
    # the AI's own one-line change summary is the confirmation surface — the
    # full updated table only appears when the model didn't produce one
    # (older model / fallback path), falling back to the read-back rendering
    summary = str(result.get("summary") or "").strip()
    confirm_text = summary if summary else rendering.edit_confirm_message(session)
    await tg.send_message_with_buttons(chat_id, confirm_text,
                                       DRAFT_BUTTONS if draft_mode else EDIT_BUTTONS)
    if clarify:  # mixed request: clear parts applied, now ask about the rest
        await tg.send_text(chat_id, str(clarify))


async def _handle_stray_text(tg: Telegram, chat_id: str, text: str) -> None:
    blocked = guardrails.check(chat_id, "edit", _is_super(chat_id)) or \
        guardrails.ai_capacity_block()  # stray text costs an interpret call too
    if blocked:
        await tg.send_text(chat_id, blocked)
        return
    session = changesets.draft_session(db.get_user(chat_id), [], _utcnow())
    try:
        result = await _call_interpret_logged(chat_id, text, _context_for(session))
        ops = result.get("operations") or []
        clarify = result.get("clarify")
        if not ops:
            await tg.send_text(chat_id, str(clarify) if clarify else rendering.NUDGE)
            return
        changesets.apply_operations(session, ops, _utcnow())
    except changesets.Ambiguous as e:
        await tg.send_text(chat_id, rendering.clarifying_message(e.names))
        return
    except (changesets.BadOperation, ValidationError, AIError):
        await tg.send_text(chat_id, rendering.NUDGE)
        return
    except Exception as e:
        log.warning("stray-text handling failed (no contents logged): %s", type(e).__name__)
        await tg.send_text(chat_id, rendering.NUDGE)
        return
    # anything the AI mapped becomes a confirmable draft — same gate as a photo,
    # including the duplicate-coverage warning (PM2)
    db.set_state(chat_id, "pending_draft", {"session": session})
    duplicates = changesets.duplicate_medicine_names(chat_id, session)
    await tg.send_message_with_buttons(chat_id, rendering.draft_message(session, duplicates), DRAFT_BUTTONS)
    if clarify:  # mixed request: draft staged, now ask about the rest
        await tg.send_text(chat_id, str(clarify))


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
    # resource caps: keep one draft (and one user's ledger) from flooding the queue
    if len(active_meds) > settings.max_medicines_per_draft:
        await tg.send_text(chat_id,
                           f"This draft has {len(active_meds)} medicines — the limit is "
                           f"{settings.max_medicines_per_draft}. Please split it into separate prescriptions.")
        return
    new_rx = [rx for rx in session["prescriptions"] if any(not m.get("removed") for m in rx["medicines"])]
    active_rx = len(db.active_prescriptions(chat_id))
    if session["type"] == "draft" and active_rx + len(new_rx) > settings.max_active_prescriptions:
        await tg.send_text(chat_id, f"You already have {active_rx} active prescriptions — the limit is "
                                    f"{settings.max_active_prescriptions}. Use /edit or /cancel first.")
        return
    # (rate limit already checked by the callback gate in handle_event)
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
    # (rate limit already checked by the callback gate in handle_event)
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
        # belt-and-suspenders ownership check: button data is bot-authored per
        # chat, but never cancel a key that doesn't belong to this chat
        rx = db.query_one("SELECT chat_id FROM prescriptions WHERE id=?", (rx_id,))
        if rx is None or rx["chat_id"] != chat_id:
            await tg.answer_callback(cb_id)
            return
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


