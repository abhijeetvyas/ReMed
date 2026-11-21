# ReMed Technical Design — Deep Dive (reference doc)

Status: DRAFT v0.1. Companion to tech-design-v0.1.md (the PM one-pager). This doc is for reference when building; the one-pager is the review surface.

---

## 1. Stack (rationale: fewest moving parts)
- **Runtime:** one long-running process. Recommended: Python 3.12 + FastAPI (webhook receiver + JSON) and an in-process scheduler loop (asyncio task). No Celery, no Redis, no microservices. A single process can comfortably serve this load forever.
- **DB:** SQLite to start (zero ops); Postgres upgrade path is clean because the schema uses no SQLite-only features. All times stored as UTC (ISO-8601) plus the user's IANA timezone string.
- **AI:** provider-agnostic client (one module) for (a) vision extraction, (b) edit interpretation. Swappable.
- **Telegram:** official Bot API over HTTPS webhook.

## 2. Channel adapter interface (WhatsApp-readiness)
The core depends on an interface, not Telegram:
- Inbound events: `photo_received(chat_id, image_bytes)`, `text_received(chat_id, text)`, `button_pressed(chat_id, callback_id, value)`.
- Outbound ops: `send_text(chat_id, text)`, `send_message_with_buttons(chat_id, text, buttons)`, `update_message_buttons(chat_id, message_id, buttons)` (used for silent Taken/Skip acks).
WhatsApp migration then = implement the same ops over Meta Cloud API, plus: Meta business verification, template registration for reminder texts, handling the 24-hour service window (reminders are business-initiated → they become approved template messages), and linking chat identity to the phone number (via contact-share at optional setup). No schema change required.

## 3. Inbound handling
- Telegram webhook posts arrive with `update_id`. Dedup: track last seen update_id; drop replays (Telegram redelivers on non-200 responses).
- Router classification: image → intake handler; text starting with "/" → command handler; "Edit Prescription" (or /edit) → edit-session starter; an active conversation state exists → free-text handler for that state; otherwise → polite /help nudge.
- **Conversation state is stored in the DB**, keyed by chat_id with a short TTL: `pending_draft`, `pending_edit`, `pending_deletion_confirm`. This survives restarts and prevents cross-talk between chats.

## 4. AI layer contracts
### 4.1 Extraction (image → draft)
Input: image bytes + extraction prompt (schema example embedded). Output JSON:
```
{ "medicines": [ { "name": str, "form": tablet|syrup|capsule|other,
    "tablets_per_dose": number (=1 unless stated),
    "slots": [breakfast|lunch|dinner, ...],
    "food": before|after, "days": int } ],
  "notes": [str] }
```
Validation (plain code, not AI): times parseable; days 1–90; ≥1 slot; names non-empty. Invalid → 1 retry with corrective hint logged → then generic failure copy + offer to type it out.

### 4.2 Edit interpretation (free text → change-set)
Input: user free text + current users/prescriptions/medicines JSON for that chat.
Output change-set:
```
{ "operations": [
   { "op": update_medicine | add_medicine | remove_medicine | set_meal_time |
           set_offset | set_timezone | cancel_prescription,
     "target": ..., "field": ..., "new_value": ... } ] }
```
- Ambiguity rule: medicine-name matching is prefix/fuzzy; two medicines matching within threshold → clarifying question, no change-set returned.
- Safety: change-sets validated same as extraction; Apply is one transaction; draft state holds unapplied change-sets for ≤ 30 min, then expires with a gentle nudge.
- Cost control: temperature 0, bounded max tokens, one model call per iteration.

## 5. Scheduling math (the heart)
On **Apply** of a prescription draft:
1. `end_date = start_date + duration_days - 1` per medicine (each medicine has its own last day).
2. For each medicine × each day × each slot, create a `dose_event` row with `scheduled_time` = meal anchor (user local time, user tz) + offset(food) — computed once, stored in UTC.
3. Create a `reminder_queue` row per dose_event with `send_at = scheduled_time`, state = pending.
Bounds: one prescription capped at 90 days (≈ 270 dose rows — trivial).
Recomputation rules:
- Meal time / offset / timezone edit → recompute `scheduled_time` for **future pending** dose_events only; past rows are immutable (adherence-record integrity rule).
- Medicine added/removed/extended → delete future pending rows for that medicine and recreate; taken/skipped history untouched.
- Pause → engine skips sending while paused, records nothing; a slot whose window passes while paused expires silently to "missed" — no user message (per v0.4).
Adherence definition (for the future doctor product): acknowledged within window = taken; explicit button = skipped; window passes silent = missed. No summaries in MVP.

