# Technical Design — ReMed (one-pager, PM view)

Status: DRAFT v0.1 — companion to prd-v0.4.md and conversation-script-v0.2.md.
A visual flowchart artifact (remed-flowchart.html) accompanies this doc.

## Big picture
One small backend service. Five moving parts, nothing more:

1. **Channel adapter (Telegram today, WhatsApp later).** Translates incoming chat events into a common internal format (photo / text / button-tap) and sends outgoing messages + buttons. The rest of the system never knows which app the user is on — this is what keeps the global/WhatsApp future cheap.
2. **Router.** Looks at each incoming message and picks a handler: new photo, "Edit Prescription", /command, or button tap.
3. **Intake pipeline (photo → schedule).** Image goes to the AI layer → returns a structured prescription (names, slots, food relation, days) → normal code validates it → draft shown → nothing happens until confirmation (Apply).
4. **Edit pipeline.** Free-text message + the current prescription object both go to the AI layer → returns a change-set → validated → full prescription table shown → Apply saves. The AI never writes directly: it only proposes; ordinary code applies.
5. **Reminder engine.** A simple loop that wakes every ~minute, asks the DB "which reminders are due in the next minute?", renders the message template, sends via the adapter, and marks the job sent (with delivery acknowledgment). Dead simple is the point.

## Data model (5 tables)
| Table | Holds | Purpose |
|---|---|---|
| **users** | chat_id, timezone, 3 meal times, −15/+45 offsets, paused flag | One row per chat; the identity/auth record |
| **prescriptions** | id, user, start_date, end_date, status (active / expired / cancelled) | One object per photo-confirmed prescription |
| **medicines** | prescription_id, name, form, tablets-per-dose (default 1), slots, before/after food, days | The confirmed line items |
| **dose_events** | medicine, date, slot, scheduled_time, status (pending / taken / skipped) | The expanded schedule — one row per reminder moment; this is where raw feedback is recorded |
| **reminder_queue** | dose_event, send_at, state (pending / sent / failed) | What the reminder engine consumes |

Key mechanic: **when a prescription is confirmed, the whole course is expanded into dose_events at once** (a 15-day × 3-slot prescription = 45 rows). Each dose_event's exact send time is precomputed (meal anchor ± offset, converted to UTC using the user's timezone). The engine afterwards only picks up due rows — no schedule math at reminder time. Timezone/meal/offset edits recompute future rows; past rows are never touched (the record stays honest).

## AI interaction (2 call types, both under a confirmation gate)
- **Extract:** prescription image → structured JSON of medicines. Validated by normal code (parseable times, days ≥ 1, drug-name sanity).
- **Interpret edit:** free text + current prescription JSON → change-set JSON. Same validation; ambiguity triggers a clarifying question instead of a guess.
- Cost: one call per photo and one per edit iteration — pennies per prescription.

## Deployment & hosting
- Single cheap VPS (~\$5/mo) or free PaaS tier; one process, one database (SQLite to start, Postgres when needed). HTTPS webhook for Telegram.
- Daily automated DB backup; secrets in environment, never in code.
- Nothing India-specific in the core (offsets, meal times, timezone all per-user) — it's a global product in English for now; WhatsApp is a second adapter plus Meta business setup, with the same data model and a phone-number linkage added.

## Reliability (MVP-honest)
- Every send gets an HTTP delivery acknowledgment; if Telegram is down we retry quietly within the slot window — no user-visible nag (per v0.4, catch-up messages are future).
- Process restarts don't lose reminders: the queue lives in the DB, not in memory.
- Bot replies are idempotent (duplicate webhook deliveries are detected and dropped).

## Deferred to the deep-dive doc
Exact table fields, state machines, edit-conflict rules, WhatsApp migration mapping, timezone/DST arithmetic, backup/monitoring specifics, cost table.