## 6. State machines (small, enumerable)
- Prescription: `draft → active → cancelled | expired (computed)`
- Dose event: `pending → taken | skipped | missed (silent)`
- Reminder job: `pending → sent | failed → retry (≤ 30 min) → expired (silent)`
- Conversation: `none ↔ pending_draft | pending_edit | pending_deletion_confirm`

## 7. Table definitions (exact fields)
```
users(chat_id TEXT PK, timezone TEXT NOT NULL, meal_breakfast TIME, meal_lunch TIME,
      meal_dinner TIME, offset_before_min INT DEFAULT -15, offset_after_min INT DEFAULT 45,
      paused BOOLEAN DEFAULT false, created_at, deleted_at NULL)
prescriptions(id UUID PK, chat_id FK→users, start_date DATE, end_date DATE,
      status TEXT CHECK draft|active|cancelled|expired, image_deleted BOOLEAN,
      created_at)
medicines(id UUID PK, prescription_id FK, name TEXT, form TEXT,
      tablets_per_dose NUMERIC DEFAULT 1, slots TEXT [json array],
      food TEXT before|after, duration_days INT, created_at)
dose_events(id UUID PK, medicine_id FK, chat_id FK, event_date DATE, slot TEXT,
      scheduled_time_utc TIMESTAMPTZ, status TEXT pending|taken|skipped|missed,
      responded_at TIMESTAMPTZ NULL, reminder_message_id NULL)
reminder_queue(dose_event_id PK/FK, chat_id, send_at TIMESTAMPTZ,
      state TEXT pending|sent|failed|expired, attempts INT, last_error NULL)
conversation_state(chat_id PK, state TEXT, payload JSON, expires_at)
```
(6 tables total; the sixth is conversation state.)
Indexes: reminder_queue(send_at, state); dose_events(chat_id, event_date); medicines(prescription_id).

## 8. Reminder engine loop
Every 30–60 s (single async task, DB-backed so restart-safe):
1. `SELECT due reminders (send_at ≤ now, state = pending, chat not paused) LIMIT 50` (marked in-flight to avoid double-send).
2. Render template from dose_event + medicine (+ slot heading, per conversation-script-v0.2).
3. Send via adapter with buttons (Taken / Skip). HTTP ok + Bot API `ok:true` = delivered → state = sent, store message_id.
4. Delivery failure → ≤ 3 attempts with backoff inside the slot window → else state = expired, silently. No user-facing catch-up messages in MVP (deferred).
5. Button press → idempotent status write (taken/skipped) → `update_message_buttons` clears the buttons (no new message, per script v0.2). A reminder already answered → press ignored.
6. Timezone-shift edit regenerates future send_at values; DST is safe because send_at is derived from anchor + user tz at (re)generation time, not from a stored fixed offset.

## 9. Privacy / retention mechanics
- Image transcribed → deleted immediately once the draft is persisted;
- `/delete` = one transaction: delete all rows for that chat (user, prescriptions, medicines, dose_events, queue, state). Typed "YES" gate in chat.
- Log hygiene: no medicine names in log lines by default; chat_id allowed (needed for support).
- Export: out of scope; trivial later since everything keys off chat_id.

## 10. Deployment shape
- Single VPS (~₹400–500/mo) or PaaS free tier. Docker Compose: `app` + (optionally) `postgres` + `caddy` (TLS for the webhook). Env vars: BOT_TOKEN, AI provider key, DB URL. Secrets never in code.
- HTTPS webhook registered with Telegram; secret-token header checked on every delivery.
- Backups: nightly dump to object storage, 7-day retention; restore rehearsed once before launch.
- Monitoring on a budget: external uptime ping on `/health` + a dead-man's-snitch alert on the reminder-loop tick (if the loop stops ticking, you get an email).
- Target total cost: one VPS + pennies of AI per prescription; Telegram free.

## 11. Test plan (build-time)
- Golden set: 15–20 real prescription photos; assert extracted JSON within tolerance; measure misread rate.
- Simulated clock: inject "now" into the reminder loop; run a full course in accelerated time.
- Failure drills: AI invalid JSON, Telegram 5xx, DB restart mid-Apply, duplicate webhook replay.
- Copy tests: every template in conversation-script-v0.2 has a fixture.

## 12. Deliberately deferred
Start-day selection UI (field exists), course/expiry notifications, downtime catch-up messages, duplicate-prescription disambiguation, timezone-change prompt, merged multi-prescription reminders (data model already prepared), WhatsApp, web OTP view, doctor dashboard.